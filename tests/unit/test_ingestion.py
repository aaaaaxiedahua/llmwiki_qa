"""Ingestion worker tests — queue lifecycle, two-step LLM flow, loop safety.

Run from repo root: PYTHONPATH=api pytest tests/unit/test_ingestion.py -v
"""

import json
import uuid
from pathlib import Path

import aiosqlite
import pytest
from config import settings
from domain import ingestion
from domain.ingestion import enqueue_document, process_one

SCHEMA = (Path(__file__).resolve().parents[2] / "shared" / "sqlite_schema.sql").read_text()
USER_ID = "u1"


@pytest.fixture
async def db(tmp_path, monkeypatch):
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    await conn.execute(
        "INSERT INTO workspace (id, name, description, user_id) VALUES ('kb1', 'ws', '', 'u1')"
    )
    await conn.commit()
    monkeypatch.setattr(settings, "INGESTION_ENABLED", True)
    monkeypatch.setattr(settings, "INGESTION_UPDATE_OVERVIEW", False)
    monkeypatch.setattr(settings, "INGESTION_MAX_RETRIES", 3)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setattr(settings, "LLM_MODEL", "test-model")
    yield conn
    await conn.close()


async def _doc(db, filename, source_kind="source", status="ready", content="正文内容" * 50):
    doc_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
        "source_kind, file_type, status, content, tags, version, document_number) "
        "VALUES (?, 'u1', ?, ?, '/', ?, ?, 'md', ?, ?, '[]', 0, 1)",
        (doc_id, filename, filename, filename, source_kind, status, content),
    )
    await db.commit()
    return doc_id


class FakeDocService:
    def __init__(self):
        self.user_id = USER_ID
        self.created: list[dict] = []
        self.updated: list[dict] = []
        self.doc_repo = self

    async def create_note(self, kb_id, filename, path, content):
        self.created.append({"filename": filename, "path": path, "content": content})
        return {"id": str(uuid.uuid4())}

    async def find_by_path(self, kb_id, user_id, filename, path):
        return None

    async def update_content(self, doc_id, content):
        self.updated.append({"doc_id": doc_id, "content": content})


class FakeFactory:
    def __init__(self, svc):
        self._svc = svc

    def document_service(self, user_id):
        return self._svc


def _mock_llm(monkeypatch, analyze=None, generate=None):
    calls = []

    async def fake_chat_once(messages):
        calls.append(messages[0]["content"][:40])
        n = len(calls)
        if n == 1:
            return analyze or json.dumps({
                "summary": "EMA 是指数移动平均",
                "concepts": [{"name": "EMA", "description": "指数移动平均", "aliases": ["指数滑动平均"]}],
                "entities": [], "relations": [], "conflicts": [],
            }, ensure_ascii=False)
        return generate or json.dumps({
            "pages": [{
                "path": "/wiki/concepts/",
                "filename": "ema.md",
                "content": "---\ntype: concept\ntitle: EMA\naliases: [指数滑动平均]\n---\n\nEMA 是指数移动平均[^1]\n\n[^1]: ema.pdf",
            }],
            "overview_update": None,
        }, ensure_ascii=False)

    async def fake_rebuild(*a, **k):
        return None

    monkeypatch.setattr(ingestion.llm_gateway, "chat_once", fake_chat_once)
    monkeypatch.setattr("services.graph.rebuild_local", fake_rebuild)
    return calls


async def test_enqueue_only_source_docs_and_dedup(db):
    src = await _doc(db, "paper.md", source_kind="source")
    wiki = await _doc(db, "note.md", source_kind="source")
    await db.execute("UPDATE documents SET source_kind = 'wiki' WHERE id = ?", (wiki,))
    await db.commit()

    assert await enqueue_document(db, src) is True
    assert await enqueue_document(db, src) is False  # dedup pending
    assert await enqueue_document(db, wiki) is False  # wiki pages never enqueued


async def test_process_one_full_flow(db, tmp_path, monkeypatch):
    doc_id = await _doc(db, "ema.pdf")
    await enqueue_document(db, doc_id)
    calls = _mock_llm(monkeypatch)
    svc = FakeDocService()

    assert await process_one(db, tmp_path, FakeFactory(svc), USER_ID) is True

    assert len(calls) == 2  # analyze + generate
    paths = [(p["path"], p["filename"]) for p in svc.created]
    assert ("/wiki/concepts/", "ema.md") in paths
    # sources summary page guaranteed even though LLM omitted it
    assert any(p == "/wiki/sources/" for p, _ in paths), paths

    cur = await db.execute(
        "SELECT status FROM ingestion_queue WHERE document_id = ?", (doc_id,)
    )
    assert (await cur.fetchone())[0] == "done"


async def test_process_one_uses_llm_sources_page_when_present(db, tmp_path, monkeypatch):
    doc_id = await _doc(db, "ema.pdf")
    await enqueue_document(db, doc_id)
    generate = json.dumps({
        "pages": [{
            "path": "/wiki/sources/",
            "filename": "ema-source.md",
            "content": "---\ntype: source\ntitle: EMA 论文\nsources: [\"ema.pdf\"]\n---\n\n摘要",
        }],
        "overview_update": None,
    }, ensure_ascii=False)
    _mock_llm(monkeypatch, generate=generate)
    svc = FakeDocService()

    await process_one(db, tmp_path, FakeFactory(svc), USER_ID)
    sources_pages = [p for p in svc.created if p["path"] == "/wiki/sources/"]
    assert len(sources_pages) == 1  # no duplicate fallback page


async def test_process_one_retries_then_fails(db, tmp_path, monkeypatch):
    doc_id = await _doc(db, "broken.pdf")
    await enqueue_document(db, doc_id)

    async def failing(messages):
        raise ingestion.llm_gateway.LLMRequestError("boom")

    monkeypatch.setattr(ingestion.llm_gateway, "chat_once", failing)

    await process_one(db, tmp_path, FakeFactory(FakeDocService()), USER_ID)
    cur = await db.execute(
        "SELECT status, attempts FROM ingestion_queue WHERE document_id = ?", (doc_id,)
    )
    assert tuple(await cur.fetchone()) == ("pending", 1)  # back to pending for retry

    await db.execute("UPDATE ingestion_queue SET attempts = 2 WHERE document_id = ?", (doc_id,))
    await db.commit()
    await process_one(db, tmp_path, FakeFactory(FakeDocService()), USER_ID)
    cur = await db.execute(
        "SELECT status, attempts FROM ingestion_queue WHERE document_id = ?", (doc_id,)
    )
    assert tuple(await cur.fetchone()) == ("failed", 3)


async def test_process_one_skips_unready_docs(db, tmp_path, monkeypatch):
    doc_id = await _doc(db, "converting.pdf", status="pending")
    await enqueue_document(db, doc_id)
    _mock_llm(monkeypatch)

    assert await process_one(db, tmp_path, FakeFactory(FakeDocService()), USER_ID) is False
    cur = await db.execute(
        "SELECT status FROM ingestion_queue WHERE document_id = ?", (doc_id,)
    )
    assert (await cur.fetchone())[0] == "pending"  # waits for conversion
