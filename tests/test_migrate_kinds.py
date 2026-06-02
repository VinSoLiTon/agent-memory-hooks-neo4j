#!/usr/bin/env python3
"""Phase D1 — `njhook migrate-kinds` (bulk re-tag legacy kinds, backfill property).

Pure: the frontmatter rewrite touches only the kind line. DB: a legacy memory's
body kind is rewritten to its semantic type, the m.kind property is set, the body
is otherwise preserved, and the rewrite is audited; a body already-semantic but
missing the property is backfilled (no content change, no audit); the migration
is idempotent; and --dry-run mutates nothing.
"""
import os
import sys
import types

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "cli"))

import njhook as cli   # noqa: E402
import audit           # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__mk"


# --- pure -------------------------------------------------------------------

def test_rewrite_frontmatter_kind_only_touches_kind_line():
    content = "---\ntitle: My note\nkind: profile\n---\n\nBody mentions kind: profile inline, keep it."
    out = cli._rewrite_frontmatter_kind(content, "preference")
    assert "kind: preference" in out
    assert "title: My note" in out
    assert out.count("kind: preference") == 1          # only the frontmatter line
    assert "kind: profile inline, keep it." in out     # inline mention untouched


def test_rewrite_no_kind_line_is_noop():
    assert cli._rewrite_frontmatter_kind("no frontmatter", "fact") == "no frontmatter"


# --- DB ---------------------------------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE r", mk=MARK)
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def _get(d, path):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.content AS c, m.kind AS k", p=path).single()
        return (r["c"], r["k"]) if r else (None, None)


def test_migrate_rewrites_legacy_and_audits(driver):
    p = f"{MARK}_legacy.md"
    with driver.session() as s:
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active', "
              "updated_at:'2026-06-01T00:00:00+00:00'})",
              p=p, c="---\ntitle: Role\nkind: profile\n---\n\nUser is a systems engineer.")
    cli.cmd_migrate_kinds(types.SimpleNamespace(dry_run=False))
    content, kind = _get(driver, p)
    assert kind == "preference"                         # property set (profile → preference)
    assert "kind: preference" in content                # frontmatter rewritten
    assert "User is a systems engineer." in content     # body preserved
    with driver.session() as s:
        t = audit.trail(s, p)
    assert any(e["operation"] == "edit" and e["actor"] == "migrate-kinds" for e in t["entries"])


def test_migrate_is_idempotent(driver):
    p = f"{MARK}_legacy.md"
    with driver.session() as s:
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active'})",
              p=p, c="---\ntitle: t\nkind: project\n---\n\nNo unsafe blocks.")
    cli.cmd_migrate_kinds(types.SimpleNamespace(dry_run=False))
    content1, kind1 = _get(driver, p)
    with driver.session() as s:
        n_rev1 = s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(:Memory {path:$p}) RETURN count(r) AS n", p=p).single()["n"]
    cli.cmd_migrate_kinds(types.SimpleNamespace(dry_run=False))   # second run
    content2, kind2 = _get(driver, p)
    with driver.session() as s:
        n_rev2 = s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(:Memory {path:$p}) RETURN count(r) AS n", p=p).single()["n"]
    assert (content1, kind1) == (content2, kind2) == (content2, "projectrule")
    assert n_rev1 == n_rev2                              # no new revision on the no-op re-run


def test_migrate_backfills_property_without_rewrite(driver):
    # already-semantic body, but the m.kind property is missing
    p = f"{MARK}_semantic.md"
    with driver.session() as s:
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active'})",
              p=p, c="---\ntitle: t\nkind: decision\n---\n\nChose Neo4j over Postgres.")
    cli.cmd_migrate_kinds(types.SimpleNamespace(dry_run=False))
    content, kind = _get(driver, p)
    assert kind == "decision"                           # property backfilled
    assert "kind: decision" in content                  # content untouched
    with driver.session() as s:
        n_rev = s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(:Memory {path:$p}) RETURN count(r) AS n", p=p).single()["n"]
    assert n_rev == 0                                    # property backfill is not an audited edit


def test_dry_run_mutates_nothing(driver):
    p = f"{MARK}_dry.md"
    with driver.session() as s:
        s.run("CREATE (:Memory {path:$p, content:$c, status:'active'})",
              p=p, c="---\ntitle: t\nkind: general\n---\n\nSome note.")
    cli.cmd_migrate_kinds(types.SimpleNamespace(dry_run=True))
    content, kind = _get(driver, p)
    assert "kind: general" in content and kind is None  # unchanged, property not set
