"""Retrieval pipeline for the local QA chat window (nashsu-style).

Deterministic multi-stage pipeline — no LLM calls in the middle:
  Stage 1: FTS5 trigram search over chunks (wiki pages + sources), title boost;
           optionally a parallel vector leg (semantic recall), RRF-fused
  Stage 2: graph expansion via services.graph.expand_related
  Stage 3: token-budget packing of the context

The vector leg is optional and off unless EMBEDDING_* is configured; when
off, Stage 1 is exactly the FTS path and results are unchanged.
"""

from __future__ import annotations

import json
import math
import re
from typing import Protocol

from infra.db.sqlite import rows_to_dicts
from services.graph import expand_related

RRF_K = 60  # standard reciprocal-rank-fusion constant


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
    # 词全被 trigram 的 3 字门槛丢弃时（如"幂等"）跳过 FTS 腿，
    # 但标题 LIKE 兜底不受此限，必须照常执行。
    rows: list[dict] = []
    if match:
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

    # Content LIKE fallback for 2-char terms: the trigram index has a 3-char
    # floor, so terms like "幂等"/"排名" can never hit it. A LIKE scan over
    # chunk bodies is slow, but such queries are rare and this is their only
    # way to reach body text at all.
    short_terms = [t for t in terms if len(t) == 2]
    if short_terms:
        like = " OR ".join("lower(dc.content) LIKE ?" for _ in short_terms)
        params = [f"%{t}%" for t in short_terms]
        cur = await db.execute(
            f"SELECT dc.document_id, dc.content, dc.page, d.filename, d.title, "
            f"d.path, d.relative_path, d.source_kind "
            f"FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
            f"WHERE ({like}) AND d.status != 'failed' LIMIT ?",
            (*params, limit * 2),
        )
        for r in rows_to_dicts(cur, await cur.fetchall()):
            matched = sum(1 for t in short_terms if t in r["content"].lower())
            entry = docs.get(r["document_id"])
            if entry is None:
                docs[r["document_id"]] = {
                    "doc_id": r["document_id"],
                    "filename": r["filename"],
                    "title": r["title"] or r["filename"],
                    "path": r["path"],
                    "relative_path": r["relative_path"],
                    "source_kind": r.get("source_kind", "source"),
                    "score": 4.0 * matched,
                    "hits": 0,
                    "snippet": r["content"][:300],
                    "page": r["page"],
                }
            else:
                entry["score"] += 4.0 * matched
                if not entry["snippet"]:
                    entry["snippet"] = r["content"][:300]
                    entry["page"] = r["page"]

    results = sorted(docs.values(), key=lambda e: e["score"], reverse=True)
    return results[:limit]


def rrf_fuse(legs: list[list[dict]], limit: int) -> list[dict]:
    """Reciprocal rank fusion: score = Σ 1/(K + rank) over each leg.

    Rank-based, so no score-scale alignment between FTS (bm25) and vector
    (cosine) legs is needed. Entries shared across legs keep the metadata of
    their best-ranked appearance.
    """
    merged: dict[str, dict] = {}
    for leg in legs:
        for rank, entry in enumerate(leg):
            doc_id = entry["doc_id"]
            existing = merged.get(doc_id)
            if existing is None:
                merged[doc_id] = {**entry, "score": 1.0 / (RRF_K + rank)}
            else:
                existing["score"] += 1.0 / (RRF_K + rank)
    results = sorted(merged.values(), key=lambda e: e["score"], reverse=True)
    return results[:limit]


GRAPH_MIN_RATIO = 0.15  # full vector coverage leaves this graph share
GRAPH_MAX_RATIO = 0.30  # sparse vector coverage moves toward this


def graph_quota(total: int, vector_coverage: float) -> int:
    """Reserve 15–30% of the result window for graph neighbors (nashsu-style).

    The quota adapts to vector coverage: when semantic retrieval comes back
    thin, graph expansion gets more seats to backstop it.
    """
    if total < 2:
        return 0
    ratio = GRAPH_MAX_RATIO - (GRAPH_MAX_RATIO - GRAPH_MIN_RATIO) * vector_coverage
    return max(1, min(total - 1, math.ceil(total * ratio)))


async def retrieve(
    db,
    query: str,
    seed_limit: int = 8,
    expand_per_seed: int = 3,
    use_vector: bool | None = None,
) -> list[dict]:
    """Stages 1+2: search, then expand through the reference graph.

    use_vector=None follows server config; explicit False forces the pure
    FTS leg (chat "fast" mode) even when embeddings are configured.

    Graph expansion is quota-reserved, not score-competitive: neighbor docs
    take reserved seats at the tail of the window rather than fighting seeds
    on score, so weak FTS/vector rounds still surface graph context.
    """
    fts_results = await search_documents(db, query, limit=seed_limit)

    from services import vector_index
    vector_on = vector_index.vector_leg_enabled() if use_vector is None else (
        use_vector and vector_index.vector_leg_enabled()
    )
    vec_results: list[dict] = []
    if vector_on:
        vec_results = await vector_index.search(db, query, limit=seed_limit)
        seeds = rrf_fuse([fts_results, vec_results], limit=seed_limit)
    else:
        seeds = fts_results
    if not seeds:
        return []

    total = seed_limit + expand_per_seed
    coverage = min(len(vec_results), seed_limit) / seed_limit if vector_on else 0.0
    quota = graph_quota(total, coverage)

    seed_ids = [s["doc_id"] for s in seeds]
    related = await expand_related(db, seed_ids, per_seed=expand_per_seed)
    candidates = [r for r in related if r["doc_id"] not in set(seed_ids)][:quota]
    for r in candidates:
        r["graph_expansion"] = True

    # Unused quota seats return to the seeds.
    return seeds[: total - len(candidates)] + candidates


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
