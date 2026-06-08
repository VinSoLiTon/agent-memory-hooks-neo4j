#!/usr/bin/env python3
"""Item #9 — consolidation stamps kind/project + skips superseded candidates.

consolidate()'s merge Cypher never set m.kind or m.project (a merged node at a
new path was untyped/unscoped), and its candidate query didn't filter status, so
a superseded-but-unarchived node could be re-merged. Two fixes pinned here:
  - status guard on BOTH sides of _fetch_pair_candidates (mirrors recall._active);
  - the merge stamps m.kind (from the kept frontmatter, normalized) and the
    PATH-based half of the project axis (clear for profile/tools, else preserve).

Static invariants need no DB; the behavioural stamping test drives consolidate()
with a canned candidate pair + a fake provider (no vector index / no LLM).
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import consolidate as consolidate_mod   # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "__cons"
SRC = os.path.join(ROOT, "dream", "consolidate.py")


# --- A: static invariants (no DB) -------------------------------------------

def test_status_guard_present_on_both_sides():
    src = open(SRC, encoding="utf-8").read()
    assert "coalesce(m1.status, 'active') = 'active'" in src
    assert "coalesce(m2.status, 'active') = 'active'" in src


def test_kind_and_project_stamping_present():
    src = open(SRC, encoding="utf-8").read()
    assert "m.kind = $new_kind" in src and "normalize_kind" in src       # kind stamped
    assert "m.project = CASE" in src and "STARTS WITH 'profile/'" in src  # path-based project CASE


# --- DB: behavioural stamping -----------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path CONTAINS $mk DETACH DELETE m", mk=MARK)

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def _seed(d, path, content, *, project=None, status="active"):
    with d.session() as s:
        s.run("CREATE (:Memory {path:$p, content:$c, project:$proj, status:$st, "
              "embedding:[0.1,0.2], updated_at:'2026-06-01T00:00:00+00:00'})",
              p=path, c=content, proj=project, st=status)


def _run_merge(d, monkeypatch, *, src1, src2, merged_path, merged_kind="decision"):
    """Drive consolidate() once with a canned pair + a fake provider that returns
    a merged memory at merged_path with `merged_kind` in its frontmatter."""
    pair = {"p1": src1, "c1": "body one", "p2": src2, "c2": "body two", "score": 0.99}
    monkeypatch.setattr(consolidate_mod, "_fetch_pair_candidates",
                        lambda ses, threshold, limit: [pair])

    def fake_fn(transcript, existing, system, model, max_tokens):
        return [{"path": merged_path,
                 "content": f"---\ntitle: merged\nkind: {merged_kind}\n---\n\nmerged body, long enough."}]

    monkeypatch.setattr(consolidate_mod, "get_provider", lambda name: ("fake", fake_fn))
    monkeypatch.setattr(consolidate_mod, "default_model", lambda name: "fake-model")
    consolidate_mod.consolidate(d, provider_name="fake", threshold=0.0, max_rounds=1, embed_fn=None)


def _node(d, path):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.kind AS kind, m.project AS project, "
                  "m.status AS status", p=path).single()
        return dict(r) if r else None


def test_merge_stamps_kind_and_clears_project_for_new_crossproject_path(driver, monkeypatch):
    _seed(driver, f"project/{MARK}_s1.md", "one", project="alpha")
    _seed(driver, f"project/{MARK}_s2.md", "two", project="alpha")
    merged = f"profile/{MARK}_merged.md"   # cross-project bucket → project must be null
    _run_merge(driver, monkeypatch, src1=f"project/{MARK}_s1.md", src2=f"project/{MARK}_s2.md",
               merged_path=merged, merged_kind="preference")
    n = _node(driver, merged)
    assert n is not None and n["status"] == "active"
    assert n["kind"] == "preference"     # stamped from frontmatter via normalize_kind
    assert n["project"] is None          # profile/ path → project cleared


def test_merge_preserves_existing_project_on_scoped_path(driver, monkeypatch):
    # merged path PRE-EXISTS as a project-scoped node → its project is preserved
    keep = f"project/{MARK}_keep.md"
    _seed(driver, keep, "old body", project="alpha")
    _seed(driver, f"project/{MARK}_s1.md", "one", project="alpha")
    _seed(driver, f"project/{MARK}_s2.md", "two", project="alpha")
    _run_merge(driver, monkeypatch, src1=f"project/{MARK}_s1.md", src2=f"project/{MARK}_s2.md",
               merged_path=keep, merged_kind="decision")
    n = _node(driver, keep)
    assert n["kind"] == "decision"
    assert n["project"] == "alpha"       # scoped path with a prior project → preserved, not invented


def test_legacy_kind_label_normalized_on_merge(driver, monkeypatch):
    _seed(driver, f"project/{MARK}_s1.md", "one")
    _seed(driver, f"project/{MARK}_s2.md", "two")
    merged = f"project/{MARK}_legacy.md"
    _run_merge(driver, monkeypatch, src1=f"project/{MARK}_s1.md", src2=f"project/{MARK}_s2.md",
               merged_path=merged, merged_kind="project")   # legacy bucket label
    assert _node(driver, merged)["kind"] == "projectrule"   # normalized to semantic type
