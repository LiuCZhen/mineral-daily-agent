"""CLI 入口：生成矿权日报。

用法：
    python -m agent.cli "给我生成一份关于 Pilbara 锂矿的今日简报"
    python -m agent.cli --topic "Pilbara 锂矿" --json
    python -m agent.cli --topic "Pilbara" --out data/briefings --rounds 2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.briefing import generate                          # noqa: E402
from agent.loop import BriefingAgent                          # noqa: E402
from agent.registry import load_projects                      # noqa: E402
from servers.common.config import DATA_DIR, SETTINGS          # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mineral-daily-agent",
        description="矿权日报 Agent：通过 MCP 编排新闻 / NI 43-101 储量 / 价格三个 server。",
    )
    parser.add_argument("topic", nargs="*", help="主题，例如 'Pilbara 锂矿'")
    parser.add_argument("--topic", dest="topic_opt", help="主题（等价的位置参数）")
    parser.add_argument("--out", default=str(DATA_DIR / "briefings"), help="简报输出目录")
    parser.add_argument("--rounds", type=int, default=2, help="自检不达标时的补采轮次上限")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出完整结果")
    parser.add_argument("--no-write", action="store_true", help="不写入文件，只打印")
    parser.add_argument("--list-projects", action="store_true", help="列出项目目录后退出")
    parser.add_argument("--offline", action="store_true",
                        help="强制离线模式（等价 MDA_OFFLINE=1，仅用缓存与合成夹具）")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.offline:
        import os
        os.environ["MDA_OFFLINE"] = "1"

    if args.list_projects:
        for entry in load_projects():
            print(f"{entry.key:22s} {entry.name}  [{entry.country}/{entry.commodity}]")
            print(f"    aliases: {', '.join(entry.aliases)}")
            print(f"    reports: {', '.join(str(r.get('path')) for r in entry.reports) or '-'}")
            print(f"    prices : {', '.join(entry.price_commodities)}")
        return 0

    topic = args.topic_opt or " ".join(args.topic).strip()
    if not topic:
        build_parser().print_help()
        return 2

    if args.json or args.no_write:
        with BriefingAgent() as agent:
            result = generate(topic, agent=agent, max_rounds=args.rounds,
                              write_file=not args.no_write,
                              output_dir=Path(args.out))
    else:
        result = generate(topic, max_rounds=args.rounds, output_dir=Path(args.out))

    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
        return 0

    print(result.markdown)
    print("=" * 72)
    print(f"自检评分：{result.critic.get('score')}/10 "
          f"({'通过' if result.critic.get('passed') else '未通过'})")
    print(f"数据降级：{'是' if result.degraded else '否'}")
    print(f"工具调用：{', '.join(result.used_tools)}")
    if result.output_path:
        print(f"已写入：{result.output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
