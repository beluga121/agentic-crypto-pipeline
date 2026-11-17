"""Convert docs/FUTURE_IMPROVEMENTS.md into docs/Future_Improvements.docx.

Usage (from the project root, after `pip install -r requirements-dev.txt`):
    python docs/build_docx.py

Handles only the small Markdown subset the source file uses: "#", "##",
and "**" headings, "- " bullets, **bold** spans, `code` spans, and plain
paragraphs. Simple on purpose: no extra Markdown parser dependency.
"""

import re
from pathlib import Path

from docx import Document
from docx.shared import Pt, RGBColor

DOCS_DIR = Path(__file__).resolve().parent
SOURCE = DOCS_DIR / "FUTURE_IMPROVEMENTS.md"
TARGET = DOCS_DIR / "Future_Improvements.docx"

# Matches **bold** or `code` spans inside a line.
INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`)")


def add_inline(paragraph, text: str) -> None:
    """Add text to a paragraph, turning **bold** and `code` spans into styled runs."""
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            paragraph.add_run(part[2:-2]).bold = True
        elif part.startswith("`") and part.endswith("`"):
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
        else:
            paragraph.add_run(part)


def build(source: Path = SOURCE, target: Path = TARGET) -> Path:
    """Build the .docx file and return its path."""
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(11)

    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.rstrip()
        if not line:
            continue
        if line.startswith("Repository:"):
            # Placeholder line: make it stand out so it is not forgotten.
            para = doc.add_paragraph()
            run = para.add_run(line)
            run.bold = True
            run.font.color.rgb = RGBColor(0x8A, 0x3F, 0xFC)
        elif line.startswith("# "):
            doc.add_heading(line[2:], level=0)
        elif line.startswith("## "):
            doc.add_heading(line[3:], level=1)
        elif line.startswith("**") and line.endswith("**"):
            doc.add_heading(line.strip("*"), level=2)
        elif line.startswith("- "):
            add_inline(doc.add_paragraph(style="List Bullet"), line[2:])
        else:
            add_inline(doc.add_paragraph(), line)

    doc.save(target)
    return target


if __name__ == "__main__":
    print(f"Wrote {build()}")
