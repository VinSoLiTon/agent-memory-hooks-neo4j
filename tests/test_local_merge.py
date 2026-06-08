#!/usr/bin/env python3
"""Item #16 — local same-path body merge (default-OFF, local providers only).

Local models see existing memories as paths-only, so a same-path UPDATE clobbers
the accumulated body (recoverable via MemoryRevision, but a fidelity regression).
When DREAM_LOCAL_MERGE=1 and the provider is local, write_memories makes ONE LLM
merge call (prior + new) and replaces the candidate body BEFORE the write — so the
MemoryRevision still snapshots the prior. Pinned: off-by-default clobber; on →
merged body written + prior snapshotted; remote provider skipped; failure /
invalid-merge fall back to the raw body; no collision = no-op. Live Neo4j.
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
SK = "test:__lm"
PATH = "general/__lm_x.md"
PRIOR = "---\ntitle: t\nkind: fact\n---\n\nPRIOR body, long enough for the quality gate."
NEW = "---\ntitle: t\nkind: fact\n---\n\nNEW body, also long enough for the gate."
MERGED = "---\ntitle: t\nkind: fact\n---\n\nMERGED prior and new, long enough for the gate."


@pytest.fixture()
def driver(monkeypatch):
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False   # hermetic — no embed server

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path=$p DETACH DELETE r", p=PATH)
            s.run("MATCH (m:Memory) WHERE m.path=$p DETACH DELETE m", p=PATH)
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:$sk}) SET sess.client='test', sess.session_id='__lm'", sk=SK)
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active', created_by:'user', "
              "updated_at:'2026-06-01T00:00:00+00:00'})", p=PATH, c=PRIOR)
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def _fake_merge(**kw):
    return [{"path": PATH, "content": MERGED}]


def _content(d):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.content AS c", p=PATH).single()
        return r["c"] if r else None


def _revision_snapshots(d):
    with d.session() as s:
        return [r["c"] for r in s.run(
            "MATCH (rev:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) "
            "RETURN rev.content_snapshot AS c", p=PATH)]


def _write(d, provider, provider_fn, monkeypatch, merge="1"):
    monkeypatch.setenv("DREAM_LOCAL_MERGE", merge)
    dream_mod.write_memories(d, SK, [{"path": PATH, "content": NEW}],
                             watermark="2026-06-02T00:00:00+00:00",
                             provider=provider, model="m", provider_fn=provider_fn)


def test_off_by_default_clobbers(driver, monkeypatch):
    monkeypatch.delenv("DREAM_LOCAL_MERGE", raising=False)   # gate off
    _write(driver, "llamacpp", _fake_merge, monkeypatch, merge="0")
    assert "NEW body" in _content(driver) and "MERGED" not in _content(driver)   # raw clobber
    assert PRIOR in _revision_snapshots(driver)                                   # prior still snapshotted


def test_local_merge_merges_same_path_collision(driver, monkeypatch):
    _write(driver, "llamacpp", _fake_merge, monkeypatch)
    assert _content(driver) == MERGED                       # merged body written...
    assert PRIOR in _revision_snapshots(driver)             # ...and the prior body snapshotted (non-destructive)


def test_remote_provider_is_skipped(driver, monkeypatch):
    _write(driver, "anthropic", _fake_merge, monkeypatch)   # gate on but provider is remote
    assert "NEW body" in _content(driver) and "MERGED" not in _content(driver)


def test_merge_failure_falls_back_to_raw(driver, monkeypatch):
    def boom(**kw):
        raise RuntimeError("local model down")
    _write(driver, "llamacpp", boom, monkeypatch)
    assert "NEW body" in _content(driver)                   # crash isolated → raw new body kept


def test_invalid_merged_body_falls_back_to_raw(driver, monkeypatch):
    def bad(**kw):
        return [{"path": PATH, "content": "no frontmatter at all"}]   # would fail the quality gate
    _write(driver, "llamacpp", bad, monkeypatch)
    assert "NEW body" in _content(driver)                   # invalid merge rejected → raw new body kept


def test_no_collision_is_noop(driver, monkeypatch):
    # a candidate at a NEW path (no existing active memory) is never merged
    monkeypatch.setenv("DREAM_LOCAL_MERGE", "1")
    n = dream_mod._local_merge_pass(driver, [{"path": "general/__lm_new.md", "content": NEW}],
                                    "llamacpp", "m", _fake_merge)
    assert n == 0