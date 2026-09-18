"""Auto-ingestion worker — nashsu-style two-step LLM distillation.

When enabled (INGESTION_ENABLED + LLM configured), new source documents are
automatically distilled into wiki pages by a serial background queue:

  Step 1 (analyze): LLM reads the document + existing wiki outline
      → structured JSON (entities, concepts, relations, conflicts)
  Step 2 (generate): LLM turns the analysis into a set of wiki pages
      → sources/ summary (guaranteed), entities/, concepts/, overview update

Loop safety: pages are written under wiki/ (source_kind='wiki'), which is
never enqueued; create_note/update_content also mark_written so the watcher
ignores app writes.
"""

import asyncio
import json
import logging
import re
from pathlib import Path

import aiosqlite
from config import settings
from services import llm_gateway

logger = logging.getLogger(__name__)

POLL_SECONDS = 2.0
MAX_SOURCE_CHARS = 24000  # truncation guard for the analyze prompt

ANALYZE_PROMPT = """你是知识库的摄入分析器。阅读下面的源文档和现有 wiki 页面清单，输出结构化分析。

现有 wiki 页面（标题 | 路径 | 别名）：
{wiki_outline}

要求：
- 只输出 JSON，不要任何其他文字。
- 结构：{{"summary": "200字内摘要", "entities": [{{"name", "description"}}],
  "concepts": [{{"name", "description", "aliases": []}}],
  "relations": ["与现有页面的关联"], "conflicts": ["与现有知识的矛盾，没有则为空"]}}

源文档《{title}》：
{content}
"""

GENERATE_PROMPT = """你是知识库的 wiki 撰写器。基于下面的摄入分析，生成 wiki 页面。

要求：
- 只输出 JSON：{{"pages": [{{"path": "/wiki/sources/|/wiki/entities/|/wiki/concepts/",
  "filename": "kebab-case.md", "content": "完整 markdown"}}], "overview_update": null}}
- 必须包含一页 /wiki/sources/ 摘要页，frontmatter 含 type: source、title、sources: ["{source_path}"]。
- 实体/概念页 frontmatter 含 type、title、aliases（有别名时必填，供检索命中）。
- 页面正文用中文，互相用 [标题](/wiki/...) 链接，脚注标注来源 [^1]: {source_path}。
- overview_update：若提供了当前 overview 内容，输出更新后的完整 overview markdown；否则为 null。

当前 overview：
{overview}

摄入分析：
{analysis}
"""


def ingestion_enabled() -> bool:
    return bool(
        settings.INGESTION_ENABLED
        and settings.LLM_API_KEY.strip()
        and settings.LLM_BASE_URL.strip()
        and settings.LLM_MODEL.strip()
    )


async def enqueue_document(db: aiosqlite.Connection, doc_id: str) -> bool:
    """Queue a source document for ingestion. Wiki pages are never enqueued."""
    cursor = await db.execute(
        "SELECT source_kind FROM documents WHERE id = ?", (doc_id,)
    )
    row = await cursor.fetchone()
    if not row or row[0] != "source":
        return False
    cursor = await db.execute(
        "SELECT 1 FROM ingestion_queue WHERE document_id = ? "
        "AND status IN ('pending', 'processing')",
        (doc_id,),
    )
    if await cursor.fetchone():
        return False
    await db.execute(
        "INSERT INTO ingestion_queue (document_id) VALUES (?)", (doc_id,)
    )
    await db.commit()
    logger.info("Enqueued for ingestion: %s", doc_id)
    return True


def _parse_json(text: str) -> dict | None:
    """Tolerantly parse an LLM JSON reply (strips ``` fences / prose)."""
    text = text.strip()
    if m := re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.S):
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            try:
                return json.loads(text[start:])
            except json.JSONDecodeError:
                return None
    return None


async def _wiki_outline(db: aiosqlite.Connection) -> str:
    cursor = await db.execute(
        "SELECT title, relative_path, metadata FROM documents "
        "WHERE source_kind = 'wiki' ORDER BY relative_path LIMIT 100"
    )
    lines = []
    for title, rel, meta in await cursor.fetchall():
        aliases = ""
        if meta:
            with_aliases = meta if isinstance(meta, dict) else _parse_json(str(meta))
            if isinstance(with_aliases, dict) and with_aliases.get("aliases"):
                aliases = ",".join(with_aliases["aliases"])
        lines.append(f"{title or ''} | {rel} | {aliases}")
    return "\n".join(lines) or "（空）"


async def _write_page(svc, kb_id: str, path: str, filename: str, content: str) -> None:
    """Create a wiki page, or update it when it already exists (re-ingest)."""
    if not path.startswith("/wiki/"):
        path = "/wiki/" + path.lstrip("/")
    if not path.endswith("/"):
        path += "/"
    try:
        await svc.create_note(kb_id, filename, path, content)
    except Exception as e:  # HTTPException 409 on conflict — update instead
        if "409" not in str(getattr(e, "status_code", "")):
            raise
        existing = await svc.doc_repo.find_by_path(kb_id, svc.user_id, filename, path)
        if existing:
            await svc.update_content(str(existing["id"]), content)


