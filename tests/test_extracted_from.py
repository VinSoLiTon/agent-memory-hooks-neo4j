#!/usr/bin/env python3
"""Phase D (PR-1) — claim-level provenance via :EXTRACTED_FROM.

Pure tests cover the heuristic attribution; the DB test proves write_memories
links a memory to the most-overlapping source event (and not to an unrelated one).
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

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

_RUST_MEM = ("---\ntitle: Rust safety\nkind: project\n---\n\n"
             "Never use unsafe blocks in Rust; rationale was the use-after-free incident.")


# --- pure attribution -------------------------------------------------------

def test_attribute_events_picks_the_overlapping_event():
    events = [
        {"event_id": "e_rust", "prompt": "we must never use unsafe blocks in rust after the use-after-free incident"},
        {"event_id": "e_other", "prompt": "the weather today is pleasant and lunch was good"},
    ]
    assert dream_mod.attribute_events(_RUST_MEM, events, k=1, min_overlap=2) == ["e_rust"]


def test_attribute_events_empty_and_no_overlap():
    assert dream_mod.attribute_events("", [{"event_id": "x", "prompt": "hi there"}], 3, 1) == []
    none = dream_mod.attribute_events(
        "---\ntitle: t\nkind: fact\n---\n\napples oranges bananas",
        [{"event_id": "x", "prompt": "completely different vocabulary entirely"}], 3, 2,
    )
    assert none == []


# --- write_memories creates the edges (live Neo4j) --------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False  # keep test off Ollama

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__xf_' DETACH DELETE e")
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'project/__xf' "
                  "OPTIONAL MATCH (r:MemoryRevision)-[:VERSION_OF]->(m) DETACH DELETE m, r")
            s.run("MATCH (s:Session {session_key:'claude_code:__xf'}) DETACH DELETE s")

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:'claude_code:__xf'}) SET sess.client='claude_code', sess.session_id='__xf'")
        s.run("CREATE (:Event {event_id:'__xf_e_rust', prompt:$p})",
              p="we must never use unsafe blocks in rust after the use-after-free incident")
        s.run("CREATE (:Event {event_id:'__xf_e_other', prompt:'the weather today is pleasant and lunch was good'})")
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def test_write_memories_links_extracted_from_to_overlapping_event(driver):
    events = [
        {"event_id": "__xf_e_rust", "prompt": "we must never use unsafe blocks in rust after the use-after-free incident"},
        {"event_id": "__xf_e_other", "prompt": "the weather today is pleasant and lunch was good"},
    ]
    dream_mod.write_memories(
        driver, "claude_code:__xf", [{"path": "project/__xf.md", "content": _RUST_MEM}],
        watermark="2026-06-01T00:00:00+00:00", project="xf", provider="test", model="test", events=events,
    )
    with driver.session() as s:
        rust = s.run("MATCH (:Memory {path:'project/__xf.md'})-[:EXTRACTED_FROM]->(:Event {event_id:'__xf_e_rust'}) "
                     "RETURN count(*) AS n").single()["n"]
        other = s.run("MATCH (:Memory {path:'project/__xf.md'})-[:EXTRACTED_FROM]->(:Event {event_id:'__xf_e_other'}) "
                      "RETURN count(*) AS n").single()["n"]
    assert rust == 1
    assert other == 0  # unrelated event not linked
