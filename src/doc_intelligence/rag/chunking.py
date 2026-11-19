"""Structure-aware chunking with exact character offsets.

Pages are split on the document's own headings (Markdown "#", "Section 4", "3. Annual leave",
all-caps lines), so a chunk is usually one section with its heading kept as metadata. Long
sections are split at sentence boundaries with overlap, and very short sections are merged with a
neighbour. Each chunk records its page and character offsets so a citation can be highlighted in
the source text.
"""

import re
from dataclasses import dataclass

MAX_CHARS = 1400
OVERLAP_CHARS = 200
MIN_CHARS = 60  # sections shorter than this are merged with a neighbour

_MARKDOWN = re.compile(r"^#{1,6}\s+\S")
_NAMED = re.compile(r"^(?:section|sec\.|article|chapter|clause|part)\s+\d+[A-Z]?\b", re.IGNORECASE)
_NUMBERED_TITLE = re.compile(r"^\d{1,3}(?:\.\d{1,3})*\.?\s+[A-Z][^.!?]{2,80}$")  # "3. Annual leave"
_ALL_CAPS = re.compile(r"^[A-Z][A-Z0-9 ,&'()/-]{4,80}$")


@dataclass(frozen=True)
class Chunk:
    position: int
    page_number: int  # 1-based
    heading: str | None
    text: str
    start: int
    end: int


def clean_heading(line: str) -> str:
    return re.sub(r"^#+\s*", "", line).strip().rstrip(".:")[:200]


def is_heading(line: str) -> bool:
    s = line.strip()
    if not s or len(s) > 100:
        return False
    if _MARKDOWN.match(s) or _ALL_CAPS.match(s) or _NUMBERED_TITLE.match(s):
        return True
    return bool(_NAMED.match(s)) and len(s) <= 90 and not s.endswith(",")


def _split_span(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Splits text[start:end] into windows of at most MAX_CHARS, preferring sentence ends."""
    if end - start <= MAX_CHARS:
        return [(start, end)]
    spans, pos = [], start
    while pos < end:
        stop = min(pos + MAX_CHARS, end)
        if stop < end:
            window = text[pos:stop]
            cut = max(window.rfind(". ", MAX_CHARS // 2), window.rfind("\n", MAX_CHARS // 2))
            if cut != -1:
                stop = pos + cut + 1
        spans.append((pos, stop))
        if stop >= end:
            break
        pos = max(stop - OVERLAP_CHARS, pos + 1)
        while pos < stop and not text[pos - 1].isspace():  # align the overlap to a word boundary
            pos += 1
    return spans


def _trim(text: str, start: int, end: int) -> tuple[int, int]:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return start, end


def chunk_page(text: str, page_number: int, carry_heading: str | None) -> tuple[list[tuple], str | None]:
    """Returns [(heading, start, end)] sections of one page and the heading in force at its end."""
    sections: list[list] = []
    heading = carry_heading
    offset = 0
    for line in text.splitlines(keepends=True):
        line_start, offset = offset, offset + len(line)
        if is_heading(line):
            heading = clean_heading(line)
            sections.append([heading, line_start, offset])
        elif line.strip():
            if not sections:
                sections.append([heading, line_start, offset])
            else:
                sections[-1][2] = offset
    # A heading with no body of its own (e.g. "5. Annual leave" directly followed by "5.1 Entitlement")
    # is merged into the following section, and both headings are kept.
    forward: list[list] = []
    pending: list | None = None
    for sec in sections:
        if pending is not None:
            sec = [f"{pending[0]} - {sec[0]}" if pending[0] and sec[0] else (sec[0] or pending[0]), pending[1], sec[2]]
            pending = None
        if sec[2] - sec[1] < MIN_CHARS and text[sec[1]:sec[2]].strip().count("\n") == 0 and sec is not sections[-1]:
            pending = sec
            continue
        forward.append(sec)
    if pending is not None:
        forward.append(pending)
    # Remaining short sections are merged into the previous section.
    merged: list[list] = []
    for sec in forward:
        if merged and (sec[2] - sec[1] < MIN_CHARS or merged[-1][2] - merged[-1][1] < MIN_CHARS) \
                and sec[2] - merged[-1][1] <= MAX_CHARS:
            merged[-1][2] = sec[2]
            merged[-1][0] = merged[-1][0] or sec[0]
        else:
            merged.append(sec)
    return [tuple(s) for s in merged], heading


_TOC_LINE = re.compile(r"^.{3,}?[\s.]+\d{1,3}$")


def is_contents_page(text: str) -> bool:
    """Detects a table of contents page.

    Such pages remain viewable but are not indexed, so they do not compete with the sections they
    list in search results.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if len(lines) < 8:
        return False
    entries = [ln for ln in lines if _TOC_LINE.match(ln) and "|" not in ln and ":" not in ln]
    if len(entries) / len(lines) < 0.7:
        return False
    numbers = [int(re.search(r"(\d{1,3})$", ln).group(1)) for ln in entries]
    rising = sum(b >= a for a, b in zip(numbers, numbers[1:], strict=False))
    return rising >= 0.8 * (len(numbers) - 1)  # contents page numbers are non-decreasing


def chunk_pages(pages: list[str]) -> list[Chunk]:
    chunks: list[Chunk] = []
    heading: str | None = None
    for page_number, text in enumerate(pages, start=1):
        if is_contents_page(text):
            continue
        sections, heading = chunk_page(text, page_number, heading)
        for sec_heading, start, end in sections:
            for s, e in _split_span(text, start, end):
                s, e = _trim(text, s, e)
                if e - s < 20:
                    continue
                body = re.sub(r"\s+", " ", text[s:e]).strip()
                chunks.append(Chunk(len(chunks), page_number, sec_heading, body, s, e))
    return chunks
