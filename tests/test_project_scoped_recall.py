#!/usr/bin/env python3
"""fix/project-scoped-recall — hard project scoping in the shared ranker.

Pins INJECT_PROJECT_SCOPE semantics in hybrid_merge:
  - "hard" + current_project: foreign project/ rows are DROPPED before the
    limit slice; same-project, empty-project, and cross-cutting (non-project/
    path) rows are kept and backfill freed slots.
  - "soft" (default): today's ranking, byte-for-byte — foreign rows still rank
    with only the RRF tie-break boost.
  - "hard" + current_project=None: behaves like soft (dashboard /search and
    CLI callers without a cwd are untouched by the wrapper env).

The drop rule is scoped to project/ PATHS, not the project property alone:
live data (2026-07-18) shows profile/ and tools/ never carry a project tag,
while most general/ rows carry the tag of the project they were distilled in
despite being cross-project by design.

Pure / static — no Neo4j needed (mirrors test_recall_engine.py).
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import recall  # noqa: E402

# Rows deliberately carry no timestamps and no importance → recency and
# importance factors are both neutral (1.0), so fused scores are pure
# RRF (+ boost) and assertable to 1e-9 like the existing engine tests.
FOREIGN = {"path": "project/lps/backfill.md", "content": "", "project": "lps", "score": 9.0}
MINE = {"path": "project/ed/curriculum.md", "content": "", "project": "ed", "score": 8.0}
TOOL = {"path": "tools/alembic/migrations.md", "content": "", "project": "", "score": 7.0}
GENERAL_TAGGED = {"path": "general/verify-outcome.md", "content": "", "project": "njhook", "score": 6.0}
UNTAGGED_PROJECT = {"path": "project/legacy-note.md", "content": "", "project": "", "score": 5.0}


def test_hard_drops_foreign_keeps_same_empty_and_cross_cutting(monkeypatch):
    monkeypatch.setattr(recall, "PROJECT_SCOPE", "hard")
    ft = [dict(FOREIGN), dict(MINE), dict(TOOL), dict(GENERAL_TAGGED), dict(UNTAGGED_PROJECT)]
    out = recall.hybrid_merge(ft, [], "ed", 10)
    paths = [r["path"] for r in out]
    assert FOREIGN["path"] not in paths                 # foreign project/ row dropped
    assert MINE["path"] in paths                        # same-project kept
    assert UNTAGGED_PROJECT["path"] in paths            # empty-project project/ row kept
    assert TOOL["path"] in paths                        # tools/ cross-cutting kept
    # Data-forced variant: general/ rows keep flowing even when tagged with a
    # foreign project (they are cross-project by design; only the path prefix
    # makes a row droppable).
    assert GENERAL_TAGGED["path"] in paths


def test_soft_default_ranking_is_unchanged(monkeypatch):
    """Byte-for-byte pin of today's soft behaviour on the same fixture: the
    foreign row is retained and every fused score matches the exact RRF (+
    in-project boost) arithmetic that predates INJECT_PROJECT_SCOPE."""
    monkeypatch.setattr(recall, "PROJECT_SCOPE", "soft")
    ft = [dict(FOREIGN), dict(MINE), dict(TOOL)]
    out = recall.hybrid_merge(ft, [], "ed", 10)
    # mine: 1/62 + PROJECT_BOOST*0.05; foreign: 1/61; tool: 1/63 (no boost)
    expected = [
        (MINE["path"], 1.0 / 62 + recall.PROJECT_BOOST * 0.05),
        (FOREIGN["path"], 1.0 / 61),
        (TOOL["path"], 1.0 / 63),
    ]
    assert len(out) == len(expected)
    for r, (p, s) in zip(out, expected):
        assert r["path"] == p
        assert abs(r["score"] - s) < 1e-9


def test_hard_without_project_behaves_like_soft(monkeypatch):
    monkeypatch.setattr(recall, "PROJECT_SCOPE", "hard")
    ft = [dict(FOREIGN), dict(MINE), dict(TOOL)]
    hard_none = recall.hybrid_merge(ft, [], None, 10)
    monkeypatch.setattr(recall, "PROJECT_SCOPE", "soft")
    soft_none = recall.hybrid_merge([dict(FOREIGN), dict(MINE), dict(TOOL)], [], None, 10)
    assert [(r["path"], r["score"]) for r in hard_none] == [(r["path"], r["score"]) for r in soft_none]
    assert FOREIGN["path"] in [r["path"] for r in hard_none]  # nothing dropped


def test_hard_backfills_freed_slots(monkeypatch):
    """limit=5 with 3 foreign rows ahead of 6 eligible ones: the foreign drop
    happens BEFORE the limit slice, so 5 eligible rows still come back."""
    monkeypatch.setattr(recall, "PROJECT_SCOPE", "hard")
    foreign = [
        {"path": f"project/ops/f{i}.md", "content": "", "project": "ops", "score": 9.0 - i}
        for i in range(3)
    ]
    eligible = (
        [{"path": f"project/ed/e{i}.md", "content": "", "project": "ed", "score": 5.0 - i} for i in range(3)]
        + [{"path": "tools/t.md", "content": "", "project": "", "score": 1.9}]
        + [{"path": "profile/p.md", "content": "", "project": "", "score": 1.8}]
        + [{"path": "general/g.md", "content": "", "project": "sks", "score": 1.7}]
    )
    out = recall.hybrid_merge(foreign + eligible, [], "ed", 5)
    paths = [r["path"] for r in out]
    assert len(out) == 5                                   # freed slots backfilled
    assert not any(p.startswith("project/ops/") for p in paths)


def test_hard_drops_in_vector_stream_too(monkeypatch):
    monkeypatch.setattr(recall, "PROJECT_SCOPE", "hard")
    vec = [dict(FOREIGN), dict(MINE)]
    out = recall.hybrid_merge([], vec, "ed", 10)
    assert [r["path"] for r in out] == [MINE["path"]]


def test_render_prompt_respects_char_budget():
    """Prompt injection is now budgeted (it previously rendered every hit in
    full, which is the 'oversized injection' half of this fix)."""
    big = "z" * 900
    rows = [{"path": f"project/ed/m{i}.md", "content": big} for i in range(5)]
    md, paths = recall.render_prompt(rows, char_budget=1800)
    assert "further memories omitted" in md
    assert 0 < len(paths) < 5
    # fixed framing (header + omission marker + citation footer) sits outside
    # the budget; the memory entries themselves respect it
    assert len(md) <= 1800 + 200 + len(f"_memory used: {', '.join(paths)}_")


def test_render_prompt_clamps_oversized_top_hit():
    """The top hit always survives, but a single memory larger than the whole
    budget (seen live: a 7.5k-char stale blob) is clamped so the budget stays
    a real ceiling."""
    huge = "z" * 5000
    md, paths = recall.render_prompt(
        [{"path": "project/ed/big.md", "content": huge}], char_budget=100)
    assert paths == ["project/ed/big.md"]      # top hit survives any budget...
    assert "memory truncated" in md            # ...but clamped, not emitted whole
    assert len(md) < 5000
