"""用本地 mock OpenAI 兼容服务验证 LLM 路径（不依赖外部 API key）。

覆盖三件事：
1. `servers/common/llm.py` 的请求构造、JSON 容错解析、未配置时的显式不可用；
2. `mineral-pdf-mcp` 的双路交叉校验：mock 故意返回与确定性解析器冲突的品位，
   必须产生 `cross_path_conflict` 告警并降低置信度（而不是静默采信）；
3. `agent` 的引言润色路径能正常拼接，且失败时回退到模板。

用法：
    python scripts/verify_llm_path.py
"""

from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common.config import FIXTURE_DIR                  # noqa: E402

# mock 模型的返回：Indicated 品位故意给错（0.63 而不是 0.41），用于触发冲突告警
MOCK_PAYLOAD = {
    "Indicated": {"tonnes_mt": 890.0, "grade": 0.63, "grade_unit": "%",
                  "metal": 8040.0, "metal_unit": "Mlb", "page": 2,
                  "evidence": "Indicated | 890.00 | 0.63 | 8,040"},
    "Inferred": {"tonnes_mt": 150.0, "grade": 0.33, "grade_unit": "%",
                 "metal": 1090.0, "metal_unit": "Mlb", "page": 2, "evidence": None},
}

NARRATIVE = "本日要点：合成样例用于验证编排链路，价格与储量数据均为测试用途。"


