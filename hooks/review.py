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

import audit


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


def _set_status(session, path: str, status: str, *, operation: str, actor: str = "user") -> int:
    """Transition a memory's status and append an audit entry recording the prior
    status + actor (H2). Returns 0 if no such memory (nothing recorded/changed)."""
    now = datetime.now(timezone.utc).isoformat()
    cur = session.run(
        "MATCH (m:Memory {path: $p}) RETURN coalesce(m.status, 'active') AS s, m.content AS c",
        p=path,
    ).single()
    if not cur:
        return 0
    audit.record(session, path, operation, actor=actor, status=cur["s"],
                 content_snapshot=cur["c"], ts=now)
    return session.run(
        "MATCH (m:Memory {path: $p}) SET m.status = $s, m.reviewed_at = $now RETURN count(m) AS n",
        p=path, s=status, now=now,
    ).single()["n"]


def approve(session, path: str) -> int:
    """Activate a pending memory so recall injects it again."""
    return _set_status(session, path, "active", operation="approve")


def reject(session, path: str) -> int:
    """Reject a memory — hidden from recall, kept for the record."""
    return _set_status(session, path, "rejected", operation="reject")


def supersede(session, winner: str, loser: str) -> None:
    """Resolve a conflict: winner stays active; loser is superseded (hidden, kept
    for lineage) with a :SUPERSEDED_BY edge; any :CONTRADICTS between them clears."""
    now = datetime.now(timezone.utc).isoformat()
    # H2 audit: record the loser's supersede and the winner's (re)activation if it
    # wasn't already active, capturing prior status + actor before the mutation.
    states = {r["p"]: r for r in session.run(
        "MATCH (m:Memory) WHERE m.path IN [$w, $l] "
        "RETURN m.path AS p, coalesce(m.status, 'active') AS s, m.content AS c", w=winner, l=loser)}
    if loser in states:
        audit.record(session, loser, "supersede", actor="user",
                     status=states[loser]["s"], content_snapshot=states[loser]["c"], ts=now)
    if winner in states and states[winner]["s"] != "active":
        audit.record(session, winner, "approve", actor="user",
                     status=states[winner]["s"], content_snapshot=states[winner]["c"], ts=now)
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
    # H2 audit: record both flags (actor 'system' — detection is automated).
    states = {r["p"]: r for r in session.run(
        "MATCH (m:Memory) WHERE m.path IN [$a, $b] "
        "RETURN m.path AS p, coalesce(m.status, 'active') AS s, m.content AS c", a=p1, b=p2)}
    for p in (p1, p2):
        if p in states:
            audit.record(session, p, "flag_contradiction", actor="system",
                         status=states[p]["s"], content_snapshot=states[p]["c"])
    session.run(
        "MATCH (a:Memory {path: $a}), (b:Memory {path: $b}) "
        "MERGE (a)-[:CONTRADICTS]->(b) "
        "SET a.status = 'pending_review', b.status = 'pending_review'",
        a=p1, b=p2,
    )


def flag_new_contradiction(session, existing_path: str, new_path: str) -> None:
    """Write-time routing for Phase E acceptance #1: link
    `new-[:CONTRADICTS]->existing` and quarantine ONLY the NEW memory to
    `pending_review`. The EXISTING active memory STAYS ACTIVE — recall keeps using
    the established memory until a human resolves the conflict (vs the symmetric
    `flag_contradiction`, which pends both). Audited (H2)."""
    cur = session.run(
        "MATCH (m:Memory {path: $p}) RETURN coalesce(m.status, 'active') AS s, m.content AS c",
        p=new_path,
    ).single()
    if cur:
        audit.record(session, new_path, "flag_contradiction", actor="dream",
                     status=cur["s"], content_snapshot=cur["c"])
    session.run(
        "MATCH (n:Memory {path: $n}), (e:Memory {path: $e}) "
        "MERGE (n)-[:CONTRADICTS]->(e) "
        "SET n.status = 'pending_review'",
        n=new_path, e=existing_path,
    )


def auto_resolve_all(session) -> int:
    """Resolve every open :CONTRADICTS pair by auto_resolve (authority×recency):
    the winner stays active, the loser is superseded. Returns the count resolved."""
    resolved = 0
    for p in list_contradictions(session):
        rows = {r["path"]: dict(r) for r in session.run(
            "MATCH (m:Memory) WHERE m.path IN [$a, $b] "
            "RETURN m.path AS path, m.created_by AS created_by, m.updated_at AS updated_at",
            a=p["a"], b=p["b"])}
        if p["a"] not in rows or p["b"] not in rows:
            continue
        winner = auto_resolve(rows[p["a"]], rows[p["b"]])
        loser = p["b"] if winner["path"] == p["a"] else p["a"]
        supersede(session, winner["path"], loser)
        resolved += 1
    return resolved


def vector_candidates(embed_fn, k: int = 5, threshold: float = 0.85):
    """Build a `find_candidates(session, path, content)` that returns the active
    memories most semantically similar to the new content (via the vector index),
    excluding the memory itself. Used by detect_contradiction in production."""
    def _find(session, path, content):
        try:
            vec = embed_fn([content])
            if not vec or not vec[0]:
                return []
        except Exception:
            return []
        try:
            rows = session.run(
                "CALL db.index.vector.queryNodes('memory_embeddings', $k, $vec) YIELD node, score "
                "WHERE score > $th AND node.path <> $path AND coalesce(node.status, 'active') = 'active' "
                "RETURN node.path AS path, node.content AS content",
                k=k, vec=vec[0], th=threshold, path=path,
            )
            return [(r["path"], r["content"]) for r in rows]
        except Exception:
            return []
    return _find


def detect_contradiction(session, path: str, content: str, judge, find_candidates,
                         on_contradiction=None) -> list[str]:
    """Pre-commit/contradiction detection: for each semantically-related active
    memory `find_candidates` surfaces, ask `judge(existing, new) -> bool`; on a
    contradiction, invoke `on_contradiction(session, existing_path, new_path)`.
    Returns the contradicting paths. `judge` is the LLM in production, a stub in
    tests; the candidate-finder and judge are injected so the logic is testable
    without an LLM.

    `on_contradiction` defaults to `flag_contradiction` (symmetric — both pend, the
    manual/dashboard flow). The nightly passes `flag_new_contradiction` so only the
    NEW memory is quarantined and the established active one keeps serving recall
    (acceptance #1)."""
    on_contradiction = on_contradiction or flag_contradiction
    flagged = []
    for cand_path, cand_content in find_candidates(session, path, content):
        if cand_path == path:
            continue
        try:
            if judge(cand_content, content):
                on_contradiction(session, cand_path, path)
                flagged.append(cand_path)
        except Exception:
            continue
    return flagged
