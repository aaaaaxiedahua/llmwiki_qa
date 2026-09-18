"""Graph service — queries and rebuilds the document reference graph.

All SQL lives here. Routes should never execute queries directly.
"""

import json
import logging
import uuid

from infra.db.sqlite import rows_to_dicts, serialized_write
from services.references import build_lookup_maps, extract_references

logger = logging.getLogger(__name__)


def _parse_json(raw, default=None):
    """Safely parse a JSON string or return the value if already parsed."""
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _build_node(r: dict) -> dict:
    meta = _parse_json(r.get("metadata"), {})
    tags = _parse_json(r.get("tags"), [])
    return {
        "id": str(r["id"]),
        "title": r["title"] or r["filename"].removesuffix(".md").replace("-", " ").replace("_", " "),
        "description": meta.get("description") if isinstance(meta, dict) else None,
        "path": r["path"],
        "file_type": r["file_type"],
        "source_kind": r.get("source_kind", "source"),
        "tags": tags if isinstance(tags, list) else [],
    }


def _build_edge(r: dict) -> dict:
    return {
        "source": str(r["source_document_id"]),
        "target": str(r["target_document_id"]),
        "type": r["reference_type"],
        "page": r["page"],
    }


# ── Hosted (asyncpg) ──

async def get_graph_hosted(conn, kb_id, user_id: str) -> dict:
    """Return {nodes, edges} for the knowledge graph viewer."""
    doc_rows = await conn.fetch(
        "SELECT id, filename, title, path, file_type, metadata, tags, "
        "CASE WHEN path LIKE '/wiki/%' THEN 'wiki' ELSE 'source' END AS source_kind "
        "FROM documents "
        "WHERE knowledge_base_id = $1 AND user_id = $2 AND NOT archived "
        "AND status != 'failed'",
        kb_id, user_id,
    )

    doc_ids = {r["id"] for r in doc_rows}

    ref_rows = await conn.fetch(
        "SELECT source_document_id, target_document_id, reference_type, page "
        "FROM document_references WHERE knowledge_base_id = $1",
        kb_id,
    )

    return {
        "nodes": [_build_node(dict(r)) for r in doc_rows],
        "edges": [_build_edge(dict(r)) for r in ref_rows
                  if r["source_document_id"] in doc_ids and r["target_document_id"] in doc_ids],
    }


async def rebuild_hosted(conn, kb_id, user_id: str) -> dict:
    """Parse wiki pages and rebuild reference edges atomically.

    Runs through RLS (authenticated role) — the database enforces that
    the user can only read/write their own documents and references.
    Uses a savepoint for atomicity within the ScopedDB transaction.
    """
    all_docs = [dict(r) for r in await conn.fetch(
        "SELECT id, filename, title, path, file_type "
        "FROM documents "
        "WHERE knowledge_base_id = $1 AND user_id = $2 AND NOT archived",
        kb_id, user_id,
    )]

    filename_to_doc, base_to_doc, wiki_path_to_doc = build_lookup_maps(all_docs)

    wiki_pages = [dict(r) for r in await conn.fetch(
        "SELECT id, filename, path, content "
        "FROM documents "
        "WHERE knowledge_base_id = $1 AND user_id = $2 "
        "AND path LIKE '/wiki/%' AND NOT archived AND file_type = 'md' "
        "AND content IS NOT NULL AND content != ''",
        kb_id, user_id,
    )]

    # Atomic: transaction wraps the delete + all inserts
    async with conn.transaction():
        await conn.execute(
            "DELETE FROM document_references "
            "WHERE knowledge_base_id = $1 "
            "AND knowledge_base_id IN (SELECT id FROM knowledge_bases WHERE user_id = $2)",
            kb_id, user_id,
        )

        total_cites = 0
        total_links = 0

        for page in wiki_pages:
            content = page["content"] or ""
            if not content:
                continue

            wiki_dir = page["path"].replace("/wiki/", "", 1) if page["path"].startswith("/wiki/") else ""
            edges = extract_references(
                content, page["id"], wiki_dir,
                filename_to_doc, base_to_doc, wiki_path_to_doc,
            )

            for edge in edges:
                if edge["type"] == "cites":
                    await conn.execute(
                        "INSERT INTO document_references "
                        "(source_document_id, target_document_id, knowledge_base_id, reference_type, page) "
                        "VALUES ($1, $2, $3, 'cites', $4) "
                        "ON CONFLICT (source_document_id, target_document_id, reference_type) "
                        "DO UPDATE SET page = EXCLUDED.page, created_at = now()",
                        page["id"], edge["target_id"], kb_id, edge["page"],
                    )
                    total_cites += 1
                else:
                    await conn.execute(
                        "INSERT INTO document_references "
                        "(source_document_id, target_document_id, knowledge_base_id, reference_type) "
                        "VALUES ($1, $2, $3, 'links_to') "
                        "ON CONFLICT (source_document_id, target_document_id, reference_type) DO NOTHING",
                        page["id"], edge["target_id"], kb_id,
                    )
                    total_links += 1

    logger.info("Rebuilt references for KB %s: %d citations, %d links", str(kb_id)[:8], total_cites, total_links)
    return {"citations": total_cites, "links": total_links}


