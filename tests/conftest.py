"""共享 pytest 夹具。

两个 Windows + 受限环境的真实踩坑，都在这里解决：
1. 系统 TEMP 目录在此环境下**不可枚举**（os.scandir/listdir 报 WinError 5），
   而 pytest 的 tmp_path / tmp_path_factory 内部会 listdir，因此不可用。
   这里用 count 递增在 workspace 内建目录，完全绕开枚举操作。
2. 离线开关必须在任何业务模块 import 之前写入环境变量。
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ---- 必须在 import 业务模块之前完成 ----
os.environ["MDA_OFFLINE"] = "1"
os.environ.setdefault("MDA_DATA_DIR", str(ROOT / "data"))


def _fresh_dir(prefix: str = "case") -> Path:
    """在 workspace 内建唯一目录。

    两个坑都绕开了：
    1. 不用 pytest 的 tmp_path —— 它的 basetemp 落在系统 TEMP，本环境下不可访问；
    2. 不用 tempfile.mkdtemp —— 它创建的目录以 0700 权限建成，本环境的文件沙箱
       会拒绝对其写入（mkdir 创建的目录才正常）。因此这里用 mkdir + 递增后缀。
    """
    counter = 0
    while True:
        counter += 1
        candidate = ROOT / f".tmp-{prefix}-{os.getpid()}-{counter}"
        if candidate.exists():
            continue
        candidate.mkdir(parents=True)
        return candidate


@pytest.fixture(scope="session")
def tmp_path_factory():
    """最小实现，满足 pytest 内置 tmp_path_factory 的常用调用面（mkdtemp/mktemp）。"""
    created: list[Path] = []

    class _Factory:
        def mktemp(self, basename: str = "case", numbered: bool = True) -> Path:
            directory = _fresh_dir(basename)
            created.append(directory)
            return directory

        def mkdtemp(self, basename: str = "case", numbered: bool = True) -> Path:
            return self.mktemp(basename, numbered)

        def getbasetemp(self) -> Path:
            return ROOT

    try:
        yield _Factory()
    finally:
        for directory in created:
            shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture()
def tmp_path():
    """覆盖 pytest 内置 fixture：返回 workspace 内可用的新目录，用完自动清理。"""
    directory = _fresh_dir()
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(scope="session")
def fixtures_dir() -> Path:
    """样例目录。

    只做两件事：先用仓库里已有的样例；缺失时才尝试重新生成。
    这样 reportlab 就只在「样例真的丢了」时才是必需的，而不是跑测试的硬门槛。
    """
    from servers.common.config import FIXTURE_DIR

    required = [
        FIXTURE_DIR / "newmont_ni43-101_synthetic.pdf",
        FIXTURE_DIR / "barrick_ni43-101_synthetic.pdf",
        FIXTURE_DIR / "pilbara_ni43-101_synthetic.pdf",
        FIXTURE_DIR / "ground_truth.json",
    ]
    if all(path.exists() for path in required):
        return FIXTURE_DIR

    # 样例不全 → 需要生成，这一步用得到 reportlab
    try:
        from scripts.make_sample_pdfs import build_all
    except ImportError as exc:                       # pragma: no cover
        pytest.skip(
            f"样例缺失且无法生成（{exc}）；样例已随仓库提供，"
            "若确实需要重新生成请先安装 reportlab"
        )
    build_all()
    return FIXTURE_DIR


@pytest.fixture()
def temp_store(tmp_path: Path):
    """每个测试一个独立 SQLite 库，避免用例互相污染。"""
    from servers.common.store import Store

    store = Store(tmp_path / "store.sqlite3")
    yield store
    store.close()


@pytest.fixture(scope="session")
def ground_truth(fixtures_dir: Path) -> dict:
    import json

    return json.loads((fixtures_dir / "ground_truth.json").read_text(encoding="utf-8"))
