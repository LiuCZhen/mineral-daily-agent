"""按已验证的 MCP server 清单逐个握手并打印工具清单。

用法：
    python scripts/mcp_smoke.py                # 全部 server
    python scripts/mcp_smoke.py news pdf       # 指定 server
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.mcp_registry import build_clients, server_specs  # noqa: E402


def main() -> int:
    wanted = sys.argv[1:] or [spec.key for spec in server_specs()]
    failures = 0
    for spec in server_specs():
        if spec.key not in wanted:
            continue
        client = build_clients([spec])[0]
        try:
            client.start()
            print(f"\n=== {spec.key} ({spec.title}) ===")
            print(f"  serverInfo: {client.server_info.get('serverInfo')}")
            print(f"  protocolVersion: {client.server_info.get('protocolVersion')}")
            for name, info in sorted(client.tools.items()):
                required = ", ".join(info.required_args()) or "-"
                print(f"  tool {name}({required})")
                print(f"      {info.description.splitlines()[0][:100]}")
        except Exception as exc:                       # noqa: BLE001
            failures += 1
            print(f"  [FAIL] {spec.key}: {type(exc).__name__}: {exc}")
            for line in (client.stderr_lines or [])[-8:]:
                print(f"      stderr| {line}")
        finally:
            client.close()
    print(f"\nfailures={failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
