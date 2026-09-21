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
    await db.execute(
        "INSERT INTO workspace (id, name, description, user_id) "
        "VALUES ('kb1', 'test', '', 'u1')"
    )
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
    assert resp.json() == {"enabled": True, "model": "test-model", "vector_enabled": False}


async def test_stream_sse_flow_and_synthesis_writeback(app):
    resp = await _post(app, {"kb_id": "kb1", "message": "EMA 原型更新是什么"})
    assert resp.status_code == 200
    body = resp.text

    assert "event: status" in body and "searching" in body
    assert '"stage": "generating"' in body
    assert "测试" in body and "回答[^1]" in body
    assert "event: done" in body
    assert "ema.md" in body  # references include the retrieved page

    # First message with no session_id auto-creates a session.
    assert "event: session" in body

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


def _long_history(n_pairs: int):
    history = []
    for i in range(n_pairs):
        history.append({"role": "user", "content": f"问题{i}"})
        history.append({"role": "assistant", "content": f"回答{i}[^1]\n\n[^1]: 页面{i}"})
    return history


async def _create_session_with_history(app, history: list[dict]) -> str:
    from services import chat_sessions

    db = app.state.sqlite_db
    session = await chat_sessions.create_session(db, "kb1", USER_ID, "旧会话")
    for i, m in enumerate(history, start=1):
        await db.execute(
            "INSERT INTO chat_messages (session_id, role, content, seq) "
            "VALUES (?, ?, ?, ?)",
            (session["id"], m["role"], m["content"], i),
        )
    await db.commit()
    return session["id"]


async def test_summary_injected_and_footnotes_stripped(app, monkeypatch):
    captured = {}

    async def fake_once(messages):
        return "用户连续问了12个测试问题。"

    async def fake_stream(messages):
        captured["messages"] = messages
        yield "ok"

    monkeypatch.setattr(chat_module.llm_gateway, "chat_once", fake_once)
    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", fake_stream)

    session_id = await _create_session_with_history(app, _long_history(12))
    resp = await _post(app, {
        "kb_id": "kb1",
        "session_id": session_id,
        "message": "EMA 原型更新是什么",
        "mode": "fast",  # skip rewrite; isolate summary behavior
    })
    assert resp.status_code == 200

    system = captured["messages"][0]["content"]
    assert "此前对话摘要：用户连续问了12个测试问题。" in system

    history_msgs = captured["messages"][1:-1]
    # window is count-based: last 10 of 24 stored messages
    assert len(history_msgs) == 10
    # footnote definitions stripped from replayed assistant answers
    assert all("[^1]:" not in m["content"] for m in history_msgs)

    # summary persisted on the session row
    cursor = await app.state.sqlite_db.execute(
        "SELECT summary, summarized_count FROM chat_sessions WHERE id = ?",
        (session_id,),
    )
    summary, count = await cursor.fetchone()
    assert summary == "用户连续问了12个测试问题。"
    assert count == 14  # 24 stored - WINDOW_SIZE 10


async def test_summary_reused_when_overflow_unchanged(app, monkeypatch):
    async def failing_once(messages):
        raise AssertionError("chat_once must not run — overflow has not grown")

    async def fake_stream(messages):
        yield "ok"

    monkeypatch.setattr(chat_module.llm_gateway, "chat_once", failing_once)
    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", fake_stream)

    session_id = await _create_session_with_history(app, _long_history(12))
    await app.state.sqlite_db.execute(
        "UPDATE chat_sessions SET summary = '旧摘要', summarized_count = 14 "
        "WHERE id = ?",
        (session_id,),
    )
    await app.state.sqlite_db.commit()

    captured = {}

    async def capturing_stream(messages):
        captured["messages"] = messages
        yield "ok"

    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", capturing_stream)
    resp = await _post(app, {
        "kb_id": "kb1", "session_id": session_id,
        "message": "EMA 原型更新是什么", "mode": "fast",
    })
    assert resp.status_code == 200
    assert "此前对话摘要：旧摘要" in captured["messages"][0]["content"]


