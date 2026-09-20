"""Rule-based text cleaning for the embedding leg only.

The wiki leg gets semantic cleaning for free: the ingestion LLM reads the
full document and naturally ignores watermarks, disclaimers and other
semantic noise. Embedded chunks are raw text — nothing filters them — so
this module applies cheap deterministic rules to chunk texts *before*
embedding. Original chunk content, FTS and the wiki pipeline are untouched.

What it catches: boilerplate repeated across a document (watermarks,
headers/footers, company names), page-number-only lines, and chunks too
short to carry meaning. What it deliberately does NOT try: semantic noise
(ads, disclaimers) — that requires an LLM, which the embedding leg doesn't
have; the residual harm is accepted.
"""

from __future__ import annotations

import re
from collections import Counter

MIN_CHUNK_CHARS = 20  # shorter chunks carry no semantics — skip embedding
MAX_BOILERPLATE_LINE_LEN = 80  # real content lines are rarely this short AND repeated
BOILERPLATE_RATIO = 0.3  # line present in ≥30% of a doc's chunks = boilerplate

_PAGE_NUMBER = re.compile(r"^[-–—\s]*(?:第\s*)?\d{1,4}\s*(?:页|/\s*\d{1,4})?[-–—\s]*$")


def _norm_line(line: str) -> str:
    return re.sub(r"\s+", " ", line).strip()


def clean_chunk_texts(texts: list[str]) -> list[str | None]:
    """Clean one document's chunk texts for embedding.

    Returns a parallel list; None means "skip — don't embed this chunk".
    Boilerplate detection is per-document: a line is only noise if *this*
    document repeats it everywhere (a common phrase across the corpus is
    still signal).
    """
    chunked_lines: list[list[str]] = []
    presence: Counter[str] = Counter()
    for text in texts:
        lines = [ln for ln in (_norm_line(raw) for raw in text.splitlines()) if ln]
        chunked_lines.append(lines)
        presence.update(set(lines))  # per-chunk presence, not total occurrences

    threshold = max(2, int(len(texts) * BOILERPLATE_RATIO))
    boilerplate = {
        line
        for line, n in presence.items()
        if n >= threshold and len(line) <= MAX_BOILERPLATE_LINE_LEN
    }

    cleaned: list[str | None] = []
    for lines in chunked_lines:
        kept = [
            line
            for line in lines
            if line not in boilerplate and not _PAGE_NUMBER.match(line)
        ]
        text = "\n".join(kept).strip()
        cleaned.append(text if len(text) >= MIN_CHUNK_CHARS else None)
    return cleaned
