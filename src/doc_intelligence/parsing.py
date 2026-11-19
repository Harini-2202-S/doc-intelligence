"""Conversion of supported file formats into pages of plain text.

A page is the natural unit of each format: a PDF page, a slide, a block of spreadsheet rows, or a
section of a Word or text document. Citations reference page text by character offset.

Text that a reader cannot see (white text in PDFs, hidden runs in Word, hidden sheets in Excel)
is a known prompt-injection vector. It is excluded before indexing and reported as a warning.
"""

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime

SECTION_CHARS = 3500  # target size of a pseudo-page for formats without pages
ROWS_PER_PAGE = 120
MIN_DOCUMENT_CHARS = 20
PAGE_BREAK = "\f"  # marker for an explicit page break in Word documents


class ParseError(Exception):
    """Raised when a file cannot be converted to text. The message is safe to return to the client."""


@dataclass
class Page:
    label: str
    text: str


@dataclass
class ParsedDocument:
    pages: list[Page]
    warnings: list[str] = field(default_factory=list)

    @property
    def char_count(self) -> int:
        return sum(len(p.text) for p in self.pages)


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _sections(blocks: list[str], label: str) -> list[Page]:
    """Groups text blocks into pseudo-pages of about SECTION_CHARS, breaking before headings."""
    pages: list[Page] = []
    current: list[str] = []
    size = 0
    for block in blocks:
        if block == PAGE_BREAK:  # explicit page breaks always start a new section
            if current:
                pages.append(Page(f"{label} {len(pages) + 1}", _tidy("\n\n".join(current))))
                current, size = [], 0
            continue
        is_heading = block.startswith("#")
        if current and (size + len(block) > SECTION_CHARS or (is_heading and size > SECTION_CHARS * 0.5)):
            pages.append(Page(f"{label} {len(pages) + 1}", _tidy("\n\n".join(current))))
            current, size = [], 0
        current.append(block)
        size += len(block) + 2
    if current:
        pages.append(Page(f"{label} {len(pages) + 1}", _tidy("\n\n".join(current))))
    return [p for p in pages if p.text]


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == datetime.min.time() else value.isoformat(sep=" ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _table_pages(rows: list[list[str]], label: str) -> list[Page]:
    """Renders each row as "Header: value | Header: value" so that every chunk is self-describing."""
    rows = [r for r in rows if any(c for c in r)]
    if not rows:
        return []
    header, body = rows[0], rows[1:]
    if not body:
        return [Page(label, " | ".join(header))]
    names = [h or f"Column {i + 1}" for i, h in enumerate(header)]
    lines = [" | ".join(f"{n}: {v}" for n, v in zip(names, row, strict=False) if v) for row in body]
    pages = []
    for start in range(0, len(lines), ROWS_PER_PAGE):
        part = lines[start:start + ROWS_PER_PAGE]
        suffix = f" (rows {start + 2}-{start + 1 + len(part)})" if len(lines) > ROWS_PER_PAGE else ""
        pages.append(Page(f"{label}{suffix}", "\n".join(part)))
    return pages


# ---------------------------------------------------------------- PDF
_WHITE_OPS = {b"g": [1], b"rg": [1, 1, 1], b"k": [0, 0, 0, 0]}
_COLOR_OPS = {b"g", b"rg", b"k", b"sc", b"scn"}


_PAINT_OPS = {b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"}


def _apply(cm: list, x: float, y: float) -> tuple[float, float]:
    return cm[0] * x + cm[2] * y + cm[4], cm[1] * x + cm[3] * y + cm[5]


def _hidden_text_visitors(hidden: list[str]) -> dict:
    """Builds pypdf visitors that collect white text not drawn over a filled, non-white area.

    White text on a coloured table header or banner is visible and kept. White text on the page
    background is invisible to a reader and is collected as hidden.
    """
    state: dict = {"white": False, "stack": [], "rect": None, "panels": []}

    def before(op: bytes, args: list, cm: list, tm: list) -> None:
        if op == b"q":
            state["stack"].append(state["white"])
        elif op == b"Q":
            state["white"] = state["stack"].pop() if state["stack"] else False
        elif op in _COLOR_OPS:
            try:
                state["white"] = [float(a) for a in args] == _WHITE_OPS.get(op)
            except (TypeError, ValueError):  # pattern colour spaces use names rather than numbers
                state["white"] = False
        elif op == b"re" and len(args) == 4:
            x, y, w, h = (float(a) for a in args)
            (x0, y0), (x1, y1) = _apply(cm, x, y), _apply(cm, x + w, y + h)
            state["rect"] = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        elif op in _PAINT_OPS:
            if state["rect"] and not state["white"]:
                state["panels"].append(state["rect"])  # filled non-white area
            state["rect"] = None

    def text_seen(text: str, cm: list, tm: list, font: object, size: float) -> None:
        if not state["white"] or not text.strip() or not cm or not tm:
            return
        x, y = _apply(cm, tm[4], tm[5])
        on_panel = any(x0 - 1 <= x <= x1 + 1 and y0 - 2 <= y <= y1 + 2 for x0, y0, x1, y1 in state["panels"])
        if not on_panel:
            hidden.append(text)

    return {"visitor_operand_before": before, "visitor_text": text_seen}


def strip_repeated_lines(pages: list[str]) -> list[str]:
    """Removes running headers and footers.

    A line is treated as a header or footer when it appears near the top or bottom of at least half
    of the pages (minimum three) after digits are normalised.
    """
    if len(pages) < 3:
        return pages

    def key(line: str) -> str:
        return re.sub(r"\d+", "#", line.strip().lower())

    counts: dict[str, int] = {}
    for text in pages:
        lines = [ln for ln in text.splitlines() if ln.strip()]
        for k in {key(ln) for ln in lines[:3] + lines[-3:]}:
            counts[k] = counts.get(k, 0) + 1
    repeated = {k for k, n in counts.items() if n >= max(3, len(pages) * 0.5) and len(k) > 3}
    if not repeated:
        return pages
    out = []
    for text in pages:
        lines = text.splitlines()
        edge = {i for i, ln in enumerate(lines) if ln.strip()}
        edge_idx = sorted(edge)[:3] + sorted(edge)[-3:]
        kept = _tidy("\n".join(ln for i, ln in enumerate(lines) if not (i in edge_idx and key(ln) in repeated)))
        out.append(kept if kept or not text.strip() else text)  # do not empty a page that had content
    return out


def _pdf_pages(data: bytes, max_pages: int) -> ParsedDocument:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data), strict=False)
    if len(reader.pages) > max_pages:
        raise ParseError(f"The PDF has more than {max_pages} pages.")
    texts, hidden_found = [], []
    for page in reader.pages:
        hidden: list[str] = []
        try:
            text = page.extract_text(**_hidden_text_visitors(hidden)) or ""
        except Exception:  # skip a damaged page rather than failing the whole document
            text = ""
        if hidden:
            for fragment in sorted(hidden, key=len, reverse=True):
                text = text.replace(fragment.strip(), "")
            hidden_found.append(" ".join(h.strip() for h in hidden))
        texts.append(_tidy(text))
    texts = strip_repeated_lines(texts)
    pages = [Page(f"Page {n}", t) for n, t in enumerate(texts, start=1)]
    warnings = []
    if hidden_found:
        sample = re.sub(r"\s+", " ", " ".join(hidden_found))[:160]
        warnings.append(f"Invisible text was found and excluded from search: “{sample}…”")
    return ParsedDocument(pages, warnings)


