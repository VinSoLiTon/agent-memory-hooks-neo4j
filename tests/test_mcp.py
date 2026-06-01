#!/usr/bin/env python3
"""Phase G (PR-2) — MCP server. Acceptance tests.

The MCP tools delegate to the shared service, so we test that logic directly
(no `mcp` package needed): get_project_context surfaces project memory, and
propose_memory lands a review-only memory without clobbering an active one. The
protocol wiring (`build_server`) is asserted to require `mcp` when absent / build
when present.
"""
import importlib.util
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "api"))

import service        # noqa: E402
import embeddings      # noqa: E402
import mcp_server      # noqa: E402  (imports cleanly without the `mcp` package)

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = embeddings.is_enabled
    embeddings.is_enabled = lambda: False

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path CONTAINS '__mcp' DETACH DELETE m")

    _clean()
    with d.session() as s:
        s.run("CREATE (:Memory {path:'profile/__mcp_p.md', "
              "content:'---\\ntitle: t\\nkind: profile\\n---\\n\\nmcp test profile note.', "
              "status:'active', importance:10, updated_at:'2027-01-01T00:00:00+00:00'})")
    try:
        yield d
    finally:
        _clean()
        embeddings.is_enabled = saved
        d.close()


def test_tool_registry_has_the_four_tools():
    assert set(mcp_server.TOOLS) == {"search_memory", "get_project_context", "record_event", "propose_memory"}


def test_build_server_requires_mcp_or_builds():
    if importlib.util.find_spec("mcp"):
        assert mcp_server.build_server() is not None
    else:
        with pytest.raises(RuntimeError):
            mcp_server.build_server()


def test_get_project_context_surfaces_profile_memory(driver):
    with driver.session() as s:
        ctx = service.project_context(s, cwd=None)
    assert "profile/__mcp_p.md" in ctx


def test_propose_memory_is_pending_and_no_clobber(driver):
    body = "---\ntitle: t\nkind: general\n---\n\nproposed body content for review."
    with driver.session() as s:
        r = service.propose_memory(s, "general/__mcp_new.md", body)
        assert r["ok"] and r["status"] == "pending_review"
        st = s.run("MATCH (m:Memory {path:'general/__mcp_new.md'}) RETURN m.status AS s").single()["s"]
        assert st == "pending_review"
        # pending memory is not recalled
        assert "general/__mcp_new.md" not in {h["path"] for h in service.recall_context(s, "proposed body content")}
        # refuses to clobber an existing ACTIVE memory
        r2 = service.propose_memory(s, "profile/__mcp_p.md", "different content")
        assert r2["ok"] is False


def test_propose_memory_rejects_bad_path(driver):
    with driver.session() as s:
        assert service.propose_memory(s, "not-a-valid-path", "body")["ok"] is False