class _Handler(BaseHTTPRequestHandler):
    """最小 OpenAI 兼容 /chat/completions 实现。

    刻意把异常全部吞掉并始终回 200：这个 mock 只是给被测代码当靶子，
    任何 handler 内部异常都会让客户端收到 5xx，从而把「环境问题」误报成
    「被测代码有问题」——那就失去了验证的意义。
    """

    calls: list[dict] = []

    def do_POST(self) -> None:                                  # noqa: N802
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw or b"{}")
            type(self).calls.append(body)
            prompt = json.dumps(body.get("messages", []), ensure_ascii=False)
            # 抽取请求要求 JSON，润色请求要自然语言：按提示词区分返回
            if "STRICT JSON" in prompt:
                content = "```json\n" + json.dumps(MOCK_PAYLOAD, ensure_ascii=False) + "\n```"
            else:
                content = NARRATIVE
            payload = {
                "id": "chatcmpl-mock", "object": "chat.completion",
                "model": body.get("model"),
                "choices": [{"index": 0, "finish_reason": "stop",
                             "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 50,
                          "total_tokens": 150},
            }
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception:                                       # noqa: BLE001
            traceback.print_exc()

    def log_message(self, *args: object) -> None:
        pass                                                     # 静默，避免污染输出


def _preflight(base_url: str) -> tuple[bool, str]:
    """确认「本机到本机 mock」这条路通不通 —— 且必须用 httpx 确认。

    被测代码（servers/common/llm.py）用的就是 httpx，所以只要 httpx 到不了 mock，
    后面必然失败。此时失败原因是**验证环境**而不是被测代码；区分开这两种失败，
    才能让 failures 计数保持可信（否则会把环境问题误报成代码缺陷）。

    实测过的真实场景：某些受限 shell 里 httpx 对 loopback 请求会拿到 502 空响应
    （服务端根本没收到请求），而标准库 urllib 正常。因此这里 httpx 一旦不可达就
    按「跳过验证」处理，并说明原因。
    """
    payload = json.dumps({"model": "preflight",
                          "messages": [{"role": "user", "content": "STRICT JSON"}]})
    headers = {"Content-Type": "application/json"}
    problems: list[str] = []

    try:
        import httpx
        response = httpx.post(f"{base_url}/chat/completions", content=payload,
                              headers=headers, timeout=15.0)
        if response.status_code == 200 and response.text.strip():
            return True, "httpx 直连 mock 正常"
        problems.append(f"httpx -> HTTP {response.status_code} {response.text[:60]!r}")
    except Exception as exc:                                    # noqa: BLE001
        problems.append(f"httpx -> {type(exc).__name__}: {exc}")

    # 顺带探一次 urllib：如果它能通，就更能确定是 httpx 被环境拦了
    try:
        import urllib.request
        request = urllib.request.Request(
            f"{base_url}/chat/completions", data=payload.encode("utf-8"),
            headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            if response.status == 200 and response.read().strip():
                problems.append("对照：urllib 可以连通（说明是 httpx 被环境拦截）")
    except Exception as exc:                                    # noqa: BLE001
        problems.append(f"urllib 也不通 -> {type(exc).__name__}: {exc}")

    return False, "; ".join(problems)


def main() -> int:
    failures = 0
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}/v1"

    reachable, detail = _preflight(base_url)
    print("== 0. mock 服务连通性自检 ==")
    print(f"   {detail}")
    if not reachable:
        print("   [SKIP] 被测代码用的 httpx 无法访问本地 mock，"
              "本环境无法完成 LLM 路径验证（属环境限制，非代码缺陷）。")
        print("   提示：请在普通终端（非受限沙箱）中重跑本脚本。")
        server.shutdown()
        return 0

    os.environ["MDA_LLM_API_KEY"] = "test-key"
    os.environ["MDA_LLM_BASE_URL"] = base_url
    os.environ["MDA_CRITIC_API_KEY"] = "test-key"
    os.environ["MDA_CRITIC_BASE_URL"] = base_url

    # 配置是 frozen dataclass 且已在 import 时构造，因此这里重建模块级 SETTINGS
    import importlib

    import servers.common.config as config
    importlib.reload(config)
    for name in ("servers.common.llm", "servers.mineral_pdf_mcp.server"):
        module = importlib.import_module(name)
        importlib.reload(module)
    import agent.loop as loop_module
    import agent.briefing as briefing_module
    importlib.reload(loop_module)
    importlib.reload(briefing_module)

    from servers.common.llm import LLMClient, extract_json_object
    from servers.mineral_pdf_mcp import server as pdf_server

    print("== 1. LLM 客户端可用性 ==")
    client = LLMClient(role="extractor")
    response = client.complete("ping", system="test")
    print(f"   model={response.model} tokens={response.usage.get('total_tokens')}")
    if response.text != NARRATIVE:
        failures += 1
        print("   [FAIL] 非 JSON 请求应返回自然语言")

    parsed = extract_json_object("```json\n{\"a\": 1}\n```")
    print(f"   json 容错解析: {parsed}")
    if parsed != {"a": 1}:
        failures += 1

    print("== 2. 储量双路交叉校验（mock 故意给错品位）==")
    pdf = FIXTURE_DIR / "barrick_ni43-101_synthetic.pdf"
    envelope = pdf_server.extract_resources_envelope(str(pdf))
    payload = envelope.to_dict()
    conflicts = payload["data"]["cross_validation"]["conflicts"]
    codes = {warning["code"] for warning in payload["warnings"]}
    print(f"   llm_available={payload['data']['cross_validation']['llm_available']}")
    print(f"   conflicts={conflicts}")
    print(f"   confidence={payload['provenance']['confidence']}")
    print(f"   warnings={sorted(codes)}")
    if not conflicts:
        failures += 1
        print("   [FAIL] 双路品位冲突未被检出")
    if "cross_path_conflict" not in codes:
        failures += 1
        print("   [FAIL] 缺少 cross_path_conflict 告警")
    if payload["provenance"]["confidence"] >= 0.95:
        failures += 1
        print("   [FAIL] 冲突未被计入置信度")
    # 确定性解析器的结果仍应为主（0.41），不能被 mock 的 0.63 覆盖
    grade = payload["data"]["resources"]["Indicated"]["grade"]
    print(f"   最终采用品位={grade}（应为 0.41）")
    if abs(float(grade) - 0.41) > 0.01:
        failures += 1
        print("   [FAIL] 确定性解析结果被 LLM 覆盖")

    print("== 3. 简报引言润色路径 ==")
    with loop_module.BriefingAgent() as agent:
        result = briefing_module.generate("Pilbara 锂矿简报", agent=agent,
                                          write_file=False, max_rounds=0)
    if NARRATIVE in result.markdown:
        print("   [OK] 润色引言已插入简报")
    else:
        failures += 1
        print("   [FAIL] 润色引言未出现在简报中")

    server.shutdown()
    print(f"\nfailures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
