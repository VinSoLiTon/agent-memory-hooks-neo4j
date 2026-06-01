#!/usr/bin/env python3
"""Phase G (PR-1) — shared service + REST API. Acceptance tests.

Proves the parity guarantee: REST `/recall` returns the same hits as calling the
shared `service.recall_context` directly (and as the hook would), and `/events`
captures through the same path as the hook. REST tests skip if Flask is absent.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "api"))

import service       # noqa: E402
import embeddings     # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
TOKEN = "zzgservicetoken"
MPATH = "general/__gsvc.md"


@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = embeddings.is_enabled
    embeddings.is_enabled = lambda: False   # fulltext-only, deterministic, off Ollama

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__gsvc' DETACH DELETE m")

    _clean()
    with d.session() as s:
        s.run("MERGE (m:Memory {path:$p}) SET m.content=$c, m.status='active', m.updated_at='2026-06-01T00:00:00+00:00'",
              p=MPATH, c=f"---\ntitle: t\nkind: general\n---\n\nA note about {TOKEN} and its usage.")
    try:
        yield d
    finally:
        _clean()
        embeddings.is_enabled = saved
        d.close()


def test_service_recall_finds_memory(driver):
    with driver.session() as s:
        hits = service.recall_context(s, TOKEN)
    assert any(h["path"] == MPATH for h in hits)


def test_rest_recall_matches_service_exactly(driver):
    pytest.importorskip("flask")
    import server as api_server
    client = api_server.app.test_client()
    resp = client.post("/recall", json={"prompt": TOKEN})
    assert resp.status_code == 200
    rest_paths = [h["path"] for h in resp.get_json()["hits"]]
    with driver.session() as s:
        direct_paths = [h["path"] for h in service.recall_context(s, TOKEN)]
    assert rest_paths == direct_paths          # parity: REST == shared core
    assert MPATH in rest_paths


def test_rest_recall_requires_prompt(driver):
    pytest.importorskip("flask")
    import server as api_server
    resp = api_server.app.test_client().post("/recall", json={})
    assert resp.status_code == 400


def test_rest_health_ok(driver):
    pytest.importorskip("flask")
    import server as api_server
    resp = api_server.app.test_client().get("/health")
    assert resp.status_code == 200 and resp.get_json()["ok"] is True


def test_rest_events_captures_through_shared_path(driver, tmp_path, monkeypatch):
    pytest.importorskip("flask")
    import server as api_server
    import log_event
    import spool
    monkeypatch.setenv("HOOKS_SPOOL_DIR", str(tmp_path))
    monkeypatch.setattr(log_event, "CAPTURE_MODE", "spool")   # capture to the temp spool, not Neo4j
    client = api_server.app.test_client()
    before = spool.backlog_count()
    resp = client.post("/events", json={
        "client": "claude_code", "session_id": "__gsvc_e",
        "hook_event_name": "UserPromptSubmit", "prompt": "hello api",
    })
    assert resp.status_code == 200 and resp.get_json()["ok"] is True
    assert spool.backlog_count() == before + 1

    # invalid client rejected
    bad = client.post("/events", json={"client": "nope", "prompt": "x"})
    assert bad.status_code == 400
