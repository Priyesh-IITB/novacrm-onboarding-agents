# -*- coding: utf-8 -*-
"""Render PART1_Requirements_and_Workflow_Analysis.md to .docx (then soffice makes the PDF).
The workflow figure is inserted immediately after the "Proposed agentic workflow" heading.
"""
import re
from docx import Document
from docx.shared import Pt, Inches, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

SRC = "PART1_Requirements_and_Workflow_Analysis.md"
OUT = "PART1_Requirements_and_Workflow_Analysis.docx"
FONT = "Calibri"

doc = Document()
s = doc.sections[0]
s.page_width, s.page_height = Inches(8.5), Inches(11)
s.top_margin = s.bottom_margin = Inches(0.8)
s.left_margin = s.right_margin = Inches(0.9)
st = doc.styles["Normal"]
st.font.name = FONT; st.font.size = Pt(10.5); st.font.color.rgb = RGBColor(0, 0, 0)
st.element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
st.paragraph_format.space_after = Pt(6); st.paragraph_format.line_spacing = 1.12

_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")


def rich(p, text):
    """Bold **x** and monospace `x`; everything else plain."""
    for chunk in re.split(r"(\*\*.+?\*\*|`[^`]+`)", text):
        if not chunk:
            continue
        m_b, m_c = _BOLD.fullmatch(chunk), _CODE.fullmatch(chunk)
        r = p.add_run(m_b.group(1) if m_b else m_c.group(1) if m_c else chunk)
        r.font.name = "Consolas" if m_c else FONT
        r.font.size = Pt(9.8 if m_c else 10.5)
        r.bold = bool(m_b)
        r._element.rPr.rFonts.set(qn("w:eastAsia"), r.font.name)


def head(text, level):
    sizes = {1: 17, 2: 13.5, 3: 11.5}
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(2 if level == 1 else 12)
    p.paragraph_format.space_after = Pt(5)
    r = p.add_run(text); r.bold = True; r.font.name = FONT; r.font.size = Pt(sizes[level])
    r._element.rPr.rFonts.set(qn("w:eastAsia"), FONT)
    return p


def add_table(rows):
    """rows[0] is the header. Markdown pipe tables become real docx tables."""
    t = doc.add_table(rows=len(rows), cols=len(rows[0]))
    t.style = "Table Grid"
    t.autofit = True
    for i, row in enumerate(rows):
        for j, cell in enumerate(row):
            para = t.cell(i, j).paragraphs[0]
            para.paragraph_format.space_after = Pt(2)
            rich(para, cell)
            for r in para.runs:
                r.font.size = Pt(9.5)
                if i == 0:
                    r.bold = True
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def split_row(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


lines = open(SRC, encoding="utf-8").read().splitlines()
figure_pending = False
i = 0
while i < len(lines):
    line = lines[i].rstrip()
    i += 1
    if not line.strip():
        continue
    # --- markdown pipe table -------------------------------------------------
    if line.lstrip().startswith("|") and i < len(lines) and set(lines[i].replace("|", "").strip()) <= set("-: "):
        rows = [split_row(line)]
        i += 1  # skip the separator row
        while i < len(lines) and lines[i].lstrip().startswith("|"):
            rows.append(split_row(lines[i]))
            i += 1
        width = max(len(r) for r in rows)
        add_table([r + [""] * (width - len(r)) for r in rows])
        continue
    if line.startswith("### "):
        head(line[4:], 3)
    elif line.startswith("## "):
        head(line[3:], 2)
        if "Proposed agentic workflow" in line:
            figure_pending = True
    elif line.startswith("# "):
        head(line[2:], 1)
    elif re.match(r"^[-*] ", line):
        p = doc.add_paragraph(style="List Bullet")
        p.paragraph_format.space_after = Pt(3)
        rich(p, line[2:])
    elif re.match(r"^\d+\. ", line):
        p = doc.add_paragraph(style="List Number")
        p.paragraph_format.space_after = Pt(3)
        rich(p, line.split(". ", 1)[1])
    else:
        p = doc.add_paragraph()
        rich(p, line)
        if figure_pending:
            fig = doc.add_paragraph(); fig.alignment = WD_ALIGN_PARAGRAPH.CENTER
            fig.add_run().add_picture("workflow.png", width=Inches(6.5))
            cap = doc.add_paragraph(); cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
            cr = cap.add_run("Figure 1. Closed-deal email to live onboarding, with every branch that ends at a person.")
            cr.italic = True; cr.font.size = Pt(9); cr.font.name = FONT
            figure_pending = False

doc.save(OUT)
print("wrote", OUT)
