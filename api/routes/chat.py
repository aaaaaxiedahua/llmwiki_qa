"""Local chat routes — wiki QA chat window backed by an OpenAI-compatible LLM.

Only registered in local mode. Enabled only when LLM_API_KEY/LLM_BASE_URL/
LLM_MODEL are configured (via .llmwiki/config.json or env).

Pipeline (deterministic, single LLM generation call per question):
  retrieve (FTS + graph expansion) → pack budget → stream answer →
  file the answer back as a /wiki/syntheses/ page.
"""

import asyncio
import json
import logging
import re
from datetime import date

from config import settings
from deps import get_user_id
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel
from services import chat_memory, llm_gateway, retrieval
from services.graph import rebuild_local

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


def chat_enabled() -> bool:
    return bool(
        settings.LLM_API_KEY.strip()
        and settings.LLM_BASE_URL.strip()
        and settings.LLM_MODEL.strip()
    )


@router.get("/v1/chat/status")
async def chat_status(request: Request):
    from services import vector_index

    return {
        "enabled": chat_enabled(),
        "model": settings.LLM_MODEL if chat_enabled() else None,
        "vector_enabled": vector_index.vector_leg_enabled(),
    }


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    kb_id: str
    message: str
    # session_id：持久化会话（历史从库里读）；仅传 history：悬浮窗免持久化快速问答
    session_id: str | None = None
    history: list[ChatMessage] = []
    # fast = 纯 FTS 关键词检索；deep = FTS + 向量语义混合检索（向量腿需已配置）
    mode: str = "deep"


class SessionRename(BaseModel):
    title: str


@router.get("/v1/chat/sessions")
async def list_chat_sessions(
    request: Request, kb_id: str, user_id: str = Depends(get_user_id)
):
    from services import chat_sessions

    return await chat_sessions.list_sessions(request.app.state.sqlite_db, kb_id)


@router.get("/v1/chat/sessions/{session_id}/messages")
async def get_chat_messages(
    request: Request, session_id: str, user_id: str = Depends(get_user_id)
):
    from services import chat_sessions

    return await chat_sessions.get_messages(request.app.state.sqlite_db, session_id)


@router.patch("/v1/chat/sessions/{session_id}")
async def rename_chat_session(
    request: Request, session_id: str, body: SessionRename,
    user_id: str = Depends(get_user_id),
):
    from services import chat_sessions

    title = " ".join(body.title.split())[: chat_sessions.TITLE_MAX_CHARS]
    if not title:
        return JSONResponse(status_code=400, content={"detail": "title is empty"})
    ok = await chat_sessions.rename_session(request.app.state.sqlite_db, session_id, title)
    if not ok:
        return JSONResponse(status_code=404, content={"detail": "session not found"})
    return {"id": session_id, "title": title}


@router.delete("/v1/chat/sessions/{session_id}", status_code=204)
async def delete_chat_session(
    request: Request, session_id: str, user_id: str = Depends(get_user_id)
):
    from services import chat_sessions

    ok = await chat_sessions.delete_session(request.app.state.sqlite_db, session_id)
    if not ok:
        return JSONResponse(status_code=404, content={"detail": "session not found"})
    return None


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


SYSTEM_PROMPT = """你是这个个人知识库的问答助手。根据下面检索到的 wiki 页面回答用户问题。

规则：
- 只依据提供的页面内容回答；页面里没有的信息，明确说"知识库中还没有相关内容"，不要编造。
- 引用页面时用脚注格式：正文中标注 [^1]、[^2]，回答末尾列出对应的脚注定义，格式为 `[^N]: 页面标题`。
- 回答用中文，简洁有条理。
{purpose}

检索到的页面：
{pages}
{summary}"""


def _build_messages(
    req: ChatRequest, packed: list[dict], purpose: str, overview: str,
    window: list[dict], summary: str,
) -> list[dict]:
    page_blocks = []
    for p in packed:
        page_blocks.append(
            f"[{p['num']}] 《{p['title']}》({p['relative_path']})\n{p['content']}"
        )
    purpose_block = f"\n本知识库的定位：{purpose}\n" if purpose else ""
    if overview:
        purpose_block += f"\n知识库概览（节选）：{overview[:500]}\n"
    summary_block = f"\n此前对话摘要：{summary}\n" if summary else ""
    system = SYSTEM_PROMPT.format(
        purpose=purpose_block,
        pages="\n\n".join(page_blocks) or "（未检索到相关页面）",
        summary=summary_block,
    )
    messages = [{"role": "system", "content": system}]
    messages.extend(window)
    messages.append({"role": "user", "content": req.message})
    return messages


def _slugify(text: str) -> str:
    slug = re.sub(r"[^\w一-鿿-]+", "-", text.lower()).strip("-")
    return slug[:50] or "answer"


async def _load_text(path) -> str:
    try:
        return await asyncio.to_thread(path.read_text, encoding="utf-8")
    except OSError:
        return ""


