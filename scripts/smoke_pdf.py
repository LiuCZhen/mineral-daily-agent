"""快速冒烟：生成合成 PDF → 自研解析器抽取 → 与 ground truth 比对。

用法：
    python scripts/smoke_pdf.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.make_sample_pdfs import build_all            # noqa: E402
from servers.common.config import FIXTURE_DIR              # noqa: E402
from servers.mineral_pdf_mcp import extractor, pdf_text    # noqa: E402


def main() -> int:
    build_all()
    truth = json.loads((FIXTURE_DIR / "ground_truth.json").read_text(encoding="utf-8"))

    failures = 0
    for case_id, gt in truth.items():
        pdf_path = FIXTURE_DIR / gt["file"]
        pages, warnings = pdf_text.extract_pages_from_file(str(pdf_path))
        result = extractor.parse_resource_tables(pages)
        shaped = extractor.build_ground_truth_shaped(result)

        print(f"\n=== {case_id} ({gt['file']}) ===")
        print(f"  pages={len(pages)} warnings={warnings}")
        print(f"  stats={result['stats']}")
        for field in ("tonnes_mt", "grade", "metal"):
            for category in ("Indicated", "Inferred"):
                want = (gt["resources"].get(category) or {}).get(field)
                got = (shaped.get(category) or {}).get(field)
                if want is None and got is None:
                    continue
                ok = _close(want, got)
                if not ok:
                    failures += 1
                flag = "OK " if ok else "MISMATCH"
                print(f"  [{flag}] {category}.{field}: want={want} got={got}")

        # 单位是字符串，大小写归一后再比（Mlb == mlb）
        for category in ("Indicated", "Inferred"):
            want = (gt["resources"].get(category) or {}).get("metal_unit")
            got = (shaped.get(category) or {}).get("metal_unit")
            if want is None and got is None:
                continue
            ok = (want or "").lower() == (got or "").lower()
            if not ok:
                failures += 1
            flag = "OK " if ok else "MISMATCH"
            print(f"  [{flag}] {category}.metal_unit: want={want} got={got}")

        missing = extractor.required_categories_present(result)
        if missing:
            print(f"  [WARN] missing categories: {missing}")

    print(f"\nfailures={failures}")
    return 0 if failures == 0 else 1


def _close(want: object, got: object, tolerance_pct: float = 5.0) -> bool:
    if want is None or got is None:
        return want is got or (want is None and got is None)
    try:
        w, g = float(want), float(got)
    except (TypeError, ValueError):
        return want == got
    if w == 0:
        return abs(g) < 1e-9
    return abs(g - w) / abs(w) * 100.0 <= tolerance_pct


if __name__ == "__main__":
    raise SystemExit(main())
