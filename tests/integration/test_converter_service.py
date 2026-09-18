"""Converter /extract tests.

Extraction shells out to opendataloader in a child process and streams the
source over httpx, so the seams we mock are `_run_in_process_group` (the
subprocess) and `httpx.AsyncClient` (the download) — not an in-process library
call. CONVERTER_SECRET is mandatory, so the fixture sets it and requests carry
the matching bearer token by default.
"""

import asyncio
import importlib
import json
import sys
import tempfile
import threading
import types
from collections.abc import MutableMapping, MutableSequence, MutableSet
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient as HTTPXAsyncClient

_SECRET = "test-secret"
_AUTH = {"Authorization": f"Bearer {_SECRET}"}


@pytest.fixture
def converter_module(monkeypatch):
    Path("/tmp/conversions").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CONVERTER_SECRET", _SECRET)
    module = importlib.import_module("converter.main")
    return importlib.reload(module)


class _FakeStreamResponse:
    def __init__(self, data: bytes):
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def raise_for_status(self):
        return None

    async def aiter_bytes(self, chunk_size: int = 65536):
        for i in range(0, len(self._data), chunk_size):
            yield self._data[i:i + chunk_size]


class _FakeAsyncClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str):
        if "alpha" in url:
            return _FakeStreamResponse(b"alpha document")
        if "beta" in url:
            return _FakeStreamResponse(b"beta document")
        return _FakeStreamResponse(b"default document")


def _install_mocks(monkeypatch, converter_module, page_payload: dict | None = None):
    """Replace the download client and the opendataloader/LibreOffice subprocess."""
    monkeypatch.setattr(converter_module.httpx, "AsyncClient", _FakeAsyncClient)

    def fake_run(command: list[str], timeout: int, what: str) -> None:
        if what == "LibreOffice":
            outdir = command[command.index("--outdir") + 1]
            source = Path(command[-1])
            Path(outdir, f"{source.stem}.pdf").write_bytes(b"%PDF-1.4 fake")
            return
        if what != "opendataloader":
            return
        pdf_path = Path(command[-2])
        output_dir = command[-1]
        if page_payload is not None:
            payload = page_payload
        else:
            source_text = pdf_path.read_text(encoding="utf-8")
            marker = pdf_path.parent.name
            payload = {
                "number of pages": 1,
                "kids": [{
                    "type": "paragraph",
                    "page number": 1,
                    "content": f"{source_text} :: {marker}",
                }],
            }
        Path(output_dir, "result.json").write_text(json.dumps(payload), encoding="utf-8")

    monkeypatch.setattr(converter_module, "_run_in_process_group", fake_run)


async def _post_extract(converter_module, payload: dict, headers: dict | None = None):
    transport = ASGITransport(app=converter_module.app)
    async with HTTPXAsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/extract", json=payload, headers=_AUTH if headers is None else headers)


async def test_request_id_echo_and_absence(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)

    with_id = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf", "request_id": "req-alpha"},
    )
    without_id = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/beta.pdf", "source_ext": "pdf"},
    )

    assert with_id.status_code == 200
    assert with_id.json()["request_id"] == "req-alpha"
    assert with_id.json()["pages"][0]["content"].startswith("alpha document")

    assert without_id.status_code == 200
    assert "request_id" not in without_id.json()
    assert without_id.json()["pages"][0]["content"].startswith("beta document")


