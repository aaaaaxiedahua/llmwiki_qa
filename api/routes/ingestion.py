"""Ingestion status route — queue counts for the web activity indicator."""

from deps import get_user_id
from domain.ingestion import ingestion_enabled
from fastapi import APIRouter, Depends, Request

router = APIRouter(tags=["ingestion"])


@router.get("/v1/ingestion/status")
async def ingestion_status(request: Request, user_id: str = Depends(get_user_id)):
    db = request.app.state.sqlite_db
    cursor = await db.execute(
        "SELECT status, COUNT(*) FROM ingestion_queue GROUP BY status"
    )
    counts = {status: n for status, n in await cursor.fetchall()}
    cursor = await db.execute(
        "SELECT q.document_id, d.title, q.error FROM ingestion_queue q "
        "JOIN documents d ON d.id = q.document_id "
        "WHERE q.status IN ('pending', 'processing') ORDER BY q.created_at LIMIT 10"
    )
    active = [{"document_id": d, "title": t, "error": e}
              for d, t, e in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT q.document_id, d.title, q.error FROM ingestion_queue q "
        "JOIN documents d ON d.id = q.document_id "
        "WHERE q.status = 'failed' ORDER BY q.finished_at DESC LIMIT 10"
    )
    failed_items = [{"document_id": d, "title": t, "error": e}
                    for d, t, e in await cursor.fetchall()]
    return {
        "enabled": ingestion_enabled(),
        "pending": counts.get("pending", 0),
        "processing": counts.get("processing", 0),
        "done": counts.get("done", 0),
        "failed": counts.get("failed", 0),
        "active": active,
        "failed_items": failed_items,
    }