# ---------------------------------------------------------------- Word
def _docx_pages(data: bytes) -> ParsedDocument:
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    doc = Document(io.BytesIO(data))
    blocks: list[str] = []
    hidden: list[str] = []
    for element in doc.element.body.iterchildren():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "p":
            if element.xpath('.//w:br[@w:type="page"]') or element.xpath("./w:pPr/w:pageBreakBefore"):
                blocks.append(PAGE_BREAK)
            para = Paragraph(element, doc)
            visible = []
            for run in para.runs:
                (hidden if run.font.hidden else visible).append(run.text)
            text = "".join(visible).strip() if para.runs else para.text.strip()
            if not text:
                if element.xpath("./w:pPr/w:sectPr"):
                    blocks.append(PAGE_BREAK)
                continue
            style = (para.style.name if para.style is not None else "") or ""
            if style == "Title":
                text = f"# {text}"
            elif style.startswith("Heading"):
                level = int(style.split()[-1]) if style.split()[-1].isdigit() else 1
                text = f"{'#' * min(level + 1, 4)} {text}"
            blocks.append(text)
            if element.xpath("./w:pPr/w:sectPr"):  # section break
                blocks.append(PAGE_BREAK)
        elif tag == "tbl":
            rows = [[c.text.strip() for c in row.cells] for row in Table(element, doc).rows]
            table = _table_pages(rows, "table")
            if table:
                blocks.append("\n".join(p.text for p in table))
    warnings = []
    if any(h.strip() for h in hidden):
        warnings.append(f"Hidden text was found and excluded from search: “{' '.join(hidden)[:160]}…”")
    return ParsedDocument(_sections(blocks, "Section"), warnings)


