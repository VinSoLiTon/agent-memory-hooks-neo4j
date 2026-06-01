#!/usr/bin/env python3
"""Phase C (C1) — shared recall engine. Acceptance tests.

Pins the fused-RRF ranking, the in-project boost, budget-truncated rendering,
the closed mode vocabulary, and the invariant that all three surfaces (hook,
dashboard, CLI) call the one engine rather than carrying their own ranking math.

Pure / static — no Neo4j needed. (DB-backed status filtering is covered by
tests/test_phase_a_history.py, which exercises recall via inject_memory's
re-exports.)
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import recall  # noqa: E402


def test_recall_modes_are_a_closed_vocabulary():
    assert recall.RECALL_MODES == frozenset({"session_start", "prompt_context", "tool_context"})


def test_query_rejects_unknown_mode():
    with pytest.raises(ValueError):
        recall.query(None, "bogus")  # mode is validated before the session is touched


def test_hybrid_merge_fuses_scores_and_orders():
    ft = [
        {"path": "a", "content": "", "project": "", "score": 9.0},
        {"path": "b", "content": "", "project": "", "score": 8.0},
    ]
    vec = [
        {"path": "b", "content": "", "project": "", "score": 0.9},
        {"path": "c", "content": "", "project": "", "score": 0.8},
    ]
    out = recall.hybrid_merge(ft, vec, None, 10)
    # b appears in both streams → highest fused score; returned score IS the fused value.
    assert [r["path"] for r in out] == ["b", "a", "c"]
    assert abs(out[0]["score"] - (1.0 / 62 + 1.0 / 61)) < 1e-9


def test_project_boost_changes_order():
    # Without the boost, y (rank 0) would outrank x (rank 1). The in-project
    # boost on x must flip them.
    ft = [
        {"path": "y", "content": "", "project": "other", "score": 5.0},
        {"path": "x", "content": "", "project": "proj", "score": 4.0},
    ]
    assert recall.hybrid_merge(ft, [], None, 10)[0]["path"] == "y"      # no boost → y wins
    assert recall.hybrid_merge(ft, [], "proj", 10)[0]["path"] == "x"    # boost → x wins


def test_render_session_start_respects_char_budget():
    big = "z" * 500
    buckets = {
        "profile": [{"path": f"profile/p{i}.md", "content": big} for i in range(10)],
        "tools": [],
        "project": [],
    }
    md, paths = recall.render_session_start(buckets, None, char_budget=600)
    assert "further memories omitted" in md
    assert 0 < len(paths) < 10  # truncated well before all ten


def test_render_prompt_lists_hits():
    md, paths = recall.render_prompt([{"path": "general/a.md", "content": "body"}])
    assert "## general/a.md" in md
    assert paths == ["general/a.md"]


def test_all_surfaces_call_the_shared_engine():
    """Negative invariant: no surface keeps its own fulltext/RRF ranking math."""
    inj = open(os.path.join(ROOT, "hooks", "inject_memory.py"), encoding="utf-8").read()
    dash = open(os.path.join(ROOT, "dashboard", "app.py"), encoding="utf-8").read()
    cli = open(os.path.join(ROOT, "cli", "njhook.py"), encoding="utf-8").read()

    for name, src in (("inject_memory", inj), ("dashboard", dash), ("cli", cli)):
        assert "import recall" in src, f"{name} must import the shared recall engine"

    # The fulltext index call and the inline RRF loop must be gone from the hook
    # and dashboard — they now live only in recall.py.
    assert "queryNodes('memory_fulltext'" not in inj
    assert "queryNodes('memory_fulltext'" not in dash
    assert "queryNodes('memory_fulltext'" not in cli
    assert "1.0 / (k + rank" not in dash  # old inline dashboard RRF removed
