#!/usr/bin/env python3
"""Phase A acceptance #6 — backup/restore round-trips the new schema fields AND
the :MemoryRevision / :SUPERSEDED_BY lineage (closes PROGRESS gap #1).

Exercises the real cmd_backup / cmd_restore code paths against a live Neo4j.
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

import njhook as cli  # noqa: E402  (cli/njhook.py)

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__bkp"


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


def _backup_args(out):
    return types.SimpleNamespace(
        out=out, with_embeddings=False, with_sessions=False, since=None,
        session_key=None, limit=0, all_sessions=False, no_tool_response=False, max_field_chars=0,
    )


def test_backup_restore_roundtrips_fields_and_lineage(driver, tmp_path):
    with driver.session() as s:
        s.run(
            """
            CREATE (m:Memory {path:$cur, content:'current body', status:'active', importance:7,
                              created_by:'dream_test', updated_at:'2026-06-02T00:00:00+00:00',
                              ingested_at:'2026-06-02T00:00:00+00:00', valid_from:'2026-06-01T00:00:00+00:00'})
            CREATE (old:Memory {path:$old, content:'old body', status:'superseded',
                                valid_until:'2026-06-02T00:00:00+00:00'})
            CREATE (old)-[:SUPERSEDED_BY]->(m)
            CREATE (:MemoryRevision {content_snapshot:'prior body', status:'active',
                                     operation:'dream_update', actor:'dream_test',
                                     ts:'2026-06-02T00:00:00+00:00'})-[:VERSION_OF]->(m)
            """,
            cur=f"{MARK}.md", old=f"{MARK}_old.md",
        )

    out = str(tmp_path / "bkp.json")
    assert cli.cmd_backup(_backup_args(out)) == 0

    # wipe the seeded nodes, then restore from the backup
    with driver.session() as s:
        s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE r", mk=MARK)
        s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)
        assert s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk RETURN count(m) AS n", mk=MARK).single()["n"] == 0

    assert cli.cmd_restore(types.SimpleNamespace(
        in_=out, with_embeddings=False, dry_run=False, allow_malformed=False)) == 0

    with driver.session() as s:
        m = s.run("MATCH (m:Memory {path:$p}) RETURN m.status AS st, m.importance AS imp, "
                  "m.created_by AS cb, m.valid_from AS vf", p=f"{MARK}.md").single()
        assert m["st"] == "active" and m["imp"] == 7 and m["cb"] == "dream_test"
        assert m["vf"] == "2026-06-01T00:00:00+00:00"

        rev = s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(:Memory {path:$p}) "
                    "RETURN r.content_snapshot AS cs", p=f"{MARK}.md").single()
        assert rev and rev["cs"] == "prior body"

        n_sup = s.run("MATCH (:Memory {path:$old})-[:SUPERSEDED_BY]->(:Memory {path:$cur}) "
                      "RETURN count(*) AS n", old=f"{MARK}_old.md", cur=f"{MARK}.md").single()["n"]
        assert n_sup == 1
