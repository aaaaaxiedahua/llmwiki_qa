"""检索质量评估：对标注问题集跑检索，计算 Recall@K / MRR。

用法（仓库根目录，PYTHONPATH=api）：

    PYTHONPATH=api python tests/eval/retrieval_eval.py --workspace workspace
    PYTHONPATH=api python tests/eval/retrieval_eval.py --workspace workspace --limit 5 -v

expected 匹配规则：大小写不敏感的子串，命中文档的 relative_path / title / filename
任一字段即算相关。数据集见 tests/eval/queries.json。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import aiosqlite
from services.retrieval import graph_quota, retrieve, rrf_fuse, search_documents

PROD_SEED_LIMIT = 8       # 生产环境 retrieve() 的种子数
PROD_WINDOW = 11          # 种子 + 图谱扩展的完整上下文窗口


async def nashsu_retrieve(db, query: str, window: int = PROD_WINDOW) -> list[dict]:
    """nashsu 式检索模拟：wiki-only 语料 + 单跳互链邻居 + 1/(rank+1) 打分。

    关键词腿仍用我们的 FTS、向量腿同源同模型，隔离的变量只有两个：
    语料范围（仅 wiki 页）与图谱设计（单边单跳、种子排名打分）。
    """
    from services import vector_index

    fts = [d for d in await search_documents(db, query, limit=window * 2)
           if d.get("source_kind") == "wiki"][:window]
    vec: list[dict] = []
    if vector_index.vector_leg_enabled():
        vec = await vector_index.search(db, query, limit=window)
    seeds = rrf_fuse([fts, vec], limit=window) if vec else fts
    if not seeds:
        return []

    # 互链邻接（无向），nashsu 的图只有这一种边
    adj: dict[str, set[str]] = {}
    cur = await db.execute(
        "SELECT source_document_id, target_document_id FROM document_references "
        "WHERE reference_type = 'links_to'"
    )
    for src, dst in await cur.fetchall():
        adj.setdefault(src, set()).add(dst)
        adj.setdefault(dst, set()).add(src)

    seed_ids = [s["doc_id"] for s in seeds]
    seed_set = set(seed_ids)
    scores: dict[str, float] = {}
    for rank, sid in enumerate(seed_ids):
        for nb in adj.get(sid, ()):
            if nb not in seed_set:
                scores[nb] = scores.get(nb, 0.0) + 1.0 / (rank + 1)

    coverage = min(len(vec), window) / window if vec else 0.0
    quota = graph_quota(window, coverage)
    top = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:quota]

    candidates: list[dict] = []
    if top:
        ph = ",".join("?" for _ in top)
        cur = await db.execute(
            f"SELECT id, filename, title, path, relative_path, source_kind "
            f"FROM documents WHERE id IN ({ph}) AND status != 'failed'",
            [d for d, _ in top],
        )
        info = {r[0]: r for r in await cur.fetchall()}
        candidates = [
            {"doc_id": doc_id, "filename": r[1], "title": r[2] or r[1], "path": r[3],
             "relative_path": r[4], "source_kind": r[5], "score": score}
            for doc_id, score in top if (r := info.get(doc_id))
        ]
    return seeds[: window - len(candidates)] + candidates


def is_relevant(doc: dict, expected: list[str]) -> bool:
    haystack = " ".join([
        doc.get("relative_path") or "",
        doc.get("title") or "",
        doc.get("filename") or "",
    ]).lower()
    return any(e.lower() in haystack for e in expected)


async def run_eval(db, queries: list[dict], limit: int, verbose: bool,
                   corpus: str = "all", mode: str = "fts") -> dict[str, tuple[float, float]]:
    """Returns per-tier (recall, mrr); key "all" is the overall metric."""
    tiers: dict[str, list[dict]] = {}
    for item in queries:
        tiers.setdefault(item.get("tier", "direct"), []).append(item)

    async def run_group(group: list[dict]) -> tuple[float, float]:
        recall_hits = 0
        rr_sum = 0.0
        for item in group:
            if mode == "hybrid":
                # 生产形态：8 种子 + 图谱配额 = 11 候选窗口
                results = await retrieve(db, item["query"],
                                         seed_limit=PROD_SEED_LIMIT, use_vector=True)
            elif mode == "nashsu":
                results = await nashsu_retrieve(db, item["query"])
            else:
                results = await search_documents(db, item["query"], limit=limit * 4)
            if corpus == "wiki":
                results = [d for d in results if d.get("source_kind") == "wiki"]
            results = results[:limit]
            first_rank = 0
            for rank, doc in enumerate(results, start=1):
                if is_relevant(doc, item["expected"]):
                    first_rank = rank
                    break

            hit = first_rank > 0
            recall_hits += hit
            rr_sum += 1.0 / first_rank if hit else 0.0

            if verbose:
                mark = "OK " if hit else "MISS"
                top = ", ".join(
                    (d.get("relative_path") or d["filename"])[:40] for d in results[:3]
                )
                print(f"[{mark}] {item['query']}")
                print(f"      rank={first_rank or '-'}  top3: {top}")

        n = len(group)
        return recall_hits / n, rr_sum / n

    out: dict[str, tuple[float, float]] = {}
    for tier, group in tiers.items():
        out[tier] = await run_group(group)
    if len(tiers) > 1:
        hits = sum(r * len(tiers[t]) for t, (r, _) in out.items())
        rrs = sum(m * len(tiers[t]) for t, (_, m) in out.items())
        out["all"] = (hits / len(queries), rrs / len(queries))
    else:
        out["all"] = list(out.values())[0]
    return out


async def main() -> None:
    parser = argparse.ArgumentParser(description="Retrieval eval: Recall@K / MRR")
    parser.add_argument("--workspace", default="workspace", help="工作区目录（含 .llmwiki/index.db）")
    parser.add_argument("--queries", default=str(Path(__file__).with_name("queries.json")))
    parser.add_argument("--limit", type=int, default=5, help="Recall@K 的 K（默认 5）")
    parser.add_argument("--corpus", choices=["all", "wiki"], default="all",
                        help="检索语料范围：all=原文档+wiki，wiki=仅 wiki 页")
    parser.add_argument("--mode", choices=["fts", "hybrid", "nashsu"], default="fts",
                        help="fts=纯关键词；hybrid=我们的生产管线(FTS+向量+图谱配额)；"
                             "nashsu=nashsu 式模拟(wiki-only+单跳互链+排名打分)")
    parser.add_argument("-v", "--verbose", action="store_true", help="逐条打印命中情况")
    args = parser.parse_args()

    db_path = Path(args.workspace) / ".llmwiki" / "index.db"
    if not db_path.exists():
        sys.exit(f"index not found: {db_path}（先运行 ./llmwiki reindex <工作区>）")

    queries = json.loads(Path(args.queries).read_text(encoding="utf-8"))

    # Qdrant 本地库路径由 WORKSPACE_PATH 派生，否则向量腿会打开空库
    from config import settings

    settings.WORKSPACE_PATH = str(Path(args.workspace).resolve())

    async with aiosqlite.connect(f"file:{db_path}?mode=ro", uri=True) as db:
        metrics = await run_eval(db, queries, args.limit, args.verbose,
                                 args.corpus, args.mode)

    print(f"\n{len(queries)} queries  mode={args.mode}  corpus={args.corpus}")
    for tier, (recall, mrr) in metrics.items():
        n = sum(1 for q in queries if q.get("tier", "direct") == tier) if tier != "all" else len(queries)
        print(f"  {tier:12s} n={n:2d}  Recall@{args.limit}={recall:.3f}  MRR@{args.limit}={mrr:.3f}")


if __name__ == "__main__":
    asyncio.run(main())
