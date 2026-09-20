"""Qdrant vector store for the local semantic-retrieval leg.

Two modes, one client API, selected by VECTOR_BACKEND:

- ``qdrant`` (default): qdrant-client's embedded local mode — no server,
  data persists under ``.llmwiki/qdrant/``. Same API as server mode.
- ``qdrant-server``: full Qdrant server (e.g. Docker), via QDRANT_URL.

The chunk_embeddings SQLite table doubles as the *registry* of what has
been embedded with the current model — the embedding worker uses it to
find missing/stale chunks with plain SQL; vectors themselves live only
in Qdrant.
"""

from __future__ import annotations

import logging
import re
from typing import Protocol

from config import settings
from services.embeddings import current_model

logger = logging.getLogger(__name__)


class VectorStore(Protocol):
    async def upsert(self, items: list[tuple[str, list[float]]]) -> None:
        """Insert or replace (chunk_id, vector) pairs."""
        ...

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        """Return (chunk_id, cosine_score) pairs, best first."""
        ...

    async def close(self) -> None:
        ...


def _point_id(chunk_id: str) -> str:
    """Qdrant point ids must be dashed UUIDs or ints; chunk ids are 32 hex chars."""
    return f"{chunk_id[:8]}-{chunk_id[8:12]}-{chunk_id[12:16]}-{chunk_id[16:20]}-{chunk_id[20:]}"


_shared_client = None
_collections_ready: set[str] = set()


def _get_client():
    """Process-wide Qdrant client singleton.

    Embedded local mode holds an exclusive lock on the storage directory, so
    worker and request handlers must share one client; server mode is just an
    HTTP client and shares fine too. Never closed — process exit cleans up.
    """
    global _shared_client
    if _shared_client is not None:
        return _shared_client
    try:
        from qdrant_client import QdrantClient
    except ImportError as e:
        raise RuntimeError(
            "The vector leg requires qdrant-client: pip install qdrant-client"
        ) from e
    from pathlib import Path

    if settings.VECTOR_BACKEND == "qdrant-server":
        _shared_client = QdrantClient(url=settings.QDRANT_URL or "http://localhost:6333")
    else:
        path = Path(settings.WORKSPACE_PATH) / ".llmwiki" / "qdrant"
        path.mkdir(parents=True, exist_ok=True)
        _shared_client = QdrantClient(path=str(path))
    return _shared_client


class QdrantVectorStore:
    """Qdrant store — embedded local mode or remote server, same client API."""

    def __init__(self, db):
        self._db = db
        self._client = _get_client()
        self._collection = "chunks_" + re.sub(
            r"[^a-z0-9]+", "_", current_model().lower()
        ).strip("_")

    def _collection_exists(self) -> bool:
        return self._collection in {
            c.name for c in self._client.get_collections().collections
        }

    def _ensure_collection(self, dim: int) -> None:
        if self._collection in _collections_ready:
            return
        from qdrant_client.models import Distance, VectorParams

        if not self._collection_exists():
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )
        _collections_ready.add(self._collection)

    async def upsert(self, items: list[tuple[str, list[float]]]) -> None:
        if not items:
            return
        from qdrant_client.models import PointStruct

        self._ensure_collection(len(items[0][1]))
        self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=_point_id(chunk_id), vector=vector, payload={"chunk_id": chunk_id}
                )
                for chunk_id, vector in items
            ],
        )
        # Registry row so the worker knows this chunk is embedded with the
        # current model (vectors themselves stay in Qdrant).
        await self._db.executemany(
            "INSERT INTO chunk_embeddings (chunk_id, model) VALUES (?, ?) "
            "ON CONFLICT(chunk_id) DO UPDATE SET model = excluded.model",
            [(chunk_id, current_model()) for chunk_id, _ in items],
        )
        await self._db.commit()

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        if self._collection not in _collections_ready:
            if not self._collection_exists():
                return []
            _collections_ready.add(self._collection)
        hits = self._client.query_points(
            collection_name=self._collection, query=vector, limit=top_k
        ).points
        return [(h.payload["chunk_id"], float(h.score)) for h in hits]

    async def close(self) -> None:
        pass  # shared process-wide client — see _get_client


def create_vector_store(db) -> VectorStore:
    """Build the Qdrant store in the mode selected by VECTOR_BACKEND."""
    backend = settings.VECTOR_BACKEND.strip().lower()
    if backend in ("qdrant", "qdrant-server"):
        return QdrantVectorStore(db)
    raise ValueError(f"Unknown VECTOR_BACKEND: {backend!r} (qdrant|qdrant-server)")