async def _file_synthesis(
    request: Request, user_id: str, kb_id: str, question: str, answer: str,
    packed: list[dict],
) -> None:
    """File the Q&A back as a /wiki/syntheses/ page — knowledge compounds."""
    if not answer.strip():
        return
    related = "\n".join(
        f"- [{p['title']}](/{p['relative_path']})" for p in packed[:5]
    )
    content = (
        f"---\ntype: synthesis\ncreated: {date.today().isoformat()}\ntags: [synthesis]\n---\n\n"
        f"> [!question] {question}\n\n{answer}\n\n## Related Pages\n\n{related}\n"
    )
    filename = f"{_slugify(question)}.md"
    try:
        svc = request.app.state.factory.document_service(user_id)
        await svc.create_note(kb_id, filename, "/wiki/syntheses/", content)
        # Refresh reference edges so the new page joins graph expansion.
        await rebuild_local(request.app.state.sqlite_db, user_id)
    except Exception as e:  # noqa: BLE001 — archival must never fail the chat
        logger.info("syntheses write-back skipped: %s", e)


@router.post("/v1/chat/stream")
async def chat_stream(req: ChatRequest, request: Request, user_id: str = Depends(get_user_id)):
    if not chat_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "error": "chat_not_configured",
                "detail": "Set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL in .env",
            },
        )

    db = request.app.state.sqlite_db

    async def gen():
        from services import chat_sessions

        session_id = req.session_id
        try:
            if session_id:
                # Persisted session: history comes from the store, not the client.
                cursor = await db.execute(
                    "SELECT 1 FROM chat_sessions WHERE id = ?", (session_id,)
                )
                if not await cursor.fetchone():
                    yield _sse("error", {"detail": "session not found"})
                    return
                stored = await chat_sessions.get_messages(db, session_id)
                cleaned = [
                    {"role": m["role"],
                     "content": chat_memory.strip_footnote_definitions(m["content"])
                     if m["role"] == "assistant" else m["content"]}
                    for m in stored
                ]
                summary = await chat_sessions.ensure_summary(db, session_id, cleaned)
            else:
                cleaned = [
                    {"role": h.role,
                     "content": chat_memory.strip_footnote_definitions(h.content)
                     if h.role == "assistant" else h.content}
                    for h in req.history
                    if h.role in ("user", "assistant")
                ]
                if cleaned:
                    # Floating panel quick chat: ephemeral, never persisted.
                    summary = ""
                else:
                    # First message of a new session: create it and tell the client.
                    session = await chat_sessions.create_session(
                        db, req.kb_id, user_id, chat_sessions.make_title(req.message)
                    )
                    session_id = session["id"]
                    yield _sse("session", session)
                    summary = ""

            _, window = chat_memory.split_window(cleaned)

            # Query rewrite gives follow-ups ("那它的缺点呢") a standalone
            # retrieval query; skipped in fast mode to keep latency low.
            query = req.message
            if req.mode != "fast" and cleaned:
                query = await chat_memory.rewrite_query(summary, window, req.message)

            yield _sse("status", {"stage": "searching"})
            candidates = await retrieval.retrieve(
                db, query, use_vector=req.mode != "fast"
            )
            packed = retrieval.pack_context(
                await retrieval.fetch_context_pages(db, candidates)
            )
            yield _sse("status", {
                "stage": "retrieved",
                "pages": [{"num": p["num"], "title": p["title"],
                           "relative_path": p["relative_path"]} for p in packed],
            })

            from pathlib import Path
            workspace = Path(request.app.state.workspace_path)
            purpose = await _load_text(workspace / "purpose.md")
            overview_row = await db.execute(
                "SELECT content FROM documents WHERE relative_path = 'wiki/overview.md'"
            )
            row = await overview_row.fetchone()
            overview = row[0] if row else ""

            yield _sse("status", {"stage": "generating"})
            answer_parts = []
            async for delta in llm_gateway.chat_stream(
                _build_messages(req, packed, purpose, overview, window, summary)
            ):
                answer_parts.append(delta)
                yield _sse("delta", {"content": delta})

            answer = "".join(answer_parts)
            references = [{"num": p["num"], "title": p["title"],
                           "relative_path": p["relative_path"]} for p in packed]
            yield _sse("done", {
                "session_id": session_id,
                "references": references,
            })

            if session_id:
                await chat_sessions.append_exchange(
                    db, session_id, req.message, answer, references
                )

            asyncio.create_task(
                _file_synthesis(request, user_id, req.kb_id, req.message, answer, packed)
            )
        except (llm_gateway.LLMConfigError, llm_gateway.LLMRequestError) as e:
            yield _sse("error", {"detail": str(e)})
        except Exception as e:
            logger.exception("chat stream failed")
            yield _sse("error", {"detail": f"internal error: {e}"})

    return StreamingResponse(gen(), media_type="text/event-stream")
