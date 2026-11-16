"""Turn uploaded files (PDF, Word, PowerPoint, text) into citeable passages ("chunks").

Each chunk remembers where it came from (page, slide or section) so answers can cite
"Triage SOP, p. 2" and the viewer can jump to it.
"""
from __future__ import annotations

import hashlib
import io
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import settings

SUPPORTED = {".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".txt": "txt", ".md": "md"}


@dataclass
class Block:
    """A contiguous run of text from one page/slide/section."""
    text: str
    page: int | None = None          # PDF page or slide number
    section: str = ""                # nearest heading / slide title
    locator: str = ""                # human label: "p. 3", "Slide 7", "Fluid therapy"


@dataclass
class Extracted:
    file_type: str
    blocks: list[Block]
    page_count: int | None = None
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(b.text) for b in self.blocks)


def file_type_for(filename: str) -> str | None:
    return SUPPORTED.get(Path(filename).suffix.lower())


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- cleaning

_ws = re.compile("[ \t\u00a0]+")


def clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    for lig, rep in (("ﬀ", "ff"), ("ﬁ", "fi"), ("ﬂ", "fl"), ("ﬃ", "ffi"), ("ﬄ", "ffl")):
        text = text.replace(lig, rep)
    text = re.sub("(?<=[a-z])Ư(?=[a-z])", "ff", text)  # 'Ư' emitted for the ff-ligature by some Word-to-PDF exports
    text = text.replace("", "•").replace("", "•").replace("", "•")
    lines = [_ws.sub(" ", ln).strip() for ln in text.split("\n")]
    out = "\n".join(lines)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


# ---------------------------------------------------------------- PDF

def _ocr_page(pdf_bytes: bytes, page_index: int) -> str:
    """Best-effort OCR for scanned pages (needs `tesseract` installed + pypdfium2)."""
    if not shutil.which("tesseract"):
        return ""
    try:
        import pypdfium2 as pdfium  # type: ignore

        pdf = pdfium.PdfDocument(pdf_bytes)
        image = pdf[page_index].render(scale=2.5).to_pil()
        buf = io.BytesIO()
        image.save(buf, format="PNG")
        res = subprocess.run(
            ["tesseract", "stdin", "stdout", "--psm", "3"],
            input=buf.getvalue(), capture_output=True, timeout=120,
        )
        return res.stdout.decode("utf-8", "ignore")
    except Exception:
        return ""


def extract_pdf(data: bytes) -> Extracted:
    blocks: list[Block] = []
    warnings: list[str] = []
    page_count = 0
    try:
        import pdfplumber  # type: ignore

        with pdfplumber.open(io.BytesIO(data)) as pdf:
            page_count = len(pdf.pages)
            reader = None
            for i, page in enumerate(pdf.pages):
                txt = page.extract_text(x_tolerance=1.5, y_tolerance=3) or ""
                if "(cid:" in txt:  # unmapped glyphs (often ligatures) → try pypdf for this page
                    try:
                        if reader is None:
                            from pypdf import PdfReader
                            reader = PdfReader(io.BytesIO(data))
                        alt = reader.pages[i].extract_text() or ""
                        if "(cid:" not in alt and len(alt) > 0.6 * len(txt):
                            txt = alt
                    except Exception:
                        pass
                    txt = re.sub(r"\(cid:\d+\)", "", txt)
                blocks.append(Block(text=clean(txt), page=i + 1, locator=f"p. {i + 1}"))
    except Exception:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        page_count = len(reader.pages)
        for i, page in enumerate(reader.pages):
            blocks.append(Block(text=clean(page.extract_text() or ""), page=i + 1, locator=f"p. {i + 1}"))

    ocr_pages = []
    for i, b in enumerate(blocks):
        if len(b.text) < 25:
            ocr = clean(_ocr_page(data, i))
            if len(ocr) > len(b.text):
                b.text = ocr
                ocr_pages.append(i + 1)
    if ocr_pages:
        warnings.append(f"Text on page(s) {', '.join(map(str, ocr_pages))} was read with OCR; check the citations.")
    empty = [b.page for b in blocks if len(b.text) < 25]
    if empty and len(empty) == len(blocks):
        warnings.append("No readable text found (scanned or image-only PDF). Install tesseract on the server for OCR, or upload a text-based version.")
    elif empty:
        warnings.append(f"Page(s) {', '.join(map(str, empty))} have little or no text (images only).")
    blocks = [b for b in blocks if b.text]
    return Extracted("pdf", blocks, page_count, warnings)


