#!/usr/bin/env python3
"""Phase A — non-destructive history. Acceptance tests.

Deterministic, no LLM: exercises write_memories / recall directly against a live
Neo4j. Verifies the revision-chain model, lifecycle status filtering, the status
index, and the negative invariant that consolidation no longer DETACH DELETEs.

Run: python -m pytest tests/test_phase_a_history.py -q   (needs a live Neo4j)
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as dream_mod        # noqa: E402  (dream/dream.py)
import inject_memory             # noqa: E402  (hooks/inject_memory.py)
import schema                    # noqa: E402  (hooks/schema.py)
import embeddings                # noqa: E402  (hooks/embeddings.py)

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

MARK = "__phasea"                # every test node carries this in its path / key
SK = "test:phasea"               # test session_key
CONSOLIDATE_SRC = os.path.join(ROOT, "dream", "consolidate.py")


def _mem(slug: str, body: str, kind: str = "general") -> dict:
    top = "profile" if kind == "profile" else kind
    return {
        "path": f"{top}/{MARK}_{slug}.md",
        "content": f"---\ntitle: Phase A test {slug}\nkind: {kind}\n---\n\n{body}",
    }


@pytest.fixture()
def driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    # Embeddings off so write_memories never reaches Ollama — keeps tests hermetic.
    saved = embeddings.is_enabled
    embeddings.is_enabled = lambda: False
    _cleanup(d)
    with d.session() as s:
        s.run("MERGE (s:Session {session_key: $sk}) SET s.client='test', s.session_id='phasea'", sk=SK)
    try:
        yield d
    finally:
        _cleanup(d)
        embeddings.is_enabled = saved
        d.close()


def _cleanup(d):
    with d.session() as s:
        s.run(
            "MATCH (m:Memory) WHERE m.path CONTAINS $mark "
            "OPTIONAL MATCH (rev:MemoryRevision)-[:VERSION_OF]->(m) "
            "DETACH DELETE m, rev",
            mark=MARK,
        )
        s.run("MATCH (dr:DreamRun) WHERE dr.run_id STARTS WITH $sk DETACH DELETE dr", sk=SK)
        s.run("MATCH (s:Session {session_key: $sk}) DETACH DELETE s", sk=SK)


def _one(d, cypher, **params):
    with d.session() as s:
        r = s.run(cypher, **params).single()
        return r[0] if r else None


# --- A1: schema -------------------------------------------------------------

def test_status_index_exists(driver):
    with driver.session() as s:
        s.execute_write(schema.create_constraints_and_indexes)
    n = _one(driver, "SHOW INDEXES YIELD name WHERE name = 'memory_status' RETURN count(*)")
    assert n == 1


# --- A3: non-destructive dream write (revision-chain) -----------------------

def test_write_creates_active_memory_no_revision_on_first_write(driver):
    m = _mem("rev", "first body, long enough to pass the quality gate.")
    dream_mod.write_memories(driver, SK, [m], watermark="2026-01-01T00:00:00",
                             provider="test", model="test")
    status = _one(driver, "MATCH (m:Memory {path:$p}) RETURN m.status", p=m["path"])
    assert status == "active"
    created_by = _one(driver, "MATCH (m:Memory {path:$p}) RETURN m.created_by", p=m["path"])
    assert created_by == "dream_test"
    assert _one(driver, "MATCH (m:Memory {path:$p}) RETURN m.ingested_at IS NOT NULL", p=m["path"])
    assert _one(driver, "MATCH (m:Memory {path:$p}) RETURN m.valid_from IS NOT NULL", p=m["path"])
    revs = _one(driver, "MATCH (:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) RETURN count(*)", p=m["path"])
    assert revs == 0
    wrote = _one(driver, "MATCH (:DreamRun)-[:WROTE]->(m:Memory {path:$p}) RETURN count(*)", p=m["path"])
    assert wrote >= 1


def test_changed_content_snapshots_revision_and_updates_in_place(driver):
    p = _mem("rev", "x")["path"]
    v1 = _mem("rev", "version one body, definitely long enough for the gate.")
    v2 = _mem("rev", "version TWO body, also long enough for the quality gate.")
    dream_mod.write_memories(driver, SK, [v1], watermark="2026-01-01T00:00:00", provider="test", model="test")
    dream_mod.write_memories(driver, SK, [v2], watermark="2026-01-02T00:00:00", provider="test", model="test")

    # path is UNIQUE — still exactly one Memory node at the path (no duplicate-path versions)
    assert _one(driver, "MATCH (m:Memory {path:$p}) RETURN count(*)", p=p) == 1
    assert _one(driver, "MATCH (m:Memory {path:$p}) RETURN m.content", p=p) == v2["content"]

    revs = _one(driver, "MATCH (:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) RETURN count(*)", p=p)
    assert revs == 1
    snap = _one(driver,
                "MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) RETURN r.content_snapshot", p=p)
    assert snap == v1["content"]


def test_identical_content_writes_no_new_revision(driver):
    p = _mem("rev", "x")["path"]
    v1 = _mem("rev", "stable body that does not change between dream runs at all.")
    dream_mod.write_memories(driver, SK, [v1], watermark="2026-01-01T00:00:00", provider="test", model="test")
    dream_mod.write_memories(driver, SK, [v1], watermark="2026-01-02T00:00:00", provider="test", model="test")
    revs = _one(driver, "MATCH (:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) RETURN count(*)", p=p)
    assert revs == 0


# --- A4: recall filters superseded / non-active -----------------------------

def test_fetch_bucket_excludes_superseded(driver):
    active = _mem("active", "active profile memory body for the bucket test.", kind="profile")
    gone = _mem("gone", "superseded profile memory body for the bucket test.", kind="profile")
    dream_mod.write_memories(driver, SK, [active, gone], watermark="2026-01-01T00:00:00", provider="test", model="test")
    # mark one superseded directly
    with driver.session() as s:
        s.run("MATCH (m:Memory {path:$p}) SET m.status='superseded'", p=gone["path"])

    with driver.session() as s:
        rows = inject_memory._fetch_bucket(s, f"profile/{MARK}_", 50)
    paths = {r["path"] for r in rows}
    assert active["path"] in paths
    assert gone["path"] not in paths


def test_fulltext_search_excludes_superseded(driver):
    token = "zzqphaseauniquetoken"
    active = _mem("ft_a", f"active body mentioning {token} once.")
    gone = _mem("ft_b", f"superseded body mentioning {token} once.")
    dream_mod.write_memories(driver, SK, [active, gone], watermark="2026-01-01T00:00:00", provider="test", model="test")
    with driver.session() as s:
        s.run("MATCH (m:Memory {path:$p}) SET m.status='superseded'", p=gone["path"])

    with driver.session() as s:
        hits = inject_memory._fulltext_search(s, token, limit=10)
    paths = {h["path"] for h in hits}
    assert active["path"] in paths
    assert gone["path"] not in paths


# --- A2: negative invariant — consolidation is non-destructive --------------

def test_consolidate_source_has_no_detach_delete():
    src = open(CONSOLIDATE_SRC, encoding="utf-8").read()
    assert "DETACH DELETE old" not in src, "consolidate must supersede, not delete, sources"
    assert "SUPERSEDED_BY" in src, "consolidate must link superseded sources to the merged memory"
