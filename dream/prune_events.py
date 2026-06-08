#!/usr/bin/env python3
"""Item #5 — event Tier-1 down-tiering (reversible, lineage-preserving).

Events are the dominant unbounded storage cost: write-once, read-by-dream-once,
fat `tool_response` / `tool_input` / `last_assistant_message` text, never pruned.
Once the dream watermark (`Session.last_dreamed_at`) has passed an event, re-dream
never re-reads it (dream.fetch_events filters strictly to `timestamp > watermark`),
so blanking the heavy text fields is safe for distillation.

Tier-1 (the only tier here): REMOVE the heavy text fields, set `tiered=true`, and
KEEP the node + ids + FIRST_EVENT/NEXT chain so :EXTRACTED_FROM provenance and
lineage survive. This is the event analog of memory archival — fully reversible
(re-ingest from the spool) and non-destructive, consistent with the repo's
audited/restorable design.

Deliberately NOT here: any DETACH DELETE of dreamed chains (Tier-2). That would
destroy provenance + restorability and contradicts the non-destructive design;
if ever wanted it needs a separate, opt-in, rehearse-restore-aware design.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

# Heavy, write-once text fields safe to blank once the watermark has passed.
# `prompt` is blanked only when explicitly opted in (it feeds event_fulltext
# recall + lineage snippets), so it's kept by default.
HEAVY_FIELDS = ("tool_response", "tool_input", "transcript", "last_assistant_message")


def _walk_chain(ses, session_key: str):
    """Yield each event's property dict in chain order, one NEXT hop at a time.

    The `(s)-[:FIRST_EVENT|NEXT*0..]->(e)` varlength expansion OOMs Neo4j's
    transaction-memory pool on long chains (see dream._walk_session_events), so —
    on this write path especially — we walk the linked list with bounded memory.
    A `seen` set guards against a corrupted/branching chain.
    """
    first = ses.run(
        "MATCH (s:Session {session_key: $sk})-[:FIRST_EVENT]->(e:Event) RETURN e",
        sk=session_key,
    ).single()
    if not first:
        return
    seen: set = set()
    node = dict(first["e"])
    while node is not None:
        eid = node.get("event_id")
        if not eid or eid in seen:
            break
        seen.add(eid)
        yield node
        nxt = ses.run(
            "MATCH (:Event {event_id: $eid})-[:NEXT]->(n:Event) RETURN n LIMIT 1",
            eid=eid,
        ).single()
        node = dict(nxt["n"]) if nxt else None


def prune_events(driver, retention_days: int, blank_prompt: bool = False,
                 dry_run: bool = False, now=None) -> dict:
    """Tier-1 down-tier eligible events. Eligible = the Session has been dreamed
    (`last_dreamed_at` set), the event is older than `retention_days`, AND its
    timestamp is at/<= the watermark (so re-dream provably never needs it). Already
    -tiered events are skipped (idempotent). Returns counts + a char estimate of
    the reclaimable heavy text."""
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=retention_days)).isoformat()
    now_iso = now.isoformat()
    fields = list(HEAVY_FIELDS) + (["prompt"] if blank_prompt else [])

    with driver.session() as ses:
        sessions = [(r["sk"], r["wm"]) for r in ses.run(
            "MATCH (s:Session) WHERE s.last_dreamed_at IS NOT NULL "
            "RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS sk, "
            "       s.last_dreamed_at AS wm"
        )]

    sessions_touched = 0
    events_tiered = 0
    chars_reclaimed = 0
    for sk, wm in sessions:
        ids: list = []
        with driver.session() as ses:
            for ev in _walk_chain(ses, sk):
                ts = str(ev.get("timestamp") or "")
                if ev.get("tiered") or not ts:
                    continue
                # past retention AND at/<= the watermark (re-dream can't need it)
                if ts < cutoff and ts <= str(wm or ""):
                    ids.append(ev.get("event_id"))
                    chars_reclaimed += sum(len(str(ev.get(f) or "")) for f in fields)
        if not ids:
            continue
        sessions_touched += 1
        events_tiered += len(ids)
        if dry_run:
            continue
        remove_clause = ", ".join(f"e.{f}" for f in fields)   # fields are hardcoded identifiers
        with driver.session() as ses:
            ses.run(
                f"UNWIND $ids AS eid MATCH (e:Event {{event_id: eid}}) "
                f"REMOVE {remove_clause} SET e.tiered = true, e.tiered_at = $now",
                ids=ids, now=now_iso,
            )
    return {"sessions": sessions_touched, "events_tiered": events_tiered,
            "chars_reclaimed": chars_reclaimed}
