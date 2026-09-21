"""Chat session persistence for the local QA window.

Sessions and messages live in the workspace SQLite index (.llmwiki/index.db).
The rolling summary of pre-window history is stored on the session row with
`summarized_count` marking how many leading messages it covers, so a summary
is only recomputed (one small LLM call) when the overflow actually grows.
"""

from __future__ import annotations

import json
import logging
import uuid

from infra.db.sqlite import rows_to_dicts, serialized_write
from services import chat_memory, llm_gateway

logger = logging.getLogger(__name__)

TITLE_MAX_CHARS = 30


def make_title(message: str) -> str:
    title = " ".join(message.split())[:TITLE_MAX_CHARS]
    return title or "新会话"


async def create_session(db, kb_id: str, user_id: str, title: str) -> dict:
    session_id = str(uuid.uuid4()).replace("-", "")
    async with serialized_write():
        await db.execute(
            "INSERT INTO chat_sessions (id, knowledge_base_id, user_id, title) "
            "VALUES (?, ?, ?, ?)",
            (session_id, kb_id, user_id, title),
        )
        await db.commit()
    return {"id": session_id, "title": title}


async def list_sessions(db, kb_id: str) -> list[dict]:
    cursor = await db.execute(
        "SELECT s.id, s.title, s.created_at, s.updated_at, "
        "(SELECT content FROM chat_messages m WHERE m.session_id = s.id "
        " ORDER BY m.seq DESC LIMIT 1) AS last_message "
        "FROM chat_sessions s WHERE s.knowledge_base_id = ? "
        "ORDER BY s.updated_at DESC",
        (kb_id,),
    )
    rows = rows_to_dicts(cursor, await cursor.fetchall())
    for r in rows:
        preview = r.pop("last_message", None) or ""
        r["preview"] = preview[:60]
    return rows


async def rename_session(db, session_id: str, title: str) -> bool:
    async with serialized_write():
        cursor = await db.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = datetime('now') "
            "WHERE id = ?",
            (title, session_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def delete_session(db, session_id: str) -> bool:
    async with serialized_write():
        cursor = await db.execute(
            "DELETE FROM chat_sessions WHERE id = ?", (session_id,)
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_messages(db, session_id: str) -> list[dict]:
    cursor = await db.execute(
        "SELECT role, content, references_json, created_at FROM chat_messages "
        "WHERE session_id = ? ORDER BY seq",
        (session_id,),
    )
    messages = []
    for row in await cursor.fetchall():
        try:
            references = json.loads(row[2]) if row[2] else []
        except (json.JSONDecodeError, TypeError):
            references = []
        messages.append({
            "role": row[0],
            "content": row[1],
            "references": references,
            "created_at": row[3],
        })
    return messages


async def append_exchange(
    db, session_id: str, question: str, answer: str, references: list[dict]
) -> None:
    """Persist one Q&A pair in a single transaction."""
    async with serialized_write():
        cursor = await db.execute(
            "SELECT COALESCE(MAX(seq), 0) FROM chat_messages WHERE session_id = ?",
            (session_id,),
        )
        base = (await cursor.fetchone())[0]
        await db.execute(
            "INSERT INTO chat_messages (session_id, role, content, seq) "
            "VALUES (?, 'user', ?, ?)",
            (session_id, question, base + 1),
        )
        await db.execute(
            "INSERT INTO chat_messages (session_id, role, content, references_json, seq) "
            "VALUES (?, 'assistant', ?, ?, ?)",
            (session_id, answer, json.dumps(references, ensure_ascii=False), base + 2),
        )
        await db.execute(
            "UPDATE chat_sessions SET updated_at = datetime('now') WHERE id = ?",
            (session_id,),
        )
        await db.commit()


async def ensure_summary(db, session_id: str, messages: list[dict]) -> str:
    """Return the session's rolling summary, recomputing only when the
    overflow (messages older than the sliding window) has grown past the
    already-summarized prefix."""
    overflow_len = max(0, len(messages) - chat_memory.WINDOW_SIZE)
    cursor = await db.execute(
        "SELECT summary, summarized_count FROM chat_sessions WHERE id = ?",
        (session_id,),
    )
    row = await cursor.fetchone()
    if not row:
        return ""
    summary, summarized_count = row
    if overflow_len == 0 or overflow_len <= summarized_count:
        return summary

    overflow = [
        {"role": m["role"], "content": m["content"]} for m in messages[:overflow_len]
    ]
    text = "\n".join(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content']}"
        for m in overflow
    )
    try:
        new_summary = await llm_gateway.chat_once([
            {"role": "system", "content": chat_memory.SUMMARIZE_PROMPT},
            {"role": "user", "content": text[:8000]},
        ])
    except (llm_gateway.LLMConfigError, llm_gateway.LLMRequestError) as e:
        logger.info("summary compression skipped: %s", e)
        return summary
    new_summary = new_summary.strip()[: chat_memory.SUMMARY_MAX_CHARS]
    if not new_summary:
        return summary
    async with serialized_write():
        await db.execute(
            "UPDATE chat_sessions SET summary = ?, summarized_count = ? WHERE id = ?",
            (new_summary, overflow_len, session_id),
        )
        await db.commit()
    return new_summary