# ── Local (aiosqlite) ──

async def get_graph_local(db, user_id: str) -> dict:
    """Return {nodes, edges} for the knowledge graph viewer (SQLite)."""
    doc_cursor = await db.execute(
        "SELECT id, filename, title, path, file_type, source_kind, metadata, tags "
        "FROM documents WHERE user_id = ? AND status != 'failed'",
        (user_id,),
    )
    doc_rows = rows_to_dicts(doc_cursor, await doc_cursor.fetchall())

    doc_ids = {r["id"] for r in doc_rows}

    ref_cursor = await db.execute(
        "SELECT source_document_id, target_document_id, reference_type, page "
        "FROM document_references",
    )
    ref_rows = rows_to_dicts(ref_cursor, await ref_cursor.fetchall())

    return {
        "nodes": [_build_node(r) for r in doc_rows],
        "edges": [_build_edge(r) for r in ref_rows
                  if r["source_document_id"] in doc_ids and r["target_document_id"] in doc_ids],
    }


async def rebuild_local(db, user_id: str) -> dict:
    """Parse wiki pages and rebuild reference edges atomically (SQLite)."""
    docs_cursor = await db.execute(
        "SELECT id, filename, title, path, file_type, source_kind "
        "FROM documents WHERE user_id = ?",
        (user_id,),
    )
    all_docs = rows_to_dicts(docs_cursor, await docs_cursor.fetchall())

    filename_to_doc, base_to_doc, wiki_path_to_doc = build_lookup_maps(all_docs)

    wiki_cursor = await db.execute(
        "SELECT id, filename, path, content FROM documents "
        "WHERE user_id = ? AND source_kind = 'wiki' AND file_type = 'md' AND content IS NOT NULL",
        (user_id,),
    )
    wiki_pages = rows_to_dicts(wiki_cursor, await wiki_cursor.fetchall())

    # Delete + inserts commit together under the write lock; roll back on any
    # failure so a mid-rebuild error can't leave the graph wiped.
    async with serialized_write():
        try:
            await db.execute("DELETE FROM document_references")

            total_cites = 0
            total_links = 0

            for page in wiki_pages:
                content = page["content"] or ""
                if not content:
                    continue

                wiki_dir = page["path"].replace("/wiki/", "", 1) if page["path"].startswith("/wiki/") else ""
                edges = extract_references(
                    content, page["id"], wiki_dir,
                    filename_to_doc, base_to_doc, wiki_path_to_doc,
                )

                for edge in edges:
                    if edge["type"] == "cites":
                        await db.execute(
                            "INSERT INTO document_references (id, source_document_id, target_document_id, reference_type, page) "
                            "VALUES (?, ?, ?, 'cites', ?) "
                            "ON CONFLICT (source_document_id, target_document_id, reference_type) "
                            "DO UPDATE SET page = excluded.page",
                            (str(uuid.uuid4()), page["id"], edge["target_id"], edge["page"]),
                        )
                        total_cites += 1
                    else:
                        await db.execute(
                            "INSERT INTO document_references (id, source_document_id, target_document_id, reference_type) "
                            "VALUES (?, ?, ?, 'links_to') "
                            "ON CONFLICT (source_document_id, target_document_id, reference_type) DO NOTHING",
                            (str(uuid.uuid4()), page["id"], edge["target_id"]),
                        )
                        total_links += 1

            await db.commit()
        except Exception:
            await db.rollback()
            raise

    logger.info("Rebuilt references: %d citations, %d links", total_cites, total_links)
    return {"citations": total_cites, "links": total_links}


