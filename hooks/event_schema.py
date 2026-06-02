#!/usr/bin/env python3
"""Phase B — canonical event schema + versioned read-time upcasting.

Single source of truth for the spooled-event schema. Two jobs:

1. **Versioning + upcasting.** Every spool record carries `schema_version`. The
   ingest worker calls `upcast()` before writing, running a v1→v2→… transformer
   chain so a record written by an OLD hook still ingests correctly after the
   schema moves on. Old spool records are never rewritten in place — they're
   normalized on read. Adding a future version = bump SCHEMA_VERSION and register
   one transformer; nothing downstream changes.

2. **OTel GenAI alignment.** `GEN_AI_FIELD_MAP` maps njhook's stored event fields
   to OpenTelemetry GenAI semantic-convention attribute names, and `to_gen_ai()`
   renders an event in that vocabulary — the canonical interchange view for any
   future OTel exporter, without renaming the stored properties (which recall,
   dream, and the dashboard read by their njhook names).

v1→v2: `app_id` (the multi-tenant / OTel `gen_ai.app.id` attribute) becomes a
first-class Event property. In v1 it lived only in the spool envelope and was
dropped on ingest; v2 folds it into `event_props` so every event carries it.
"""
from __future__ import annotations

SCHEMA_VERSION = 2

# njhook stored field  →  OpenTelemetry GenAI semantic-convention attribute.
# Closed vocabulary; the canonical interchange view (see to_gen_ai). Approximates
# the OTel GenAI semconv — documented as such, stable for exporters to target.
GEN_AI_FIELD_MAP = {
    "client":        "gen_ai.system",
    "app_id":        "gen_ai.app.id",
    "model":         "gen_ai.request.model",
    "event_name":    "gen_ai.operation.name",
    "tool_name":     "gen_ai.tool.name",
    "tool_input":    "gen_ai.tool.input",
    "prompt":        "gen_ai.prompt",
    "tool_response": "gen_ai.completion",
}

# Fields carried through to_gen_ai() unchanged (identity / timing).
_PASSTHROUGH = ("event_id", "timestamp")


def _v1_to_v2(record: dict) -> dict:
    """v1→v2: promote `app_id` from the envelope into `event_props` so it's stored
    on the Event node. Falls back to the record's client, then 'unknown'."""
    rec = dict(record)
    ep = dict(rec.get("event_props") or {})
    if not ep.get("app_id"):
        ep["app_id"] = rec.get("app_id") or ep.get("client") or rec.get("client") or "unknown"
    rec["event_props"] = ep
    rec["schema_version"] = 2
    return rec


# version N → transformer producing version N+1
_UPCASTERS = {1: _v1_to_v2}


def upcast(record: dict) -> dict:
    """Normalize a spool record to the current SCHEMA_VERSION by applying the
    transformer chain. A record with no `schema_version` is treated as v1.
    Idempotent for already-current records. Never mutates the input."""
    rec = dict(record)
    try:
        v = int(rec.get("schema_version") or 1)
    except (TypeError, ValueError):
        v = 1
    while v < SCHEMA_VERSION:
        fn = _UPCASTERS.get(v)
        if fn is None:
            break  # no path forward; leave as-is rather than guess
        rec = fn(rec)
        try:
            v = int(rec.get("schema_version") or (v + 1))
        except (TypeError, ValueError):
            v = v + 1
    return rec


def to_gen_ai(event_props: dict) -> dict:
    """Render a stored event in OTel GenAI attribute vocabulary — the canonical
    interchange view. Only mapped fields (present + non-None) are emitted, plus
    the identity/timing passthrough fields."""
    out: dict = {}
    for k in _PASSTHROUGH:
        if event_props.get(k) is not None:
            out[k] = event_props[k]
    for njhook_field, otel_attr in GEN_AI_FIELD_MAP.items():
        v = event_props.get(njhook_field)
        if v is not None:
            out[otel_attr] = v
    return out
