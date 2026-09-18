"""Chat route tests — SSE flow with a mocked LLM gateway, in-memory SQLite.

Run from repo root: PYTHONPATH=api pytest tests/unit/test_chat_route.py -v
"""

import uuid
from pathlib import Path

import aiosqlite
import httpx
import pytest
from config import settings
from deps import get_user_id
from fastapi import FastAPI
from routes import chat as chat_module

SCHEMA = (Path(__file__).resolve().parents[2] / "shared" / "sqlite_schema.sql").read_text()
USER_ID = "u1"


class FakeDocService:
    def __init__(self):
        self.notes: list[dict] = []

    async def create_note(self, kb_id, filename, path, content):
        self.notes.append({"kb_id": kb_id, "filename": filename, "path": path, "content": content})
        return {"id": str(uuid.uuid4())}


class FakeFactory:
    def __init__(self, doc_service):
        self._doc_service = doc_service

    def document_service(self, user_id):
        return self._doc_service


@pytest.fixture
async def app(tmp_path, monkeypatch):
    db = await aiosqlite.connect(":memory:")
    await db.executescript(SCHEMA)
    doc_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
        "source_kind, file_type, status, content, tags, version, document_number) "
        "VALUES (?, 'u1', 'ema.md', 'EMA 原型更新', '/wiki/', 'wiki/ema.md', "
        "'wiki', 'md', 'ready', 'EMA 是指数移动平均更新机制', '[]', 0, 1)",
        (doc_id,),
    )
    await db.execute(
        "INSERT INTO document_chunks (id, document_id, chunk_index, content, token_count) "
        "VALUES (?, ?, 0, 'EMA 是指数移动平均更新机制', 20)",
        (str(uuid.uuid4()), doc_id),
    )
    await db.commit()

    async def fake_stream(messages):
        yield "测试"
        yield "回答[^1]"

    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", fake_stream)
    monkeypatch.setattr(chat_module, "rebuild_local", lambda *a, **k: None)
    monkeypatch.setattr(settings, "LLM_API_KEY", "sk-test")
    monkeypatch.setattr(settings, "LLM_BASE_URL", "https://example.com/v1")
    monkeypatch.setattr(settings, "LLM_MODEL", "test-model")

    doc_service = FakeDocService()
    app = FastAPI()
    app.include_router(chat_module.router)
    app.dependency_overrides[get_user_id] = lambda: USER_ID
    app.state.sqlite_db = db
    app.state.workspace_path = str(tmp_path)
    app.state.factory = FakeFactory(doc_service)
    app.state.doc_service = doc_service  # for assertions
    yield app
    await db.close()


async def _post(app, body):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.post("/v1/chat/stream", json=body, timeout=10)
        return resp


async def test_status_enabled(app):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/v1/chat/status")
    assert resp.json() == {"enabled": True, "model": "test-model"}


async def test_stream_sse_flow_and_synthesis_writeback(app):
    resp = await _post(app, {"kb_id": "kb1", "message": "EMA 原型更新是什么"})
    assert resp.status_code == 200
    body = resp.text

    assert "event: status" in body and "searching" in body
    assert '"stage": "generating"' in body
    assert "测试" in body and "回答[^1]" in body
    assert "event: done" in body
    assert "ema.md" in body  # references include the retrieved page

    # Wait for the fire-and-forget synthesis write-back.
    for _ in range(50):
        if app.state.doc_service.notes:
            break
        import asyncio
        await asyncio.sleep(0.02)
    notes = app.state.doc_service.notes
    assert notes, "synthesis page was not written"
    assert notes[0]["path"] == "/wiki/syntheses/"
    assert "测试回答[^1]" in notes[0]["content"]
    assert "EMA 原型更新是什么" in notes[0]["content"]


async def test_stream_503_when_not_configured(app, monkeypatch):
    monkeypatch.setattr(settings, "LLM_API_KEY", "")
    resp = await _post(app, {"kb_id": "kb1", "message": "hi"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "chat_not_configured"


async def test_stream_llm_error_becomes_sse_error_event(app, monkeypatch):
    async def failing_stream(messages):
        raise chat_module.llm_gateway.LLMRequestError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", failing_stream)
    resp = await _post(app, {"kb_id": "kb1", "message": "EMA"})
    assert resp.status_code == 200
    assert "event: error" in resp.text and "boom" in resp.text
