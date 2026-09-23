"""Text chunker with header breadcrumb tracking.

Char-based budgets sized for the embedding model: bge-large-zh-v1.5 truncates
at 512 tokens and CJK is ~1 token/char, so chunks are capped at 500 chars —
safe for pure Chinese, conservative for English. The cap is a *budget*, not a
guillotine: oversized text descends a priority ladder of natural boundaries
(heading sections → blank-line paragraphs → lines → sentence terminators →
whitespace) and only hard-slices as the last resort. Fenced code blocks and
tables are atomic — never torn; if one exceeds the cap it is emitted whole
and flagged ``oversized`` (the model truncates, but the structure survives).
Chunks under 100 chars are merged into a neighbor instead of dropped, and
consecutive chunks share ~100 chars of boundary-aligned overlap.
"""

import logging
import re
from dataclasses import dataclass

import asyncpg

logger = logging.getLogger(__name__)

TARGET_CHARS = 400
MAX_CHARS = 500
OVERLAP_CHARS = 100  # 20% of the cap, boundary-aligned
MIN_CHARS = 100  # smaller chunks merge into a neighbor, never dropped
MAX_CHUNK_CHARS = 10_000  # hosted DB constraint chk_chunks_content_length

HEADER_RE = re.compile(r'^(#{1,6})\s+(.+)$')
FENCE_RE = re.compile(r'(?m)^[ \t]*(`{3,})')
BLANK_RE = re.compile(r'\n\s*\n')
SENTENCE_RE = re.compile(r'[。！？!?；;]|\.(?=\s)')
WS_RE = re.compile(r'\s')
_CJK_RE = re.compile(r'[　-〿㐀-䶿一-鿿＀-￯]')


