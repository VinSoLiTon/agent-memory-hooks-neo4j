#!/usr/bin/env python3
"""Item #14 — recall-effectiveness worklists (read-only cohorts).

access_count was bumped on every injection, conflating deterministically-injected
with actually-useful, and there was no signal for never-surfaced dead-weight
memories — the input a pruning decision needs. Two read-only cohorts:
  - never_injected: active, non-profile, access_count=0, aged (Cypher filter → DB);
  - effectively_dead: used (access_count>0) but recency below a floor — decay
    computed in Python via the ONE recency_factor (no second decay impl).
The injection_count schema change was dropped (no usefulness signal to record).
"""
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import recall  # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "__co"


# --- effectively_dead: Python floor logic (fake session, no DB) -------------

class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    def run(self, *a, **k):
        return self._rows


def test_effectively_dead_uses_recency_floor():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"path": "project/old.md", "last_accessed_at": "2025-01-01T00:00:00+00:00",
         "updated_at": None, "ingested_at": None, "access_count": 3},     # ~1.4y old → tiny recency
        {"path": "project/fresh.md", "last_accessed_at": "2026-06-01T00:00:00+00:00",
         "updated_at": None, "ingested_at": None, "access_count": 2},     # fresh → ~1.0
    ]
    out = recall.effectively_dead(_FakeSession(rows), floor=0.5, now=now)
    paths = [r["path"] for r in out]
    assert "project/old.md" in paths and "project/fresh.md" not in paths
    assert all(r["recency"] < 0.5 for r in out)                          # floor enforced
    assert out[0]["recency"] == recall.recency_factor(rows[0], now)      # reuses the one ranker


def test_effectively_dead_sorts_ascending_by_recency():
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    rows = [
        {"path": "project/a.md", "last_accessed_at": "2025-06-01T00:00:00+00:00", "access_count": 1},
        {"path": "project/b.md", "last_accessed_at": "2025-01-01T00:00:00+00:00", "access_count": 1},
    ]
    out = recall.effectively_dead(_FakeSession(rows), floor=0.9, now=now)
    assert [r["path"] for r in out] == ["project/b.md", "project/a.md"]   # deadest first


# --- never_injected: Cypher WHERE (DB) --------------------------------------

@pytest.fixture()
def cohorts_driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path CONTAINS $mk DETACH DELETE m", mk=MARK)

    _clean()
    # "__co_recent" must stay inside the min_age_days=30 window on EVERY run —
    # a hardcoded date here was a time bomb (it aged past the cutoff ~40 days
    # after it was written and the exclusion assert started failing).
    recent = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with d.session() as s:
        s.run("""
            CREATE (:Memory {path:'project/__co_old.md',    content:'b', status:'active', access_count:0, ingested_at:'2025-01-01T00:00:00+00:00'})
            CREATE (:Memory {path:'profile/__co_p.md',       content:'b', status:'active', access_count:0, ingested_at:'2025-01-01T00:00:00+00:00'})
            CREATE (:Memory {path:'project/__co_recent.md',  content:'b', status:'active', access_count:0, ingested_at:$recent})
            CREATE (:Memory {path:'project/__co_used.md',    content:'b', status:'active', access_count:5, ingested_at:'2025-01-01T00:00:00+00:00'})
        """, recent=recent)
    try:
        yield d
    finally:
        _clean()
        d.close()


def test_never_injected_excludes_profile_used_and_recent(cohorts_driver):
    with cohorts_driver.session() as s:
        paths = {m["path"] for m in recall.never_injected(s, min_age_days=30, limit=500)}
    assert "project/__co_old.md" in paths        # aged, access=0, non-profile → in
    assert "profile/__co_p.md" not in paths       # profile exempt
    assert "project/__co_recent.md" not in paths  # too recent
    assert "project/__co_used.md" not in paths    # access_count > 0
