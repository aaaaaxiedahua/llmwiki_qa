"""Retrieval pipeline for the local QA chat window (nashsu-style).

Deterministic multi-stage pipeline — no LLM calls in the middle:
  Stage 1: FTS5 trigram search over chunks (wiki pages + sources), title boost
  Stage 2: graph expansion via services.graph.expand_related
  Stage 3: token-budget packing of the context

The vector leg is deliberately a Protocol placeholder: plugging in embeddings
later means adding another Retriever implementation, not changing the flow.
"""

from __future__ import annotations

import json
import re
from typing import Protocol

from infra.db.sqlite import rows_to_dicts
from services.graph import expand_related


# Rough token estimate: CJK chars ≈ 1 token each, ASCII words ≈ 0.75.
def estimate_tokens(text: str) -> int:
    cjk = len(re.findall(r"[一-鿿]", text))
    return cjk + int((len(text) - cjk) * 0.3)


class Retriever(Protocol):
    async def search(self, db, query: str, limit: int) -> list[dict]: ...


def build_match_query(query: str) -> str:
    """Build a safe FTS5 MATCH expression: OR of quoted terms.

    trigram can only match tokens of 3+ chars, so shorter terms are dropped.
    """
    terms = re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", query)
    terms = [t for t in terms if len(t) >= 3]
    if not terms:
        return ""
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)


async def search_documents(db, query: str, limit: int = 10) -> list[dict]:
    """Stage 1: chunk-level FTS search aggregated to documents, + title boost."""
    match = build_match_query(query)
    if not match:
        return []

    cursor = await db.execute(
        "SELECT dc.document_id, dc.content, dc.page, d.filename, d.title, "
        "d.path, d.relative_path, d.source_kind, bm25(chunks_fts) AS rank "
        "FROM chunks_fts "
        "JOIN document_chunks dc ON dc.rowid = chunks_fts.rowid "
        "JOIN documents d ON d.id = dc.document_id "
        "WHERE chunks_fts MATCH ? AND d.status != 'failed' "
        "ORDER BY rank LIMIT ?",
        (match, limit * 4),
    )
    rows = rows_to_dicts(cursor, await cursor.fetchall())

    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+|[一-鿿]+", query)]
    docs: dict[str, dict] = {}
    for r in rows:
        doc_id = r["document_id"]
        # bm25 rank: more negative = better; negate so higher = better.
        score = -r["rank"]
        entry = docs.get(doc_id)
        if entry is None:
            entry = {
                "doc_id": doc_id,
                "filename": r["filename"],
                "title": r["title"] or r["filename"],
                "path": r["path"],
                "relative_path": r["relative_path"],
                "source_kind": r.get("source_kind", "source"),
                "score": 0.0,
                "hits": 0,
                "snippet": r["content"][:300],
                "page": r["page"],
            }
            docs[doc_id] = entry
        entry["score"] += score
        entry["hits"] += 1

    # Title/metadata leg: title or aliases/description matches mark a doc
    # relevant even when its chunks never mention the term (+10 per term).
    if terms:
        like = " OR ".join(
            "lower(title) LIKE ? OR lower(COALESCE(metadata, '')) LIKE ?"
            for _ in terms
        )
        params: list = []
        for t in terms:
            params.extend([f"%{t}%", f"%{t}%"])
        cur = await db.execute(
            f"SELECT id, filename, title, path, relative_path, source_kind, metadata "
            f"FROM documents WHERE ({like}) AND status != 'failed' LIMIT ?",
            (*params, limit),
        )
        for r in rows_to_dicts(cur, await cur.fetchall()):
            meta = r["metadata"]
            meta_text = meta if isinstance(meta, str) else json.dumps(meta or {}, ensure_ascii=False)
            haystack = ((r["title"] or "") + " " + meta_text).lower()
            matched = sum(1 for t in terms if t in haystack)
            entry = docs.get(r["id"])
            if entry is None:
                docs[r["id"]] = {
                    "doc_id": r["id"],
                    "filename": r["filename"],
                    "title": r["title"] or r["filename"],
                    "path": r["path"],
                    "relative_path": r["relative_path"],
                    "source_kind": r.get("source_kind", "source"),
                    "score": 10.0 * matched,
                    "hits": 0,
                    "snippet": "",
                    "page": None,
                }
            else:
                entry["score"] += 10.0 * matched

    results = sorted(docs.values(), key=lambda e: e["score"], reverse=True)
    return results[:limit]


async def retrieve(
    db,
    query: str,
    seed_limit: int = 8,
    expand_per_seed: int = 3,
) -> list[dict]:
    """Stages 1+2: search, then expand through the reference graph."""
    seeds = await search_documents(db, query, limit=seed_limit)
    if not seeds:
        return []

    seed_ids = [s["doc_id"] for s in seeds]
    related = await expand_related(db, seed_ids, per_seed=expand_per_seed)

    by_id = {s["doc_id"]: s for s in seeds}
    for rel in related:
        existing = by_id.get(rel["doc_id"])
        if existing:
            existing["score"] += rel["score"]
        else:
            by_id[rel["doc_id"]] = rel

    results = sorted(by_id.values(), key=lambda e: e["score"], reverse=True)
    return results[: seed_limit + expand_per_seed]


async def fetch_context_pages(db, candidates: list[dict]) -> list[dict]:
    """Load full content for wiki pages, best snippet for source docs."""
    pages = []
    for c in candidates:
        if c.get("source_kind") == "wiki":
            cursor = await db.execute(
                "SELECT content FROM documents WHERE id = ?", (c["doc_id"],)
            )
            row = await cursor.fetchone()
            content = row[0] if row and row[0] else c.get("snippet", "")
        else:
            content = c.get("snippet", "")
        pages.append({**c, "content": content})
    return pages


def pack_context(
    pages: list[dict],
    budget_tokens: int = 6000,
) -> list[dict]:
    """Stage 3: greedily pack pages into the token budget (highest score first).

    The top page is always included (truncated if necessary) so a query never
    gets an empty context.
    """
    packed = []
    used = 0
    for i, p in enumerate(pages, start=1):
        cost = estimate_tokens(p["content"])
        if used + cost <= budget_tokens:
            packed.append({**p, "num": i})
            used += cost
            continue
        remaining = budget_tokens - used
        if not packed or remaining > 200:
            chars = max(remaining, 400) * 2
            packed.append({**p, "content": p["content"][:chars], "num": i})
        break
    return packed
