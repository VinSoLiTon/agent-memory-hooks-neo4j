#!/usr/bin/env python3
"""Phase E (PR-1) — conflict/review workflow. Acceptance tests.

Pure: auto-resolution by authority then recency. DB: flag → pending_review +
:CONTRADICTS and recall hides them (acceptance #3); approve → active (re-injected);
supersede → loser superseded + :SUPERSEDED_BY + :CONTRADICTS cleared (#2, #4).
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import review as rv      # noqa: E402
import recall            # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "profile/__rev"
A, B = f"{MARK}_a.md", f"{MARK}_b.md"


# --- pure auto-resolution ---------------------------------------------------

def test_auto_resolve_authority_then_recency():
    user = {"created_by": "user", "updated_at": "2026-01-01", "path": "a"}
    hosted = {"created_by": "dream_anthropic", "updated_at": "2026-12-01", "path": "b"}
    local = {"created_by": "dream_ollama", "updated_at": "2026-12-31", "path": "c"}
    assert rv.auto_resolve(user, hosted)["path"] == "a"    # user beats hosted regardless of recency
    assert rv.auto_resolve(hosted, local)["path"] == "b"   # hosted beats local
    older = {"created_by": "dream_ollama", "updated_at": "2026-01-01", "path": "x"}
    newer = {"created_by": "dream_ollama", "updated_at": "2026-06-01", "path": "y"}
    assert rv.auto_resolve(older, newer)["path"] == "y"    # tie on authority → newer wins


# --- DB workflow ------------------------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)

    _clean()
    with d.session() as s:
        s.run("CREATE (:Memory {path:$a, content:'memory A body', status:'active', "
              "created_by:'dream_ollama', updated_at:'2026-06-01T00:00:00+00:00'})", a=A)
        s.run("CREATE (:Memory {path:$b, content:'memory B body', status:'active', "
              "created_by:'dream_anthropic', updated_at:'2026-06-02T00:00:00+00:00'})", b=B)
    try:
        yield d
    finally:
        _clean()
        d.close()


def _status(s, path):
    return s.run("MATCH (m:Memory {path:$p}) RETURN m.status AS s", p=path).single()["s"]


def test_flag_routes_to_pending_and_recall_excludes(driver):
    with driver.session() as s:
        rv.flag_contradiction(s, A, B)
        assert _status(s, A) == "pending_review"
        con = s.run("MATCH (:Memory {path:$a})-[:CONTRADICTS]-(:Memory {path:$b}) RETURN count(*) AS n",
                    a=A, b=B).single()["n"]
        assert con >= 1
        paths = {r["path"] for r in recall.fetch_bucket(s, f"{MARK}_", 50)}
        assert A not in paths and B not in paths   # pending memories never inject


def test_approve_reactivates_into_recall(driver):
    with driver.session() as s:
        rv.flag_contradiction(s, A, B)
        rv.approve(s, A)
        assert _status(s, A) == "active"
        assert A in {r["path"] for r in recall.fetch_bucket(s, f"{MARK}_", 50)}


def test_reject_hides_from_recall(driver):
    with driver.session() as s:
        rv.reject(s, A)
        assert _status(s, A) == "rejected"
        assert A not in {r["path"] for r in recall.fetch_bucket(s, f"{MARK}_", 50)}


def test_supersede_marks_loser_links_and_clears_contradiction(driver):
    with driver.session() as s:
        rv.flag_contradiction(s, A, B)
        rv.supersede(s, A, B)   # A wins, B loses
        assert _status(s, A) == "active"
        assert _status(s, B) == "superseded"
        edge = s.run("MATCH (:Memory {path:$l})-[:SUPERSEDED_BY]->(:Memory {path:$w}) RETURN count(*) AS n",
                     l=B, w=A).single()["n"]
        assert edge == 1
        con = s.run("MATCH (:Memory {path:$w})-[:CONTRADICTS]-(:Memory {path:$l}) RETURN count(*) AS n",
                    w=A, l=B).single()["n"]
        assert con == 0   # contradiction resolved
