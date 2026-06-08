#!/usr/bin/env python3
"""Item #21 — strip embeddings from terminally-superseded memories.

A superseded memory keeps its content (lineage/audit), but its embedding is dead
weight: it bloats storage and keeps occupying an HNSW vector-index slot, polluting
ANN top-k. At the two production supersession sites (review.supersede,
consolidate merge) the embedding props are removed (default ON; env escape hatch).
A one-shot `njhook prune-embeddings` backfills graphs that predate the change.
Recall/lineage are unaffected (recall already filters status='active').
"""
import io
import os
import sys
import types
from contextlib import redirect_stdout

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "cli"))

import review            # noqa: E402
import njhook as cli     # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__strip"


# --- static guards (no DB) --------------------------------------------------

def test_both_supersession_sites_strip_embedding():
    rev = open(os.path.join(ROOT, "hooks", "review.py"), encoding="utf-8").read()
    con = open(os.path.join(ROOT, "dream", "consolidate.py"), encoding="utf-8").read()
    assert "REMOVE l.embedding" in rev                 # supersede() strips the loser
    assert "REMOVE old.embedding" in con               # consolidate strips the merged sources
    assert "MEMORY_STRIP_SUPERSEDED_EMBEDDING" in rev and "MEMORY_STRIP_SUPERSEDED_EMBEDDING" in con


# --- DB -----------------------------------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)

    _clean()
    try:
        yield d
    finally:
        _clean()
        d.close()


def _seed(d, suffix, status="active"):
    p = f"{MARK}_{suffix}.md"
    with d.session() as s:
        s.run("CREATE (:Memory {path:$p, content:'body', status:$st, "
              "embedding:[0.1,0.2], embedding_model:'nomic', embedding_dim:768, "
              "updated_at:'2026-06-01T00:00:00+00:00'})", p=p, st=status)
    return p


def _emb(d, p):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.embedding AS e, m.status AS st", p=p).single()
        return (r["e"], r["st"]) if r else (None, None)


def test_supersede_strips_loser_embedding_keeps_winner(driver, monkeypatch):
    monkeypatch.setenv("MEMORY_STRIP_SUPERSEDED_EMBEDDING", "1")
    win, lose = _seed(driver, "win"), _seed(driver, "lose")
    with driver.session() as s:
        review.supersede(s, win, lose)
    le, lst = _emb(driver, lose)
    we, _ = _emb(driver, win)
    assert lst == "superseded" and le is None          # loser vector stripped (drops from HNSW)
    assert we is not None                              # winner untouched


def test_gate_off_retains_superseded_embedding(driver, monkeypatch):
    monkeypatch.setenv("MEMORY_STRIP_SUPERSEDED_EMBEDDING", "0")
    win, lose = _seed(driver, "win2"), _seed(driver, "lose2")
    with driver.session() as s:
        review.supersede(s, win, lose)
    le, lst = _emb(driver, lose)
    assert lst == "superseded" and le is not None      # escape hatch retains the vector


def test_prune_embeddings_backfill(driver):
    p = _seed(driver, "old", status="superseded")      # predates the auto-strip
    saved = cli.driver
    cli.driver = lambda: GraphDatabase.driver(URI, auth=(USER, PWD),
                                              notifications_disabled_classifications=["UNRECOGNIZED"])
    try:
        # dry-run counts, writes nothing
        buf = io.StringIO()
        with redirect_stdout(buf):
            cli.cmd_prune_embeddings(types.SimpleNamespace(dry_run=True))
        assert _emb(driver, p)[0] is not None and "would strip" in buf.getvalue()
        # real run strips
        with redirect_stdout(io.StringIO()):
            cli.cmd_prune_embeddings(types.SimpleNamespace(dry_run=False))
        assert _emb(driver, p)[0] is None
    finally:
        cli.driver = saved
