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
from datetime import datetime, timezone
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import recall  # noqa: E402
import schema  # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


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


# --- C2: ranking signals (importance x decayed recency) ---------------------

def test_importance_factor_is_neutral_at_5_and_clamped():
    assert recall.importance_factor(5) == 1.0
    assert recall.importance_factor(10) == 2.0
    assert recall.importance_factor(1) == 0.2
    assert recall.importance_factor(99) == 2.0     # clamped to 10
    assert recall.importance_factor(None) == 1.0   # missing → neutral
    assert recall.importance_factor("bad") == 1.0  # malformed → neutral


def test_recency_factor_decays_and_defaults_to_one():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    old = recall.recency_factor({"path": "project/x.md", "last_accessed_at": "2025-06-01T00:00:00+00:00"}, now)
    fresh = recall.recency_factor({"path": "project/y.md", "last_accessed_at": "2026-06-01T00:00:00+00:00"}, now)
    none = recall.recency_factor({"path": "project/z.md"}, now)
    assert 0.0 < old < 0.5      # ~1y old at a 30d half-life → heavily decayed
    assert abs(fresh - 1.0) < 1e-6
    assert none == 1.0          # no timestamp → treated as fresh, not penalized


def test_importance_promotes_a_lower_ranked_hit():
    # b is ranked below a by RRF, but its high importance must lift it to #1.
    ft = [
        {"path": "a", "content": "x", "project": "", "score": 9.0, "importance": 2},
        {"path": "b", "content": "x", "project": "", "score": 8.0, "importance": 10},
    ]
    out = recall.hybrid_merge(ft, [], None, 10, now=datetime(2026, 6, 1, tzinfo=timezone.utc))
    assert out[0]["path"] == "b"


# --- C3: raw-event retrieval ------------------------------------------------

def test_render_event_context_labels_and_omits_when_empty():
    out = recall.render_event_context(
        [{"event_name": "PostToolUse", "tool": "Bash", "ts": "2026-06-01T00:00:00", "snippet": "ran ls -la"}]
    )
    assert "Relevant prior activity" in out
    assert "ran ls -la" in out and "Bash" in out
    assert recall.render_event_context([]) == ""


def test_event_search_empty_query_short_circuits():
    # Empty/blank query returns [] before touching the DB (session can be None).
    assert recall.event_search(None, "   ") == []


@pytest.fixture()
def evt_driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    with d.session() as s:
        s.execute_write(schema.create_constraints_and_indexes)  # ensure event_fulltext exists
    saved_min = recall.EVENT_MIN_SCORE
    recall.EVENT_MIN_SCORE = 0.0  # don't let a low fulltext score drop the seeded event
    with d.session() as s:
        s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__c3evt' DETACH DELETE e")
        s.run("CREATE (e:Event {event_id:'__c3evt_1', event_name:'UserPromptSubmit', "
              "prompt:$p, timestamp:'2026-06-01T00:00:00+00:00'})",
              p="ZZQEVENTTOKEN distinctive payload about widgets")
    try:
        yield d
    finally:
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__c3evt' DETACH DELETE e")
        recall.EVENT_MIN_SCORE = saved_min
        d.close()


def test_event_search_finds_raw_event(evt_driver):
    with evt_driver.session() as s:
        hits = recall.event_search(s, "ZZQEVENTTOKEN", limit=5)
    assert any("ZZQEVENTTOKEN" in h["snippet"] for h in hits)
    assert all("path" not in h for h in hits)  # events are not memories


def test_value_density_orders_session_start_truncation():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    buckets = {
        "profile": [
            {"path": "profile/long.md", "content": "L" * 1000, "importance": 2},   # bulky + trivial
            {"path": "profile/short.md", "content": "short", "importance": 9},      # concise + important
        ],
        "tools": [], "project": [],
    }
    md, paths = recall.render_session_start(buckets, None, char_budget=5000, now=now)
    # concise high-importance memory is ordered ahead of the bulky trivial one
    assert md.index("profile/short.md") < md.index("profile/long.md")
    assert paths[0] == "profile/short.md"
