#!/usr/bin/env python3
"""Item #24 — --max-sessions backlog guard with deterministic oldest-first order.

fetch_events returned ALL qualifying sessions with no LIMIT/ordering, so a backlog
could dream hundreds in one run. The cap bounds a run; oldest-first ordering
(never-dreamed first, then ascending watermark, then sk) makes the cap FIFO so the
next run resumes where this one stopped (the watermark is the implicit cursor).

The ordering is a pure helper (instant unit tests). The cap is exercised against a
live driver but with the chain walk MOCKED, so the test doesn't traverse the real
graph's huge sessions.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as dream_mod   # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "test:__ms_"


# --- pure ordering (instant) ------------------------------------------------

def test_order_targets_never_dreamed_first_then_ascending_wm():
    targets = [
        {"sk": "d", "wm": "2026-01-02T00:00:00+00:00"},
        {"sk": "a", "wm": None},                              # never dreamed → oldest backlog
        {"sk": "c", "wm": "2026-01-01T00:00:00+00:00"},
        {"sk": "b", "wm": None},
    ]
    assert [t["sk"] for t in dream_mod._order_targets(targets)] == ["a", "b", "c", "d"]


def test_order_targets_sk_tiebreak_and_determinism():
    targets = [{"sk": "z", "wm": "2026-01-01T00:00:00+00:00"},
               {"sk": "a", "wm": "2026-01-01T00:00:00+00:00"}]
    once = [t["sk"] for t in dream_mod._order_targets(targets)]
    twice = [t["sk"] for t in dream_mod._order_targets(targets)]
    assert once == ["a", "z"] == twice                       # equal wm → sk tiebreak; stable


# --- cap (live driver, MOCKED chain walk → fast) ----------------------------

@pytest.fixture()
def driver(monkeypatch):
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__ms_' DETACH DELETE e")
            s.run("MATCH (s:Session) WHERE s.session_key STARTS WITH $mk DETACH DELETE s", mk=MARK)

    _clean()
    with d.session() as s:
        # 3 never-dreamed seeds (wm NULL) with a recent LATEST_EVENT so they pass the
        # cheap pre-walk gate; the walk itself is mocked below.
        for suffix in ("a", "b", "c"):
            s.run(
                "CREATE (sess:Session {session_key:$sk, client:'test', session_id:$sid}) "
                "CREATE (e:Event {event_id:$eid, event_name:'UserPromptSubmit', timestamp:'2099-01-01T00:00:00+00:00', prompt:'x'}) "
                "CREATE (sess)-[:FIRST_EVENT]->(e) CREATE (sess)-[:LATEST_EVENT]->(e)",
                sk=f"{MARK}{suffix}", sid=f"__ms_{suffix}", eid=f"__ms_{suffix}_1")
    # mock the per-session walk to O(1) — return one far-future event so every
    # walked session qualifies, without traversing the real graph's long chains.
    monkeypatch.setattr(dream_mod, "_walk_session_events",
                        lambda ses, sk: [{"event_id": f"{sk}#e", "timestamp": "2099-01-01T00:00:00+00:00"}])
    try:
        yield d
    finally:
        _clean()
        d.close()


def _mine(out):
    return [sk for sk, _ in out if sk.startswith(MARK)]


def test_cap_truncates_the_run(driver):
    capped = dream_mod.fetch_events(driver, None, None, max_sessions=2)
    assert len(capped) == 2                               # hard cap regardless of backlog size


def test_uncapped_returns_more_than_the_cap(driver):
    out = dream_mod.fetch_events(driver, None, None)
    assert len(_mine(out)) == 3                           # all 3 seeds qualify when uncapped
    assert len(out) >= 3                                  # ...so cap=2 genuinely truncated


def test_seeds_appear_in_sk_order(driver):
    # all 3 seeds are never-dreamed (wm NULL) → ordered by sk among themselves
    assert _mine(dream_mod.fetch_events(driver, None, None)) == [f"{MARK}a", f"{MARK}b", f"{MARK}c"]
