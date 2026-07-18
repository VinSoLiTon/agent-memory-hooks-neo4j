#!/usr/bin/env python3
"""Item #10 — anti-poisoning gate on UPDATES of existing-active memories.

Both admission gates exempted updates to existing-active paths, opening an
adversarial-update bypass: a prompt-injected session could overwrite a trusted
memory's body. The poison gate now covers updates NON-DESTRUCTIVELY — a
suspicious update (directive + thin session + novel) is HELD: the established
body stays active and the rejected incoming body is recorded as an `update_held`
revision (rejected_content + reason) for audit/recovery. Default-ON
(DREAM_GATE_UPDATES=1). Live Neo4j.
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

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
SK = "test:__ug"
PATH = "general/__ug_x.md"
PRIOR = "Original safe body that the user trusts."
# directive ("always") + distinctive tokens + thin session. The body is almost
# entirely nonsense tokens ON PURPOSE: novelty_score is computed against the
# LIVE corpus sharing the path prefix, and an earlier wording ("... never run
# ... safety checks") decayed below POISON_NOVELTY_MIN=0.6 once the general/
# corpus grew to contain those common words (novelty 0.5 measured 2026-07-18,
# gate silently stopped firing). Nonsense tokens keep novelty ~1.0 against any
# future corpus, so the test pins the GATE, not the corpus of the day.
SUSPICIOUS = "---\ntitle: t\nkind: constraint\n---\n\nAlways ZZZNOVELXYZ QQRANDOMTOKEN VVFAKEGUARD WWBOGUSSTEP XXNONCEWORD."
BENIGN = "---\ntitle: t\nkind: fact\n---\n\nOriginal safe body that the user trusts, lightly expanded."


@pytest.fixture()
def driver(monkeypatch):
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path=$p DETACH DELETE r", p=PATH)
            s.run("MATCH (m:Memory) WHERE m.path=$p DETACH DELETE m", p=PATH)
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:$sk}) SET sess.client='test', sess.session_id='__ug'", sk=SK)
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active', created_by:'user', "
              "updated_at:'2026-06-01T00:00:00+00:00'})", p=PATH, c=PRIOR)
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def _thin_events(text):
    # 2 events (< POISON_MIN_EVENTS=5) carrying the body text, so it's grounded but thin
    return [{"event_id": "__ug_e1", "prompt": text, "tool_input": "", "tool_response": ""},
            {"event_id": "__ug_e2", "prompt": "ok", "tool_input": "", "tool_response": ""}]


def _content(d):
    with d.session() as s:
        return s.run("MATCH (m:Memory {path:$p}) RETURN m.content AS c", p=PATH).single()["c"]


def _held_revisions(d):
    with d.session() as s:
        return [dict(r) for r in s.run(
            "MATCH (rev:MemoryRevision {operation:'update_held'})-[:VERSION_OF]->(m:Memory {path:$p}) "
            "RETURN rev.rejected_content AS rc, rev.reason AS reason", p=PATH)]


def _write(d, candidate, monkeypatch, gate="1"):
    monkeypatch.setenv("DREAM_GATE_UPDATES", gate)
    dream_mod.write_memories(d, SK, [{"path": PATH, "content": candidate}],
                             watermark="2026-06-02T00:00:00+00:00",
                             provider="test", model="test", events=_thin_events(candidate))


def test_suspicious_update_is_held_prior_body_kept(driver, monkeypatch):
    _write(driver, SUSPICIOUS, monkeypatch)
    assert _content(driver) == PRIOR                 # established body NOT overwritten
    held = _held_revisions(driver)
    assert held and "ZZZNOVELXYZ" in held[0]["rc"]   # rejected body recorded...
    assert "anti-poisoning" in held[0]["reason"]     # ...with a reason


def test_benign_update_overwrites_normally(driver, monkeypatch):
    _write(driver, BENIGN, monkeypatch)              # not directive → not held
    assert "lightly expanded" in _content(driver)    # the update was written
    assert _held_revisions(driver) == []


def test_gate_off_lets_suspicious_update_through(driver, monkeypatch):
    _write(driver, SUSPICIOUS, monkeypatch, gate="0")   # rollback escape hatch
    assert "ZZZNOVELXYZ" in _content(driver)            # overwritten (old behaviour)
    assert _held_revisions(driver) == []
