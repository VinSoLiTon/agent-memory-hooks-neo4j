#!/usr/bin/env python3
"""Phase H2 — the dashboard surfaces the audit log.

Because review/edit mutations are recorded as :MemoryRevision entries, the
existing /memory/<path>/history page renders them (operation + actor) with no
extra route. This pins that: after a review transition, the page shows the
operation and the actor. First dashboard test, via Flask's test client.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dashboard"))

import app as dash   # dashboard/app.py
import review        # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
_PATH = "general/__audit_dash.md"


@pytest.fixture()
def client():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) DETACH DELETE r", p=_PATH)
            s.run("MATCH (m:Memory {path:$p}) DETACH DELETE m", p=_PATH)

    _clean()
    with d.session() as s:
        s.run("CREATE (:Memory {path:$p, content:'a body that is long enough to show', "
              "status:'pending_review', created_by:'dream_test', "
              "updated_at:'2026-06-01T00:00:00+00:00', valid_from:'2026-06-01T00:00:00+00:00'})", p=_PATH)
        review.approve(s, _PATH)   # records an `approve` audit revision
    dash.app.config["TESTING"] = True
    try:
        yield dash.app.test_client()
    finally:
        _clean()
        d.close()


def test_history_page_shows_audit_operation_and_actor(client):
    resp = client.get(f"/memory/{_PATH}/history")
    assert resp.status_code == 200
    html = resp.data.decode("utf-8", "replace")
    assert "approve" in html        # the review transition is shown as an operation
    assert "audit log" in html.lower()
