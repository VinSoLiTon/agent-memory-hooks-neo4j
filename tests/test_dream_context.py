#!/usr/bin/env python3
"""Dream existing-context hardening. Acceptance tests.

Root cause fixed here: the dream phase fed the entire active memory store as
`<existing_memories>`, which swamped small local models (qwen3.5/gemma4 stalled
or regurgitated). Two defences:
  - fetch_existing_memories is SCOPED (profile/+tools/+session project) and
    excludes superseded/archived memories, with a char-cap backstop.
  - render_existing(paths_only=True) gives local models just the existing paths.

Pure tests need no Neo4j; the scoping test does.
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
MARK = "__dctx"


# --- render_existing (pure) -------------------------------------------------

def test_render_existing_paths_only_omits_bodies():
    out = dream_mod.render_existing([{"path": "profile/role.md", "content": "SECRETBODY"}], paths_only=True)
    assert "profile/role.md" in out
    assert "SECRETBODY" not in out   # paths-only must not leak the body
    assert "```" not in out          # nor the fenced-body formatting


def test_render_existing_full_includes_bodies():
    out = dream_mod.render_existing([{"path": "profile/role.md", "content": "SECRETBODY"}])
    assert "SECRETBODY" in out


def test_render_existing_empty():
    assert dream_mod.render_existing([]) == "(no existing memories)"


# --- fetch_existing_memories scoping (DB) -----------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved_cap = os.environ.get("DREAM_EXISTING_MAX_CHARS")
    os.environ["DREAM_EXISTING_MAX_CHARS"] = "100000000"  # disable trim so real graph data can't interfere
    _cleanup(d)
    with d.session() as s:
        s.run(
            """
            CREATE (:Memory {path:$p1, content:'profile body',  status:'active'})
            CREATE (:Memory {path:$p2, content:'tools body',    status:'active'})
            CREATE (:Memory {path:$p3, content:'projA body',    status:'active', project:'projA'})
            CREATE (:Memory {path:$p4, content:'projB body',    status:'active', project:'projB'})
            CREATE (:Memory {path:$p5, content:'projA stale',   status:'superseded', project:'projA'})
            """,
            p1=f"profile/{MARK}_p.md", p2=f"tools/{MARK}_t.md",
            p3=f"project/{MARK}_a.md", p4=f"project/{MARK}_b.md",
            p5=f"project/{MARK}_super.md",
        )
    try:
        yield d
    finally:
        _cleanup(d)
        if saved_cap is None:
            os.environ.pop("DREAM_EXISTING_MAX_CHARS", None)
        else:
            os.environ["DREAM_EXISTING_MAX_CHARS"] = saved_cap
        d.close()


def _cleanup(d):
    with d.session() as s:
        s.run("MATCH (m:Memory) WHERE m.path CONTAINS $mark DETACH DELETE m", mark=MARK)


def test_fetch_existing_is_scoped_and_excludes_superseded(driver):
    paths = {m["path"] for m in dream_mod.fetch_existing_memories(driver, "projA")}
    assert f"profile/{MARK}_p.md" in paths        # cross-project profile → always
    assert f"tools/{MARK}_t.md" in paths          # cross-project tools → always
    assert f"project/{MARK}_a.md" in paths         # this session's project
    assert f"project/{MARK}_b.md" not in paths     # other project → excluded
    assert f"project/{MARK}_super.md" not in paths  # superseded → excluded
