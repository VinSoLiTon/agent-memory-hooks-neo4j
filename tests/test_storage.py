#!/usr/bin/env python3
"""Item #15 — on-demand storage accounting (`njhook storage`).

`stats` reports counts only; there was no way to see WHERE bytes accumulate, so
every pruning decision was blind. Pins the byte-math helpers (pure) and the
end-to-end command over a seeded graph (DB). Estimates only — labelled as such.
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

import njhook as cli   # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__store"
SK = "test:__store"


# --- pure helpers -----------------------------------------------------------

def test_embedding_bytes_is_8_per_float():
    assert cli._embedding_bytes(768) == 6144     # 768 doubles * 8 bytes
    assert cli._embedding_bytes(0) == 0
    assert cli._embedding_bytes(None) == 0


def test_human_bytes_scales():
    assert cli._human_bytes(0) == "0 B"
    assert cli._human_bytes(512) == "512 B"
    assert cli._human_bytes(2048).endswith("KB")
    assert cli._human_bytes(5 * 1024 * 1024).endswith("MB")


# --- DB integration ---------------------------------------------------------

@pytest.fixture()
def graph():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH '__st_' DETACH DELETE e")
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        s.run("CREATE (:Memory {path:$p1, content:$c100, status:'active', embedding:$emb})",
              p1=f"{MARK}_a.md", c100="x" * 100, emb=[0.1] * 768)
        s.run("CREATE (:Memory {path:$p2, content:$c250, status:'active'})",
              p2=f"{MARK}_b.md", c250="y" * 250)
        s.run(
            """
            CREATE (sess:Session {session_key:$sk, client:'test', session_id:'__store'})
            CREATE (e1:Event {event_id:'__st_1', event_name:'PostToolUse', timestamp:'2026-06-01T00:00:00+00:00',
                              tool_response:$tr})
            CREATE (e2:Event {event_id:'__st_2', event_name:'UserPromptSubmit', timestamp:'2026-06-01T00:00:01+00:00',
                              prompt:$pr})
            CREATE (sess)-[:FIRST_EVENT]->(e1)-[:NEXT]->(e2)
            CREATE (sess)-[:LATEST_EVENT]->(e2)
            """, sk=SK, tr="r" * 400, pr="p" * 50)
    # cmd_storage uses the real cli.driver() against this same local Neo4j, so the
    # seeded MARK data appears in its aggregates (assertions use >= to tolerate the
    # rest of the graph). No monkeypatch — that would let cmd_storage close a shared
    # driver out from under the fixture.
    try:
        yield d
    finally:
        _clean()
        d.close()


def _run_storage(**kw):
    args = types.SimpleNamespace(top=kw.get("top", 10), json=kw.get("json", False))
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.cmd_storage(args)
    return rc, buf.getvalue()


def test_storage_reports_bucket_and_embedding_bytes(graph):
    import json as _json
    rc, out = _run_storage(json=True)
    assert rc == 0
    data = _json.loads(out)
    by_bucket = {b["bucket"]: b for b in data["memory_bytes_by_bucket"]}
    # our two seeded general/ memories: 100 + 250 chars (other buckets may exist)
    assert by_bucket["general"]["chars"] >= 350
    # one 768-float embedding → at least 6144 bytes estimated (graph may have more)
    assert data["embedding"]["bytes_est"] >= 6144
    assert data["reclaimable_event_bytes"] is None   # stubbed pending item #5's tier


def test_storage_human_output_labels_estimates(graph):
    rc, out = _run_storage()
    assert rc == 0
    assert "ESTIMATES" in out and "(est.)" in out
    assert "reclaimable: n/a" in out                 # reserved slot, no invented number
    assert "Event text:" in out and "by client:" in out   # chain-free event breakdown


def test_storage_event_bytes_are_chain_free_and_global(graph):
    import json as _json
    _, out = _run_storage(json=True)
    data = _json.loads(out)
    # our seeded session's events carry tool_response(400) + prompt(50) = 450 chars;
    # the global total includes the rest of the graph, so just assert it cleared our floor.
    assert data["event_bytes_total"] >= 450
    assert isinstance(data["event_bytes_by_client"], list)
