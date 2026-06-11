#!/usr/bin/env python3
"""Transient-failure back-off: a 0-yield caused by the local model being busy /
unreachable must DEFER the session (leave the watermark, retry next run), not
advance past it (which drops the session forever) and not egress to a fallback.

This matters most in full-offline mode (DREAM_FALLBACK_PROVIDER=none): without the
back-off, every transient llama.cpp blip would silently lose whatever sessions were
in flight. A *clean* empty result (the model ran fine and found nothing) still
advances the watermark, so genuinely-empty sessions aren't re-dreamed forever.

Pure checks for safe_distil/_defer_session + a live-Neo4j run of the real distil
loop (main) with the provider stubbed to raise vs. return [].
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

SK = "test:__backoff"
SID = "__backoff"


# --- pure unit checks -------------------------------------------------------

def test_safe_distil_marks_error_out_on_exception():
    def boom(*a, **k):
        raise ConnectionError("llama.cpp busy")
    err: list = []
    out = dream_mod.safe_distil(boom, "t", "", "m", "sys", "llamacpp", error_out=err)
    assert out == []
    assert err == [True]            # transient error signalled


def test_safe_distil_clean_empty_leaves_error_out_untouched(monkeypatch):
    monkeypatch.setattr(dream_mod, "call_provider", lambda *a, **k: [])
    err: list = []
    out = dream_mod.safe_distil(lambda *a, **k: [], "t", "", "m", "sys", "llamacpp", error_out=err)
    assert out == []
    assert err == []                # a genuine 0-yield is NOT an error


def test_defer_only_on_error_with_no_memories():
    assert dream_mod._defer_session(True, []) is True        # transient + empty → defer
    assert dream_mod._defer_session(False, []) is False      # clean empty → advance
    assert dream_mod._defer_session(True, [{"path": "p"}]) is False   # produced something
    assert dream_mod._defer_session(False, [{"path": "p"}]) is False


# --- live integration: the watermark behaviour ------------------------------

@pytest.fixture()
def seeded(monkeypatch):
    """A session with two events and NO watermark, so the distil loop will pick it up."""
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    monkeypatch.setattr(dream_mod.embeddings, "is_enabled", lambda: False)
    # full-offline: no remote fallback to muddy the watermark behaviour
    monkeypatch.setenv("DREAM_FALLBACK_PROVIDER", "none")
    monkeypatch.setenv("DREAM_USAGE_CAPTURE", "0")
    monkeypatch.setenv("DREAM_CONTRADICTION_CHECK", "0")
    # explicit cap → skip the live llama.cpp /props n_ctx probe in _derived_transcript_cap
    monkeypatch.setenv("DREAM_TRANSCRIPT_MAX_CHARS", "16000")

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH $p DETACH DELETE e", p=SID)
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:$sk}) SET sess.client='test', sess.session_id=$sid",
              sk=SK, sid=SID)
        # two chained events; the loop walks FIRST_EVENT/NEXT, write advances to the last ts
        s.run(
            "MATCH (sess:Session {session_key:$sk}) "
            "CREATE (e1:Event {event_id:$id1, client:'test', timestamp:'2026-06-01T00:00:00+00:00', "
            "        prompt:'do the thing on port 5000', cwd:'C:/tmp', tool_input:'', tool_response:''}), "
            "       (e2:Event {event_id:$id2, client:'test', timestamp:'2026-06-01T00:05:00+00:00', "
            "        prompt:'confirmed', cwd:'C:/tmp', tool_input:'', tool_response:''}) "
            "CREATE (sess)-[:FIRST_EVENT]->(e1)-[:NEXT]->(e2) "
            "CREATE (sess)-[:LATEST_EVENT]->(e2)",
            sk=SK, id1=SID + "_e1", id2=SID + "_e2")
    try:
        yield d
    finally:
        _clean()
        d.close()


def _watermark(d):
    with d.session() as s:
        return s.run("MATCH (s:Session {session_key:$sk}) RETURN s.last_dreamed_at AS w",
                     sk=SK).single()["w"]


def _run_distil(monkeypatch, provider_fn):
    """Drive the real distil loop over just our seeded session, with get_provider
    stubbed to return our fake llamacpp provider."""
    monkeypatch.setattr(dream_mod, "get_provider", lambda name: ("llamacpp", provider_fn))
    monkeypatch.setattr(dream_mod, "default_model", lambda name: "stub-gemma")
    monkeypatch.setattr(sys, "argv", ["dream.py", "--session", SK])
    dream_mod.main()


def test_transient_error_defers_without_advancing_watermark(seeded, monkeypatch):
    d = seeded
    assert _watermark(d) is None

    def boom(*a, **k):
        raise ConnectionError("llama.cpp model swapped out / busy")

    _run_distil(monkeypatch, boom)
    assert _watermark(d) is None, "watermark advanced on a transient error — session would be lost"
    with d.session() as s:
        last = s.run("MATCH (r:NightlyRun) RETURN r.deferred AS df, r.written AS w "
                     "ORDER BY r.ts DESC LIMIT 1").single()
    assert last["df"] == 1 and last["w"] == 0          # recorded as deferred, nothing written


def test_clean_empty_advances_watermark(seeded, monkeypatch):
    d = seeded
    assert _watermark(d) is None

    _run_distil(monkeypatch, lambda *a, **k: [])        # model ran fine, found nothing

    assert _watermark(d) == "2026-06-01T00:05:00+00:00", "genuine 0-yield must advance the watermark"
    with d.session() as s:
        last = s.run("MATCH (r:NightlyRun) RETURN r.deferred AS df ORDER BY r.ts DESC LIMIT 1").single()
    assert last["df"] == 0                              # not deferred