def _estimate_tokens(text: str) -> int:
    """CJK-aware estimate: ~1 token per CJK char, ~4 chars/token otherwise."""
    if not text:
        return 1
    cjk = len(_CJK_RE.findall(text))
    return max(1, cjk + (len(text) - cjk) // 4)


@dataclass
class Chunk:
    index: int
    content: str
    page: int | None
    start_char: int
    token_count: int
    header_breadcrumb: str = ""
    oversized: bool = False  # atomic unit over MAX_CHARS, emitted whole


@dataclass
class _Atom:
    start: int
    end: int
    oversized: bool
    heading: bool
    crumb: str


def chunk_text(
    content: str,
    page: int | None = None,
    start_char_offset: int = 0,
) -> list[Chunk]:
    """Chunk a text string with header tracking, overlap and min-merge."""
    if not content or not content.strip():
        return []

    atoms = _build_atoms(content)
    spans = _pack_atoms(atoms)
    spans = _merge_small(spans, content)

    chunks: list[Chunk] = []
    for start, end, oversized, crumb in spans:
        raw = content[start:end]
        text = raw.strip()
        lead = len(raw) - len(raw.lstrip())
        chunks.append(Chunk(
            index=len(chunks),
            content=text,
            page=page,
            start_char=start_char_offset + start + lead,
            token_count=_estimate_tokens(text),
            header_breadcrumb=crumb,
            oversized=oversized,
        ))
    return chunks


# ── atom extraction ────────────────────────────────────────────────────────

def _build_atoms(content: str) -> list[_Atom]:
    """Split content into atoms: spans no bigger than MAX_CHARS (or atomic
    oversized code/table blocks), each tagged with its header breadcrumb."""
    atoms: list[_Atom] = []
    header_stack: list[tuple[int, str]] = []

    def crumb() -> str:
        return " > ".join(t for _, t in header_stack)

    for start, end, atomic in _blocks(content):
        block = content[start:end]
        header = HEADER_RE.match(block)
        if header and not atomic:
            level = len(header.group(1))
            header_stack = [(lv, t) for lv, t in header_stack if lv < level]
            header_stack.append((level, header.group(2).strip()))
        if end - start <= MAX_CHARS:
            atoms.append(_Atom(start, end, False, bool(header and not atomic), crumb()))
        elif atomic:
            # Never tear code/tables. Past the DB limit there is no choice.
            if end - start <= MAX_CHUNK_CHARS:
                atoms.append(_Atom(start, end, True, False, crumb()))
            else:
                for i in range(start, end, MAX_CHUNK_CHARS):
                    atoms.append(_Atom(i, min(i + MAX_CHUNK_CHARS, end), True, False, crumb()))
        else:
            for s, e in _split_span(content, start, end):
                atoms.append(_Atom(s, e, False, bool(header and not atomic), crumb()))
    return atoms


def _blocks(content: str) -> list[tuple[int, int, bool]]:
    """(start, end, atomic) blocks: fenced code spans first, then paragraphs
    split on blank lines; all-| tables are atomic too."""
    spans: list[tuple[int, int, bool]] = []
    fences = _fence_spans(content)
    pos = 0
    for fs, fe in fences:
        spans.extend(_paragraphs(content, pos, fs))
        spans.append((fs, fe, True))
        pos = fe
    spans.extend(_paragraphs(content, pos, len(content)))
    return spans


def _fence_spans(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    open_start: int | None = None
    for m in FENCE_RE.finditer(text):
        if open_start is None:
            open_start = m.start()
        else:
            line_end = text.find('\n', m.end())
            spans.append((open_start, len(text) if line_end == -1 else line_end))
            open_start = None
    if open_start is not None:  # unclosed fence runs to end of text
        spans.append((open_start, len(text)))
    return spans


def _paragraphs(text: str, start: int, end: int) -> list[tuple[int, int, bool]]:
    out: list[tuple[int, int, bool]] = []
    pos = start
    for m in BLANK_RE.finditer(text, start, end):
        _add_paragraph(text, pos, m.start(), out)
        pos = m.end()
    _add_paragraph(text, pos, end, out)
    return out


def _add_paragraph(text: str, s: int, e: int, out: list[tuple[int, int, bool]]) -> None:
    while s < e and text[s].isspace():
        s += 1
    while e > s and text[e - 1].isspace():
        e -= 1
    if s >= e:
        return
    lines = [ln.strip() for ln in text[s:e].splitlines() if ln.strip()]
    atomic = bool(lines) and all(ln.startswith('|') for ln in lines)
    out.append((s, e, atomic))


def _split_span(text: str, s: int, e: int, level: int = 0) -> list[tuple[int, int]]:
    """Recursive boundary ladder: lines → sentence terminators → whitespace →
    hard slice. Every returned span is ≤ MAX_CHARS."""
    if e - s <= MAX_CHARS:
        return [(s, e)]
    if level >= 3:
        return [(i, min(i + MAX_CHARS, e)) for i in range(s, e, MAX_CHARS)]
    pattern = (re.compile(r'\n'), SENTENCE_RE, WS_RE)[level]
    points = [m.end() for m in pattern.finditer(text, s, e) if m.end() < e]
    if not points:
        return _split_span(text, s, e, level + 1)
    out: list[tuple[int, int]] = []
    prev = s
    for p in points + [e]:
        out.extend(_split_span(text, prev, p, level + 1))
        prev = p
    return out


# ── packing, overlap, min-merge ────────────────────────────────────────────

def _pack_atoms(atoms: list[_Atom]) -> list[tuple[int, int, bool, str]]:
    """Greedy-pack atoms into ≤MAX_CHARS chunk spans with ~OVERLAP_CHARS
    boundary-aligned overlap. Headings flush a substantive chunk so chunks
    don't straddle sections."""
    spans: list[tuple[int, int, bool, str]] = []
    i, n = 0, len(atoms)
    while i < n:
        start, end, oversized = atoms[i].start, atoms[i].end, atoms[i].oversized
        j = i
        if not oversized:
            while j + 1 < n:
                na = atoms[j + 1]
                if na.oversized:
                    break
                if na.heading and end - start >= MIN_CHARS:
                    break
                if na.end - start > MAX_CHARS:
                    break
                j += 1
                end = na.end
        # The last atom's crumb reflects the deepest section this chunk
        # reaches — a heading merged mid-chunk still shows up.
        spans.append((start, end, oversized, atoms[j].crumb))

        nxt = j + 1
        if nxt < n and not oversized and not atoms[nxt].oversized and not atoms[nxt].heading:
            # Rewind to the earliest atom boundary within the overlap budget.
            k = j
            while k - 1 > i and end - atoms[k - 1].start <= OVERLAP_CHARS:
                k -= 1
            if k > i:
                nxt = k
        i = nxt
    return spans


def _merge_small(
    spans: list[tuple[int, int, bool, str]],
    content: str,
) -> list[tuple[int, int, bool, str]]:
    """Merge <MIN_CHARS chunks into a neighbor when the result stays ≤MAX_CHARS.
    A document whose only chunk is tiny is kept as-is."""
    merged: list[tuple[int, int, bool, str]] = []
    for span in spans:
        start, end, oversized, _ = span
        if (
            merged
            and len(content[start:end].strip()) < MIN_CHARS
            and not oversized
            and not merged[-1][2]
            and end - merged[-1][0] <= MAX_CHARS
        ):
            prev = merged[-1]
            merged[-1] = (prev[0], end, prev[2], prev[3])
        else:
            merged.append(span)
    if len(merged) > 1:
        start, end, oversized, _ = merged[0]
        nxt = merged[1]
        if (
            len(content[start:end].strip()) < MIN_CHARS
            and not oversized
            and not nxt[2]
            and nxt[1] - start <= MAX_CHARS
        ):
            merged[1] = (start, nxt[1], nxt[2], nxt[3])
            merged.pop(0)
    return merged


# ── pages + persistence ────────────────────────────────────────────────────

def chunk_pages(page_contents: list[tuple[int, str]]) -> list[Chunk]:
    """Chunk multiple pages, preserving page numbers. Each (page_number, content) tuple."""
    all_chunks: list[Chunk] = []
    for page_num, content in page_contents:
        page_chunks = chunk_text(content, page=page_num)
        for c in page_chunks:
            c.index = len(all_chunks)
            all_chunks.append(c)
    return all_chunks


async def store_chunks(
    pool_or_conn,
    document_id: str,
    user_id: str,
    knowledge_base_id: str,
    chunks: list[Chunk],
):
    if isinstance(pool_or_conn, asyncpg.Connection):
        await _store_chunks_on_conn(pool_or_conn, document_id, user_id, knowledge_base_id, chunks)
    else:
        conn = await pool_or_conn.acquire()
        try:
            await _store_chunks_on_conn(conn, document_id, user_id, knowledge_base_id, chunks)
        finally:
            await pool_or_conn.release(conn)


async def _store_chunks_on_conn(
    conn: asyncpg.Connection,
    document_id: str,
    user_id: str,
    knowledge_base_id: str,
    chunks: list[Chunk],
):
    await conn.execute("DELETE FROM document_chunks WHERE document_id = $1", document_id)

    if not chunks:
        return

    # source_content seeds the immutable raw text; content starts identical
    # but may diverge later when highlight CRUD writes annotations into the
    # chunk via api/services/highlight_chunks.
    await conn.execute(
        "INSERT INTO document_chunks "
        "(document_id, user_id, knowledge_base_id, chunk_index, content, source_content, "
        " page, start_char, token_count, header_breadcrumb) "
        "SELECT $1::uuid, $2::uuid, $3::uuid, row.chunk_index, row.content, row.content, "
        "       row.page, row.start_char, row.token_count, row.header_breadcrumb "
        "FROM UNNEST($4::int[], $5::text[], $6::int[], $7::int[], $8::int[], $9::text[]) "
        "AS row(chunk_index, content, page, start_char, token_count, header_breadcrumb)",
        document_id,
        user_id,
        knowledge_base_id,
        [c.index for c in chunks],
        [c.content for c in chunks],
        [c.page for c in chunks],
        [c.start_char for c in chunks],
        [c.token_count for c in chunks],
        [c.header_breadcrumb for c in chunks],
    )
    logger.info("Stored %d chunks for doc %s", len(chunks), document_id[:8])
