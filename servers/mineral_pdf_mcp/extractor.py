"""NI 43-101 资源量表格的确定性解析器（LLM 不可用时的兜底与交叉校验器）。

为什么要有它：
题面要求「Extractor 调强模型」。但把准确率 100% 押在模型输出上是工程不成熟的做法，
而且断网/额度耗尽时整个交付物就废了。所以这里提供一条纯规则的解析路径：
- 它不依赖网络，可离线复现，是评审 5 分钟内 demo 的保障；
- 同时它作为「第二意见」，与 LLM 结果做交叉校验（见 cross_check），
  两路不一致 → 降置信度 → 触发 Critic 挑刺或 abstain。

解析难点（真实 NI 43-101 报告里全都存在）：
1. 表格跨页：表头在某页、数据在下一页；
2. 合计量（Total）与分项（含 Inferred 嵌套在 Indicated 之内）混淆 —— 本解析器显式标注 nesting；
3. 品位单位混用：g/t Au、% Cu、% Li2O、% TFe，且常有 CuEq/ZnEq 等价品位干扰；
4. 金属量单位混用：oz（金衡盎司）与 t（公吨）、Mlb（百万磅）、kt；
5. 负数/零品位（"–"、"—"、Nil）表示该分类下无矿量。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# 分类标题 → 标准字段名
CATEGORY_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bmeasured\s*(?:&\s*)?(?:and\s*)?indicated\b|\bm\s*&\s*i\b|\bm\+i\b", re.I),
     "MeasuredAndIndicated"),
    (re.compile(r"\bmeasured\b", re.I), "Measured"),
    (re.compile(r"\bindicated\b", re.I), "Indicated"),
    (re.compile(r"\binferred\b", re.I), "Inferred"),
    (re.compile(r"\btotal\b|\bsum\b|\bcombined\b", re.I), "Total"),
]

# 品位单位优先级：等价品位（Eq）不是题目要的，必须排除
GRADE_UNIT_PATTERNS: list[tuple[re.Pattern[str], str, bool]] = [
    (re.compile(r"g\s*/\s*t\s*(?:au|ag)?\b", re.I), "g/t", False),
    (re.compile(r"\b(?:cu|zn|pb|ni|mo|li2o|fe|tfe)\s*eq\.?\b", re.I), "%", True),   # 等价品位
    (re.compile(r"%\s*(?:cu|zn|pb|ni|mo|li2o|fe|tfe|au|ag)?\b", re.I), "%", False),
    (re.compile(r"\bppm\b", re.I), "ppm", False),
]

# 金属量单位 → 公吨换算
METAL_UNIT_TO_TONNE: dict[str, float] = {
    "oz": 31.1034768 / 1_000_000.0,   # 金衡盎司 → 公吨
    "koz": 31.1034768 / 1_000.0,      # 千盎司
    "moz": 31.1034768 / 1_000.0 * 1000.0,
    "t": 1.0,
    "kt": 1_000.0,
    "mt": 1_000_000.0,
    "mlb": 453.59237,                 # 百万磅 → 公吨
    "klb": 453.59237 / 1000.0,
    "lb": 0.45359237,
    "mkg": 1_000_000.0,
}

_NUM = r"[-+]?\d[\d, ]*(?:\.\d+)?"
NULL_TOKEN = re.compile(r"^(?:[-–—−]{1,2}|nil|none|n/?a|not applicable|0)$", re.I)


@dataclass
class ResourceRow:
    category: str
    tonnes_mt: float | None
    grade_value: float | None
    grade_unit: str | None
    grade_is_equivalent: bool
    metal_value: float | None
    metal_unit: str | None
    metal_tonnes: float | None
    included_in: str | None = None      # 嵌套关系，如 Inferred 含于 Indicated
    is_total: bool = False
    raw_line: str = ""
    page: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "tonnes_mt": self.tonnes_mt,
            "grade_value": self.grade_value,
            "grade_unit": self.grade_unit,
            "grade_is_equivalent": self.grade_is_equivalent,
            "metal_value": self.metal_value,
            "metal_unit": self.metal_unit,
            "metal_tonnes": self.metal_tonnes,
            "included_in": self.included_in,
            "is_total": self.is_total,
            "raw_line": self.raw_line,
            "page": self.page,
        }


def _to_float(token: str) -> float | None:
    cleaned = token.replace(",", "").replace(" ", "").replace("\u00a0", "")
    if NULL_TOKEN.match(cleaned):
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def detect_category(line: str) -> str | None:
    for pattern, name in CATEGORY_PATTERNS:
        if pattern.search(line):
            return name
    return None


def detect_grade_unit(line: str) -> tuple[str | None, bool]:
    for pattern, unit, is_eq in GRADE_UNIT_PATTERNS:
        if pattern.search(line):
            return unit, is_eq
    return None, False


def detect_metal_unit(line: str) -> str | None:
    # 更长/更具体的单位优先（moz 不能先被 oz 命中）
    for token in ("moz", "koz", "mlb", "klb", "mkg", "kt", "mt", "lb", "oz", "t"):
        if re.search(rf"\b{token}\b", line, re.I):
            return token
    return None


def _metal_to_tonnes(value: float | None, unit: str | None) -> float | None:
    if value is None or unit is None:
        return None
    factor = METAL_UNIT_TO_TONNE.get(unit.lower())
    return None if factor is None else value * factor


def _looks_like_header(line: str) -> bool:
    """表头行：没有具体数值，只有列名。"""
    return bool(
        re.search(r"tonnes|grade|contained|metal|category|classification|cut-?off",
                  line, re.I)
    ) and not re.search(_NUM + r"\s*$", line)


# 脚注/说明文字里出现分类词，但不是数据行 —— line 模式必须挡掉
_FOOTNOTE_HINTS = re.compile(
    r"notes?\s*[:(]|due to rounding|considered too speculative|not included in|"
    r"reported separately|for comparison only|cut-?off grade and are classified|"
    r"in accordance with|are considered too|do not have demonstrated",
    re.I,
)


def _is_data_line(line: str) -> bool:
    """一行是否可能是资源量数据行（而非脚注/说明）。"""
    if _FOOTNOTE_HINTS.search(line):
        return False
    if len(line) > 180:
        return False
    return len(re.findall(rf"(?<![\w.]){_NUM}(?![\w])", line)) >= 2


def parse_rows_from_lines(
    lines: Iterable[str], *, page: int | None = None
) -> list[ResourceRow]:
    """从（已按页/行切好的）文本行解析资源量行。

    规则：一行里若出现分类词 + ≥2 个数值，则按 [Mt, grade, metal] 顺序取值；
    数值个数不足时留空但不丢弃该行（记为候选，供 Critic 复核）。
    """
    rows: list[ResourceRow] = []
    for raw in lines:
        line = raw.strip()
        if not line or len(line) < 4 or _looks_like_header(line):
            continue
        if not _is_data_line(line):
            continue
        category = detect_category(line)
        if category is None:
            continue
        numbers = [
            _to_float(m.group(0))
            for m in re.finditer(rf"(?<![\w.]){_NUM}(?![\w])", line)
        ]
        # 分类词本身常带数字（如 "NI 43-101"），排除明显属于标题/编号的数值
        numbers = [n for n in numbers if n is not None]
        if not numbers:
            continue

        grade_unit, is_eq = detect_grade_unit(line)
        metal_unit = detect_metal_unit(line)

        tonnes_mt = numbers[0] if len(numbers) >= 1 else None
        grade_value = numbers[1] if len(numbers) >= 2 else None
        metal_value = numbers[2] if len(numbers) >= 3 else None

        # 单位合理性校验：Mt 量级通常在 0.01 ~ 100000
        if tonnes_mt is not None and not (0.001 <= abs(tonnes_mt) <= 500_000):
            tonnes_mt, grade_value, metal_value = None, tonnes_mt, grade_value

        # 品位合理性：g/t 通常 < 1000，百分比通常 < 100
        if grade_value is not None and grade_unit == "g/t" and abs(grade_value) > 5000:
            grade_value = None
        if grade_value is not None and grade_unit == "%" and abs(grade_value) > 100:
            grade_value = None

        rows.append(
            ResourceRow(
                category=category,
                tonnes_mt=tonnes_mt,
                grade_value=grade_value,
                grade_unit=grade_unit,
                grade_is_equivalent=is_eq,
                metal_value=metal_value,
                metal_unit=metal_unit,
                metal_tonnes=_metal_to_tonnes(metal_value, metal_unit),
                is_total=category == "Total",
                raw_line=line,
                page=page,
            )
        )
    return rows


def annotate_nesting(rows: list[ResourceRow]) -> list[ResourceRow]:
    """标注「含于」关系：NI 43-101 常把 Inferred 写在 Indicated 行内（含于关系）。

    判据：同一段落内，Inferred 行紧跟在非 Total 行之后，且其 Mt 明显小于前一行。
    """
    for idx, row in enumerate(rows):
        if row.category != "Inferred" or row.tonnes_mt is None:
            continue
        for prev in reversed(rows[:idx]):
            if prev.is_total or prev.tonnes_mt in (None, 0):
                continue
            if prev.category in {"Indicated", "Measured", "MeasuredAndIndicated"}:
                if row.tonnes_mt < prev.tonnes_mt * 0.9:
                    row.included_in = prev.category
                break
    return rows


def tonnes_from_grade(
    tonnes_mt: float | None, grade_value: float | None, grade_unit: str | None
) -> float | None:
    """由 Mt + 品位反算金属吨，用于与表格金属量列交叉校验。"""
    if tonnes_mt is None or grade_value is None or grade_unit is None:
        return None
    mass_t = tonnes_mt * 1_000_000.0
    if grade_unit == "g/t":
        return mass_t * grade_value / 1_000_000.0      # g/t → t
    if grade_unit == "%":
        return mass_t * grade_value / 100.0
    if grade_unit == "ppm":
        return mass_t * grade_value / 1_000_000.0
    return None


def cross_check(row: ResourceRow, tolerance_pct: float = 5.0) -> dict[str, Any]:
    """品位×矿量 vs 表格金属量的一致性检查。"""
    derived = tonnes_from_grade(row.tonnes_mt, row.grade_value, row.grade_unit)
    if derived is None or row.metal_tonnes in (None, 0):
        return {"consistent": None, "reason": "insufficient_fields",
                "derived_tonnes": derived, "table_tonnes": row.metal_tonnes}
    delta_pct = abs(derived - row.metal_tonnes) / abs(row.metal_tonnes) * 100.0
    return {
        "consistent": delta_pct <= tolerance_pct,
        "delta_pct": round(delta_pct, 2),
        "derived_tonnes": derived,
        "table_tonnes": row.metal_tonnes,
    }


SECTION_START = re.compile(
    r"(?:\d{1,2}(?:\.\d{1,2})*)\s*(?:mineral\s+)?resources?\s+(?:estimate|summary|table)|"
    r"mineral\s+resources?\s+estimate|"
    r"resource\s+statement|"
    r"table\s+\d+\s*[-–:]\s*mineral\s+resources?",
    re.I,
)
SECTION_END = re.compile(r"mineral\s+reserve|capital\s+cost|economic\s+analysis", re.I)


def extract_relevant_pages(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """先按章节切分（resource 章节起 → reserve 章节止），减少误命中。"""
    selected: list[tuple[int, str]] = []
    in_section = False
    for page_no, text in pages:
        if not in_section and SECTION_START.search(text):
            in_section = True
        if in_section:
            selected.append((page_no, text))
            if SECTION_END.search(text):
                in_section = False
    return selected or pages


# ------------------------------------------------------- cell 版式解析
_HEADER_TOKENS = re.compile(
    r"\b(tonnes|tonnage|grade|contained|metal|category|classification|class|cut-?off|"
    r"quantity|amount|cu\s*eq|au\s*eq|li2o|m\s*&\s*i)\b", re.I)


def _classify_header(cell: str) -> str | None:
    """列语义判定。必须拒绝正文句子 —— 真实报告里 'Grade column shows % Cu; ...'
    这类说明文字就混在表格附近，误判会让整个数据区错位。
    """
    low = cell.lower()
    tokens = low.split()
    if len(tokens) > 4:
        return None                       # 太长的多半是句子，不是列名
    # 'classification' / 'classified' 都会命中 'class'，必须先排除动词形态
    if re.search(r"categ|classif|^class\b|\bclass\b", low):
        return "category"
    if re.match(r"^\d+(\.\d+)*\s", low):
        return None                       # 章节号开头的标题，如 "14. Mineral Resource..."
    if re.search(r"\b(tonnes|tonnage|quantity)\b", low) and "metal" not in low:
        return "tonnes"
    if "grade" in low:
        return "eq_grade" if re.search(r"\beq\b|equivalent", low) else "grade"
    if "metal" in low or "contained" in low:
        return "metal"
    return None


def _unit_from_header(header: str) -> str | None:
    """从表头文字里提取单位，如 'Grade (g/t)' → 'g/t'，'Tonnes (Mt)' → 'Mt'。"""
    in_parens = re.findall(r"[(（]([^)）]{1,24})[)）]", header)
    if in_parens:
        return in_parens[0].strip()
    unit, _ = detect_grade_unit(header)
    if unit:
        return unit
    m = re.search(r"\b(mt|kt|moz|koz|mlb|klb|oz|t|ppm)\b", header, re.I)
    return m.group(1) if m else None


_COLUMN_TOKEN = re.compile(
    r"categ|classif|\bclass\b|\btonnes?\b|tonnage|grade|metal|contained|quantity|"
    r"\bmt\b|\bkt\b|\boz\b|\bmlb\b|\bg/t\b|%|\bcu\b|\baq\b|cut-?off", re.I)
_NUMBER_IN_TEXT = re.compile(r"\d")


def _looks_like_column_header(text: str) -> bool:
    """单格判定：分类列名、词数少、无数字。"""
    low = text.lower().strip()
    if not re.search(r"categ|classif|^class\b|\bclass\b", low):
        return False
    tokens = low.split()
    if len(tokens) > 4:
        return False
    return not _NUMBER_IN_TEXT.search(text)


def _is_header_row(cells: list[str]) -> bool:
    """表头判定：以「分类列名」起头，随后是若干列名，且整行不含数值。

    三个必须同时成立的条件，缺一个就会把正文/脚注误当表头：
    1. 第一格是分类列名（Category / Classification）；
    2. 整行没有纯数值格；
    3. 整行没有数字 —— 用来挡住 'Grade column shows % Cu ...' 这类说明文字。
    """
    if not cells:
        return False
    if not _looks_like_column_header(cells[0]):
        return False
    joined = " ".join(cells).lower()
    if not re.search(r"tonnes|tonnage|grade|metal|contained|quantity", joined):
        return False
    if any(_to_float(c) is not None for c in cells):
        return False
    return not _NUMBER_IN_TEXT.search(" ".join(cells))


def parse_cell_layout(lines: list[str], *, page: int | None = None) -> list[ResourceRow]:
    """解析「每单元格独立成行」的抽取结果。

    真实工具（pdfplumber page.extract_text()、reportlab 输出、部分 OCR）常见此版式：
        Category / Tonnes (Mt) / Grade (g/t) / Contained metal (oz) /
        Indicated / 21.60 / 2.74 / 1,903,000
    表头给出列语义与单位，因此这里比 line 模式更可靠 —— 主路径。

    三个必须处理的真实情况：
    1. 分页续表：第二页只有数据行、没有重复表头 → 沿用上一张表的列定义；
    2. 同分类多行（分域/分表）：合并补全字段，而不是互相覆盖；
    3. 干扰表（CuEq 等价品位）：列语义不同，必须与主表分开累积。
    """
    cells = [line.strip() for line in lines if line.strip()]
    rows: list[ResourceRow] = []
    index = 0
    column_roles: list[str] = []
    column_units: list[str | None] = []
    current_rows: dict[str, ResourceRow] = {}

    def flush() -> None:
        if current_rows:
            rows.extend(current_rows.values())
            current_rows.clear()

    while index < len(cells):
        # 1) 新表头：重开一张表
        probe = cells[index:index + 4]
        if _is_header_row(probe):
            roles: list[str] = []
            units: list[str | None] = []
            scan = index
            while scan < len(cells) and len(roles) < 6:
                role = _classify_header(cells[scan])
                if role is None:
                    if roles:
                        break
                    scan += 1
                    continue
                roles.append(role)
                units.append(_unit_from_header(cells[scan]))
                scan += 1
            if "category" in roles and len(roles) >= 2:
                flush()
                column_roles, column_units = roles, units
                index = scan
                continue
            index += 1
            continue

        # 2) 数据区（含无表头的续表）
        if column_roles:
            expected = len(column_roles)
            block = cells[index:index + expected]
            category = detect_category(block[0]) if block else None
            if category is not None and len(block) == expected:
                row = _row_from_cells(category, block, column_roles, column_units, page,
                                      cells, index)
                if row is not None:
                    existing = current_rows.get(category)
                    if existing is None:
                        current_rows[category] = row
                    else:
                        _merge_row(existing, row)
                    index += expected
                    while (index < len(cells)
                           and re.fullmatch(r"\(\d+\)", cells[index] or "")):
                        index += 1
                    continue
            elif category is None and _is_row_start_of_new_table(block, cells, index):
                # 干扰表/未知表：结束当前表，继续扫描
                flush()
                index += 1
                continue
        index += 1

    flush()
    return rows


def _is_row_start_of_new_table(block: list[str], cells: list[str], index: int) -> bool:
    """启发式：当前格看起来是新表的首行（分类词）但列数不匹配 → 结束当前表。"""
    if not block:
        return False
    if detect_category(block[0]) is None:
        return False
    # 往后看几格，若出现分类词则更像是同一张表的数据行
    ahead = cells[index + 1:index + 3]
    return not any(detect_category(c) for c in ahead)


def _row_from_cells(category: str, block: list[str], roles: list[str],
                    units: list[str | None], page: int | None,
                    cells: list[str] | None = None, index: int = 0) -> ResourceRow | None:
    """按列语义取值。列错位（如缺列）时尝试右移对齐，仍不合理则放弃该行。"""
    for shift in (0, 1):
        if shift and (cells is None or index + len(roles) + shift > len(cells)):
            break
        attempt = block[shift:shift + len(roles)] if shift else block
        if len(attempt) < len(roles):
            continue
        values: dict[str, float | None] = {}
        unit_of: dict[str, str | None] = {}
        for offset, role in enumerate(roles):
            raw = attempt[offset]
            value = _to_float(raw)
            values[role] = value
            unit_of[role] = _normalize_unit(units[offset], raw, metal=(role == "metal"))
        tonnes = values.get("tonnes")
        grade = values.get("grade")
        grade_unit = unit_of.get("grade")
        metal = values.get("metal")
        # 表格里用 "—" 表示「未估算」，此时不能从表头继承单位：
        # 带单位但无值会被下游误读成「金属量为 0」，必须保持字段整体缺失。
        metal_unit = unit_of.get("metal") if metal is not None else None
        if grade is None:
            grade_unit = None

        plausible = True
        if tonnes is None or not (0.001 <= abs(tonnes) <= 500_000):
            plausible = False
        if grade is not None and grade_unit == "%" and abs(grade) > 100:
            plausible = False
        if grade is not None and grade_unit == "g/t" and abs(grade) > 5000:
            plausible = False
        if grade_unit is None and grade is not None:
            plausible = False
        if not plausible:
            continue

        return ResourceRow(
            category=category,
            tonnes_mt=tonnes,
            grade_value=grade,
            grade_unit=grade_unit,
            grade_is_equivalent=False,
            metal_value=metal,
            metal_unit=metal_unit,
            metal_tonnes=_metal_to_tonnes(metal, metal_unit),
            is_total=category == "Total",
            raw_line=" | ".join(block),
            page=page,
        )
    return None


def _prefer_richer(left: ResourceRow, right: ResourceRow) -> ResourceRow:
    """同分类多行（主表 vs. 干扰表/续表）择一：字段更全者优先。

    Barrick 样例里主表是 % Cu，紧随其后是 CuEq 干扰表；
    干扰表的品位单位无法识别（表头是 '% CuEq'），因此字段更少，会被这条规则淘汰。
    """
    return left if _richness(left) >= _richness(right) else right


def _richness(row: ResourceRow) -> int:
    return sum(
        1 for value in (row.tonnes_mt, row.grade_value, row.grade_unit,
                        row.metal_value, row.metal_unit, row.metal_tonnes)
        if value not in (None, "")
    )


def _merge_row(target: ResourceRow, source: ResourceRow) -> None:
    """同分类重复出现时，用后一行的非空字段补全前一行（应对分页续表）。"""
    for attr in ("tonnes_mt", "grade_value", "grade_unit", "metal_value", "metal_unit",
                 "metal_tonnes"):
        if getattr(target, attr) in (None, "") and getattr(source, attr) not in (None, ""):
            setattr(target, attr, getattr(source, attr))
    if target.page is None:
        target.page = source.page
    target.raw_line = f"{target.raw_line} || {source.raw_line}"


def _normalize_unit(unit: str | None, cell: str, metal: bool = False) -> str | None:
    """表头单位是首选；表头缺失时回落到单元格内的单位线索。"""
    if unit:
        low = unit.lower().strip()
        if metal and low in METAL_UNIT_TO_TONNE:
            return low
        if not metal and low in {"g/t", "%", "ppm", "g/t au", "g/t ag"}:
            return "g/t" if low.startswith("g/t") else low
        if low in METAL_UNIT_TO_TONNE:
            return low
    if metal:
        return detect_metal_unit(cell)
    detected, _ = detect_grade_unit(cell)
    return detected



def parse_resource_tables(
    pages: list[tuple[int, str]], *, tolerance_pct: float = 5.0
) -> dict[str, Any]:
    """主入口：页文本 → 结构化资源量 + 交叉校验结果。

    两种版式都支持，且可同时命中同一份文件：
    - cell 模式：PDF 表格抽取后每个单元格独立成行（pdfplumber/reportlab 的常见输出）
    - line 模式：数据在同一文本行内（HTML→text、部分原生 PDF）
    两条路径的结果按分类合并，cell 模式优先（它带表头语义，更可靠）。
    """
    scoped = extract_relevant_pages(pages)
    cell_rows: list[ResourceRow] = []
    line_rows: list[ResourceRow] = []
    for page_no, text in scoped:
        cell_rows.extend(parse_cell_layout(text.splitlines(), page=page_no))
        line_rows.extend(parse_rows_from_lines(text.splitlines(), page=page_no))

    merged: dict[tuple[str, bool], ResourceRow] = {}
    for row in cell_rows:                      # cell 优先写入
        key = (row.category, row.is_total)
        existing = merged.get(key)
        merged[key] = row if existing is None else _prefer_richer(existing, row)
    for row in line_rows:                      # line 仅在完全缺失该分类时补充
        merged.setdefault((row.category, row.is_total), row)
    rows = annotate_nesting(list(merged.values()))
    rows.sort(key=lambda r: (r.page or 0, r.category))

    by_category: dict[str, dict[str, Any]] = {}
    for row in rows:
        if row.is_total or row.included_in:
            continue
        current = by_category.get(row.category)
        if current is None or (row.tonnes_mt or 0) > (current.get("tonnes_mt") or 0):
            by_category[row.category] = {
                **row.to_dict(),
                "cross_check": cross_check(row, tolerance_pct),
            }

    totals = [r.to_dict() for r in rows if r.is_total]
    nested = [
        {**r.to_dict(), "cross_check": cross_check(r, tolerance_pct)}
        for r in rows if r.included_in
    ]
    checks = [
        {"category": r.category, **cross_check(r, tolerance_pct)}
        for r in rows if not r.is_total
    ]
    consistent = [c for c in checks if c["consistent"] is not None]
    consistency_rate = (
        sum(1 for c in consistent if c["consistent"]) / len(consistent) if consistent else None
    )

    return {
        "categories": by_category,
        "totals": totals,
        "nested": nested,
        "rows": [r.to_dict() for r in rows],
        "stats": {
            "rows_found": len(rows),
            "rows_cell_layout": len(cell_rows),
            "rows_line_layout": len(line_rows),
            "categories_found": sorted(by_category),
            "pages_scanned": len(scoped),
            "pages_total": len(pages),
            "grade_metal_consistency_rate": (
                None if consistency_rate is None else round(consistency_rate, 3)
            ),
        },
    }


def required_categories_present(result: dict[str, Any]) -> list[str]:
    """题目点名要 Indicated 与 Inferred；nested（含于关系）也算已抽到。"""
    present = set((result.get("categories") or {}).keys())
    present |= {row.get("category") for row in (result.get("nested") or [])}
    return [c for c in ("Indicated", "Inferred") if c not in present]


def build_ground_truth_shaped(result: dict[str, Any]) -> dict[str, Any]:
    """把解析结果压成与 ground truth JSON 同构的形状，便于字段级比对。

    注意两个真实语义，不能简化掉：
    - Inferred 常以「含于 Indicated 之内」的增量形式出现（nested），
      它仍应作为一个可汇报的储量分类返回，只是要标明 contains/included_in 关系；
    - 缺失字段（表格里是 "—"）必须保持 None，而不是补 0 —— 这是 abstain 的触发条件。
    """
    by_category = dict(result.get("categories") or {})
    for row in result.get("nested") or []:
        by_category.setdefault(row["category"], row)

    out: dict[str, Any] = {}
    for category in ("Indicated", "Inferred"):
        row = by_category.get(category)
        if not row:
            out[category] = None
            continue
        out[category] = {
            "tonnes_mt": row.get("tonnes_mt"),
            "grade": row.get("grade_value"),
            "grade_unit": row.get("grade_unit"),
            "metal": row.get("metal_value"),
            "metal_unit": row.get("metal_unit"),
            "metal_tonnes": row.get("metal_tonnes"),
            "page": row.get("page"),
            "included_in": row.get("included_in"),
        }
    return out
