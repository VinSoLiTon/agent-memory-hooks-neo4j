#!/usr/bin/env python3
"""Item #8 — per-nightly run-ledger node + dream-stats + health signal.

A nightly that distilled 0 memories or fell back on every session looked
identical to a perfect run: the per-write :DreamRun is skipped on zero-yield
sessions, and health only greps exit=0. This adds a per-nightly :NightlyRun node
(written unconditionally), a `njhook dream-stats` surface, and a health row.

Pure tests for the health verdict (no DB); a DB test that the ledger node is
written with the right counts; CLI subparser + dashboard route smoke.
"""
import os
import sys
from datetime import datetime, timezone, timedelta

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))
sys.path.insert(0, os.path.join(ROOT, "cli"))

import njhook            # noqa: E402  (cli)
import dream as dream_mod  # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
NOW = datetime(2026, 6, 1, tzinfo=timezone.utc)
NOW_ISO = NOW.isoformat()


# --- pure: _nightly_health_row ----------------------------------------------

def test_health_none_warns():
    st, name, _ = njhook._nightly_health_row(None, 48)
    assert st == "warn" and name == "nightly run"


def test_health_zero_yield_warns():
    row = {"ts": NOW_ISO, "sessions_seen": 5, "with_yield": 0, "fallback_fired": 0}
    st, _, msg = njhook._nightly_health_row(row, 48, now=NOW)
    assert st == "warn" and "0/5" in msg


def test_health_all_fallback_warns():
    row = {"ts": NOW_ISO, "sessions_seen": 3, "with_yield": 3, "fallback_fired": 3}
    assert njhook._nightly_health_row(row, 48, now=NOW)[0] == "warn"


def test_health_healthy_ok():
    row = {"ts": NOW_ISO, "sessions_seen": 3, "with_yield": 2, "fallback_fired": 1, "written": 4}
    assert njhook._nightly_health_row(row, 48, now=NOW)[0] == "ok"


def test_health_stale_warns():
    old = (NOW - timedelta(hours=72)).isoformat()
    row = {"ts": old, "sessions_seen": 3, "with_yield": 2, "fallback_fired": 1}
    assert njhook._nightly_health_row(row, 48, now=NOW)[0] == "warn"


def test_health_row_is_pure_no_driver_arg():
    import inspect
    params = list(inspect.signature(njhook._nightly_health_row).parameters)
    assert params[:2] == ["latest", "stale_hours"]   # like _rehearsal_health_row, no driver


# --- CLI subparser -----------------------------------------------------------

def test_dream_stats_subcommand_registered():
    ns = njhook.build_parser().parse_args(["dream-stats", "--limit", "5"])
    assert ns.fn is njhook.cmd_dream_stats and ns.limit == 5


# --- DB: the ledger node is written unconditionally --------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:NightlyRun) WHERE r.run_id STARTS WITH '__nl' DETACH DELETE r")

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def test_write_nightly_run_persists_counts(driver):
    stats = {"sessions_seen": 4, "with_yield": 3, "fallback_fired": 1,
             "written": 7, "skipped_sensitive": 1}
    dream_mod._write_nightly_run(driver, "__nl_test", stats, "llamacpp", "gemma", duration_ms=1234)
    with driver.session() as s:
        node = dict(s.run("MATCH (r:NightlyRun {run_id:'__nl_test'}) RETURN r", ).single()["r"])
    assert node["sessions_seen"] == 4 and node["with_yield"] == 3
    assert node["fallback_fired"] == 1 and node["written"] == 7 and node["skipped_sensitive"] == 1
    assert node["duration_ms"] == 1234 and node["provider"] == "llamacpp"
    assert node["ts"]   # stamped


def test_write_nightly_run_swallows_errors():
    # a broken driver must never crash the nightly (observability is best-effort)
    class _Boom:
        def session(self):
            raise RuntimeError("db down")
    dream_mod._write_nightly_run(_Boom(), "__nl_x", {"sessions_seen": 0, "with_yield": 0,
                                 "fallback_fired": 0, "written": 0, "skipped_sensitive": 0},
                                 "p", "m", duration_ms=0)   # no exception = pass


# --- dashboard /nightly route ------------------------------------------------

def test_dashboard_nightly_route_renders():
    sys.path.insert(0, os.path.join(ROOT, "dashboard"))
    import app as dash   # dashboard/app.py
    dash.app.config["TESTING"] = True
    resp = dash.app.test_client().get("/nightly")
    assert resp.status_code == 200
    assert b"Nightly runs" in resp.data
