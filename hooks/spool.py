#!/usr/bin/env python3
"""Phase B — local append-only spool for durable event capture.

The hook hot path can append a normalized event here instead of writing straight
to Neo4j, so capture never silently fails when Neo4j is down/slow. The ingest
worker (hooks/ingest.py) drains the spool into the graph with idempotent replay.

Layout (under HOOKS_SPOOL_DIR, default ~/.njhook/spool):
  events-YYYY-MM-DD.jsonl   one JSON record per line, fsync'd on append
  dlq.jsonl                 records that could not be ingested (with the error)

A spool record is:
  {"schema_version": 1, "client": ..., "session_id": ..., "app_id": ...,
   "event_props": {...}}    # event_props is exactly what _append_event expects

Everything is best-effort and dynamic (the spool dir is read from the env on each
call) so tests can point it at a temp directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

SCHEMA_VERSION = 1


def _dir() -> Path:
    return Path(os.environ.get("HOOKS_SPOOL_DIR", str(Path.home() / ".njhook" / "spool")))


def _dlq_file() -> Path:
    return _dir() / "dlq.jsonl"


def _append_line(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text + "\n")
        fh.flush()
        os.fsync(fh.fileno())  # durability: survive a crash right after the hook returns


def append(record: dict, day: str) -> None:
    """Append one event record to the day's spool file (fsync'd)."""
    _append_line(_dir() / f"events-{day}.jsonl", json.dumps(record, default=str))


def to_dlq(raw: str, error: str) -> None:
    """Dead-letter a record that can't be ingested, preserving the raw line + error."""
    _append_line(_dlq_file(), json.dumps({"error": error, "raw": raw}))


def event_files() -> list[Path]:
    d = _dir()
    return sorted(d.glob("events-*.jsonl")) if d.exists() else []


def iter_records():
    """Yield (file, lineno, parsed_record_or_None, raw_line) across all spool files,
    in chronological file order. parsed_record is None for malformed JSON."""
    for f in event_files():
        with open(f, encoding="utf-8") as fh:
            for i, raw in enumerate(fh):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    rec = None
                yield f, i, rec, raw


def backlog_count() -> int:
    """Number of un-ingested event lines currently in the spool (for health)."""
    n = 0
    for f in event_files():
        with open(f, encoding="utf-8") as fh:
            n += sum(1 for ln in fh if ln.strip())
    return n


def dlq_count() -> int:
    f = _dlq_file()
    if not f.exists():
        return 0
    with open(f, encoding="utf-8") as fh:
        return sum(1 for ln in fh if ln.strip())
