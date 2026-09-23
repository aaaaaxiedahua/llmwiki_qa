"""Vector-leg orchestration: embedding production and semantic search.

Only wiki pages (source_kind='wiki') enter the vector corpus — sources stay
searchable via FTS and citations but are never embedded. The distilled wiki
is the semantic layer; raw source noise never enters the vector space.

Production is a serial pull worker in the same style as the ingestion
worker: it polls document_chunks for rows missing a current-model registry
entry, cleans the texts (rule-based, embedding-leg only), embeds them in
batches, and upserts into the configured VectorStore. Being pull-based, it
self-heals and doubles as the backfill path — no write-site hooks needed.

Search embeds the query, asks the VectorStore for top chunks, then
aggregates chunk hits to document entries shaped like FTS results so the
retrieval pipeline can RRF-fuse the two legs.
"""

from __future__ import annotations

import asyncio
import logging

from config import settings
from services.embeddings import current_model, embed_texts, embedding_enabled
from services.text_cleaner import clean_chunk_texts
from services.vector_store import create_vector_store

logger = logging.getLogger(__name__)

POLL_SECONDS = 5.0


def vector_leg_enabled() -> bool:
    return settings.MODE == "local" and embedding_enabled()


async def embedding_stats(db) -> dict:
    """Progress of the vector index: total embeddable chunks vs. done."""
    cursor = await db.execute(
        "SELECT COUNT(*) FROM document_chunks dc "
        "JOIN documents d ON d.id = dc.document_id "
        "WHERE d.status != 'failed' AND d.source_kind = 'wiki'"
    )
    total = (await cursor.fetchone())[0]
    cursor = await db.execute(
        "SELECT COUNT(*) FROM chunk_embeddings WHERE model = ?",
        (current_model(),),
    )
    embedded = (await cursor.fetchone())[0]
    return {
        "enabled": vector_leg_enabled(),
        "model": current_model(),
        "total_chunks": total,
        "embedded_chunks": embedded,
    }


async def embed_pending(db, batch_size: int | None = None) -> int:
    """Embed one batch of chunks missing a current-model vector. Returns count."""
    limit = batch_size or settings.EMBEDDING_BATCH_SIZE
    # Chunk rows are immutable per chunk_id (re-chunking deletes + reinserts
    # with new ids), so "no registry row for the current model" covers both
    # new and stale chunks. Fetch extra because cleaning may skip some.
    # Only wiki pages are embedded: sources stay FTS/citation-only — the
    # distilled wiki is the semantic corpus (nashsu-style), so distillation
    # noise and source boilerplate never enter the vector space.
    cursor = await db.execute(
        "SELECT dc.id, COALESCE(NULLIF(dc.source_content, ''), dc.content), "
        "d.title, dc.header_breadcrumb "
        "FROM document_chunks dc "
        "JOIN documents d ON d.id = dc.document_id "
        "LEFT JOIN chunk_embeddings ce ON ce.chunk_id = dc.id AND ce.model = ? "
        "WHERE ce.chunk_id IS NULL AND d.status != 'failed' "
        "AND d.source_kind = 'wiki' "
        "ORDER BY dc.rowid LIMIT ?",
        (current_model(), limit * 4),
    )
    rows = await cursor.fetchall()
    if not rows:
        return 0

    cleaned = clean_chunk_texts([r[1] for r in rows])
    # Embedding input is nashsu-style: document title + heading breadcrumb +
    # chunk text, so a chunk vector carries its document/section context.
    to_embed = [
        (r[0], "\n".join(p for p in (r[2], r[3], text) if p))
        for r, text in zip(rows, cleaned, strict=True)
        if text is not None
    ][:limit]
    if not to_embed:
        return 0

    vectors = await embed_texts([text for _, text in to_embed])
    store = create_vector_store(db)
    try:
        await store.upsert(list(zip([cid for cid, _ in to_embed], vectors, strict=True)))
    finally:
        await store.close()
    return len(to_embed)


async def run_embed_worker(db) -> None:
    """Serial embedding loop. Cancel to stop."""
    logger.info(
        "Embedding worker started (backend=%s, model=%s)",
        settings.VECTOR_BACKEND,
        current_model(),
    )
    while True:
        try:
            if await embed_pending(db) == 0:
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — worker must survive
            logger.exception("Embedding worker error")
            await asyncio.sleep(POLL_SECONDS)


async def search(db, query: str, limit: int = 10) -> list[dict]:
    """Semantic search: query → vector → top chunks → aggregated doc entries."""
    if not vector_leg_enabled():
        return []
    try:
        [query_vector] = await embed_texts([query])
    except Exception as e:  # noqa: BLE001 — vector leg must never break chat
        logger.warning("Vector leg query embedding failed, skipping: %s", e)
        return []

    store = create_vector_store(db)
    try:
        hits = await store.search(query_vector, top_k=limit * 4)
    except Exception as e:  # noqa: BLE001
        logger.warning("Vector store search failed, skipping: %s", e)
        return []
    finally:
        await store.close()
    if not hits:
        return []

    id_placeholders = ",".join("?" for _ in hits)
    cursor = await db.execute(
        f"SELECT dc.id, dc.document_id, dc.content, dc.page, "
        f"d.filename, d.title, d.path, d.relative_path, d.source_kind "
        f"FROM document_chunks dc JOIN documents d ON d.id = dc.document_id "
        f"WHERE dc.id IN ({id_placeholders}) AND d.status != 'failed' "
        f"AND d.source_kind = 'wiki'",
        [chunk_id for chunk_id, _ in hits],
    )
    chunk_meta = {r[0]: r for r in await cursor.fetchall()}

    docs: dict[str, dict] = {}
    for chunk_id, score in hits:
        row = chunk_meta.get(chunk_id)
        if row is None:  # orphaned vector (chunk deleted, index not cleaned)
            continue
        _, doc_id, content, page, filename, title, path, rel_path, source_kind = row
        entry = docs.get(doc_id)
        if entry is None:
            docs[doc_id] = {
                "doc_id": doc_id,
                "filename": filename,
                "title": title or filename,
                "path": path,
                "relative_path": rel_path,
                "source_kind": source_kind or "source",
                "score": score,
                "hits": 1,
                "snippet": (content or "")[:300],
                "page": page,
            }
        else:
            entry["hits"] += 1
            if score > entry["score"]:
                entry["score"] = score
                entry["snippet"] = (content or "")[:300]
                entry["page"] = page

    results = sorted(docs.values(), key=lambda e: e["score"], reverse=True)
    return results[:limit]
