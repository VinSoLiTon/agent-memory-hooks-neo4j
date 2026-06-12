#!/usr/bin/env python3
"""DREAM_MIN_EVENTS gate: trivially-short sessions skip the LLM entirely.

~89% of captured sessions are a lone SessionStart / single prompt with no
cross-event pattern to distill. Sending them to the model is a wasted call that
always yields nothing (and, pre-offline, triggered the remote fallback). The gate
skips any session with fewer than DREAM_MIN_EVENTS events (default 2) WITHOUT an
LLM call, but still advances the watermark so it's retired (re-dreamed only if it
later grows past the threshold). DREAM_MIN_EVENTS=1 disables it.

Live Neo4j, driving the real main() loop with get_provider stubbed so we can
assert whether the provider was actually invoked.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as dream_mod  # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

SK = "test:__minev"
SID = "__minev"
T1 = "2026-06-01T00:00:00+00:00"
T2 = "2026-06-01T00:05:00+00:00"


@pytest.fixture()
def driver(monkeypatch):
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    monkeypatch.setattr(dream_mod.embeddings, "is_enabled", lambda: False)
    monkeypatch.setenv("DREAM_FALLBACK_PROVIDER", "none")
    monkeypatch.setenv("DREAM_USAGE_CAPTURE", "0")
    monkeypatch.setenv("DREAM_CONTRADICTION_CHECK", "0")
    monkeypatch.setenv("DREAM_TRANSCRIPT_MAX_CHARS", "16000")
    monkeypatch.delenv("DREAM_MIN_EVENTS", raising=False)  # default 2 unless a test sets it

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH $p DETACH DELETE e", p=SID)
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)
            # main() writes a real :NightlyRun (model from default_model → 'stub'); don't
            # leave it polluting the live ledger / dashboard.
            s.run("MATCH (r:NightlyRun) WHERE coalesce(r.model,'') STARTS WITH 'stub' DETACH DELETE r")

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def _seed(d, n_events):
    """Seed SK with n_events chained events, no watermark."""
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:$sk}) SET sess.client='test', sess.session_id=$sid",
              sk=SK, sid=SID)
        if n_events == 1:
            s.run("MATCH (sess:Session {session_key:$sk}) "
                  "CREATE (e1:Event {event_id:$id1, client:'test', timestamp:$t1, "
                  "        prompt:'just one prompt', cwd:'C:/tmp', tool_input:'', tool_response:''}) "
                  "CREATE (sess)-[:FIRST_EVENT]->(e1) CREATE (sess)-[:LATEST_EVENT]->(e1)",
                  sk=SK, id1=SID + "_e1", t1=T1)
        else:
            s.run("MATCH (sess:Session {session_key:$sk}) "
                  "CREATE (e1:Event {event_id:$id1, client:'test', timestamp:$t1, "
                  "        prompt:'first prompt about port 5000', cwd:'C:/tmp', tool_input:'', tool_response:''}), "
                  "       (e2:Event {event_id:$id2, client:'test', timestamp:$t2, "
                  "        prompt:'second, follow-up', cwd:'C:/tmp', tool_input:'', tool_response:''}) "
                  "CREATE (sess)-[:FIRST_EVENT]->(e1)-[:NEXT]->(e2) CREATE (sess)-[:LATEST_EVENT]->(e2)",
                  sk=SK, id1=SID + "_e1", id2=SID + "_e2", t1=T1, t2=T2)


def _watermark(d):
    with d.session() as s:
        return s.run("MATCH (s:Session {session_key:$sk}) RETURN s.last_dreamed_at AS w",
                     sk=SK).single()["w"]


def _run(monkeypatch, dry=False):
    """Drive main() over our seeded session; return a list recording provider calls."""
    calls: list = []

    def stub(*a, **k):
        calls.append(1)
        return []

    monkeypatch.setattr(dream_mod, "get_provider", lambda name: ("llamacpp", stub))
    monkeypatch.setattr(dream_mod, "default_model", lambda name: "stub")
    argv = ["dream.py", "--session", SK] + (["--dry-run"] if dry else [])
    monkeypatch.setattr(sys, "argv", argv)
    dream_mod.main()
    return calls


def test_one_event_session_skipped_no_llm_call(driver, monkeypatch):
    _seed(driver, 1)
    calls = _run(monkeypatch)
    assert calls == [], "provider was called on a 1-event session — should have been skipped"
    assert _watermark(driver) == T1, "skipped session's watermark must advance (retire it)"
    with driver.session() as s:
        last = s.run("MATCH (r:NightlyRun) RETURN r.skipped_short AS short, r.written AS w "
                     "ORDER BY r.ts DESC LIMIT 1").single()
    assert last["short"] == 1 and last["w"] == 0


def test_two_event_session_is_processed(driver, monkeypatch):
    _seed(driver, 2)
    calls = _run(monkeypatch)
    assert calls == [1], "2-event session should reach the provider (>= DREAM_MIN_EVENTS=2)"
    assert _watermark(driver) == T2
    with driver.session() as s:
        short = s.run("MATCH (r:NightlyRun) RETURN r.skipped_short AS short "
                      "ORDER BY r.ts DESC LIMIT 1").single()["short"]
    assert short == 0


def test_min_events_1_disables_the_gate(driver, monkeypatch):
    monkeypatch.setenv("DREAM_MIN_EVENTS", "1")
    _seed(driver, 1)
    calls = _run(monkeypatch)
    assert calls == [1], "DREAM_MIN_EVENTS=1 must process even a 1-event session"


def test_dry_run_skip_does_not_advance_watermark(driver, monkeypatch):
    _seed(driver, 1)
    calls = _run(monkeypatch, dry=True)
    assert calls == []                       # still skipped (no LLM)
    assert _watermark(driver) is None        # but dry-run writes nothing
