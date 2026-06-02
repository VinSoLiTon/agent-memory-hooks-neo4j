#!/usr/bin/env python3
"""Phase D1 — typed `kind` vocabulary (acceptance #1 + #4).

Pure: the closed vocabulary round-trips frozenset ↔ JSON-schema enum ↔ quality
validator; normalize maps legacy→semantic; the quality gate accepts the 15
semantic types AND the 4 legacy labels (migration window) and rejects anything
else. DB: the dream write stamps a queryable `m.kind` node property (Cypher leg
of the round-trip), normalizing a legacy frontmatter label to its semantic type.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import memory_types as mt   # noqa: E402
import quality              # noqa: E402
import prompts              # noqa: E402
import dream as dream_mod   # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__dtype"


# --- pure: vocabulary -------------------------------------------------------

def test_vocabulary_is_closed_and_all_active():
    assert mt.MEMORY_KINDS == {
        "preference", "projectrule", "decision", "procedure", "fact",
        "constraint", "toolpattern", "incident", "openquestion",
        "commitment", "goal", "context", "learning", "observation", "artifact"}
    assert len(mt.MEMORY_KINDS) == 15
    assert mt.LEGACY_KINDS == {"profile", "tool", "project", "general"}
    assert mt.VALID_KINDS == mt.MEMORY_KINDS | mt.LEGACY_KINDS


def test_round_trip_frozenset_schema_validator():
    # acceptance #1: Python frozenset == JSON-schema enum == validator's set
    schema_enum = prompts.DREAM_JSON_SCHEMA["properties"]["memories"]["items"]["properties"]["kind"]["enum"]
    assert set(schema_enum) == mt.MEMORY_KINDS
    assert quality.VALID_KINDS is mt.VALID_KINDS   # validator uses the single source


def test_is_valid_kind():
    assert mt.is_valid_kind("preference") and mt.is_valid_kind("PROFILE")  # case-insensitive
    assert mt.is_valid_kind("project")    # legacy accepted during the window
    assert not mt.is_valid_kind("bogus") and not mt.is_valid_kind(None)


def test_normalize_kind_maps_legacy_to_semantic():
    assert mt.normalize_kind("profile") == "preference"
    assert mt.normalize_kind("tool") == "toolpattern"
    assert mt.normalize_kind("project") == "projectrule"
    assert mt.normalize_kind("general") == "context"
    assert mt.normalize_kind("decision") == "decision"      # semantic passthrough
    assert mt.normalize_kind("bogus") == mt.DEFAULT_KIND     # unknown → default


def test_parse_kind():
    assert mt.parse_kind("---\ntitle: t\nkind: incident\n---\n\nbody") == "incident"
    assert mt.parse_kind("no frontmatter here") is None


# --- pure: quality gate window ----------------------------------------------

def _mem(kind):
    return {"path": "general/x.md",
            "content": f"---\ntitle: t\nkind: {kind}\n---\n\nA body long enough to pass the length gate."}


def test_quality_accepts_semantic_and_legacy_rejects_unknown():
    for k in mt.MEMORY_KINDS:
        assert quality.validate_memory(_mem(k)) == [], k        # all 15 semantic
    for k in mt.LEGACY_KINDS:
        assert quality.validate_memory(_mem(k)) == [], k        # legacy window
    errs = quality.validate_memory(_mem("frobnicate"))
    assert any("invalid" in e for e in errs)                    # negative


# --- DB: write stamps the kind property -------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE r", mk=MARK)
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)
            s.run("MATCH (s:Session {session_key:'claude_code:__dtype'}) DETACH DELETE s")

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:'claude_code:__dtype'}) SET sess.client='claude_code', sess.session_id='__dtype'")
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def _kind_prop(d, path):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.kind AS k", p=path).single()
        return r["k"] if r else None


def test_write_stamps_semantic_kind_property(driver):
    dream_mod.write_memories(
        driver, "claude_code:__dtype",
        [{"path": f"{MARK}_a.md", "content": "---\ntitle: t\nkind: preference\n---\n\nUser prefers spaces over tabs."}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test", events=None)
    assert _kind_prop(driver, f"{MARK}_a.md") == "preference"


def test_write_normalizes_legacy_frontmatter_to_semantic_property(driver):
    # legacy frontmatter still validates (window); the stored property is semantic
    dream_mod.write_memories(
        driver, "claude_code:__dtype",
        [{"path": f"{MARK}_b.md", "content": "---\ntitle: t\nkind: profile\n---\n\nUser is a systems engineer."}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test", events=None)
    assert _kind_prop(driver, f"{MARK}_b.md") == "preference"   # profile → preference


def test_write_uses_top_level_kind_field_when_present(driver):
    dream_mod.write_memories(
        driver, "claude_code:__dtype",
        [{"path": f"{MARK}_c.md", "kind": "constraint",
          "content": "---\ntitle: t\nkind: constraint\n---\n\nNo network calls in unit tests."}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test", events=None)
    assert _kind_prop(driver, f"{MARK}_c.md") == "constraint"