async def test_second_round_reads_history_from_store(app, monkeypatch):
    captured = {}

    async def fake_stream(messages):
        captured["messages"] = messages
        yield "ok"

    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", fake_stream)

    session_id = await _create_session_with_history(app, [
        {"role": "user", "content": "第一轮问题"},
        {"role": "assistant", "content": "第一轮回答[^1]\n\n[^1]: 页面A"},
    ])
    resp = await _post(app, {
        "kb_id": "kb1", "session_id": session_id,
        "message": "EMA 原型更新是什么", "mode": "fast",
    })
    assert resp.status_code == 200
    contents = [m["content"] for m in captured["messages"]]
    assert "第一轮问题" in contents
    assert any("第一轮回答[^1]" in c and "[^1]: 页面A" not in c for c in contents)


async def test_exchange_persisted_after_stream(app):
    resp = await _post(app, {"kb_id": "kb1", "message": "EMA 原型更新是什么"})
    assert resp.status_code == 200
    # extract session id from the session event
    import json as _json
    session_id = None
    for frame in resp.text.split("\n\n"):
        if frame.startswith("event: session"):
            data_line = [ln for ln in frame.split("\n") if ln.startswith("data:")][0]
            session_id = _json.loads(data_line[5:])["id"]
    assert session_id

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        messages = (await client.get(f"/v1/chat/sessions/{session_id}/messages")).json()
        sessions = (await client.get("/v1/chat/sessions", params={"kb_id": "kb1"})).json()

    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["content"] == "EMA 原型更新是什么"
    assert "测试回答[^1]" in messages[1]["content"]
    assert messages[1]["references"], "assistant references persisted"
    assert sessions[0]["id"] == session_id
    assert sessions[0]["title"] == "EMA 原型更新是什么"
    assert sessions[0]["preview"]


async def test_session_rename_and_delete(app):
    from services import chat_sessions

    session = await chat_sessions.create_session(
        app.state.sqlite_db, "kb1", USER_ID, "旧标题"
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.patch(
            f"/v1/chat/sessions/{session['id']}", json={"title": "  新标题  "}
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "新标题"

        await chat_sessions.append_exchange(
            app.state.sqlite_db, session["id"], "q", "a", []
        )
        resp = await client.delete(f"/v1/chat/sessions/{session['id']}")
        assert resp.status_code == 204

        sessions = (await client.get("/v1/chat/sessions", params={"kb_id": "kb1"})).json()
    assert sessions == []
    # messages cascade-deleted with the session
    cursor = await app.state.sqlite_db.execute(
        "SELECT COUNT(*) FROM chat_messages WHERE session_id = ?", (session["id"],)
    )
    assert (await cursor.fetchone())[0] == 0


async def test_unknown_session_id_returns_error_event(app):
    resp = await _post(app, {
        "kb_id": "kb1", "session_id": "nonexistent", "message": "hi",
    })
    assert resp.status_code == 200
    assert "session not found" in resp.text


async def test_deep_mode_rewrites_query_for_retrieval(app, monkeypatch):
    once_calls = []

    async def fake_once(messages):
        once_calls.append(messages)
        return "EMA 原型更新是什么"

    async def fake_stream(messages):
        yield "ok"

    monkeypatch.setattr(chat_module.llm_gateway, "chat_once", fake_once)
    monkeypatch.setattr(chat_module.llm_gateway, "chat_stream", fake_stream)

    resp = await _post(app, {
        "kb_id": "kb1",
        "message": "那它的更新机制呢",  # follow-up with anaphora
        "history": [{"role": "user", "content": "EMA 是什么"},
                    {"role": "assistant", "content": "指数移动平均。"}],
        "mode": "deep",
    })
    assert resp.status_code == 200
    # retrieval hit the FTS index via the rewritten standalone query,
    # evidenced by the reference to the EMA page
    assert "ema.md" in resp.text
    assert once_calls, "rewrite should run in deep mode"


async def test_fast_mode_skips_rewrite(app, monkeypatch):
    async def fake_once(messages):
        raise AssertionError("chat_once must not be called in fast mode without overflow")

    monkeypatch.setattr(chat_module.llm_gateway, "chat_once", fake_once)

    resp = await _post(app, {
        "kb_id": "kb1",
        "message": "EMA 原型更新是什么",
        "history": [{"role": "user", "content": "hi"},
                    {"role": "assistant", "content": "hello"}],
        "mode": "fast",
    })
    assert resp.status_code == 200
    assert "ema.md" in resp.text
