"""生成 mcp-config.json 与 docker-compose.yml。

单一事实来源是 agent/mcp_registry.py —— 避免出现
「配置文件里写的命令和 Agent 实际跑的不是一回事」这种最常见的不一致。

用法：
    python scripts/make_configs.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.mcp_registry import mcp_config, server_specs      # noqa: E402

COMPOSE_HEADER = """# 由 scripts/make_configs.py 生成 —— 不要手改，改 agent/mcp_registry.py 后重跑。
#
# 说明：MCP 是 stdio 协议，一个 server 一个进程，因此这里用三个常驻容器
# （每个都保持 stdin 打开，等价于本地 `python -m servers.*.server`）。
# 用法：
#   docker compose up -d --build          # 起三个 server
#   docker compose exec agent python -m agent.cli "Pilbara 锂矿简报"
#   docker compose run --rm agent python -m pytest tests -q
"""


def build_compose() -> str:
    lines = [COMPOSE_HEADER, "services:"]
    for spec in server_specs():
        service = spec.key.replace("_", "-")
        lines += [
            f"  {service}:",
            "    build: .",
            f"    image: mineral-daily-agent:latest",
            f"    container_name: {service}",
            "    stdin_open: true",
            "    tty: false",
            f"    command: [\"python\", \"-m\", \"{spec.module}\"]",
            "    environment:",
            "      - MDA_DATA_DIR=/app/data",
            "      - MDA_OFFLINE=${MDA_OFFLINE:-0}",
            "      - MDA_LLM_API_KEY=${MDA_LLM_API_KEY:-}",
            "      - MDA_CRITIC_API_KEY=${MDA_CRITIC_API_KEY:-}",
            "      - MDA_LLM_BASE_URL=${MDA_LLM_BASE_URL:-https://api.deepseek.com/v1}",
            "    volumes:",
            "      - ./data:/app/data",
            "    restart: unless-stopped",
            "",
        ]
    lines += [
        "  agent:",
        "    build: .",
        "    image: mineral-daily-agent:latest",
        "    container_name: mineral-daily-agent",
        "    profiles: [\"cli\"]",
        "    command: [\"python\", \"-m\", \"agent.cli\", \"--list-projects\"]",
        "    environment:",
        "      - MDA_DATA_DIR=/app/data",
        "      - MDA_OFFLINE=${MDA_OFFLINE:-0}",
        "      - MDA_LLM_API_KEY=${MDA_LLM_API_KEY:-}",
        "      - MDA_CRITIC_API_KEY=${MDA_CRITIC_API_KEY:-}",
        "      - MDA_LLM_BASE_URL=${MDA_LLM_BASE_URL:-https://api.deepseek.com/v1}",
        "    volumes:",
        "      - ./data:/app/data",
        "      - ./:/app",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    config_path = ROOT / "mcp-config.json"
    # 用绝对路径写 cwd 与解释器，保证 Claude Desktop / Cursor 直接可用
    config_path.write_text(
        json.dumps(mcp_config(sys.executable, root=ROOT), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {config_path}")

    compose_path = ROOT / "docker-compose.yml"
    compose_path.write_text(build_compose(), encoding="utf-8")
    print(f"wrote {compose_path}")

    print("\n下一步：把 mcp-config.json 的内容并入 Claude Desktop / Cursor 的配置即可验证。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
