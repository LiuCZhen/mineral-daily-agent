"""环境自检：一条命令看清当前解释器能否跑本项目、缺什么、怎么补。

为什么需要它：
题面要求「5 分钟内跑起来」，而最常见的卡点不是代码，是环境（解释器版本、
缺包、镜像不可达）。这个脚本把「能不能跑」变成一次可读的诊断输出。

用法：
    python scripts/check_env.py
    python scripts/check_env.py --json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import platform
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

OK = "OK"
MISSING = "MISSING"
OPTIONAL = "OPTIONAL"

# (模块名, 等级, 用途)
DEPENDENCIES: list[tuple[str, str, str]] = [
    ("httpx", "required", "HTTP 客户端：抓取新闻/价格/PDF、调用 LLM"),
    ("pytest", "test", "运行测试套件"),
    ("reportlab", "fixture", "生成合成 NI 43-101 样例 PDF（样例已随仓库提供，可跳过）"),
    ("lxml", "optional", "更稳的 HTML 正文抽取（缺失则退化到标准库 HTMLParser）"),
]

# 项目 import 链里必需的标准库模块（缺失说明解释器不完整）
STDLIB_NEEDED = [
    "sqlite3", "zlib", "csv", "xml.etree.ElementTree", "html.parser",
    "urllib.parse", "dataclasses", "statistics", "subprocess", "threading",
    "http.server", "itertools", "base64", "hashlib", "copy", "typing",
]


def check_stdlib() -> dict:
    missing = [name for name in STDLIB_NEEDED if importlib.util.find_spec(name) is None]
    return {"required": STDLIB_NEEDED, "missing": missing, "ok": not missing}


def check_dependencies() -> list[dict]:
    """真实 import 一遍，而不只是 find_spec。

    find_spec 只证明「文件在」，不证明「能 import」——例如依赖被某个
    半成品环境破坏、或存在循环导入时，find_spec 仍然为真。日报的可用性
    取决于能否真正导入，因此这里以 import 结果为准。
    """
    import importlib

    results = []
    for module, level, purpose in DEPENDENCIES:
        installed = False
        error: str | None = None
        try:
            importlib.import_module(module)
            installed = True
        except Exception as exc:                        # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"
        results.append({
            "module": module,
            "level": level,
            "purpose": purpose,
            "installed": installed,
            "version": _version_of(module) if installed else None,
            "error": error,
            "status": (OK if installed
                       else (MISSING if level in ("required", "test") else OPTIONAL)),
        })
    return results


def _version_of(module: str) -> str | None:
    try:
        import importlib.metadata as meta

        package = {"lxml": "lxml", "pytest": "pytest"}.get(module, module)
        return meta.version(package)
    except Exception:                                    # noqa: BLE001
        return None


def check_sources() -> dict:
    """关键文件是否就位（缺了说明仓库不完整或工作目录不对）。"""
    wanted = {
        "MCP 协议栈": ROOT / "servers" / "common" / "mcp_protocol.py",
        "新闻 server": ROOT / "servers" / "mining_news_mcp" / "server.py",
        "储量 server": ROOT / "servers" / "mineral_pdf_mcp" / "server.py",
        "价格 server": ROOT / "servers" / "lme_price_mcp" / "server.py",
        "Agent 入口": ROOT / "agent" / "cli.py",
    }
    missing = [label for label, path in wanted.items() if not path.exists()]
    return {"checked": len(wanted), "missing": missing, "ok": not missing}


def check_fixtures() -> dict:
    """合成样例是否已生成（离线 demo 与测试都依赖它们）。"""
    fixtures = ROOT / "data" / "fixtures"
    pdfs = sorted(path.name for path in fixtures.glob("*_ni43-101_synthetic.pdf"))
    return {
        "dir": str(fixtures),
        "pdfs": pdfs,
        "ground_truth": (fixtures / "ground_truth.json").exists(),
        "projects": (fixtures / "projects.json").exists(),
        "ok": len(pdfs) >= 3 and (fixtures / "ground_truth.json").exists(),
    }


def check_project_import() -> dict:
    """真正 import 一遍三个 server，确认没有隐藏的依赖问题。"""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    result: dict = {"ok": False, "error": None, "tools": 0}
    try:
        from servers.lme_price_mcp import server as price_server
        from servers.mineral_pdf_mcp import server as pdf_server
        from servers.mining_news_mcp import server as news_server

        total = (len(news_server.server.tools) + len(pdf_server.server.tools)
                 + len(price_server.server.tools))
        result.update(ok=True, tools=total)
    except Exception as exc:                             # noqa: BLE001
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def build_report() -> dict:
    stdlib_info = check_stdlib()
    deps = check_dependencies()
    sources = check_sources()
    fixtures = check_fixtures()
    imports = check_project_import()

    blocked = [d for d in deps if d["status"] == MISSING]
    # 离线 demo 需要：标准库完整、源码在位、三个 server 能 import、样例 PDF 已存在。
    # httpx 是 import 链上的硬依赖（servers/common/http.py 顶层导入），
    # 即使完全离线也必须有；夹具已随仓库提供，所以不需要 reportlab。
    can_run_demo = (stdlib_info["ok"] and sources["ok"] and imports["ok"]
                    and fixtures["ok"]
                    and not any(d["module"] == "httpx" for d in blocked))
    can_run_tests = can_run_demo and not any(
        d["module"] == "pytest" for d in blocked)
    # 给出针对当前状态的补装建议
    suggestions: list[str] = []
    if not stdlib_info["ok"]:
        suggestions.append(
            "当前解释器的标准库不完整（缺 " + ", ".join(stdlib_info["missing"])
            + "），请更换为完整的 CPython / Conda 解释器。")
    to_install = [d["module"] for d in deps
                  if d["module"] in ("httpx", "pytest") and not d["installed"]]
    if to_install:
        names = " ".join(to_install)
        suggestions.append(f"conda：conda install -c conda-forge {names} -y")
        suggestions.append(f"pip ：python -m pip install {names}"
                           " -i https://pypi.tuna.tsinghua.edu.cn/simple")
    if not fixtures["ok"]:
        reportlab_missing = any(
            d["module"] == "reportlab" and not d["installed"] for d in deps)
        suggestions.append(
            "样例未生成：python scripts/make_sample_pdfs.py"
            + ("（需要先装 reportlab：conda install -c conda-forge reportlab -y）"
               if reportlab_missing else ""))
    elif any(d["module"] == "reportlab" and not d["installed"] for d in deps):
        suggestions.append(
            "合成样例已随仓库提供，无需 reportlab 也能跑 Demo 与测试；"
            "仅当你要重新生成样例时才需要装它（conda install -c conda-forge reportlab -y）。")
    if not imports["ok"]:
        suggestions.append(f"项目 import 失败：{imports['error']}")
    if sys.version_info < (3, 10):
        suggestions.append(
            f"当前 Python {platform.python_version()} 低于建议的 3.10；"
            "代码因 `from __future__ import annotations` 仍可解析，"
            "但依赖包可能装不上，建议换 3.10+ 解释器。")

    return {
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
            "recommended_ok": sys.version_info >= (3, 10),
        },
        "stdlib": stdlib_info,
        "dependencies": deps,
        "sources": sources,
        "fixtures": fixtures,
        "project_import": imports,
        "can_run_offline_demo": can_run_demo,
        "can_run_tests": can_run_tests,
        "suggestions": suggestions,
    }


def render(report: dict) -> str:
    lines: list[str] = []
    py = report["python"]
    lines.append("=" * 66)
    lines.append("矿权日报 Agent — 环境自检")
    lines.append("=" * 66)
    lines.append(f"解释器 : {py['executable']}")
    lines.append(f"版本   : Python {py['version']} ({py['implementation']})"
                 + ("" if py["recommended_ok"] else "  ← 低于建议的 3.10"))
    lines.append("")

    std = report["stdlib"]
    lines.append(f"[标准库] {'通过' if std['ok'] else '缺失: ' + ', '.join(std['missing'])}")

    lines.append("")
    lines.append("[第三方依赖]")
    for dep in report["dependencies"]:
        flag = {"OK": "已安装", "MISSING": "缺失(必需)", "OPTIONAL": "未安装(可选)"}[dep["status"]]
        version = f" {dep['version']}" if dep["version"] else ""
        detail = f"  ← {dep['error']}" if dep.get("error") else ""
        lines.append(f"  {dep['module']:<11} {flag}{version:<10} — {dep['purpose']}{detail}")

    fx = report["fixtures"]
    lines.append("")
    lines.append(f"[合成样例] {'已就绪' if fx['ok'] else '不完整'}"
                 f"（{len(fx['pdfs'])} 份 PDF，ground_truth="
                 f"{'有' if fx['ground_truth'] else '缺'}）")

    imp = report["project_import"]
    lines.append(f"[三个 server import] "
                 + (f"通过，共暴露 {imp['tools']} 个工具" if imp["ok"] else f"失败：{imp['error']}"))

    lines.append("")
    lines.append("-" * 66)
    lines.append(f"离线上跑 Demo      : {'可以' if report['can_run_offline_demo'] else '不可以'}")
    lines.append(f"跑完整测试套件      : {'可以' if report['can_run_tests'] else '不可以'}")
    if report["suggestions"]:
        lines.append("")
        lines.append("建议动作：")
        for item in report["suggestions"]:
            lines.append(f"  - {item}")
    else:
        lines.append("")
        lines.append("环境完全就绪，可直接执行：")
        lines.append("  $env:MDA_OFFLINE = \"1\"   # PowerShell")
        lines.append("  python -m agent.cli --topic \"Pilbara 锂矿\"")
    lines.append("=" * 66)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查当前解释器能否运行本项目")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args(argv)

    report = build_report()
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render(report))
    # 退出码：离线上能跑 Demo 即视为通过（缺 pytest 不影响 demo）
    return 0 if report["can_run_offline_demo"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
