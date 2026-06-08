#!/usr/bin/env python3
"""Item #5 — Tier-1 event down-tiering (reversible, lineage-preserving).

Pins the watermark-safety guarantee (only events at/<= the dream watermark are
blanked, so re-dream never needs them), that heavy fields are removed while the
node + ids + NEXT chain survive, that prompt is kept by default (feeds
event_fulltext), idempotency, dry-run, and that :EXTRACTED_FROM lineage to a
tiered event still resolves. Live Neo4j.
"""
import os
import sys
from datetime import datetime, timezone

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import prune_events as pe   # noqa: E402
import recall              # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
SK = "test:__prune"
NOW = datetime(2026, 6, 8, tzinfo=timezone.utc)   # cutoff = 2026-05-09 at retention 30


@pytest.fixture()
def driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__pr_' DETACH DELETE e")
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__pr' DETACH DELETE m")
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        # chain: e1(old,<=wm) -> e2(old,<=wm) -> e3(old, but PAST wm) -> e4(recent)
        # watermark = 2026-01-03 → e1,e2 eligible; e3 past-wm (guarded); e4 too recent.
        s.run(
            """
            CREATE (sess:Session {session_key:$sk, client:'test', session_id:'__prune',
                                  last_dreamed_at:'2026-01-03T00:00:00+00:00'})
            CREATE (e1:Event {event_id:'__pr_1', event_name:'PostToolUse', timestamp:'2026-01-01T00:00:00+00:00',
                              tool_response:$fat, tool_input:'in1', last_assistant_message:'msg1', prompt:'KEEPTOKEN one'})
            CREATE (e2:Event {event_id:'__pr_2', event_name:'PostToolUse', timestamp:'2026-01-02T00:00:00+00:00',
                              tool_response:$fat, tool_input:'in2'})
            CREATE (e3:Event {event_id:'__pr_3', event_name:'PostToolUse', timestamp:'2026-01-10T00:00:00+00:00',
                              tool_response:$fat})
            CREATE (e4:Event {event_id:'__pr_4', event_name:'PostToolUse', timestamp:'2026-06-08T00:00:00+00:00',
                              tool_response:$fat})
            CREATE (sess)-[:FIRST_EVENT]->(e1)
            CREATE (e1)-[:NEXT]->(e2)-[:NEXT]->(e3)-[:NEXT]->(e4)
            CREATE (sess)-[:LATEST_EVENT]->(e4)
            """,
            sk=SK, fat="x" * 500,
        )
    try:
        yield d
    finally:
        _clean()
        d.close()


def _ev(d, eid):
    with d.session() as s:
        r = s.run("MATCH (e:Event {event_id:$e}) RETURN e", e=eid).single()
        return dict(r["e"]) if r else None


def _chain_ids(d):
    with d.session() as s:
        return [r["id"] for r in s.run(
            "MATCH (s:Session {session_key:$sk})-[:FIRST_EVENT]->(e) "
            "MATCH p=(e)-[:NEXT*0..]->(x) RETURN x.event_id AS id ORDER BY x.timestamp", sk=SK)]


def test_tier1_blanks_eligible_keeps_recent_and_past_watermark(driver):
    res = pe.prune_events(driver, retention_days=30, now=NOW, session_keys=[SK])
    assert res["events_tiered"] == 2 and res["sessions"] == 1   # only e1, e2
    e1, e2, e3, e4 = (_ev(driver, f"__pr_{i}") for i in (1, 2, 3, 4))
    # eligible events: heavy fields gone, tiered flag set
    for e in (e1, e2):
        assert e["tiered"] is True and e.get("tiered_at")
        assert e.get("tool_response") is None and e.get("tool_input") is None
        assert e.get("last_assistant_message") is None
    # prompt kept by default (feeds event_fulltext)
    assert e1.get("prompt") == "KEEPTOKEN one"
    # past-watermark event (old but > last_dreamed_at) is NOT touched — re-dream may need it
    assert not e3.get("tiered") and e3.get("tool_response") is not None
    # too-recent event untouched
    assert not e4.get("tiered") and e4.get("tool_response") is not None


def test_chain_and_ids_intact_after_tiering(driver):
    pe.prune_events(driver, retention_days=30, now=NOW, session_keys=[SK])
    assert _chain_ids(driver) == ["__pr_1", "__pr_2", "__pr_3", "__pr_4"]   # nothing deleted/reordered


def test_idempotent(driver):
    pe.prune_events(driver, retention_days=30, now=NOW, session_keys=[SK])
    again = pe.prune_events(driver, retention_days=30, now=NOW, session_keys=[SK])
    assert again["events_tiered"] == 0   # already-tiered events are skipped


def test_dry_run_writes_nothing(driver):
    res = pe.prune_events(driver, retention_days=30, now=NOW, dry_run=True, session_keys=[SK])
    assert res["events_tiered"] == 2 and res["chars_reclaimed"] > 0
    assert not _ev(driver, "__pr_1").get("tiered")   # nothing actually written


def test_blank_prompt_opt_in_removes_prompt(driver):
    pe.prune_events(driver, retention_days=30, now=NOW, blank_prompt=True, session_keys=[SK])
    assert _ev(driver, "__pr_1").get("prompt") is None   # opt-in blanks it


def test_lineage_to_tiered_event_still_resolves(driver):
    # a Memory extracted from a soon-to-be-tiered event keeps its provenance edge
    with driver.session() as s:
        s.run("MATCH (e:Event {event_id:'__pr_1'}) "
              "CREATE (m:Memory {path:'general/__pr_mem.md', content:'body', status:'active'}) "
              "MERGE (m)-[:EXTRACTED_FROM]->(e)")
    pe.prune_events(driver, retention_days=30, now=NOW, session_keys=[SK])
    with driver.session() as s:
        lin = recall.memory_lineage(s, "general/__pr_mem.md")
    assert any(e["event_id"] == "__pr_1" for e in lin["source_events"])   # edge survived tiering


def test_session_keys_scope_isolates_other_sessions(driver):
    # scoping to a different (non-existent) session must tier NOTHING here — proving
    # the pass is session-scoped and never touches the rest of the graph.
    res = pe.prune_events(driver, retention_days=30, now=NOW, session_keys=["test:__nope"])
    assert res["events_tiered"] == 0 and res["sessions"] == 0
    assert not _ev(driver, "__pr_1").get("tiered")   # SK's eligible events untouched
