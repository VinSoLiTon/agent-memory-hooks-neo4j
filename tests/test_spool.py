#!/usr/bin/env python3
"""Phase B — durable capture (spool + ingest). Acceptance tests.

Pure spool tests use a temp HOOKS_SPOOL_DIR (no Neo4j). The ingest tests use a
live Neo4j to prove idempotent replay (the Event.event_id UNIQUE constraint is the
inbox) and dead-lettering of malformed records.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import spool          # noqa: E402
import ingest as ingest_mod  # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


def _rec(eid: str, sid: str = "__spooltest", prompt: str = "hello spool"):
    return {
        "schema_version": 1, "client": "claude_code", "session_id": sid, "app_id": "claude_code",
        "event_props": {
            "event_id": eid, "event_name": "UserPromptSubmit", "client": "claude_code",
            "timestamp": "2026-06-01T00:00:00+00:00", "prompt": prompt,
        },
    }


@pytest.fixture()
def spool_dir(tmp_path):
    saved = os.environ.get("HOOKS_SPOOL_DIR")
    os.environ["HOOKS_SPOOL_DIR"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOOKS_SPOOL_DIR", None)
        else:
            os.environ["HOOKS_SPOOL_DIR"] = saved


# --- pure spool -------------------------------------------------------------

def test_append_and_iter_in_order(spool_dir):
    spool.append(_rec("__spool_a"), day="2026-06-01")
    spool.append(_rec("__spool_b"), day="2026-06-01")
    eids = [r["event_props"]["event_id"] for _, _, r, _ in spool.iter_records()]
    assert eids == ["__spool_a", "__spool_b"]
    assert spool.backlog_count() == 2


def test_iter_marks_malformed_as_none(spool_dir):
    (spool_dir / "events-2026-06-01.jsonl").write_text('{"good":1}\nnot json\n', encoding="utf-8")
    parsed = [r for _, _, r, _ in spool.iter_records()]
    assert parsed[0] == {"good": 1}
    assert parsed[1] is None


def test_dlq_count(spool_dir):
    assert spool.dlq_count() == 0
    spool.to_dlq("bad raw", "boom")
    assert spool.dlq_count() == 1


# --- ingest (live Neo4j) ----------------------------------------------------

@pytest.fixture()
def driver(spool_dir):
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__spool_' DETACH DELETE e")
            s.run("MATCH (s:Session {session_key:'claude_code:__spooltest'}) DETACH DELETE s")

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def _event_count(d, eid):
    with d.session() as s:
        return s.run("MATCH (e:Event {event_id:$id}) RETURN count(e) AS n", id=eid).single()["n"]


def test_ingest_writes_then_drains(driver):
    spool.append(_rec("__spool_idem1"), day="2026-06-01")
    r = ingest_mod.ingest(driver)
    assert r["processed"] == 1
    assert _event_count(driver, "__spool_idem1") == 1
    assert spool.backlog_count() == 0  # fully-drained file is removed


def test_ingest_replay_is_idempotent(driver):
    spool.append(_rec("__spool_idem1"), day="2026-06-01")
    ingest_mod.ingest(driver)
    # replay the SAME event — the Event already exists, so it's skipped, no duplicate.
    spool.append(_rec("__spool_idem1"), day="2026-06-01")
    r2 = ingest_mod.ingest(driver)
    assert r2["processed"] == 0 and r2["skipped"] == 1
    assert _event_count(driver, "__spool_idem1") == 1


def test_ingest_dead_letters_malformed(driver, spool_dir):
    (spool_dir / "events-2026-06-01.jsonl").write_text("not json at all\n", encoding="utf-8")
    r = ingest_mod.ingest(driver)
    assert r["processed"] == 0 and r["dlq"] == 1
    assert spool.dlq_count() == 1
