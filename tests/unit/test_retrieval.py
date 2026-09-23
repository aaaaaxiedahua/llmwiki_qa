"""Unit tests for the retrieval pipeline (trigram FTS + graph expansion).

Runs against in-memory SQLite — no Postgres, no fixtures beyond the schema.
Run from repo root: PYTHONPATH=api pytest tests/unit/test_retrieval.py -v
"""

import uuid
from pathlib import Path

import aiosqlite
import pytest
from services.graph import expand_related
from services.retrieval import (
    build_match_query,
    estimate_tokens,
    pack_context,
    retrieve,
    search_documents,
)

SCHEMA = (Path(__file__).resolve().parents[2] / "shared" / "sqlite_schema.sql").read_text()


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    yield conn
    await conn.close()


async def _doc(db, filename, title, source_kind, content=""):
    doc_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
        "source_kind, file_type, status, content, tags, version, document_number) "
        "VALUES (?, 'u1', ?, ?, '/wiki/', ?, ?, 'md', 'ready', ?, '[]', 0, 1)",
        (doc_id, filename, title, f"wiki/{filename}", source_kind, content),
    )
    return doc_id


async def _chunk(db, doc_id, idx, content):
    await db.execute(
        "INSERT INTO document_chunks (id, document_id, chunk_index, content, token_count) "
        "VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), doc_id, idx, content, len(content)),
    )


async def _ref(db, src, dst, rtype):
    await db.execute(
        "INSERT INTO document_references (source_document_id, target_document_id, reference_type) "
        "VALUES (?, ?, ?)",
        (src, dst, rtype),
    )


async def test_build_match_query_cjk_and_english():
    q = build_match_query("EMA 原型更新是怎么实现的")
    assert '"EMA"' in q
    assert '"原型更新是怎么实现的"' in q
    assert " OR " in q
    assert build_match_query("ab") == ""  # sub-3-char terms dropped


async def test_search_documents_cjk_trigram(db):
    a = await _doc(db, "semi.md", "半监督学习", "wiki")
    await _chunk(db, a, 0, "半监督学习用少量标注数据和大量无标注数据训练模型")
    b = await _doc(db, "unrelated.md", "做饭指南", "wiki")
    await _chunk(db, b, 0, "今天介绍红烧肉的做法")
    await db.commit()

    results = await search_documents(db, "半监督学习")
    assert results and results[0]["doc_id"] == a


async def test_search_documents_title_boost(db):
    a = await _doc(db, "a.md", "EMA 原型更新", "wiki")
    await _chunk(db, a, 0, "指数移动平均相关的正文内容在这里")
    b = await _doc(db, "b.md", "别的标题", "wiki")
    await _chunk(db, b, 0, "EMA 出现在正文但标题没有")
    await db.commit()

    results = await search_documents(db, "EMA")
    titles = {r["doc_id"]: r["title"] for r in results}
    assert results[0]["doc_id"] == a, titles


async def test_search_documents_short_term_falls_back_to_title_leg(db):
    """2 字词被 trigram 门槛丢弃后，标题 LIKE 兜底仍应命中。"""
    a = await _doc(db, "a.md", "幂等设计", "wiki")
    await _chunk(db, a, 0, "与查询无关的正文内容占位")
    await db.commit()

    results = await search_documents(db, "幂等")
    assert results and results[0]["doc_id"] == a


async def test_search_documents_short_term_falls_back_to_content_leg(db):
    """2 字词的正文 LIKE 兜底：标题不含该词的文档也能命中并带摘要。"""
    a = await _doc(db, "a.md", "可靠性设计", "wiki")
    await _chunk(db, a, 0, "本文介绍排名算法与重试机制的设计")
    b = await _doc(db, "b.md", "无关标题", "wiki")
    await _chunk(db, b, 0, "红烧肉的做法与火候")
    await db.commit()

    results = await search_documents(db, "排名")
    assert results and results[0]["doc_id"] == a
    assert "排名" in results[0]["snippet"]


def test_graph_quota_adapts_to_vector_coverage():
    from services.retrieval import graph_quota

    assert graph_quota(11, 0.0) == 4  # 向量缺席 → 上限 30%
    assert graph_quota(11, 1.0) == 2  # 向量满覆盖 → 下限 15%
    assert graph_quota(1, 0.0) == 0   # 窗口太小不保留


