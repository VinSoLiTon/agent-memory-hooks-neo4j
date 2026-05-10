"""Derive a stable project slug from a cwd.

Heuristic: walk up looking for a .git directory; use that folder's name as
the slug. If no .git is found, fall back to the leaf folder name. Returns
None for an empty/invalid cwd.

Used by:
- inject_memory.py to scope SessionStart context and boost recall
- dream.py to tag memories with the project they came from
"""
from __future__ import annotations

import os
from pathlib import Path


def derive_project(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        p = Path(cwd).expanduser()
        # Don't .resolve() if cwd doesn't exist (test fixtures, captured-but-deleted paths).
        if p.exists():
            p = p.resolve()
    except Exception:
        return None

    cur = p
    while True:
        try:
            if (cur / ".git").exists():
                return cur.name.lower() or None
            parent = cur.parent
            if parent == cur:
                break
            cur = parent
        except Exception:
            break
    leaf = p.name.lower()
    return leaf or None


def dominant_project(cwds: list[str | None]) -> str | None:
    """Pick the most-frequent project across a list of cwds (one per event)."""
    counts: dict[str, int] = {}
    for c in cwds:
        proj = derive_project(c)
        if proj:
            counts[proj] = counts.get(proj, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]
