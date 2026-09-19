"""调用单个 MCP 工具并打印结构化结果。

用法：
    python scripts/mcp_call.py <server-key> <tool> [json参数]
示例：
    python scripts/mcp_call.py mining-news-mcp search "{\"query\":\"Pilbara lithium\",\"days\":30}"
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.mcp_registry import build_clients, spec_by_key  # noqa: E402


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    key, tool = sys.argv[1], sys.argv[2]
    arguments = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}

    spec = spec_by_key(key)
    client = build_clients([spec])[0]
    client.start()
    try:
        result = client.call_tool(tool, arguments)
        print(f"ok={result.ok} is_error={result.is_error} "
              f"duration_ms={result.duration_ms} attempts={result.attempts} "
              f"degraded={result.degraded}")
        if result.error:
            print(f"error: {result.error}")
        print(result.text[:6000])
        return 0 if result.ok else 1
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
