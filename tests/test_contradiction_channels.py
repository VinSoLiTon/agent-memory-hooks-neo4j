#!/usr/bin/env python3
"""PR (NOW-4): default-on contradiction detection + a fulltext candidate channel.

Contradiction detection shipped OFF (DREAM_CONTRADICTION_CHECK unset, never set
by the nightly) AND drew candidates only from cosine neighbours > 0.85 — so
antonym-style contradictions ("deploy via Docker" vs "never containers"), which
are lexically close but semantically opposite, were never even surfaced to the
judge. This adds a fulltext second channel (unioned with the vector channel) and
turns detection on by default in the nightly.

Pure tests for the finder logic; one DB test that the fulltext channel finds a
lexically-overlapping active memory via the real index; a static check that the
nightly defaults the gate on.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import review   # noqa: E402
import recall   # noqa: E402
import schema   # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__chan"


# --- fulltext_candidates (pure, injected search_fn) -------------------------

def test_fulltext_candidates_maps_hits_and_drops_self():
    def fake_search(session, content, limit=5):
        return [{"path": "general/self.md", "content": "x"},      # the new memory itself
                {"path": "general/other.md", "content": "y"}]
    out = review.fulltext_candidates(fake_search)(None, "general/self.md", "body")
    assert out == [("general/other.md", "y")]                     # self dropped, hit mapped


def test_fulltext_candidates_swallows_search_errors():
    def boom(session, content, limit=5):
        raise RuntimeError("index missing")
    assert review.fulltext_candidates(boom)(None, "p", "c") == []  # never blocks the check


# --- union_candidates (pure) ------------------------------------------------

def test_union_candidates_dedupes_by_path_first_wins():
    vec = lambda s, p, c: [("a", "from-vec"), ("b", "from-vec")]
    ft = lambda s, p, c: [("b", "from-ft"), ("c", "from-ft")]      # 'b' is a dup
    out = review.union_candidates(vec, ft)(None, "self", "content")
    assert [p for p, _ in out] == ["a", "b", "c"]                  # union, order preserved
    assert dict(out)["b"] == "from-vec"                            # first finder wins on dup


def test_union_candidates_drops_self_path():
    f = lambda s, p, c: [("self", "x"), ("a", "y")]
    out = review.union_candidates(f)(None, "self", "content")
    assert [p for p, _ in out] == ["a"]


# --- DB: the fulltext channel finds a low-cosine lexical match ---------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    with d.session() as s:
        s.execute_write(schema.create_constraints_and_indexes)   # ensure memory_fulltext exists

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)

    _clean()
    with d.session() as s:
        # two active memories sharing a distinctive token but semantically OPPOSITE
        s.run("CREATE (:Memory {path:$p, content:'Always deploy via QQDOCKERDEPLOY containers.', "
              "status:'active', updated_at:'2026-06-01T00:00:00+00:00'})", p=f"{MARK}_existing.md")
    try:
        yield d
    finally:
        _clean()
        d.close()


def test_fulltext_channel_surfaces_lexical_overlap_candidate(driver):
    finder = review.fulltext_candidates(recall.fulltext_search)
    new_path = f"{MARK}_new.md"
    new_content = "Never use QQDOCKERDEPLOY; deploy on bare metal instead."
    with driver.session() as s:
        cands = finder(s, new_path, new_content)
    paths = [p for p, _ in cands]
    # the existing memory is a candidate via shared lexical token (cosine would
    # rate these two LOW — opposite stances — so the vector channel could miss it).
    assert f"{MARK}_existing.md" in paths


# --- nightly defaults the gate ON -------------------------------------------

def test_nightly_enables_contradiction_check_by_default():
    src = open(os.path.join(ROOT, "dream", "run_dream.cmd"), encoding="utf-8").read()
    assert 'DREAM_CONTRADICTION_CHECK=1' in src
