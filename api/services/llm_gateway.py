"""OpenAI-compatible streaming chat client for the local QA chat window.

Works with any provider exposing /chat/completions (Kimi, DeepSeek, etc.).
Raises LLMConfigError when the feature is not configured.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
from config import settings


class LLMConfigError(RuntimeError):
    pass


class LLMRequestError(RuntimeError):
    pass


def _require_config() -> tuple[str, str, str]:
    base_url = settings.LLM_BASE_URL.strip().rstrip("/")
    api_key = settings.LLM_API_KEY.strip()
    model = settings.LLM_MODEL.strip()
    if not (base_url and api_key and model):
        raise LLMConfigError(
            "Chat is not configured — set llm.base_url / llm.api_key / llm.model "
            "in .llmwiki/config.json"
        )
    return base_url, api_key, model


async def chat_stream(messages: list[dict]) -> AsyncIterator[str]:
    """Yield content deltas from a streaming chat completion."""
    base_url, api_key, model = _require_config()
    payload = {"model": model, "messages": messages, "stream": True}

    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        try:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=payload,
            ) as resp:
                if resp.status_code != 200:
                    body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
                    raise LLMRequestError(f"LLM API {resp.status_code}: {body}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:
                        yield content
        except httpx.TimeoutException as e:
            raise LLMRequestError(f"LLM request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise LLMRequestError(f"LLM request failed: {e}") from e


async def chat_once(messages: list[dict]) -> str:
    """Non-streaming convenience: collect the full reply."""
    parts = [delta async for delta in chat_stream(messages)]
    return "".join(parts)
