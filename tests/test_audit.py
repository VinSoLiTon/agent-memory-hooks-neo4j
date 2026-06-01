#!/usr/bin/env python3
"""Phase H2 — audit log (acceptance #2: every mutation reconstructable).

Pure: closed operation vocabulary; record() rejects out-of-vocab. DB: record +
trail reconstruct an ordered, attributed history; every review surface
(approve/reject/supersede/flag) and a manual edit leave an audit entry; recent()
gives a graph-wide view; and the audit log survives a real backup/restore cycle.
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

import audit            # noqa: E402
import review           # noqa: E402
import njhook as cli    # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__audit"


# --- pure -------------------------------------------------------------------

def test_operations_is_closed_vocab():
    assert isinstance(audit.OPERATIONS, frozenset)
    assert {"approve", "reject", "supersede", "edit"} <= audit.OPERATIONS


def test_record_rejects_unknown_operation():
    # validation happens before any DB access, so a null session is fine
    with pytest.raises(ValueError):
        audit.record(None, "general/x.md", "frobnicate", actor="user")


# --- DB ---------------------------------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH $mk "
                  "DETACH DELETE r", mk=MARK)
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def _mk(s, suffix, status="active", content="body"):
    s.run("CREATE (:Memory {path:$p, content:$c, status:$st, created_by:'dream_test', "
          "updated_at:'2026-06-01T00:00:00+00:00', valid_from:'2026-06-01T00:00:00+00:00'})",
          p=f"{MARK}{suffix}.md", c=content, st=status)
    return f"{MARK}{suffix}.md"


def test_trail_reconstructs_ordered_attributed_history(driver):
    with driver.session() as s:
        p = _mk(s, "_t", status="rejected")
        # explicit increasing ts → deterministic ordering
        audit.record(s, p, "approve", actor="user", status="pending_review",
                     content_snapshot="body", ts="2026-06-01T00:00:01+00:00")
        audit.record(s, p, "reject", actor="user", status="active",
                     content_snapshot="body", ts="2026-06-01T00:00:02+00:00")
        t = audit.trail(s, p)
    ops = [e["operation"] for e in t["entries"]]
    assert ops == ["approve", "reject", "current"]
    assert t["entries"][0]["prior_status"] == "pending_review"
    assert t["entries"][0]["result_status"] == "active"      # approve → active
    assert t["entries"][1]["result_status"] == "rejected"    # reject  → rejected
    assert t["entries"][-1]["result_status"] == "rejected"   # current node state
    assert t["current_status"] == "rejected"


def test_trail_none_for_missing(driver):
    with driver.session() as s:
        assert audit.trail(s, f"{MARK}_nope.md") is None


def test_review_approve_reject_are_audited(driver):
    with driver.session() as s:
        p = _mk(s, "_rv", status="pending_review")
        assert review.approve(s, p) == 1
        assert review.reject(s, p) == 1
        t = audit.trail(s, p)
    ops = [e["operation"] for e in t["entries"]]
    assert "approve" in ops and "reject" in ops
    assert t["current_status"] == "rejected"
    assert all(e["actor"] == "user" for e in t["entries"] if e["operation"] in ("approve", "reject"))


def test_supersede_is_audited(driver):
    with driver.session() as s:
        win = _mk(s, "_win")
        lose = _mk(s, "_lose")
        review.supersede(s, win, lose)
        t = audit.trail(s, lose)
        ops = [e["operation"] for e in t["entries"]]
        assert "supersede" in ops
        assert t["current_status"] == "superseded"
        n = s.run("MATCH (:Memory {path:$l})-[:SUPERSEDED_BY]->(:Memory {path:$w}) RETURN count(*) AS n",
                  l=lose, w=win).single()["n"]
        assert n == 1


def test_flag_contradiction_is_audited_as_system(driver):
    with driver.session() as s:
        a = _mk(s, "_a")
        b = _mk(s, "_b")
        review.flag_contradiction(s, a, b)
        ta, tb = audit.trail(s, a), audit.trail(s, b)
    for t in (ta, tb):
        flags = [e for e in t["entries"] if e["operation"] == "flag_contradiction"]
        assert flags and flags[0]["actor"] == "system"
        assert t["current_status"] == "pending_review"


def test_edit_is_audited(driver):
    with driver.session() as s:
        p = _mk(s, "_ed", content="original body")
        # mirror cmd_edit's recording of the prior body before overwrite
        audit.record(s, p, "edit", actor="user", status="active", content_snapshot="original body")
        s.run("MATCH (m:Memory {path:$p}) SET m.content='new body'", p=p)
        t = audit.trail(s, p)
    edits = [e for e in t["entries"] if e["operation"] == "edit"]
    assert edits and edits[0]["snapshot_len"] == len("original body")


def test_recent_is_graph_wide_newest_first(driver):
    with driver.session() as s:
        p1 = _mk(s, "_r1")
        p2 = _mk(s, "_r2")
        audit.record(s, p1, "approve", actor="user", status="pending_review", ts="2026-06-01T00:00:01+00:00")
        audit.record(s, p2, "reject", actor="user", status="active", ts="2026-06-01T00:00:09+00:00")
        rows = audit.recent(s, 50)
    ours = [r for r in rows if r["path"].startswith(MARK)]
    assert ours[0]["path"] == p2          # newest ts first
    assert {r["operation"] for r in ours} >= {"approve", "reject"}


def _backup_args(out):
    return types.SimpleNamespace(
        out=out, with_embeddings=False, with_sessions=False, since=None,
        session_key=None, limit=0, all_sessions=False, no_tool_response=False, max_field_chars=0,
    )


def test_audit_log_survives_backup_restore(driver, tmp_path):
    with driver.session() as s:
        p = _mk(s, "_bk", status="pending_review")
        review.approve(s, p)   # creates an `approve` audit revision

    out = str(tmp_path / "audit_bkp.json")
    assert cli.cmd_backup(_backup_args(out)) == 0

    with driver.session() as s:
        s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH $mk "
              "DETACH DELETE r", mk=MARK)
        s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)

    assert cli.cmd_restore(types.SimpleNamespace(
        in_=out, with_embeddings=False, dry_run=False, allow_malformed=False)) == 0

    with driver.session() as s:
        ops = [r["op"] for r in s.run(
            "MATCH (rev:MemoryRevision)-[:VERSION_OF]->(:Memory {path:$p}) RETURN rev.operation AS op", p=f"{MARK}_bk.md")]
    assert "approve" in ops   # the audit entry round-tripped
