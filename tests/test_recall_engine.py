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


def test_hybrid_merge_vector_only_when_fulltext_empty():
    """Vector-only fallback: fulltext yields nothing, vector hits still rank
    (closes PROGRESS gap #2 — pins the vector-only path in the fused ranker)."""
    vec = [
        {"path": "v1", "content": "", "project": "", "score": 0.9},
        {"path": "v2", "content": "", "project": "", "score": 0.8},
    ]
    out = recall.hybrid_merge([], vec, None, 10)
    assert [r["path"] for r in out] == ["v1", "v2"]
    assert abs(out[0]["score"] - 1.0 / 61) < 1e-9


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


# --- Phase F: memory evolution history --------------------------------------

@pytest.fixture()
def hist_driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean(s):
        s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH 'general/__hist' DETACH DELETE r")
        s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__hist' DETACH DELETE m")

    with d.session() as s:
        _clean(s)
        s.run(
            """
            CREATE (m:Memory {path:$p, content:'C2 current', updated_at:'2026-06-03T00:00:00+00:00',
                              status:'active', created_by:'dream_test'})
            CREATE (:MemoryRevision {content_snapshot:'C0 oldest', operation:'dream_update',
                                     actor:'dream_test', ts:'2026-06-01T00:00:00+00:00'})-[:VERSION_OF]->(m)
            CREATE (:MemoryRevision {content_snapshot:'C1 middle', operation:'dream_update',
                                     actor:'dream_test', ts:'2026-06-02T00:00:00+00:00'})-[:VERSION_OF]->(m)
            """,
            p="general/__hist.md",
        )
    try:
        yield d
    finally:
        with d.session() as s:
            _clean(s)
        d.close()


def test_memory_history_orders_versions_oldest_to_current(hist_driver):
    with hist_driver.session() as s:
        h = recall.memory_history(s, "general/__hist.md")
    assert [v["label"] for v in h["versions"]] == ["v1", "v2", "current"]
    assert [v["content"] for v in h["versions"]] == ["C0 oldest", "C1 middle", "C2 current"]
    assert h["status"] == "active"


def test_memory_history_missing_path_returns_none(hist_driver):
    with hist_driver.session() as s:
        assert recall.memory_history(s, "general/__hist_does_not_exist.md") is None


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


# --- Phase F slice 2: as-of reconstruction + lineage ------------------------

def test_content_as_of_reconstructs_point_in_time():
    versions = [
        {"label": "v1", "ts": "2026-06-02T00:00:00+00:00", "content": "C0 oldest"},
        {"label": "v2", "ts": "2026-06-03T00:00:00+00:00", "content": "C1 middle"},
        {"label": "current", "ts": "2026-06-03T00:00:00+00:00", "content": "C2 current"},
    ]
    assert recall.content_as_of(versions, "2026-06-01T12:00:00+00:00") == "C0 oldest"   # before first change
    assert recall.content_as_of(versions, "2026-06-02T12:00:00+00:00") == "C1 middle"   # between changes
    assert recall.content_as_of(versions, "2026-06-04T00:00:00+00:00") == "C2 current"  # after all changes


@pytest.fixture()
def lineage_driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean(s):
        s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH 'general/__lin' DETACH DELETE r")
        s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__lin' DETACH DELETE m")
        s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__lin_' DETACH DELETE e")

    with d.session() as s:
        _clean(s)
        s.run(
            """
            CREATE (m:Memory {path:$cur, content:'current', status:'active', updated_at:'2026-06-03T00:00:00+00:00'})
            CREATE (old:Memory {path:$old, content:'old', status:'superseded'})
            CREATE (old)-[:SUPERSEDED_BY]->(m)
            CREATE (e:Event {event_id:'__lin_e1', event_name:'UserPromptSubmit', prompt:'the source prompt', timestamp:'2026-06-02T00:00:00+00:00'})
            CREATE (m)-[:EXTRACTED_FROM]->(e)
            """,
            cur="general/__lin.md", old="general/__lin_old.md",
        )
    try:
        yield d
    finally:
        with d.session() as s:
            _clean(s)
        d.close()


def test_memory_lineage_returns_source_events_and_supersession(lineage_driver):
    with lineage_driver.session() as s:
        lin = recall.memory_lineage(s, "general/__lin.md")
    assert lin is not None
    assert any(e["event_id"] == "__lin_e1" for e in lin["source_events"])
    assert "general/__lin_old.md" in lin["supersedes"]
    assert lin["superseded_by"] == []
