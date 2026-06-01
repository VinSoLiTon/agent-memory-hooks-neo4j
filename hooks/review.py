#!/usr/bin/env python3
"""Phase E — conflict/review workflow (resolution machinery).

The lifecycle: a memory is `active` (injected by recall), `pending_review`
(flagged, advisory-only — recall hides it), `rejected` (hidden), or `superseded`
(replaced; hidden, kept for lineage). Recall filters `status='active'`, so
pending/rejected/superseded never inject.

This module is the engine reused by the CLI (`njhook review …`) and, later, the
dashboard conflict view. Pre-commit LLM contradiction *detection* (E1) lands
separately and feeds this queue; here we provide the surfaces + resolution.
"""
from __future__ import annotations

from datetime import datetime, timezone


# Auto-resolution authority (lower rank = higher authority). `created_by` encodes
# the writer ('user', 'dream_anthropic', 'dream_ollama', 'consolidate', …); we map
# it to a tier. Client-level authority (claude_code > codex > …) is a later
# refinement once memories carry their originating client.
def _authority_rank(created_by: str | None) -> int:
    cb = (created_by or "").lower()
    if "user" in cb:
        return 0                       # human edits win
    if "anthropic" in cb or "openai" in cb:
        return 1                       # hosted dream (more capable)
    if "ollama" in cb:
        return 2                       # local dream
    if "consolidate" in cb:
        return 3
    return 4                           # unknown


def auto_resolve(a: dict, b: dict) -> dict:
    """Pick the winner of a conflict by source authority, then recency. `a`/`b`
    are dicts with `created_by`, `updated_at`, `path`. Returns the winner."""
    ra, rb = _authority_rank(a.get("created_by")), _authority_rank(b.get("created_by"))
    if ra != rb:
        return a if ra < rb else b
    return a if str(a.get("updated_at") or "") >= str(b.get("updated_at") or "") else b


# --- session-based operations ----------------------------------------------

def list_pending(session) -> list[dict]:
    return [dict(r) for r in session.run(
        "MATCH (m:Memory) WHERE m.status = 'pending_review' "
        "RETURN m.path AS path, m.created_by AS created_by, m.updated_at AS updated_at "
        "ORDER BY coalesce(m.updated_at, '') DESC"
    )]


def list_contradictions(session) -> list[dict]:
    return [dict(r) for r in session.run(
        "MATCH (a:Memory)-[:CONTRADICTS]-(b:Memory) WHERE a.path < b.path "
        "RETURN a.path AS a, b.path AS b ORDER BY a.path"
    )]


def _set_status(session, path: str, status: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    return session.run(
        "MATCH (m:Memory {path: $p}) SET m.status = $s, m.reviewed_at = $now RETURN count(m) AS n",
        p=path, s=status, now=now,
    ).single()["n"]


def approve(session, path: str) -> int:
    """Activate a pending memory so recall injects it again."""
    return _set_status(session, path, "active")


def reject(session, path: str) -> int:
    """Reject a memory — hidden from recall, kept for the record."""
    return _set_status(session, path, "rejected")


def supersede(session, winner: str, loser: str) -> None:
    """Resolve a conflict: winner stays active; loser is superseded (hidden, kept
    for lineage) with a :SUPERSEDED_BY edge; any :CONTRADICTS between them clears."""
    now = datetime.now(timezone.utc).isoformat()
    session.run(
        "MATCH (w:Memory {path: $w}), (l:Memory {path: $l}) "
        "SET w.status = 'active', l.status = 'superseded', l.valid_until = $now "
        "MERGE (l)-[:SUPERSEDED_BY]->(w) "
        "WITH w, l OPTIONAL MATCH (w)-[c:CONTRADICTS]-(l) DELETE c",
        w=winner, l=loser, now=now,
    )


def flag_contradiction(session, p1: str, p2: str) -> None:
    """Mark two memories as contradicting and route both to review (advisory-only
    until a human/auto-resolution picks a winner)."""
    session.run(
        "MATCH (a:Memory {path: $a}), (b:Memory {path: $b}) "
        "MERGE (a)-[:CONTRADICTS]->(b) "
        "SET a.status = 'pending_review', b.status = 'pending_review'",
        a=p1, b=p2,
    )