# Relevance signal weights (nashsu 4-signal model, simplified to the two
# signals our existing document_references table already supports).
LINK_WEIGHT = 3.0
SOURCE_OVERLAP_WEIGHT = 4.0
HOP_DECAY = 0.5


async def expand_related(
    db, seed_ids: list[str], per_seed: int = 3, hops: int = 2
) -> list[dict]:
    """Graph expansion from seed documents (local SQLite).

    Two signals: direct [[links]] (x3.0, both directions) and source overlap
    (x4.0 per shared cited source). Scores decay by HOP_DECAY per hop.
    Seeds themselves are excluded. Returns [{doc_id, score, ...}] best-first.
    """
    if not seed_ids:
        return []

    scores: dict[str, float] = {}
    frontier = list(seed_ids)
    seen = set(seed_ids)

    for hop in range(hops):
        decay = HOP_DECAY ** hop
        placeholders = ",".join("?" for _ in frontier)

        # Signal 1: direct links, both directions.
        cur = await db.execute(
            f"SELECT target_document_id AS id FROM document_references "
            f"WHERE reference_type = 'links_to' AND source_document_id IN ({placeholders}) "
            f"UNION "
            f"SELECT source_document_id AS id FROM document_references "
            f"WHERE reference_type = 'links_to' AND target_document_id IN ({placeholders})",
            (*frontier, *frontier),
        )
        linked = [row[0] for row in await cur.fetchall()]
        for doc_id in linked:
            scores[doc_id] = scores.get(doc_id, 0.0) + LINK_WEIGHT * decay

        # Signal 2 (hop 1 only): source overlap — pages citing a source that
        # any seed also cites.
        if hop == 0:
            cur = await db.execute(
                f"SELECT target_document_id FROM document_references "
                f"WHERE reference_type = 'cites' AND source_document_id IN ({placeholders})",
                frontier,
            )
            cited_sources = [row[0] for row in await cur.fetchall()]
            if cited_sources:
                src_ph = ",".join("?" for _ in cited_sources)
                cur = await db.execute(
                    f"SELECT source_document_id AS id, "
                    f"COUNT(DISTINCT target_document_id) AS shared "
                    f"FROM document_references "
                    f"WHERE reference_type = 'cites' AND target_document_id IN ({src_ph}) "
                    f"GROUP BY source_document_id",
                    cited_sources,
                )
                for row in await cur.fetchall():
                    scores[row[0]] = scores.get(row[0], 0.0) + SOURCE_OVERLAP_WEIGHT * min(row[1], 3)

        # Next hop expands only through newly discovered link neighbors.
        frontier = [d for d in set(linked) if d not in seen]
        seen.update(frontier)
        if not frontier:
            break

    # Drop the seeds, hydrate doc info, cap results.
    for sid in seed_ids:
        scores.pop(sid, None)
    if not scores:
        return []

    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    top = top[: per_seed * len(seed_ids)]
    id_ph = ",".join("?" for _ in top)
    cur = await db.execute(
        f"SELECT id, filename, title, path, relative_path, source_kind "
        f"FROM documents WHERE id IN ({id_ph}) AND status != 'failed'",
        [doc_id for doc_id, _ in top],
    )
    info = {r["id"]: r for r in rows_to_dicts(cur, await cur.fetchall())}

    results = []
    for doc_id, score in top:
        meta = info.get(doc_id)
        if not meta:
            continue
        results.append({
            "doc_id": doc_id,
            "filename": meta["filename"],
            "title": meta["title"] or meta["filename"],
            "path": meta["path"],
            "relative_path": meta["relative_path"],
            "source_kind": meta.get("source_kind", "source"),
            "score": score,
            "hits": 0,
            "snippet": "",
            "page": None,
        })
    return results
