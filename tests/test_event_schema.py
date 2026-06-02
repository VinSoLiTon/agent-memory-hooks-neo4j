#!/usr/bin/env python3
"""Phase B PR-2 — canonical schema, read-time upcasting, OTel view, DLQ rate.

Pure: the v1→v2 upcaster chain (folds app_id into event_props), idempotency,
no-mutation, the OTel gen_ai.* render, and the spool health-row logic (fail on
DLQ *rate*, not static count). DB: the ingest worker upcasts a v1 record so the
Event ends up with an app_id property; DLQ records carry a timestamp so a rate is
computable.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "cli"))

import event_schema as es   # noqa: E402
import spool                # noqa: E402
import ingest as ingest_mod  # noqa: E402
import njhook as cli        # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


# --- pure: upcasting --------------------------------------------------------

def test_schema_version_is_two():
    assert es.SCHEMA_VERSION == 2


def test_upcast_v1_folds_app_id_from_envelope():
    rec = {"schema_version": 1, "client": "codex", "app_id": "tenant-x",
           "event_props": {"event_id": "e1", "client": "codex"}}
    out = es.upcast(rec)
    assert out["schema_version"] == 2
    assert out["event_props"]["app_id"] == "tenant-x"


def test_upcast_v1_defaults_app_id_to_client():
    rec = {"schema_version": 1, "client": "codex",
           "event_props": {"event_id": "e1", "client": "codex"}}
    assert es.upcast(rec)["event_props"]["app_id"] == "codex"


def test_upcast_v1_defaults_to_unknown_when_nothing():
    rec = {"schema_version": 1, "event_props": {"event_id": "e1"}}
    assert es.upcast(rec)["event_props"]["app_id"] == "unknown"


def test_upcast_missing_version_treated_as_v1():
    rec = {"client": "codex", "app_id": "t", "event_props": {"event_id": "e1"}}
    out = es.upcast(rec)
    assert out["schema_version"] == 2 and out["event_props"]["app_id"] == "t"


def test_upcast_v2_is_idempotent():
    rec = {"schema_version": 2, "client": "codex",
           "event_props": {"event_id": "e1", "app_id": "keep-me"}}
    out = es.upcast(rec)
    assert out["event_props"]["app_id"] == "keep-me" and out["schema_version"] == 2


def test_upcast_does_not_mutate_input():
    rec = {"schema_version": 1, "client": "codex", "app_id": "t",
           "event_props": {"event_id": "e1"}}
    es.upcast(rec)
    assert "app_id" not in rec["event_props"] and rec["schema_version"] == 1


# --- pure: OTel gen_ai view -------------------------------------------------

def test_to_gen_ai_maps_and_passes_through():
    ev = {"event_id": "e1", "timestamp": "2026-06-02T00:00:00+00:00",
          "client": "claude_code", "app_id": "t", "model": "opus",
          "prompt": "hi", "tool_name": None}
    g = es.to_gen_ai(ev)
    assert g["gen_ai.system"] == "claude_code"
    assert g["gen_ai.app.id"] == "t"
    assert g["gen_ai.request.model"] == "opus"
    assert g["gen_ai.prompt"] == "hi"
    assert g["event_id"] == "e1" and g["timestamp"].startswith("2026")
    assert "gen_ai.tool.name" not in g     # None is omitted


def test_gen_ai_field_map_is_closed():
    assert set(es.GEN_AI_FIELD_MAP) == {
        "client", "app_id", "model", "event_name", "tool_name", "tool_input",
        "prompt", "tool_response"}
    assert all(v.startswith("gen_ai.") for v in es.GEN_AI_FIELD_MAP.values())


# --- pure: spool health row -------------------------------------------------

def test_spool_health_row_branches():
    assert cli._spool_health_row(0, 0, 0.0, 5.0, "/dlq")[0] == "ok"
    assert cli._spool_health_row(3, 0, 0.0, 5.0, "/dlq")[0] == "ok"       # backlog only
    assert cli._spool_health_row(0, 2, 1.0, 5.0, "/dlq")[0] == "warn"     # static dead-letters
    assert cli._spool_health_row(0, 9, 9.0, 5.0, "/dlq")[0] == "fail"     # rate over threshold


# --- DB: ingest upcasts; DLQ rate -------------------------------------------

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


@pytest.fixture()
def driver(spool_dir):
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__esc_' DETACH DELETE e")
            s.run("MATCH (s:Session {session_key:'codex:__esctest'}) DETACH DELETE s")

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def test_ingest_upcasts_v1_record_app_id_onto_event(driver):
    # a v1 spool record with app_id ONLY in the envelope (pre-PR-2 shape)
    spool.append({
        "schema_version": 1, "client": "codex", "session_id": "__esctest", "app_id": "tenant-42",
        "event_props": {"event_id": "__esc_1", "event_name": "UserPromptSubmit",
                        "client": "codex", "timestamp": "2026-06-02T00:00:00+00:00", "prompt": "hi"},
    }, day="2026-06-02")
    r = ingest_mod.ingest(driver)
    assert r["processed"] == 1
    with driver.session() as s:
        app_id = s.run("MATCH (e:Event {event_id:'__esc_1'}) RETURN e.app_id AS a").single()["a"]
    assert app_id == "tenant-42"   # upcaster folded it onto the Event node


def test_dlq_records_carry_timestamp_for_rate(spool_dir):
    spool.to_dlq("bad1", "boom")
    spool.to_dlq("bad2", "boom")
    assert spool.dlq_count() == 2
    assert spool.dlq_rate_per_hour() >= 2.0
    # a legacy DLQ line with no ts is counted but ignored by the rate
    (spool_dir / "dlq.jsonl").open("a", encoding="utf-8").write('{"error":"old","raw":"x"}\n')
    assert spool.dlq_count() == 3
    assert spool.dlq_rate_per_hour() >= 2.0   # legacy line not double-counted into the rate
