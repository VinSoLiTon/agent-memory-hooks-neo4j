#!/usr/bin/env python3
"""Phase D3 — retrieval eval harness.

A deterministic, CI-gateable check that the shared recall engine returns the right
memory for a query. Seeds a small golden fixture (distinctive tokens, so it doesn't
collide with the real graph), runs `recall.prompt_query`, and scores hit@k + MRR.
This is the regression guard for the ranking signals (fulltext + vector + RRF +
importance + recency) that previously had only unit tests.

Run:  python dream/eval_retrieval.py        (seeds, scores, prints, cleans up)
The LLM distillation eval (quality of dream output across providers) is separate.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import recall  # noqa: E402

MARK = "__evalr"

# Golden memories. Every token a query searches on is a coined marker (qqz*) that
# can't appear in a real graph — so the eval is deterministic regardless of what
# else is stored (a CI gate must not depend on graph contents). The rust+role pair
# share `qqzrust`, so the eval also exercises ranking discrimination: the query for
# each must out-rank the other on the *non*-shared markers.
GOLDEN_MEMORIES = [
    {"path": f"project/{MARK}_rust.md",
     "content": "qqzrust safety rule: never use qqzunsafe blocks; the qqzuaf incident cost two days."},
    {"path": f"profile/{MARK}_role.md",
     "content": "qqzrust the user is a qqzsystems engineer who prefers qqzterse answers."},
    {"path": f"tools/{MARK}_ripgrep.md",
     "content": "qqzrg prefer ripgrep over grep for code search; qqzfaster by far."},
    {"path": f"general/{MARK}_backup.md",
     "content": "qqzbk always run the backup command before a qqzmigration."},
]

GOLDEN_QUERIES = [
    {"query": "qqzrust qqzunsafe qqzuaf", "expected": f"project/{MARK}_rust.md"},
    {"query": "qqzrust qqzsystems qqzterse", "expected": f"profile/{MARK}_role.md"},
    {"query": "qqzrg qqzfaster", "expected": f"tools/{MARK}_ripgrep.md"},
    {"query": "qqzbk qqzmigration", "expected": f"general/{MARK}_backup.md"},
]


def seed(driver) -> None:
    cleanup(driver)
    with driver.session() as s:
        for m in GOLDEN_MEMORIES:
            s.run("MERGE (m:Memory {path: $p}) SET m.content = $c, m.status = 'active', "
                  "m.updated_at = '2026-06-01T00:00:00+00:00'", p=m["path"], c=m["content"])


def cleanup(driver) -> None:
    with driver.session() as s:
        s.run("MATCH (m:Memory) WHERE m.path CONTAINS $mark DETACH DELETE m", mark=MARK)


def score(driver, k: int = 5) -> dict:
    """Return {k, hit_at_k, mrr, queries}. hit_at_k = fraction of queries whose
    expected memory is in the top-k; MRR = mean reciprocal rank of the expected."""
    results = []
    with driver.session() as s:
        for q in GOLDEN_QUERIES:
            paths = [h["path"] for h in recall.prompt_query(s, q["query"], limit=k)]
            rank = paths.index(q["expected"]) + 1 if q["expected"] in paths else 0
            results.append({"query": q["query"], "expected": q["expected"], "rank": rank, "top": paths})
    n = len(results) or 1
    return {
        "k": k,
        "hit_at_k": sum(1 for r in results if r["rank"] > 0) / n,
        "mrr": sum((1.0 / r["rank"]) if r["rank"] > 0 else 0.0 for r in results) / n,
        "queries": results,
    }


def main() -> int:
    from neo4j import GraphDatabase
    import embeddings
    # Fulltext-only, like the CI gate. The coined qqz* tokens can't appear in a
    # real memory, so on the fulltext path the golden set is perfectly isolated
    # from the live graph; vector similarity on meaningless tokens is pure noise
    # and would let unrelated real memories crowd out the fixture. This eval
    # validates the ranking/discrimination signals, not semantic embedding.
    embeddings.is_enabled = lambda: False
    d = GraphDatabase.driver(
        os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("HOOKS_NEO4J_USER", "neo4j"),
              os.environ.get("HOOKS_NEO4J_PASSWORD", "password")),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )
    try:
        seed(d)
        rep = score(d)
        print(f"retrieval eval: hit@{rep['k']}={rep['hit_at_k']:.2f}  MRR={rep['mrr']:.3f}")
        for r in rep["queries"]:
            mark = "OK " if r["rank"] == 1 else ("~  " if r["rank"] > 1 else "MISS")
            print(f"  [{mark}] rank={r['rank']}  {r['query']!r} -> {r['expected']}")
        return 0
    finally:
        cleanup(d)
        d.close()


if __name__ == "__main__":
    raise SystemExit(main())
