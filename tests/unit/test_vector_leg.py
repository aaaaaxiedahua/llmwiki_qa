"""Vector leg tests — text cleaning, numpy store, embed_pending, RRF fusion.

Run from repo root: PYTHONPATH=api pytest tests/unit/test_vector_leg.py -v
"""

import uuid
from pathlib import Path

import aiosqlite
import pytest
from config import settings
from services import vector_index
from services.retrieval import rrf_fuse
from services.text_cleaner import clean_chunk_texts
from services.vector_store import QdrantVectorStore

SCHEMA = (Path(__file__).resolve().parents[2] / "shared" / "sqlite_schema.sql").read_text()


# --- text_cleaner ---


def test_clean_removes_cross_chunk_boilerplate():
    texts = [
        f"机密水印\n第{i}节正文内容，足够长的段落，讲述不同的知识点{i}。" for i in range(6)
    ]
    cleaned = clean_chunk_texts(texts)
    assert all(c is not None for c in cleaned)
    assert all("机密水印" not in c for c in cleaned)


def test_clean_keeps_non_repeated_lines():
    texts = ["只出现一次的页眉\n这是一段足够长的正文内容，包含真正的语义信息。"]
    cleaned = clean_chunk_texts(texts)
    assert cleaned[0] is not None
    assert "只出现一次的页眉" in cleaned[0]  # 单 chunk 文档无跨块重复，不应误删


def test_clean_drops_page_numbers_and_short_chunks():
    texts = ["12", "- 3 -", "短"]
    assert clean_chunk_texts(texts) == [None, None, None]


# --- numpy vector store + embed_pending ---


@pytest.fixture
async def db(tmp_path, monkeypatch):
    conn = await aiosqlite.connect(":memory:")
    await conn.executescript(SCHEMA)
    await conn.execute(
        "INSERT INTO workspace (id, name, description, user_id) VALUES ('kb1', 'ws', '', 'u1')"
    )
    monkeypatch.setattr(settings, "MODE", "local")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "test-model")
    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(settings, "VECTOR_BACKEND", "qdrant")
    # Qdrant 本地嵌入模式落盘目录隔离到 tmp_path
    monkeypatch.setattr(settings, "WORKSPACE_PATH", str(tmp_path))
    # 进程级共享 client 单例：每个测试重置，避免串到上一个 tmp_path
    from services import vector_store

    vector_store._shared_client = None
    vector_store._collections_ready.clear()
    yield conn
    await conn.close()


async def _doc_with_chunks(db, texts: list[str]) -> str:
    doc_id = uuid.uuid4().hex
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
        "source_kind, file_type, status, content, tags, version, document_number) "
        "VALUES (?, 'u1', 't.md', 'T', '/', 't.md', 'source', 'md', 'ready', '', '[]', 0, 1)",
        (doc_id,),
    )
    for i, text in enumerate(texts):
        await db.execute(
            "INSERT INTO document_chunks (id, document_id, chunk_index, content, "
            "source_content, token_count) VALUES (?, ?, ?, ?, ?, 10)",
            (uuid.uuid4().hex, doc_id, i, text, text),
        )
    await db.commit()
    return doc_id


async def test_embed_pending_and_qdrant_search(db, monkeypatch):
    await _doc_with_chunks(db, ["苹果是一种水果，富含多种维生素。" * 3])

    async def fake_embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(vector_index, "embed_texts", fake_embed)
    assert await vector_index.embed_pending(db) == 1
    # Registry filled — nothing left to do
    assert await vector_index.embed_pending(db) == 0

    store = QdrantVectorStore(db)
    hits = await store.search([1.0, 0.0, 0.0], top_k=5)
    assert len(hits) == 1
    assert hits[0][1] == pytest.approx(1.0)

    # Model switch invalidates old vectors: worker re-embeds into a new collection
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "other-model")
    store2 = QdrantVectorStore(db)
    assert await store2.search([1.0, 0.0, 0.0], top_k=5) == []
    assert await vector_index.embed_pending(db) == 1
    hits = await store2.search([1.0, 0.0, 0.0], top_k=5)
    assert len(hits) == 1
    await store.close()
    await store2.close()


async def test_embed_pending_skips_failed_docs_and_short_chunks(db, monkeypatch):
    await _doc_with_chunks(db, ["短"])
    await db.execute("INSERT INTO documents (id, user_id, filename, title, path, "
                     "relative_path, source_kind, file_type, status, content, tags, "
                     "version, document_number) VALUES ('d2', 'u1', 'f.pdf', 'F', '/', "
                     "'f.pdf', 'source', 'pdf', 'failed', '', '[]', 0, 2)")
    await db.execute("INSERT INTO document_chunks (id, document_id, chunk_index, "
                     "content, source_content, token_count) VALUES ('c2', 'd2', 0, "
                     "'失败文档的块，不应被嵌入，即使内容足够长', '同上', 10)")
    await db.commit()

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(vector_index, "embed_texts", fake_embed)
    assert await vector_index.embed_pending(db) == 0


