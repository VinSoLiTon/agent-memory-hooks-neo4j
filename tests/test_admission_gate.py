#!/usr/bin/env python3
"""Phase D PR-2 — A-MAC grounding admission gate. Acceptance tests.

Pure: grounding_score is high for a memory supported by the transcript, ~0 for a
fabricated one, 1.0 with no source. DB: a fabricated NEW memory is routed to
pending_review (hidden from recall) while a grounded one goes active; and an
update to an EXISTING active memory is never gated (no clobber/hide).
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import quality            # noqa: E402
import dream as dream_mod  # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

_SRC = "we discussed rust unsafe blocks and the use-after-free incident at length"
# Grounded content is intentionally NON-directive so this suite isolates the
# grounding gate; the anti-poisoning gate (directive + thin + novel) is exercised
# separately in test_anti_poisoning.py. (A "Never use ..." phrasing would also
# trip H3 here and confuse which gate held the memory.)
_GROUNDED = "---\ntitle: t\nkind: general\n---\n\nThe rust unsafe blocks discussion covered the use-after-free incident."
_FABRICATED = "---\ntitle: t\nkind: general\n---\n\nQuarterly revenue projections for the marketing department roadmap."


# --- pure grounding ---------------------------------------------------------

def test_grounding_score_high_low_and_no_source():
    assert quality.grounding_score(_GROUNDED, _SRC) > 0.5
    assert quality.grounding_score(_FABRICATED, _SRC) < 0.2
    assert quality.grounding_score(_GROUNDED, "") == 1.0   # no source → don't gate


# --- gate in write_memories (live Neo4j) ------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__gate' "
                  "OPTIONAL MATCH (r:MemoryRevision)-[:VERSION_OF]->(m) DETACH DELETE m, r")
            s.run("MATCH (s:Session {session_key:'claude_code:__gate'}) DETACH DELETE s")

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:'claude_code:__gate'}) SET sess.client='claude_code', sess.session_id='__gate'")
        s.run("CREATE (:Memory {path:'general/__gate_existing.md', content:'existing active body', "
              "status:'active', updated_at:'2026-06-01T00:00:00+00:00'})")
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def _status(d, path):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.status AS s", p=path).single()
        return r["s"] if r else None


def test_gate_routes_fabricated_new_memory_to_pending(driver):
    events = [{"event_id": "__gate_e1", "prompt": _SRC, "tool_input": "", "tool_response": ""}]
    dream_mod.write_memories(
        driver, "claude_code:__gate",
        [{"path": "general/__gate_grounded.md", "content": _GROUNDED},
         {"path": "general/__gate_fab.md", "content": _FABRICATED}],
        watermark="2026-06-01T00:00:00+00:00", project=None, provider="test", model="test", events=events,
    )
    assert _status(driver, "general/__gate_grounded.md") == "active"
    assert _status(driver, "general/__gate_fab.md") == "pending_review"


def test_gate_does_not_hide_existing_active_memory(driver):
    # Low-grounding UPDATE to an already-active memory must NOT be gated/hidden.
    events = [{"event_id": "__gate_e2", "prompt": "totally unrelated weather and lunch chatter"}]
    dream_mod.write_memories(
        driver, "claude_code:__gate",
        [{"path": "general/__gate_existing.md",
          "content": "---\ntitle: t\nkind: general\n---\n\nQuarterly revenue projections marketing fiscal department."}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test", events=events,
    )
    assert _status(driver, "general/__gate_existing.md") == "active"
