#!/usr/bin/env python3
"""Item #18 — LLM critique / faithfulness gate (dream/critic.py + dream.py).

The grounding gate (token overlap) passes a fluent hallucination that reuses the
session's vocabulary but inverts a value ("port 5000" → "port 9999"): the bodies
are lexically close, so overlap is high. The opt-in critique pass (DREAM_CRITIQUE=1)
is the semantic check that catches it — it re-reads each NEW candidate against the
bounded transcript and routes an unfaithful one to pending_review.

Conservative direction is the INVERSE of the contradiction judge: the critic is
lenient (True/faithful on any error or ambiguity) so a flaky model can only MISS a
hallucination, never quarantine a good memory. NEW-only: an update to an
existing-active memory is never touched. Live Neo4j; critic injected as a stub.
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
import critic as critic_mod  # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

SK = "test:__cg"
FAITH = "general/__cg_faithful.md"
WRONG = "general/__cg_wrongport.md"
UPD = "general/__cg_update.md"

# Both share vocabulary with the transcript → the grounding gate passes BOTH; only
# the inverted port distinguishes the hallucination. Non-directive bodies so the
# anti-poisoning gate stays out of the way.
FAITHFUL_BODY = "---\ntitle: t\nkind: fact\n---\n\nThe dashboard runs on port 5000 on localhost."
WRONGPORT_BODY = "---\ntitle: t\nkind: fact\n---\n\nThe dashboard runs on port 9999 on localhost."
PRIOR_UPD = "---\ntitle: t\nkind: fact\n---\n\nThe dashboard runs on port 5000 on localhost."


def _events():
    # transcript that supports port 5000 (NOT 9999); rich enough to clear grounding
    text = ("we started the dashboard and it runs on port 5000 on localhost; "
            "the dashboard server bound to 127.0.0.1:5000 as configured")
    return [{"event_id": "__cg_e1", "prompt": text, "tool_input": "", "tool_response": ""},
            {"event_id": "__cg_e2", "prompt": "confirmed the dashboard on port 5000",
             "tool_input": "", "tool_response": ""}]


@pytest.fixture()
def driver(monkeypatch):
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False
    paths = [FAITH, WRONG, UPD]

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path IN $p DETACH DELETE r", p=paths)
            s.run("MATCH (m:Memory) WHERE m.path IN $p DETACH DELETE m", p=paths)
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:$sk}) SET sess.client='test', sess.session_id='__cg'", sk=SK)
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def _status(d, path):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN coalesce(m.status,'active') AS s", p=path).single()
        return r["s"] if r else None


def _write(d, memories, critic_fn):
    dream_mod.write_memories(d, SK, memories, watermark="2026-06-02T00:00:00+00:00",
                             provider="test", model="test", events=_events(),
                             critic_fn=critic_fn)


# stub critic: faithful unless the body inverts the port to 9999
_STUB = lambda content, transcript: "9999" not in content


def test_unfaithful_new_candidate_held_faithful_stays_active(driver, monkeypatch):
    monkeypatch.setenv("DREAM_CRITIQUE", "1")
    _write(driver,
           [{"path": FAITH, "content": FAITHFUL_BODY}, {"path": WRONG, "content": WRONGPORT_BODY}],
           critic_fn=_STUB)
    assert _status(driver, FAITH) == "active"          # faithful → injected
    assert _status(driver, WRONG) == "pending_review"  # hallucinated port → quarantined


def test_gate_off_lets_unfaithful_through(driver, monkeypatch):
    monkeypatch.delenv("DREAM_CRITIQUE", raising=False)   # opt-in; default off
    _write(driver, [{"path": WRONG, "content": WRONGPORT_BODY}], critic_fn=_STUB)
    assert _status(driver, WRONG) == "active"             # critic never ran


def test_critic_error_is_lenient(driver, monkeypatch):
    monkeypatch.setenv("DREAM_CRITIQUE", "1")

    def _boom(content, transcript):
        raise RuntimeError("model down")

    _write(driver, [{"path": WRONG, "content": WRONGPORT_BODY}], critic_fn=_boom)
    assert _status(driver, WRONG) == "active"             # True-on-error → not quarantined


def test_update_to_existing_active_is_exempt(driver, monkeypatch):
    monkeypatch.setenv("DREAM_CRITIQUE", "1")
    with driver.session() as s:
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active', created_by:'user', "
              "updated_at:'2026-06-01T00:00:00+00:00'})", p=UPD, c=PRIOR_UPD)
    # critic would fail this body, but it's an UPDATE to an existing-active path → exempt
    _write(driver, [{"path": UPD, "content": WRONGPORT_BODY}],
           critic_fn=lambda content, transcript: False)
    assert _status(driver, UPD) == "active"               # established memory never quarantined


# --- pure helpers (no DB / no LLM) ------------------------------------------

def test_is_faithful_only_explicit_no_fails():
    assert critic_mod.is_faithful("yes") is True
    assert critic_mod.is_faithful("Yes, every claim is supported") is True
    assert critic_mod.is_faithful("") is True            # empty → lenient
    assert critic_mod.is_faithful("   ") is True
    assert critic_mod.is_faithful("maybe") is True       # ambiguous → lenient
    assert critic_mod.is_faithful("no") is False
    assert critic_mod.is_faithful("No — port is wrong") is False


def test_get_critic_falls_back_to_anthropic_for_unknown_provider():
    assert callable(critic_mod.get_critic("nope", "m"))
    assert callable(critic_mod.get_critic("llamacpp", "m"))
