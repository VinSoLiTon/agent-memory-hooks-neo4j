#!/usr/bin/env python3
"""Phase H4 — backup/restore rehearsal + health row (acceptance #3).

Pure: the health-row logic for never-run / fresh-ok / stale-ok / failed. DB: a
real rehearsal runs the actual cmd_backup→cmd_restore round-trip on a disposable
marker, records a :RehearsalRun, leaves no marker behind, and the health row
then reads it as ok.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "cli"))

import njhook as cli  # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

_NOW = datetime(2026, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


# --- pure: health row -------------------------------------------------------

def test_health_row_never_run():
    status, name, msg = cli._rehearsal_health_row(None, 30, now=_NOW)
    assert status == "warn" and name == "restore rehearsal" and "never run" in msg


def test_health_row_fresh_ok():
    latest = {"ts": (_NOW - timedelta(days=3)).isoformat(), "ok": True, "detail": "x"}
    status, _, msg = cli._rehearsal_health_row(latest, 30, now=_NOW)
    assert status == "ok" and "3d ago" in msg


def test_health_row_stale_ok_warns():
    latest = {"ts": (_NOW - timedelta(days=40)).isoformat(), "ok": True, "detail": "x"}
    status, _, msg = cli._rehearsal_health_row(latest, 30, now=_NOW)
    assert status == "warn" and ">30d" in msg


def test_health_row_failed_is_fail():
    latest = {"ts": (_NOW - timedelta(days=1)).isoformat(), "ok": False, "detail": "boom"}
    status, _, msg = cli._rehearsal_health_row(latest, 30, now=_NOW)
    assert status == "fail" and "boom" in msg


def test_health_row_boundary_equal_is_ok():
    # exactly at the threshold is NOT stale (strict `>`)
    latest = {"ts": (_NOW - timedelta(days=30)).isoformat(), "ok": True, "detail": "x"}
    status, _, _ = cli._rehearsal_health_row(latest, 30, now=_NOW)
    assert status == "ok"


# --- DB: real round-trip rehearsal ------------------------------------------

@pytest.fixture()
def db():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    # remove any rehearsal artifacts so assertions are deterministic
    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH 'general/__rehearsal' DETACH DELETE r")
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__rehearsal' DETACH DELETE m")
            s.run("MATCH (rr:RehearsalRun) DETACH DELETE rr")
    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def test_run_rehearsal_round_trips_and_records(db):
    res = cli.run_rehearsal()
    assert res["ok"] is True, res["detail"]

    with db.session() as s:
        # exactly one RehearsalRun, ok=True
        rr = s.run("MATCH (rr:RehearsalRun) RETURN rr.ts AS ts, rr.ok AS ok, rr.detail AS detail "
                   "ORDER BY rr.ts DESC LIMIT 1").single()
        assert rr and rr["ok"] is True
        # the disposable marker left nothing behind
        leftover = s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__rehearsal' "
                         "RETURN count(m) AS n").single()["n"]
        assert leftover == 0

    # and the health row reads the fresh run as ok
    status, _, _ = cli._rehearsal_health_row(dict(rr), 30)
    assert status == "ok"
