"""PDF 抽取调试：打印每页原始抽取文本与字体信息。

用法：
    python scripts/debug_pdf.py [pdf路径]
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from servers.common.config import FIXTURE_DIR          # noqa: E402
from servers.mineral_pdf_mcp import pdf_text           # noqa: E402


def main() -> int:
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        FIXTURE_DIR / "newmont_ni43-101_synthetic.pdf")
    data = target.read_bytes()
    print(f"file={target.name} bytes={len(data)}")

    parser = pdf_text._Parser(data)
    parser.parse_all()
    print(f"objects={len(parser.objects)} streams={len(parser.streams)}")
    page_objs = parser.page_objects()
    print(f"page_objects={len(page_objs)}")

    fonts = pdf_text._FontMap(parser)
    print(f"font_maps={list(fonts.by_obj)} two_byte={fonts.two_byte}")
    for name, mapping in fonts.by_obj.items():
        sample = list(mapping.items())[:6]
        print(f"  font obj {name}: entries={len(mapping)} sample={sample}")

    for index, page_obj in enumerate(page_objs, start=1):
        raw_contents = page_obj.get("Contents")
        print(f"  page {index}: raw_contents={raw_contents!r}")
        resources = parser.resolve(page_obj.get("Resources")) or {}
        contents = parser.resolve(raw_contents)
        print(f"    resolved_contents={contents!r}"[:300])
        refs = []
        if isinstance(contents, dict):
            contents = [contents]
        for item in (contents or []):
            rnum = pdf_text._ref_num(item)
            refs.append(rnum)
        print(f"  page {index}: content_refs={refs} resources_keys={list(resources)}")

    print("\n--- objects ---")
    for num, obj in sorted(parser.objects.items()):
        kind = type(obj).__name__
        extra = ""
        if isinstance(obj, dict):
            extra = f"keys={sorted(obj)}"
            if obj.get("Type") == "Font":
                extra += f" subtype={obj.get('Subtype')} hasToUnicode={'ToUnicode' in obj}"
        print(f"  obj {num}: {kind} {extra} stream={'yes' if num in parser.streams else 'no'}")
    print(f"stream object numbers={sorted(parser.streams)}")
    for num, raw in sorted(parser.streams.items()):
        print(f"\n--- decoded stream {num} ({len(raw)} bytes) ---")
        print(raw[:900].decode("latin-1", errors="replace"))

    doc = pdf_text.extract_document(data)
    for page in doc.pages:
        print(f"\n----- page {page.number} fonts={page.fonts} chars={len(page.text)} -----")
        print(page.text[:1500])
    print(f"\nwarnings={doc.warnings}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