# ---------------------------------------------------------------- Word

def extract_docx(data: bytes) -> Extracted:
    import docx  # python-docx
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(io.BytesIO(data))
    blocks: list[Block] = []
    heading = ""
    buf: list[str] = []

    def flush():
        text = clean("\n".join(buf))
        if text:
            blocks.append(Block(text=text, section=heading, locator=heading or "Document"))
        buf.clear()

    def para_text(p: Paragraph) -> str:
        t = p.text.strip()
        if not t:
            return ""
        style = (p.style.name or "").lower() if p.style is not None else ""
        if "list" in style or p._p.pPr is not None and p._p.pPr.numPr is not None:
            return f"- {t}"
        return t

    def is_heading(p: Paragraph) -> bool:
        style = (p.style.name or "").lower() if p.style is not None else ""
        if style.startswith("heading") or style == "title":
            return True
        t = p.text.strip()
        # Short, fully-bold standalone line → treat as a heading (common in SOP documents)
        runs = [r for r in p.runs if r.text.strip()]
        return bool(runs) and len(t) <= 90 and all(r.bold for r in runs) and not t.endswith(".")

    body = document.element.body
    for child in body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            p = Paragraph(child, document)
            if not p.text.strip():
                continue
            if is_heading(p):
                flush()
                heading = p.text.strip()[:120]
                buf.append(heading)
            else:
                buf.append(para_text(p))
        elif tag == "tbl":
            table = Table(child, document)
            for r in table.rows:
                cells = []
                for c in r.cells:
                    ct = " ".join(x.strip() for x in c.text.split("\n") if x.strip())
                    if ct and (not cells or cells[-1] != ct):
                        cells.append(ct)
                if cells:
                    buf.append(" | ".join(cells))
            buf.append("")
    flush()
    warnings = [] if blocks else ["No text found in this Word document."]
    return Extracted("docx", blocks, None, warnings)


# ---------------------------------------------------------------- PowerPoint

def extract_pptx(data: bytes) -> Extracted:
    from pptx import Presentation
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    prs = Presentation(io.BytesIO(data))
    blocks: list[Block] = []

    def shape_texts(shape) -> list[str]:
        out: list[str] = []
        if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
            for s in shape.shapes:
                out.extend(shape_texts(s))
            return out
        if getattr(shape, "has_table", False) and shape.has_table:
            for r in shape.table.rows:
                cells = [c.text.strip().replace("\n", " ") for c in r.cells]
                if any(cells):
                    out.append(" | ".join(cells))
            return out
        if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
            for para in shape.text_frame.paragraphs:
                t = "".join(run.text for run in para.runs).strip()
                if t:
                    out.append(("- " if para.level > 0 else "") + t)
        return out

    for idx, slide in enumerate(prs.slides, start=1):
        title = ""
        if slide.shapes.title is not None and slide.shapes.title.has_text_frame:
            title = slide.shapes.title.text_frame.text.strip().replace("\n", " ")
        lines: list[str] = []
        for shape in slide.shapes:
            if slide.shapes.title is not None and shape.shape_id == slide.shapes.title.shape_id:
                continue
            lines.extend(shape_texts(shape))
        notes = ""
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            notes = slide.notes_slide.notes_text_frame.text.strip()
        parts = [title] if title else []
        parts += lines
        if notes:
            parts.append(f"Speaker notes: {notes}")
        text = clean("\n".join(parts))
        if text:
            blocks.append(Block(text=text, page=idx, section=title[:120], locator=f"Slide {idx}"))
    warnings = [] if blocks else ["No text found on any slide."]
    return Extracted("pptx", blocks, len(prs.slides), warnings)


