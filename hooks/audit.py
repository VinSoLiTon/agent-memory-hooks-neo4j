#!/usr/bin/env python3
"""Phase H2 — audit log over the :MemoryRevision chain.

Every mutation to a memory appends an immutable
`:MemoryRevision {ts, operation, actor, status, content_snapshot}` linked
`-[:VERSION_OF]->(:Memory)`. That chain *is* the audit log: `trail()`
reconstructs one memory's full mutation history and `recent()` gives a
graph-wide governance view. Because it reuses the Phase A revision node, the
existing `njhook backup`/`restore` round-trips the audit log for free.

Before H2 the review/edit paths overwrote `m.status`/`m.content` in place,
recording only the latest `reviewed_at` — so status-transition *history* and
reviewer attribution were lost (a memory that went pending→active→rejected kept
only "rejected"). `record()` closes that gap; the dream write already records a
`dream_update` revision on content change.

Convention: `status` on an entry is the memory's status *before* the operation
(the prior state — matching the dream write's snapshot). The resulting status is
implied by the operation (see RESULT_STATUS) or by the next entry / current node,
so a transition is fully reconstructable from prior + operation.
"""
from __future__ import annotations

from datetime import datetime, timezone

# Closed vocabulary of mutation operations.
OPERATIONS = frozenset({
    "dream_create", "dream_update", "edit",
    "approve", "reject", "supersede", "flag_contradiction", "quarantine",
})

# The status an operation moves the memory INTO (None = unchanged by the op).
RESULT_STATUS = {
    "approve": "active",
    "reject": "rejected",
    "supersede": "superseded",
    "flag_contradiction": "pending_review",
    "quarantine": "pending_review",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def record(session, path: str, operation: str, *, actor: str,
           status: str | None = None, content_snapshot: str | None = None,
           ts: str | None = None) -> int:
    """Append an immutable audit entry (a :MemoryRevision) for a mutation of
    `path`. `status` is the memory's prior status; `content_snapshot` is the body
    as of the mutation. Returns 1 if the memory exists (entry written), else 0.
    Raises ValueError on an out-of-vocabulary operation."""
    if operation not in OPERATIONS:
        raise ValueError(f"unknown audit operation {operation!r}; choices: {sorted(OPERATIONS)}")
    rec = session.run(
        "MATCH (m:Memory {path:$p}) "
        "CREATE (rev:MemoryRevision {ts:$ts, operation:$op, actor:$actor, "
        "                            status:$status, content_snapshot:$cs}) "
        "MERGE (rev)-[:VERSION_OF]->(m) "
        "RETURN count(rev) AS n",
        p=path, ts=ts or _now(), op=operation, actor=actor,
        status=status, cs=content_snapshot,
    ).single()
    return rec["n"] if rec else 0


def trail(session, path: str) -> dict | None:
    """Reconstruct a memory's full mutation history. Returns
    {path, created_at, created_by, current_status, entries[]} (entries oldest →
    newest, each with ts/operation/actor/prior_status/result_status/snapshot_len),
    terminated by a synthetic `current` entry showing the present state. None if
    no such memory."""
    node = session.run(
        "MATCH (m:Memory {path:$p}) "
        "RETURN m.status AS status, m.created_by AS created_by, m.updated_at AS updated_at, "
        "       coalesce(m.valid_from, m.ingested_at) AS created_at, m.content AS content",
        p=path,
    ).single()
    if not node:
        return None

    entries: list[dict] = []
    for r in session.run(
        "MATCH (rev:MemoryRevision)-[:VERSION_OF]->(:Memory {path:$p}) "
        "RETURN rev.ts AS ts, rev.operation AS operation, rev.actor AS actor, "
        "       rev.status AS prior_status, rev.content_snapshot AS snapshot "
        "ORDER BY rev.ts",
        p=path,
    ):
        snap = r["snapshot"]
        entries.append({
            "ts": r["ts"], "operation": r["operation"], "actor": r["actor"],
            "prior_status": r["prior_status"],
            "result_status": RESULT_STATUS.get(r["operation"]),
            "snapshot_len": len(snap) if snap is not None else None,
        })

    entries.append({
        "ts": node["updated_at"], "operation": "current", "actor": node["created_by"],
        "prior_status": None, "result_status": node["status"],
        "snapshot_len": len(node["content"]) if node["content"] is not None else None,
    })
    return {
        "path": path, "created_at": node["created_at"], "created_by": node["created_by"],
        "current_status": node["status"], "entries": entries,
    }


def recent(session, limit: int = 20) -> list[dict]:
    """The most recent mutations across all memories (newest first) — a
    graph-wide governance view. Each row: path/ts/operation/actor."""
    return [dict(r) for r in session.run(
        "MATCH (rev:MemoryRevision)-[:VERSION_OF]->(m:Memory) "
        "RETURN m.path AS path, rev.ts AS ts, rev.operation AS operation, rev.actor AS actor "
        "ORDER BY rev.ts DESC LIMIT $n",
        n=limit,
    )]