async def test_vector_search_aggregates_to_docs(db, monkeypatch):
    doc_id = await _doc_with_chunks(db, [
        "机器学习是人工智能的一个重要分支，研究如何从数据中学习规律。",
        "深度学习使用多层神经网络来拟合复杂的非线性函数映射。",
    ])
    cur = await db.execute(
        "SELECT id FROM document_chunks WHERE document_id = ?", (doc_id,)
    )
    chunk_ids = [r[0] for r in await cur.fetchall()]

    async def fake_embed(texts):
        return [[1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(vector_index, "embed_texts", fake_embed)
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://x/v1")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk")
    assert vector_index.vector_leg_enabled()

    await vector_index.embed_pending(db)
    results = await vector_index.search(db, "查询", limit=5)
    assert len(results) == 1
    assert results[0]["doc_id"] == doc_id
    assert results[0]["hits"] == len(chunk_ids)
    assert results[0]["score"] == pytest.approx(1.0)


# --- 快速/深度检索开关 + 索引统计 ---


async def test_retrieve_fast_mode_skips_vector_leg(db, monkeypatch):
    from services import retrieval

    await _doc_with_chunks(db, ["幂等设计可以防止重复提交造成的数据不一致问题"])
    monkeypatch.setattr(vector_index, "vector_leg_enabled", lambda: True)

    called = False

    async def spy(db, query, limit=10):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(vector_index, "search", spy)

    # fast：跳过向量腿，纯 FTS 照常返回
    results = await retrieval.retrieve(db, "幂等设计", use_vector=False)
    assert not called
    assert results

    # deep（默认）：走向量腿
    await retrieval.retrieve(db, "幂等设计", use_vector=True)
    assert called


async def test_embedding_stats(db, monkeypatch):
    await _doc_with_chunks(db, ["机器学习是人工智能的一个重要分支研究内容"])

    stats = await vector_index.embedding_stats(db)
    assert stats["total_chunks"] == 1
    assert stats["embedded_chunks"] == 0

    async def fake_embed(texts):
        return [[1.0, 0.0] for _ in texts]

    monkeypatch.setattr(vector_index, "embed_texts", fake_embed)
    await vector_index.embed_pending(db)
    stats = await vector_index.embedding_stats(db)
    assert stats["embedded_chunks"] == 1


# --- 嵌入后端开关（api / local） ---

def test_backend_switch_api_requires_full_config(monkeypatch):
    from services import embeddings

    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "api")
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    assert not embeddings.embedding_enabled()

    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "https://x/v1")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "sk")
    monkeypatch.setattr(settings, "EMBEDDING_MODEL", "m")
    assert embeddings.embedding_enabled()
    assert embeddings.current_model() == "m"


def test_backend_switch_local_offline(monkeypatch):
    from services import embeddings

    monkeypatch.setattr(settings, "EMBEDDING_BACKEND", "local")
    # 本地后端无需任何 API 配置；是否启用只取决于 fastembed 是否安装
    monkeypatch.setattr(settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(settings, "EMBEDDING_API_KEY", "")
    import importlib.util

    assert embeddings.embedding_enabled() == (
        importlib.util.find_spec("fastembed") is not None
    )
    assert embeddings.current_model() == embeddings.DEFAULT_LOCAL_MODEL


# --- RRF fusion ---


def test_rrf_fuse_merges_legs_by_rank():
    fts = [{"doc_id": "a", "score": 100.0}, {"doc_id": "b", "score": 50.0}]
    vec = [{"doc_id": "b", "score": 0.9}, {"doc_id": "c", "score": 0.8}]
    merged = rrf_fuse([fts, vec], limit=10)
    ids = [e["doc_id"] for e in merged]
    assert ids[0] == "b"  # 两腿都命中，排名跃升第一
    assert set(ids) == {"a", "b", "c"}
    assert merged[0]["score"] == pytest.approx(1 / 61 + 1 / 60)


def test_rrf_preserves_single_leg_order():
    leg = [{"doc_id": d, "score": s} for d, s in [("a", 9), ("b", 5), ("c", 1)]]
    assert [e["doc_id"] for e in rrf_fuse([leg], limit=10)] == ["a", "b", "c"]