async def process_one(db, workspace: Path, factory, user_id: str) -> bool:
    """Claim and process one pending queue item. Returns True if one ran."""
    cursor = await db.execute(
        "UPDATE ingestion_queue SET status = 'processing', attempts = attempts + 1 "
        "WHERE id = ("
        "  SELECT q.id FROM ingestion_queue q JOIN documents d ON d.id = q.document_id "
        "  WHERE q.status = 'pending' AND d.status = 'ready' "
        "  ORDER BY q.created_at LIMIT 1"
        ") RETURNING id, document_id"
    )
    row = await cursor.fetchone()
    await db.commit()
    if not row:
        return False
    queue_id, doc_id = row

    try:
        cursor = await db.execute(
            "SELECT title, content, relative_path FROM documents WHERE id = ?",
            (doc_id,),
        )
        doc = await cursor.fetchone()
        if not doc or not (doc[1] or "").strip():
            raise RuntimeError("document has no text content")
        title, content, rel_path = doc

        kb_cursor = await db.execute("SELECT id FROM workspace LIMIT 1")
        kb_id = (await kb_cursor.fetchone())[0]
        svc = factory.document_service(user_id)

        # Step 1: analyze
        analysis_text = await llm_gateway.chat_once([
            {"role": "user", "content": ANALYZE_PROMPT.format(
                wiki_outline=await _wiki_outline(db),
                title=title, content=content[:MAX_SOURCE_CHARS],
            )}
        ])
        analysis = _parse_json(analysis_text) or {"summary": analysis_text[:500]}

        # Step 2: generate pages (+ optional overview rewrite)
        overview = ""
        if settings.INGESTION_UPDATE_OVERVIEW:
            cur = await db.execute(
                "SELECT content FROM documents WHERE relative_path = 'wiki/overview.md'"
            )
            r = await cur.fetchone()
            overview = (r[0] if r else "") or ""
        gen_text = await llm_gateway.chat_once([
            {"role": "user", "content": GENERATE_PROMPT.format(
                source_path=rel_path,
                overview=overview[:6000] if overview else "（无）",
                analysis=json.dumps(analysis, ensure_ascii=False, indent=2),
            )}
        ])
        gen = _parse_json(gen_text) or {}
        pages = [p for p in gen.get("pages", [])
                 if p.get("filename") and p.get("content")]

        # Guarantee the sources/ summary page even if the LLM skipped it
        if not any(p.get("path", "").startswith("/wiki/sources/") for p in pages):
            stem = Path(rel_path).stem
            pages.insert(0, {
                "path": "/wiki/sources/",
                "filename": f"{stem}.md",
                "content": (
                    f"---\ntype: source\ntitle: {title}\nsources: [\"{rel_path}\"]\n---\n\n"
                    f"## 摘要\n\n{analysis.get('summary', '（无摘要）')}\n"
                ),
            })

        for p in pages:
            await _write_page(svc, kb_id, p.get("path", "/wiki/sources/"),
                              p["filename"], p["content"])
            logger.info("Ingested page: %s%s", p.get("path"), p["filename"])

        new_overview = (gen.get("overview_update") or "").strip()
        if overview and settings.INGESTION_UPDATE_OVERVIEW and new_overview:
            await _write_page(svc, kb_id, "/wiki/", "overview.md", new_overview)

        from services.graph import rebuild_local
        await rebuild_local(db, user_id)

        await db.execute(
            "UPDATE ingestion_queue SET status = 'done', "
            "finished_at = datetime('now'), error = NULL WHERE id = ?",
            (queue_id,),
        )
        await db.commit()
        logger.info("Ingestion done: %s (%d pages)", rel_path, len(pages))
    except Exception as e:  # noqa: BLE001 — retry policy lives here
        max_retries = settings.INGESTION_MAX_RETRIES
        cur = await db.execute(
            "SELECT attempts FROM ingestion_queue WHERE id = ?", (queue_id,)
        )
        attempts = (await cur.fetchone() or [max_retries])[0]
        failed = attempts >= max_retries
        await db.execute(
            "UPDATE ingestion_queue SET status = ?, error = ?, "
            "finished_at = CASE WHEN ? THEN datetime('now') ELSE NULL END "
            "WHERE id = ?",
            ("failed" if failed else "pending", str(e)[:500], failed, queue_id),
        )
        await db.commit()
        logger.warning("Ingestion %s (attempt %d): %s",
                       "failed" if failed else "retry", attempts, e)
    return True


async def run_worker(db, workspace: Path, factory, user_id: str) -> None:
    """Serial ingestion loop. Cancel to stop."""
    logger.info("Ingestion worker started")
    while True:
        try:
            if not await process_one(db, workspace, factory, user_id):
                await asyncio.sleep(POLL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — worker must survive
            logger.exception("Ingestion worker error")
            await asyncio.sleep(POLL_SECONDS)