async def test_concurrent_request_isolation(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)

    resp_a, resp_b = await asyncio.gather(
        _post_extract(
            converter_module,
            {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf", "request_id": "req-a"},
        ),
        _post_extract(
            converter_module,
            {"source_url": "https://bucket.s3.amazonaws.com/beta.pdf", "source_ext": "pdf", "request_id": "req-b"},
        ),
    )

    body_a = resp_a.json()
    body_b = resp_b.json()

    assert resp_a.status_code == 200
    assert resp_b.status_code == 200
    assert body_a["request_id"] == "req-a"
    assert body_b["request_id"] == "req-b"
    assert body_a["pages"][0]["content"].startswith("alpha document")
    assert body_b["pages"][0]["content"].startswith("beta document")
    assert body_a["pages"][0]["content"] != body_b["pages"][0]["content"]


async def test_capacity_response_is_retryable(monkeypatch, converter_module):
    monkeypatch.setattr(converter_module, "MAX_CONCURRENT_EXTRACTIONS", 1)
    monkeypatch.setattr(converter_module, "_active_extractions", 1)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 503
    assert resp.headers["retry-after"] == "1"
    assert "capacity" in resp.json()["detail"].lower()


async def test_cancellation_keeps_extraction_slot_until_worker_stops(
    monkeypatch,
    converter_module,
    tmp_path,
):
    started = threading.Event()
    release = threading.Event()

    def blocking_extract(_pdf_path, _output_dir):
        started.set()
        assert release.wait(timeout=5)
        return []

    monkeypatch.setattr(converter_module, "_extract_pages", blocking_extract)

    async def run_in_slot():
        with converter_module._extraction_slot():
            return await converter_module._run_extraction(
                tmp_path / "source.pdf",
                str(tmp_path),
                "cancelled-request",
            )

    task = asyncio.create_task(run_in_slot())
    assert await asyncio.to_thread(started.wait, 2)
    task.cancel()
    await asyncio.sleep(0)

    assert converter_module._active_extractions == 1
    assert not task.done()
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert converter_module._active_extractions == 0


async def test_office_file_converted_then_extracted(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.docx", "source_ext": "docx"},
    )

    assert resp.status_code == 200
    assert resp.json()["page_count"] == 1


async def test_malicious_page_count_is_clamped(monkeypatch, converter_module):
    """A crafted `number of pages` must not drive an unbounded assembly loop."""
    _install_mocks(monkeypatch, converter_module, page_payload={
        "number of pages": 10_000_000_000,
        "kids": [{"type": "paragraph", "page number": 1, "content": "hi"}],
    })

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 200
    assert resp.json()["page_count"] == converter_module.MAX_PAGES


async def test_non_integer_page_count_does_not_crash(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module, page_payload={"number of pages": "lots", "kids": []})

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 200
    assert resp.json()["page_count"] == 0


async def test_extraction_json_bytes_are_capped(monkeypatch, converter_module):
    _install_mocks(
        monkeypatch,
        converter_module,
        page_payload={
            "number of pages": 1,
            "kids": [{"type": "paragraph", "page number": 1, "content": "text"}],
        },
    )
    monkeypatch.setattr(converter_module, "MAX_EXTRACTION_JSON_BYTES", 16)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 500


async def test_extraction_element_count_is_capped(monkeypatch, converter_module):
    _install_mocks(
        monkeypatch,
        converter_module,
        page_payload={
            "number of pages": 1,
            "kids": [
                {"type": "paragraph", "page number": 1, "content": "one"},
                {"type": "paragraph", "page number": 1, "content": "two"},
            ],
        },
    )
    monkeypatch.setattr(converter_module, "MAX_EXTRACTION_ELEMENTS", 1)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 500


async def test_extraction_nested_child_count_is_capped(monkeypatch, converter_module):
    _install_mocks(
        monkeypatch,
        converter_module,
        page_payload={
            "number of pages": 1,
            "kids": [{
                "type": "list",
                "page number": 1,
                "list items": [
                    {"content": "one", "kids": []},
                    {"content": "two", "kids": []},
                ],
            }],
        },
    )
    monkeypatch.setattr(converter_module, "MAX_ELEMENT_CHILDREN", 1)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 500


async def test_extraction_per_element_text_is_capped(monkeypatch, converter_module):
    _install_mocks(
        monkeypatch,
        converter_module,
        page_payload={
            "number of pages": 1,
            "kids": [{"type": "paragraph", "page number": 1, "content": "12345"}],
        },
    )
    monkeypatch.setattr(converter_module, "MAX_ELEMENT_TEXT_BYTES", 4)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 500


async def test_extraction_total_text_is_capped(monkeypatch, converter_module):
    _install_mocks(
        monkeypatch,
        converter_module,
        page_payload={
            "number of pages": 1,
            "kids": [
                {"type": "paragraph", "page number": 1, "content": "abc"},
                {"type": "paragraph", "page number": 1, "content": "def"},
            ],
        },
    )
    monkeypatch.setattr(converter_module, "MAX_EXTRACTED_TEXT_BYTES", 7)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 500


async def test_extraction_serialized_response_is_capped(monkeypatch, converter_module):
    _install_mocks(
        monkeypatch,
        converter_module,
        page_payload={
            "number of pages": 1,
            "kids": [{"type": "paragraph", "page number": 1, "content": "ok"}],
        },
    )
    monkeypatch.setattr(converter_module, "MAX_EXTRACTION_RESPONSE_BYTES", 20)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"},
    )

    assert resp.status_code == 500


async def test_temp_directory_cleanup(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)

    created_paths = []
    real_tempdir = tempfile.TemporaryDirectory

    class RecordingTemporaryDirectory:
        def __init__(self, *args, **kwargs):
            self._ctx = real_tempdir(*args, **kwargs)

        def __enter__(self):
            path = self._ctx.__enter__()
            created_paths.append(path)
            return path

        def __exit__(self, exc_type, exc, tb):
            return self._ctx.__exit__(exc_type, exc, tb)

    monkeypatch.setattr(converter_module.tempfile, "TemporaryDirectory", RecordingTemporaryDirectory)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf", "request_id": "cleanup-check"},
    )

    assert resp.status_code == 200
    assert len(created_paths) == 1
    temp_path = Path(created_paths[0])
    assert not temp_path.exists()
    assert not list(Path("/tmp/conversions").glob(f"{temp_path.name}*"))


async def test_auth_enforcement(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)
    monkeypatch.setattr(converter_module, "CONVERTER_SECRET", "top-secret")

    payload = {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf"}

    missing = await _post_extract(converter_module, payload, headers={})
    wrong = await _post_extract(converter_module, payload, headers={"Authorization": "Bearer nope"})
    correct = await _post_extract(converter_module, payload, headers={"Authorization": "Bearer top-secret"})

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert correct.status_code == 200


async def test_s3_url_validation(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)

    resp = await _post_extract(
        converter_module,
        {"source_url": "https://example.com/not-s3.pdf", "source_ext": "pdf", "request_id": "bad-url"},
    )

    assert resp.status_code == 400
    assert resp.json()["detail"] == "URLs must point to S3"


async def test_no_persistent_state_between_requests(monkeypatch, converter_module):
    _install_mocks(monkeypatch, converter_module)

    def mutable_globals():
        result = {}
        for name, value in vars(converter_module).items():
            if name.startswith("__") or name == "app":
                continue
            if isinstance(value, (MutableMapping, MutableSequence)):
                result[name] = type(value).__name__
            elif isinstance(value, MutableSet) and name not in {
                "OFFICE_EXTENSIONS",
                "PDF_EXTENSIONS",
                "SUPPORTED_EXTENSIONS",
            }:
                result[name] = type(value).__name__
        return result

    before = mutable_globals()
    assert before == {}
    assert converter_module.app.state._state == {}

    first = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/alpha.pdf", "source_ext": "pdf", "request_id": "state-a"},
    )
    second = await _post_extract(
        converter_module,
        {"source_url": "https://bucket.s3.amazonaws.com/beta.pdf", "source_ext": "pdf", "request_id": "state-b"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert mutable_globals() == before
    assert converter_module.app.state._state == {}