# ---------------------------------------------------------------- PowerPoint
def _chart_text(chart: object) -> str:
    """Renders a chart's categories and series values as text so they can be searched."""
    try:
        plot = chart.plots[0]
        categories = [str(c) for c in plot.categories]
        title = chart.chart_title.text_frame.text.strip() if chart.has_title else ""
        lines = [f"Chart{': ' + title if title else ''} ({chart.chart_type.name.replace('_', ' ').lower()})"]
        for series in plot.series:
            values = ", ".join(f"{c} {_cell(v)}" for c, v in zip(categories, series.values, strict=False))
            lines.append(f"{series.name}: {values}")
        return "\n".join(lines)
    except Exception:  # unsupported chart type: keep the slide, omit the data
        return "Chart"



def _pptx_pages(data: bytes, max_pages: int) -> ParsedDocument:
    from pptx import Presentation

    prs = Presentation(io.BytesIO(data))
    if len(prs.slides) > max_pages:
        raise ParseError(f"The presentation has more than {max_pages} slides.")
    pages = []
    for number, slide in enumerate(prs.slides, start=1):
        parts: list[str] = []
        title = slide.shapes.title.text_frame.text.strip() if slide.shapes.title is not None else ""
        if title:
            parts.append(f"# {title}")
        for shape in slide.shapes:
            if shape == slide.shapes.title:
                continue
            if shape.has_text_frame:
                text = "\n".join(p.text.strip() for p in shape.text_frame.paragraphs if p.text.strip())
                if text:
                    parts.append(text)
            if getattr(shape, "has_table", False) and shape.has_table:
                rows = [[c.text.strip() for c in row.cells] for row in shape.table.rows]
                parts.extend(p.text for p in _table_pages(rows, "table"))
            if getattr(shape, "has_chart", False) and shape.has_chart:
                parts.append(_chart_text(shape.chart))
        if slide.has_notes_slide:
            notes = slide.notes_slide.notes_text_frame.text.strip()
            if notes:
                parts.append(f"Speaker notes: {notes}")
        pages.append(Page(f"Slide {number}", _tidy("\n".join(parts))))
    return ParsedDocument(pages)


# ---------------------------------------------------------------- Spreadsheets
def _xlsx_pages(data: bytes, max_pages: int, max_rows: int) -> ParsedDocument:
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    pages, warnings, total_rows = [], [], 0
    try:
        for ws in wb.worksheets:
            if ws.sheet_state != "visible":
                warnings.append(f"Hidden sheet “{ws.title}” was skipped.")
                continue
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([_cell(v) for v in row])
                total_rows += 1
                if total_rows > max_rows:
                    raise ParseError(f"The spreadsheet has more than {max_rows} rows.")
            pages.extend(_table_pages(rows, f"Sheet: {ws.title}"))
    finally:
        wb.close()
    if len(pages) > max_pages:
        raise ParseError(f"The spreadsheet is too large (more than {max_pages} sections).")
    return ParsedDocument(pages, warnings)


def _csv_pages(text: str, max_rows: int) -> ParsedDocument:
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = []
    for row in csv.reader(io.StringIO(text), dialect):
        rows.append([c.strip() for c in row])
        if len(rows) > max_rows:
            raise ParseError(f"The CSV has more than {max_rows} rows.")
    return ParsedDocument(_table_pages(rows, "Rows"))


# ---------------------------------------------------------------- Text
def _text_pages(text: str) -> ParsedDocument:
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    return ParsedDocument(_sections(blocks, "Part"))


def parse_document(data: bytes, file_format: str, *, max_pages: int = 300, max_rows: int = 5000) -> ParsedDocument:
    try:
        if file_format == "pdf":
            parsed = _pdf_pages(data, max_pages)
        elif file_format == "docx":
            parsed = _docx_pages(data)
        elif file_format == "pptx":
            parsed = _pptx_pages(data, max_pages)
        elif file_format == "xlsx":
            parsed = _xlsx_pages(data, max_pages, max_rows)
        elif file_format == "csv":
            parsed = _csv_pages(data.decode("utf-8-sig"), max_rows)
        elif file_format in ("md", "txt"):
            parsed = _text_pages(data.decode("utf-8-sig"))
        else:
            raise ParseError(f"Unsupported format: {file_format}")
    except ParseError:
        raise
    except Exception as exc:  # parser libraries raise various exception types on malformed input
        raise ParseError(f"The file could not be read ({type(exc).__name__}); it may be damaged.") from exc

    if file_format != "pdf":  # PDF pages keep their real numbers, even when a page is blank
        parsed.pages = [p for p in parsed.pages if p.text.strip()]
    if parsed.char_count < MIN_DOCUMENT_CHARS:
        if file_format == "pdf":
            raise ParseError("No readable text found. This looks like a scanned PDF; OCR is not supported yet.")
        raise ParseError("The document contains no readable text.")
    return parsed
