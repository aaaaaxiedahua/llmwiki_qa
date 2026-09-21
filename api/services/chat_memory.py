"""Sliding-window chat memory helpers for the local QA chat.

The window stays count-based: the last WINDOW_SIZE messages are sent
verbatim. Everything older is compressed into a rolling summary stored on
the session row — see services/chat_sessions.ensure_summary, which recomputes
only when the overflow grows.

Follow-up questions ("那它的缺点呢") are rewritten into standalone queries
before retrieval, using the summary + window as context.

Every failure degrades to the previous no-memory behavior — memory must
never break the chat flow.
"""

from __future__ import annotations

import logging
import re

from services import llm_gateway

logger = logging.getLogger(__name__)

WINDOW_SIZE = 10
SUMMARY_MAX_CHARS = 1200

# Footnote definitions like "[^1]: 页面标题" — dead links once detached
# from the retrieval round that produced them, so strip before replaying.
_FOOTNOTE_DEF_RE = re.compile(r"^\[\^\d+\]:.*$", re.MULTILINE)
_BLANK_RUN_RE = re.compile(r"\n{3,}")

SUMMARIZE_PROMPT = (
    "你是对话摘要助手。把下面的对话历史压缩成一段简洁的中文摘要，保留："
    "用户关心的主题、已得出的结论、提到的专有名词和文档名。"
    "不超过200字，直接输出摘要，不要任何前缀。"
)

_REWRITE_PROMPT = (
    "根据对话上下文，把用户的最新问题改写成一句独立的、可直接用于检索的查询。"
    "补全指代（它、这个、那、上面提到的等）和省略的主语。"
    "如果问题本身已经独立完整，原样返回。只输出改写后的查询，不要解释。"
)


def strip_footnote_definitions(content: str) -> str:
    cleaned = _FOOTNOTE_DEF_RE.sub("", content)
    return _BLANK_RUN_RE.sub("\n\n", cleaned).strip()


def split_window(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (overflow, window): window is the last WINDOW_SIZE messages."""
    if len(messages) <= WINDOW_SIZE:
        return [], messages
    return messages[:-WINDOW_SIZE], messages[-WINDOW_SIZE:]


async def rewrite_query(
    summary: str, window: list[dict], question: str
) -> str:
    """Rewrite a follow-up question into a standalone retrieval query."""
    if not summary and not window:
        return question
    context_parts = []
    if summary:
        context_parts.append(f"此前对话摘要：{summary}")
    context_parts.extend(
        f"{'用户' if m['role'] == 'user' else '助手'}: {m['content'][:500]}"
        for m in window[-6:]
    )
    try:
        rewritten = await llm_gateway.chat_once([
            {"role": "system", "content": _REWRITE_PROMPT},
            {"role": "user", "content": "\n".join(context_parts)
             + f"\n\n最新问题：{question}"},
        ])
    except (llm_gateway.LLMConfigError, llm_gateway.LLMRequestError) as e:
        logger.info("query rewrite skipped: %s", e)
        return question
    rewritten = rewritten.strip()
    return rewritten or question