# ---------------------------------------------------------------- text / markdown

def extract_text(data: bytes, kind: str) -> Extracted:
    text = data.decode("utf-8", "ignore")
    blocks: list[Block] = []
    heading = ""
    buf: list[str] = []

    def flush():
        t = clean("\n".join(buf))
        if t:
            blocks.append(Block(text=t, section=heading, locator=heading or "Document"))
        buf.clear()

    for line in text.splitlines():
        m = re.match(r"^\s{0,3}#{1,4}\s+(.*)$", line)
        if m:
            flush()
            heading = m.group(1).strip().strip("*")[:120]
            buf.append(heading)
        else:
            buf.append(line)
    flush()
    return Extracted(kind, blocks, None, [] if blocks else ["File is empty."])


def extract(filename: str, data: bytes) -> Extracted:
    kind = file_type_for(filename)
    if kind == "pdf":
        return extract_pdf(data)
    if kind == "docx":
        return extract_docx(data)
    if kind == "pptx":
        return extract_pptx(data)
    if kind in ("txt", "md"):
        return extract_text(data, kind)
    raise ValueError(f"Unsupported file type: {Path(filename).suffix}. Upload PDF, Word (.docx), PowerPoint (.pptx), .txt or .md.")


# ---------------------------------------------------------------- chunking

def _split_long(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    paras = [p for p in re.split(r"\n(?=\S)", text) if p.strip()]
    out, cur = [], ""
    for p in paras:
        if len(p) > limit:  # very long paragraph → split on sentences
            for s in re.split(r"(?<=[.;:])\s+", p):
                if len(cur) + len(s) + 1 > limit and cur:
                    out.append(cur.strip())
                    cur = ""
                cur += s + " "
            continue
        if len(cur) + len(p) + 1 > limit and cur:
            out.append(cur.strip())
            cur = ""
        cur += p + "\n"
    if cur.strip():
        out.append(cur.strip())
    # small overlap so a dose split across chunks still has context
    with_overlap = [out[0]]
    for prev, nxt in zip(out, out[1:]):
        tail = prev[-160:]
        cut = tail.find("\n")
        tail = tail[cut + 1:] if 0 <= cut < 120 else tail
        with_overlap.append(f"…{tail}\n{nxt}")
    return with_overlap


def chunk(extracted: Extracted, limit: int | None = None) -> list[Block]:
    """Merge small blocks (same page/section) and split large ones."""
    limit = limit or settings.chunk_chars
    merged: list[Block] = []
    for b in extracted.blocks:
        prev = merged[-1] if merged else None
        same_unit = prev is not None and prev.page == b.page and (
            extracted.file_type in ("pdf", "pptx") or prev.section == b.section)
        if prev is not None and same_unit and len(prev.text) + len(b.text) < limit * 0.6:
            prev.text = prev.text + "\n" + b.text
            continue
        # Merge tiny sections (e.g. a heading with one line) into the previous Word/text chunk
        if (prev is not None and extracted.file_type in ("docx", "md", "txt") and len(b.text) < 220
                and len(prev.text) + len(b.text) < limit * 0.8):
            prev.text = prev.text + "\n\n" + b.text
            continue
        merged.append(Block(b.text, b.page, b.section, b.locator))

    final: list[Block] = []
    for b in merged:
        parts = _split_long(b.text, limit)
        for i, p in enumerate(parts):
            loc = b.locator
            if len(parts) > 1 and extracted.file_type not in ("pdf", "pptx"):
                loc = f"{b.locator} (part {i + 1})"
            text = p
            if i > 0 and b.section and extracted.file_type in ("docx", "md", "txt") and not p.startswith(b.section):
                text = f"[{b.section}]\n{p}"
            final.append(Block(text, b.page, b.section, loc))
    return final
