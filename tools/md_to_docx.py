"""把 Markdown 使用手册转成 Word 文档。

让 ``.md`` 保持唯一事实来源：改手册只改 markdown，重跑一次这个脚本即可，
不用维护两份内容。

    python tools/md_to_docx.py 使用手册.md 使用手册.docx

支持：标题、表格、代码块、引用块、有序/无序列表、分隔线，
以及行内 ``**粗体**`` / ``*斜体*`` / `` `代码` ``。

中文需要显式设 ``w:eastAsia`` 字体 —— 只设 ``run.font.name`` 不足以让
中文按预期字体渲染（见 office-docx 技能说明）。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

BODY_FONT = "Calibri"
CJK_FONT = "微软雅黑"
MONO_FONT = "Consolas"

_ORDERED = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_UNORDERED = re.compile(r"^\s*[-*]\s+(.*)$")
#: Markdown 链接。Word 里锚点跳转不成立，所以只保留可读文字。
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")


def _style_run(run, *, mono: bool = False, bold: bool = False, italic: bool = False,
               size: float | None = None, color: str | None = None) -> None:
    """设置中英文字体。中文必须额外设 w:eastAsia，否则字体不生效。"""
    name = MONO_FONT if mono else BODY_FONT
    run.font.name = name
    run._element.rPr.rFonts.set(qn("w:eastAsia"), MONO_FONT if mono else CJK_FONT)
    run.bold = bold or run.bold
    run.italic = italic or run.italic
    if size is not None:
        run.font.size = Pt(size)
    if color:
        run.font.color.rgb = RGBColor.from_string(color)


def add_runs(paragraph, text: str, *, mono: bool = False, base_bold: bool = False) -> None:
    """把行内标记渲染成 run：**粗体** / *斜体* / `代码` / [链接](地址)。"""
    text = _LINK.sub(lambda m: m.group(1), text)
    # "粗体套行内代码"（**`x`**）先降级成普通行内代码：下面按反引号切分后，
    # 那对 ** 会失去配对、变成字面量显示出来
    text = re.sub(r"\*\*`([^`]+)`\*\*", r"`\1`", text)
    text = re.sub(r"\*`([^`]+)`\*", r"`\1`", text)
    # 先按行内代码切开，代码段内不再解析强调标记
    for chunk in re.split(r"(`[^`]*`)", text):
        if not chunk:
            continue
        if chunk.startswith("`") and chunk.endswith("`") and len(chunk) > 1:
            run = paragraph.add_run(chunk[1:-1])
            _style_run(run, mono=True, bold=base_bold, size=9.5)
            continue
        for piece in re.split(r"(\*\*[^*]+\*\*|\*[^*]+\*)", chunk):
            if not piece:
                continue
            bold = base_bold
            italic = False
            body = piece
            if piece.startswith("**") and piece.endswith("**") and len(piece) > 4:
                bold, body = True, piece[2:-2]
            elif piece.startswith("*") and piece.endswith("*") and len(piece) > 2:
                italic, body = True, piece[1:-1]
            run = paragraph.add_run(body)
            _style_run(run, mono=mono, bold=bold, italic=italic)


def add_code_block(document: Document, lines: list[str]) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.left_indent = Pt(18)
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(8)
    for index, line in enumerate(lines):
        if index:
            paragraph.add_run().add_break()
        run = paragraph.add_run(line if line else " ")
        _style_run(run, mono=True, size=9)


def add_table(document: Document, rows: list[list[str]]) -> None:
    if not rows:
        return
    width = max(len(r) for r in rows)
    table = document.add_table(rows=0, cols=width)
    table.style = "Light Grid Accent 1"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    for row_index, cells in enumerate(rows):
        cells = cells + [""] * (width - len(cells))
        row = table.add_row()
        for column, text in enumerate(cells):
            cell = row.cells[column]
            cell.text = ""
            add_runs(cell.paragraphs[0], text, base_bold=(row_index == 0))
    document.add_paragraph()


def _split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _is_separator(line: str) -> bool:
    return bool(re.match(r"^\s*\|[\s:|-]+\|\s*$", line))


def convert(md_path: Path, docx_path: Path) -> dict:
    lines = md_path.read_text(encoding="utf-8").splitlines()
    document = Document()

    # 正文字体
    normal = document.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), CJK_FONT)

    stats = {"headings": 0, "tables": 0, "codes": 0, "paragraphs": 0, "lists": 0, "quotes": 0}

    index = 0
    in_code = False
    code_lines: list[str] = []
    table_rows: list[list[str]] = []

    def flush_table() -> None:
        nonlocal table_rows
        if table_rows:
            # 丢掉 |---|---| 分隔行
            body = [r for r in table_rows if not all(set(c) <= set("-: ") for c in r)]
            if body:
                add_table(document, body)
                stats["tables"] += 1
            table_rows = []

    while index < len(lines):
        line = lines[index]

        # 代码块
        if line.strip().startswith("```"):
            if in_code:
                add_code_block(document, code_lines)
                stats["codes"] += 1
                code_lines = []
                in_code = False
            else:
                flush_table()
                in_code = True
            index += 1
            continue
        if in_code:
            code_lines.append(line)
            index += 1
            continue

        # 表格
        if line.strip().startswith("|") and line.strip().endswith("|"):
            table_rows.append(_split_row(line))
            index += 1
            continue
        flush_table()

        stripped = line.strip()
        if not stripped:
            index += 1
            continue

        # 分隔线
        if re.match(r"^-{3,}$", stripped) or re.match(r"^\*{3,}$", stripped):
            index += 1
            continue

        # 标题
        heading = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2)
            if level == 1:
                paragraph = document.add_heading("", level=0)
            else:
                paragraph = document.add_heading("", level=min(level - 1, 4))
            add_runs(paragraph, text, base_bold=True)
            stats["headings"] += 1
            index += 1
            continue

        # 引用块
        if stripped.startswith(">"):
            paragraph = document.add_paragraph(style="Intense Quote")
            add_runs(paragraph, stripped.lstrip("> ").strip())
            stats["quotes"] += 1
            index += 1
            continue

        # 列表
        ordered = _ORDERED.match(line)
        unordered = _UNORDERED.match(line)
        if ordered:
            # 手写序号而不用 List Number 样式：Word 的编号列表会在整个文档里
            # 连续累加，第二个列表就从 16 开始 —— 手册里读起来很怪。
            paragraph = document.add_paragraph()
            paragraph.paragraph_format.left_indent = Pt(21)
            paragraph.paragraph_format.first_line_indent = Pt(-12)
            add_runs(paragraph, f"{ordered.group(1)}. {ordered.group(2)}")
            stats["lists"] += 1
            index += 1
            continue
        if unordered:
            paragraph = document.add_paragraph(style="List Bullet")
            add_runs(paragraph, unordered.group(1))
            stats["lists"] += 1
            index += 1
            continue

        paragraph = document.add_paragraph()
        add_runs(paragraph, stripped)
        stats["paragraphs"] += 1
        index += 1

    if in_code and code_lines:
        add_code_block(document, code_lines)
    flush_table()

    docx_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(str(docx_path))
    return stats


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    md_path, docx_path = Path(argv[1]), Path(argv[2])
    if not md_path.is_file():
        print(f"找不到 {md_path}")
        return 1
    stats = convert(md_path, docx_path)
    size = docx_path.stat().st_size / 1024
    print(f"已生成 {docx_path}（{size:.0f} KB）")
    print("  标题 {headings}  表格 {tables}  代码块 {codes}  段落 {paragraphs}  列表 {lists}  引用 {quotes}".format(**stats))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
