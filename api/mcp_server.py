#!/usr/bin/env python3
"""Phase G — MCP server: expose njhook memory to any MCP-capable agent.

Four tools over the SAME shared core (`hooks/service.py`) the hook, CLI, and REST
API use — so an MCP client gets identical recall to everything else:

    search_memory(prompt, cwd?, limit?)   -> ranked memory hits
    get_project_context(cwd)              -> session-start memory context (markdown)
    record_event(client, payload)         -> capture an event (shared capture path)
    propose_memory(path, content)         -> propose a memory for review (pending_review)

The `mcp` package is imported lazily in `build_server()` so this module (and the
tool functions, which are unit-tested) load without it; running the server
without `mcp` prints an install hint. `propose_memory` is synchronous (not the
experimental MCP Tasks primitive) — a deliberate stability choice.

Run:  pip install mcp  &&  python api/mcp_server.py     (stdio transport)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import service       # noqa: E402  — shared recall/capture core
import log_event     # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
VALID_CLIENTS = ("claude_code", "codex", "cursor", "gemini")

_driver = None


def driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
            notifications_disabled_classifications=["UNRECOGNIZED"],
        )
    return _driver


# --- tool implementations (plain functions — unit-tested via service) -------

def tool_search_memory(prompt: str, cwd: str | None = None, limit: int = 5) -> list:
    with driver().session() as s:
        return service.recall_context(s, prompt, cwd=cwd, limit=int(limit))


def tool_get_project_context(cwd: str | None = None) -> str:
    with driver().session() as s:
        return service.project_context(s, cwd=cwd) or "(no memory for this project yet)"


def tool_record_event(client: str, payload: dict) -> dict:
    if client not in VALID_CLIENTS:
        return {"ok": False, "error": f"client must be one of {VALID_CLIENTS}"}
    log_event.log_event(payload or {}, client=client)
    return {"ok": True}


def tool_propose_memory(path: str, content: str) -> dict:
    with driver().session() as s:
        return service.propose_memory(s, path, content, created_by="mcp")


TOOLS = {
    "search_memory": tool_search_memory,
    "get_project_context": tool_get_project_context,
    "record_event": tool_record_event,
    "propose_memory": tool_propose_memory,
}


def build_server():
    """Construct the MCP server, registering TOOLS. Imports `mcp` lazily so the
    rest of this module loads without the package installed."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore
    except Exception as e:  # pragma: no cover - exercised only when mcp is absent
        raise RuntimeError(
            "the `mcp` package is required to run the MCP server — `pip install mcp`"
        ) from e

    mcp = FastMCP("njhook-memory")

    @mcp.tool()
    def search_memory(prompt: str, cwd: str | None = None, limit: int = 5) -> list:
        """Recall the most relevant memories for a prompt (project-scoped by cwd)."""
        return tool_search_memory(prompt, cwd, limit)

    @mcp.tool()
    def get_project_context(cwd: str | None = None) -> str:
        """The session-start memory context (profile + tools + project) for a repo."""
        return tool_get_project_context(cwd)

    @mcp.tool()
    def record_event(client: str, payload: dict) -> dict:
        """Capture an agent event through njhook's shared capture path."""
        return tool_record_event(client, payload)

    @mcp.tool()
    def propose_memory(path: str, content: str) -> dict:
        """Propose a new memory for human review (lands as pending_review)."""
        return tool_propose_memory(path, content)

    return mcp


def main():  # pragma: no cover - process entrypoint
    try:
        server = build_server()
    except RuntimeError as e:
        print(e, file=sys.stderr)
        return 1
    server.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
