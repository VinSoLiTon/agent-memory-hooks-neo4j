#!/usr/bin/env python3
"""Phase G — shared service layer.

ONE recall/capture core behind every interface (hook, CLI, REST, and later MCP),
so they can't drift. Recall goes through `recall.py`; capture goes through
`log_event` (scrub + opt-out + spool/direct). The interfaces are thin shells over
the functions here, which is what makes "same query → equivalent hits across every
interface" true by construction.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone

import recall
from project import derive_project

_PATH_RE = re.compile(r"^(profile|tools|project|general)/[A-Za-z0-9._/-]+\.md$")


def recall_context(session, prompt: str, cwd: str | None = None,
                   limit: int = recall.MAX_PROMPT_HITS) -> list[dict]:
    """Ranked memory hits for a prompt, project-scoped by `cwd`. The programmatic
    equivalent of the hook's prompt recall — used by `njhook recall`, REST
    `/recall`, and the MCP `search_memory` tool. Returns plain dicts."""
    project = derive_project(cwd) if cwd else None
    hits = recall.prompt_query(session, prompt or "", current_project=project, limit=limit)
    return [
        {"path": h["path"], "score": round(float(h["score"]), 6),
         "project": h.get("project", ""), "content": h["content"]}
        for h in hits
    ]


def project_context(session, cwd: str | None = None) -> str:
    """The session-start memory context for a project (profile + tools + project
    buckets, budget-rendered) — the SessionStart injection as a callable. Backs
    the MCP `get_project_context` tool and any 'bootstrap me on this repo' caller."""
    project = derive_project(cwd) if cwd else None
    buckets = recall.session_start_buckets(session, project)
    md, _paths = recall.render_session_start(buckets, project)
    return md


def propose_memory(session, path: str, content: str, created_by: str = "mcp") -> dict:
    """Propose a new memory for review (not active recall). Lands as
    `pending_review` so `njhook review` adjudicates it — an agent proposes, a
    human (or auto-resolution) accepts. Refuses to clobber an existing ACTIVE
    memory. Backs the MCP `propose_memory` tool."""
    if not path or not _PATH_RE.match(path):
        return {"ok": False, "error": "path must match ^(profile|tools|project|general)/...\\.md$"}
    if not content or not content.strip():
        return {"ok": False, "error": "content required"}
    existing = session.run(
        "MATCH (m:Memory {path: $p}) RETURN coalesce(m.status, 'active') AS s", p=path
    ).single()
    if existing and existing["s"] == "active":
        return {"ok": False, "error": f"an active memory already exists at {path}; use `njhook edit`/review"}
    now = datetime.now(timezone.utc).isoformat()
    session.run(
        "MERGE (m:Memory {path: $p}) "
        "SET m.content = $c, m.status = 'pending_review', m.created_by = $cb, "
        "    m.updated_at = $now, m.ingested_at = $now, m.valid_from = coalesce(m.valid_from, $now)",
        p=path, c=content, cb=created_by, now=now,
    )
    return {"ok": True, "path": path, "status": "pending_review"}
