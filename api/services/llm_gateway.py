"""Streaming chat client for the local QA chat window and auto-ingestion.

Two protocols, selected by LLM_PROTOCOL:
- "openai" (default): any OpenAI-compatible /chat/completions endpoint
  (Kimi, DeepSeek, local vLLM, ...), Bearer auth
- "anthropic": Anthropic /v1/messages endpoint (or a relay speaking it,
  e.g. a local CC Switch proxy), x-api-key auth

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
            "Chat is not configured — set LLM_BASE_URL / LLM_API_KEY / LLM_MODEL "
            "in .env"
        )
    return base_url, api_key, model


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    """Anthropic takes system prompts as a top-level field, not a message."""
    system_parts = [m["content"] for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    return "\n\n".join(system_parts), rest


async def _stream_openai(client, base_url, api_key, model, messages):
    async with client.stream(
        "POST",
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": messages, "stream": True},
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


async def _stream_anthropic(client, base_url, api_key, model, messages):
    system, rest = _split_system(messages)
    payload = {
        "model": model,
        "messages": rest,
        "max_tokens": settings.LLM_MAX_TOKENS,
        "stream": True,
    }
    if system:
        payload["system"] = system
    async with client.stream(
        "POST",
        f"{base_url}/v1/messages",
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        json=payload,
    ) as resp:
        if resp.status_code != 200:
            body = (await resp.aread()).decode("utf-8", errors="replace")[:300]
            raise LLMRequestError(f"LLM API {resp.status_code}: {body}")
        async for line in resp.aiter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:].strip())
            except json.JSONDecodeError:
                continue
            etype = event.get("type")
            if etype == "message_stop":
                return
            if etype == "error":
                raise LLMRequestError(f"LLM stream error: {event.get('error', {})}")
            if etype != "content_block_delta":
                continue
            delta = event.get("delta", {})
            # skip thinking deltas — only user-visible text counts
            if delta.get("type") == "text_delta" and delta.get("text"):
                yield delta["text"]


async def chat_stream(messages: list[dict]) -> AsyncIterator[str]:
    """Yield content deltas from a streaming chat completion."""
    base_url, api_key, model = _require_config()
    protocol = settings.LLM_PROTOCOL.strip().lower() or "openai"
    handler = {"openai": _stream_openai, "anthropic": _stream_anthropic}.get(protocol)
    if handler is None:
        raise LLMConfigError(f"Unknown LLM_PROTOCOL: {protocol!r} (openai|anthropic)")

    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        try:
            async for delta in handler(client, base_url, api_key, model, messages):
                yield delta
        except httpx.TimeoutException as e:
            raise LLMRequestError(f"LLM request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise LLMRequestError(f"LLM request failed: {e}") from e


async def chat_once(messages: list[dict]) -> str:
    """Non-streaming convenience: collect the full reply."""
    parts = [delta async for delta in chat_stream(messages)]
    return "".join(parts)
