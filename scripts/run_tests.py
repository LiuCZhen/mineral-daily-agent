"""测试入口：屏蔽全局 user site-packages 后运行 pytest。

为什么需要它：
Windows 上 `%APPDATA%\\Python\\Python3XX\\site-packages`（user site）会被自动加入
`sys.path`，且**先于** conda 环境生效。若那里装着带 pytest 插件的包（如 langsmith），
pytest 在加载 entry-point 插件时会 import 它，一旦它自身依赖缺失
（典型：langsmith → requests → urllib3）就会在收集阶段直接崩溃：

    ModuleNotFoundError: No module named 'urllib3'

这与本项目无关，但会让评审第一步就卡住。关键细节：**必须在 pytest 启动前**设置
`PYTHONNOUSERSITE=1` —— pytest 先加载 entry-point 插件、后导入 conftest，
因此写在 conftest.py 里来不及（已实测）。

用法：
    python scripts/run_tests.py                 # 等价于 python -m pytest
    python scripts/run_tests.py -q tests/test_mcp_protocol.py
    python scripts/run_tests.py --no-guard      # 不屏蔽 user site（对照排查用）
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _user_site_packages() -> list[str]:
    try:
        import site

        return [p for p in (site.getusersitepackages(),) if p]
    except Exception:                                     # noqa: BLE001
        return []


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    guard = "--no-guard" not in args
    args = [a for a in args if a != "--no-guard"]

    if guard:
        user_sites = _user_site_packages()
        os.environ["PYTHONNOUSERSITE"] = "1"
        # 同进程内直接把 user site 从 sys.path 摘掉，双保险
        sys.path[:] = [p for p in sys.path if p not in user_sites]
        print(f"[run_tests] 已设置 PYTHONNOUSERSITE=1"
              f"{'，并移除 ' + ', '.join(user_sites) if user_sites else ''}")

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    try:
        import pytest
    except ImportError:
        print("未安装 pytest：conda install -c conda-forge pytest -y", file=sys.stderr)
        return 2

    return int(pytest.main(args or ["tests"]))


if __name__ == "__main__":
    raise SystemExit(main())
