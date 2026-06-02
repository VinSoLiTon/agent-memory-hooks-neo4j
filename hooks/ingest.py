#!/usr/bin/env python3
"""Phase B — ingest worker: drain the spool into Neo4j with idempotent replay.

Run via `njhook ingest`. For each spooled event:
  - malformed JSON or missing event_id → dead-letter (DLQ), keep going.
  - if an :Event with that event_id already exists → skip (idempotent: the
    Event's existence IS the inbox marker, so a crash after the Neo4j write but
    before removing the spool line can never produce a duplicate on replay).
  - else append it to the session chain via the same writer the hook uses.

A spool file is deleted only once every one of its records has been ingested,
skipped, or dead-lettered. If a Neo4j write fails (e.g. DB down), we stop and
KEEP the remaining files so a later run retries — nothing is lost.
"""
from __future__ import annotations

import sys
from collections import defaultdict

import spool          # hooks/spool.py
import log_event      # hooks/log_event.py — reuse _append_event + ensure_minimal_constraints
import event_schema   # hooks/event_schema.py — read-time upcasting (Phase B PR-2)


def ingest(driver) -> dict:
    """Drain the spool into `driver`. Returns {processed, skipped, dlq}."""
    with driver.session() as s:
        s.execute_write(log_event.ensure_minimal_constraints)

    by_file: dict = defaultdict(list)
    for f, _i, rec, raw in spool.iter_records():
        by_file[f].append((rec, raw))

    processed = skipped = dlq = 0
    drained = []
    for f, items in by_file.items():
        clean = True
        for rec, raw in items:
            if rec is None:
                spool.to_dlq(raw, "invalid JSON")
                dlq += 1
                continue
            # Read-time upcasting: normalize an older-schema record to the current
            # version before extracting fields. Old spool records are never
            # rewritten — they're transformed on read (Phase B PR-2 / B3).
            rec = event_schema.upcast(rec)
            ev = rec.get("event_props") or {}
            eid = ev.get("event_id")
            if not eid:
                spool.to_dlq(raw, "missing event_id")
                dlq += 1
                continue
            try:
                with driver.session() as s:
                    exists = s.run(
                        "MATCH (e:Event {event_id: $id}) RETURN count(e) > 0 AS x", id=eid
                    ).single()["x"]
                    if exists:
                        skipped += 1
                        continue
                    s.execute_write(
                        log_event._append_event,
                        rec.get("session_id", "unknown"),
                        rec.get("client", "unknown"),
                        ev,
                    )
                    processed += 1
            except Exception as e:
                # Neo4j unavailable / write error — keep this file (and the rest)
                # for a later retry. Idempotency makes re-processing safe.
                print(f"ingest: write failed ({e}); keeping spool for retry", file=sys.stderr)
                clean = False
                break
        if clean:
            drained.append(f)

    for f in drained:
        try:
            f.unlink()
        except Exception:
            pass

    return {"processed": processed, "skipped": skipped, "dlq": dlq}
