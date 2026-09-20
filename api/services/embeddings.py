"""Embedding backends for the local vector leg.

Two backends, selected by EMBEDDING_BACKEND:

- ``api`` (default): any OpenAI-compatible ``/embeddings`` endpoint
  (SiliconFlow, OpenAI, vLLM, ...). Needs EMBEDDING_BASE_URL /
  EMBEDDING_API_KEY / EMBEDDING_MODEL — deliberately independent of
  ``LLM_*``: chat relays often don't serve an embeddings endpoint.
- ``local``: fastembed/onnxruntime running a BGE model fully offline
  (``pip install fastembed``, ~200MB of deps + model download on first run).

The whole vector leg is off unless the selected backend is configured;
when off, retrieval behaves exactly as pure FTS.
"""

from __future__ import annotations

import asyncio

import httpx
from config import settings

DEFAULT_API_MODEL = "BAAI/bge-large-zh-v1.5"
DEFAULT_LOCAL_MODEL = "BAAI/bge-small-zh-v1.5"  # fastembed 支持的 BGE 中文模型


class EmbeddingConfigError(RuntimeError):
    pass


class EmbeddingRequestError(RuntimeError):
    pass


def _backend() -> str:
    return settings.EMBEDDING_BACKEND.strip().lower() or "api"


def embedding_enabled() -> bool:
    if _backend() == "local":
        # find_spec is cheap — importing fastembed itself takes seconds.
        # Missing package = leg off, not a crash loop in the worker.
        import importlib.util

        return importlib.util.find_spec("fastembed") is not None
    return bool(
        settings.EMBEDDING_BASE_URL.strip()
        and settings.EMBEDDING_API_KEY.strip()
        and settings.EMBEDDING_MODEL.strip()
    )


def current_model() -> str:
    """Registry/index key for the active backend's model."""
    if _backend() == "local":
        return settings.LOCAL_EMBEDDING_MODEL.strip() or DEFAULT_LOCAL_MODEL
    return settings.EMBEDDING_MODEL.strip()


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a batch of texts with the configured backend, preserving order."""
    if not texts:
        return []
    if _backend() == "local":
        return await _embed_local(texts)
    return await _embed_api(texts)


async def _embed_api(texts: list[str]) -> list[list[float]]:
    if not embedding_enabled():
        raise EmbeddingConfigError(
            "Embeddings are not configured — set EMBEDDING_BASE_URL / "
            "EMBEDDING_API_KEY / EMBEDDING_MODEL in .env "
            "(or EMBEDDING_BACKEND=local for the offline model)"
        )
    base_url = settings.EMBEDDING_BASE_URL.strip().rstrip("/")
    async with httpx.AsyncClient(timeout=settings.LLM_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {settings.EMBEDDING_API_KEY.strip()}"},
                json={"model": current_model(), "input": texts},
            )
        except httpx.TimeoutException as e:
            raise EmbeddingRequestError(f"Embedding request timed out: {e}") from e
        except httpx.HTTPError as e:
            raise EmbeddingRequestError(f"Embedding request failed: {e}") from e
    if resp.status_code != 200:
        raise EmbeddingRequestError(
            f"Embedding API {resp.status_code}: {resp.text[:300]}"
        )
    data = resp.json().get("data", [])
    if len(data) != len(texts):
        raise EmbeddingRequestError(
            f"Embedding API returned {len(data)} vectors for {len(texts)} inputs"
        )
    return [item["embedding"] for item in sorted(data, key=lambda d: d["index"])]


_local_model = None


def _embed_batch_sync(texts: list[str]) -> list[list[float]]:
    global _local_model
    if _local_model is None:
        try:
            from fastembed import TextEmbedding
        except ImportError as e:
            raise EmbeddingConfigError(
                "EMBEDDING_BACKEND=local requires fastembed: pip install fastembed"
            ) from e
        _local_model = TextEmbedding(model_name=current_model())
    return [vec.tolist() for vec in _local_model.embed(texts)]


async def _embed_local(texts: list[str]) -> list[list[float]]:
    # fastembed/onnxruntime is synchronous and CPU-bound — keep the event
    # loop responsive by running batches in a worker thread.
    return await asyncio.to_thread(_embed_batch_sync, texts)
