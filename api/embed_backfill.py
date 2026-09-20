"""Backfill embeddings for an existing workspace: ``llmwiki embed <ws>``.

Runs the same pull loop as the background embedding worker until every
chunk has a current-model vector, printing progress. Requires EMBEDDING_*
to be configured (.env or environment).
"""

from __future__ import annotations

import asyncio
import sys

from config import settings
from services.embeddings import current_model, embedding_enabled
from services.vector_index import embed_pending


async def _main() -> int:
    if not embedding_enabled():
        print(
            "Embeddings not configured — set EMBEDDING_BASE_URL / "
            "EMBEDDING_API_KEY / EMBEDDING_MODEL in .env",
            file=sys.stderr,
        )
        return 1

    from infra.db.sqlite import create_pool

    db_path = f"{settings.WORKSPACE_PATH}/.llmwiki/index.db"
    db = await create_pool(db_path, init_schema=False)

    cursor = await db.execute(
        "SELECT COUNT(*) FROM document_chunks dc "
        "JOIN documents d ON d.id = dc.document_id WHERE d.status != 'failed'"
    )
    total = (await cursor.fetchone())[0]
    print(f"Embedding backfill: {total} chunks (model={current_model()}, "
          f"backend={settings.VECTOR_BACKEND})")

    done = 0
    try:
        while True:
            n = await embed_pending(db)
            if n == 0:
                break
            done += n
            print(f"\r  {done}/{total} chunks", end="", flush=True)
    finally:
        await db.close()
    print(f"\n✓ Done — {done} chunks embedded")
    return 0


def main() -> None:
    sys.exit(asyncio.run(_main()))


if __name__ == "__main__":
    main()
