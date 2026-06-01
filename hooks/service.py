#!/usr/bin/env python3
"""Phase G — shared service layer.

ONE recall/capture core behind every interface (hook, CLI, REST, and later MCP),
so they can't drift. Recall goes through `recall.py`; capture goes through
`log_event` (scrub + opt-out + spool/direct). The interfaces are thin shells over
the functions here, which is what makes "same query → equivalent hits across every
interface" true by construction.
"""
from __future__ import annotations

import recall
from project import derive_project


def recall_context(session, prompt: str, cwd: str | None = None,
                   limit: int = recall.MAX_PROMPT_HITS) -> list[dict]:
    """Ranked memory hits for a prompt, project-scoped by `cwd`. The programmatic
    equivalent of the hook's prompt recall — used by `njhook recall`, REST
    `/recall`, and (later) the MCP `search_memory` tool. Returns plain dicts."""
    project = derive_project(cwd) if cwd else None
    hits = recall.prompt_query(session, prompt or "", current_project=project, limit=limit)
    return [
        {"path": h["path"], "score": round(float(h["score"]), 6),
         "project": h.get("project", ""), "content": h["content"]}
        for h in hits
    ]
