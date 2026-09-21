"""Chat memory tests — window split, footnote stripping, query rewrite.

Run from repo root: PYTHONPATH=api pytest tests/unit/test_chat_memory.py -v
"""

from services import chat_memory, llm_gateway


def test_split_window_short_history_is_all_window():
    msgs = [{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}]
    overflow, window = chat_memory.split_window(msgs)
    assert overflow == []
    assert window == msgs


def test_split_window_overflows_beyond_window_size():
    msgs = [{"role": "user", "content": str(i)} for i in range(15)]
    overflow, window = chat_memory.split_window(msgs)
    assert len(window) == chat_memory.WINDOW_SIZE
    assert len(overflow) == 5
    assert window[0]["content"] == "5"
    assert overflow[-1]["content"] == "4"


def test_strip_footnote_definitions_removes_defs_keeps_inline_refs():
    content = "回答正文[^1]，再看[^2]。\n\n[^1]: 页面A\n[^2]: 页面B\n"
    cleaned = chat_memory.strip_footnote_definitions(content)
    assert "回答正文[^1]" in cleaned
    assert "[^1]: 页面A" not in cleaned
    assert "[^2]: 页面B" not in cleaned


def test_strip_footnote_definitions_collapses_blank_runs():
    content = "第一段。\n\n[^1]: x\n[^2]: y\n"
    cleaned = chat_memory.strip_footnote_definitions(content)
    assert cleaned == "第一段。"


async def test_rewrite_query_no_context_returns_original():
    assert await chat_memory.rewrite_query("", [], "它呢") == "它呢"


async def test_rewrite_query_uses_llm(monkeypatch):
    captured = []

    async def fake_once(messages):
        captured.append(messages)
        return "EMA 的缺点是什么"

    monkeypatch.setattr(llm_gateway, "chat_once", fake_once)
    window = [{"role": "user", "content": "EMA 是什么"}]
    out = await chat_memory.rewrite_query("聊 EMA", window, "那它的缺点呢")
    assert out == "EMA 的缺点是什么"
    assert "那它的缺点呢" in captured[0][-1]["content"]
    assert "聊 EMA" in captured[0][-1]["content"]


async def test_rewrite_query_llm_failure_returns_original(monkeypatch):
    async def failing(messages):
        raise llm_gateway.LLMRequestError("boom")

    monkeypatch.setattr(llm_gateway, "chat_once", failing)
    window = [{"role": "user", "content": "EMA 是什么"}]
    assert await chat_memory.rewrite_query("", window, "那它的缺点呢") == "那它的缺点呢"