async def test_retrieve_reserves_graph_quota_seats(db):
    """图谱邻居占尾部保留席位，不靠分数挤进种子区。"""
    seeds = []
    for i in range(6):
        d = await _doc(db, f"s{i}.md", f"种子{i}", "wiki")
        await _chunk(db, d, 0, f"配额测试的共同关键词内容第{i}篇")
        seeds.append(d)
    neighbor = await _doc(db, "n.md", "图谱邻居", "wiki")  # 无 chunk，纯图谱进入
    await _ref(db, seeds[0], neighbor, "links_to")
    stranger = await _doc(db, "x.md", "无关文档", "wiki")
    await _chunk(db, stranger, 0, "完全无关的内容")
    await db.commit()

    results = await retrieve(db, "配额测试的共同关键词", use_vector=False)
    ids = [r["doc_id"] for r in results]
    assert ids[-1] == neighbor
    assert results[-1]["graph_expansion"] is True
    assert stranger not in ids


async def test_expand_related_links_and_source_overlap(db):
    seed = await _doc(db, "seed.md", "种子页", "wiki")
    linked = await _doc(db, "linked.md", "被链接页", "wiki")
    overlap = await _doc(db, "overlap.md", "同源页", "wiki")
    source = await _doc(db, "paper.pdf", "原始论文", "source")
    stranger = await _doc(db, "stranger.md", "无关页", "wiki")

    await _ref(db, seed, linked, "links_to")
    await _ref(db, seed, source, "cites")
    await _ref(db, overlap, source, "cites")
    await db.commit()

    results = await expand_related(db, [seed], per_seed=5)
    by_id = {r["doc_id"]: r for r in results}

    assert seed not in by_id
    assert linked in by_id, "links_to neighbor missing"
    assert overlap in by_id, "source-overlap neighbor missing"
    assert stranger not in by_id
    assert by_id[overlap]["score"] > by_id[linked]["score"], "overlap x4 should beat link x3"


async def test_expand_related_two_hop_decay(db):
    a = await _doc(db, "a.md", "A", "wiki")
    b = await _doc(db, "b.md", "B", "wiki")
    c = await _doc(db, "c.md", "C", "wiki")
    await _ref(db, a, b, "links_to")
    await _ref(db, b, c, "links_to")
    await db.commit()

    results = await expand_related(db, [a], per_seed=5, hops=2)
    by_id = {r["doc_id"]: r for r in results}
    assert b in by_id and c in by_id
    assert by_id[b]["score"] > by_id[c]["score"], "hop-2 score should decay"


async def test_retrieve_merges_search_and_graph(db):
    seed = await _doc(db, "ema.md", "EMA 原型更新", "wiki")
    await _chunk(db, seed, 0, "EMA 原型更新使用指数移动平均")
    neighbor = await _doc(db, "mt.md", "Mean Teacher", "wiki")
    await _ref(db, seed, neighbor, "links_to")
    await db.commit()

    results = await retrieve(db, "EMA 原型更新")
    ids = [r["doc_id"] for r in results]
    assert seed in ids and neighbor in ids


async def test_search_documents_alias_metadata_leg(db):
    doc_id = str(uuid.uuid4())
    await db.execute(
        "INSERT INTO documents (id, user_id, filename, title, path, relative_path, "
        "source_kind, file_type, status, content, tags, metadata, version, document_number) "
        "VALUES (?, 'u1', 'mt.md', 'Mean Teacher', '/wiki/', 'wiki/mt.md', "
        "'wiki', 'md', 'ready', '正文不含别名', '[]', "
        '\'{"aliases": ["EMA 原型更新", "教师模型"]}\', 0, 1)',
        (doc_id,),
    )
    await _chunk(db, doc_id, 0, "正文讲的是一致性正则化训练")
    await db.commit()

    results = await search_documents(db, "教师模型")
    assert results and results[0]["doc_id"] == doc_id


async def test_estimate_tokens_and_pack_budget():
    assert estimate_tokens("半监督学习") == 5
    pages = [
        {"content": "x" * 1000, "score": 2.0},
        {"content": "y" * 1000, "score": 1.0},
    ]
    packed = pack_context(pages, budget_tokens=550)
    assert len(packed) == 2  # second page truncated into remaining budget
    assert packed[0]["num"] == 1
    tiny = pack_context(pages, budget_tokens=10)
    assert len(tiny) == 1  # top page always included, truncated
