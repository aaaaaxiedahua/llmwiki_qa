"""Vector-leg routes (local mode only): index status and manual backfill.

The background worker already embeds chunks continuously — the backfill
endpoint just runs the same pull loop eagerly until the index is complete,
so the UI "构建检索" button is a trigger with visible progress, not a new
task mechanism.
"""

import asyncio
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from services import vector_index

logger = logging.getLogger(__name__)

router = APIRouter(tags=["embeddings"])

_backfill_task: asyncio.Task | None = None


async def _run_backfill(db) -> None:
    total = 0
    try:
        while True:
            n = await vector_index.embed_pending(db)
            if n == 0:
                break
            total += n
        logger.info("Embedding backfill finished: %d chunks", total)
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Embedding backfill failed")


@router.get("/v1/embeddings/status")
async def embeddings_status(request: Request):
    db = request.app.state.sqlite_db
    stats = await vector_index.embedding_stats(db)
    stats["backfill_running"] = _backfill_task is not None and not _backfill_task.done()
    return stats


@router.post("/v1/embeddings/backfill")
async def embeddings_backfill(request: Request):
    global _backfill_task
    if not vector_index.vector_leg_enabled():
        return JSONResponse(
            status_code=503,
            content={
                "error": "embeddings_not_configured",
                "detail": "配置 EMBEDDING_*（或 EMBEDDING_BACKEND=local）后重启服务",
            },
        )
    if _backfill_task is not None and not _backfill_task.done():
        return {"started": False, "detail": "backfill already running"}

    # Dedicated connection: the long-running loop must not hold the request
    # handler's connection (see the worker connections in main.lifespan).
    from infra.db.sqlite import create_pool

    db = await create_pool(
        f"{request.app.state.workspace_path}/.llmwiki/index.db", init_schema=False
    )

    async def run_and_close():
        try:
            await _run_backfill(db)
        finally:
            await db.close()

    _backfill_task = asyncio.create_task(run_and_close())
    return {"started": True}
