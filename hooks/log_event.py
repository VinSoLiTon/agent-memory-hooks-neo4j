#!/usr/bin/env python3
"""
Shared hook script that logs events to Neo4j as a linked list per session.

Used by both Claude Code (.claude/hooks/) and Codex (.codex/hooks/) — pass
--client to tag the originating tool. The hook payloads from the two clients
share almost all fields (session_id, hook_event_name, transcript_path, cwd,
tool_name, tool_input, tool_response, prompt, model, source); Codex adds
turn_id, tool_use_id, last_assistant_message, stop_hook_active.

Graph: (Session)-[:FIRST_EVENT]->(Event)-[:NEXT]->(Event)->...
       (Session)-[:LATEST_EVENT]->(last Event)
"""

import argparse
import json
import sys
import os
from datetime import datetime, timezone

from neo4j import GraphDatabase

# privacy.py lives next to this script; allow direct invocation regardless of cwd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from privacy import is_optout, scrub, sensitivity_for  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

# Phase B: capture mode. "direct" (default) writes the event straight to Neo4j on
# the hook hot path (legacy behaviour). "spool" appends a durable JSONL record and
# returns immediately; the `njhook ingest` worker drains it into Neo4j later. Spool
# mode means capture never silently fails when Neo4j is down — at the cost of
# needing the ingest worker scheduled. Flip the default to "spool" once ingest runs.
CAPTURE_MODE = os.environ.get("HOOKS_CAPTURE_MODE", "direct").lower()
from event_schema import SCHEMA_VERSION  # noqa: E402  single source of truth (Phase B PR-2)


MAX_RESPONSE_CHARS = 4000

# M2: transcript capture is OFF by default. Opt-in via HOOKS_CAPTURE_TRANSCRIPT=1.
# When enabled, transcripts are still capped at HOOKS_TRANSCRIPT_MAX_CHARS to
# prevent multi-MB blobs from bloating the graph. Transcripts are duplicate
# data (every event is already stored individually); the on-by-default capture
# was costing storage and scrub time for ~no marginal value.
CAPTURE_TRANSCRIPT = os.environ.get("HOOKS_CAPTURE_TRANSCRIPT") == "1"
TRANSCRIPT_MAX_CHARS = int(os.environ.get("HOOKS_TRANSCRIPT_MAX_CHARS", "20000"))


def get_driver():
    # PR-G #2: silence harmless "property does not exist" notifications.
    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )


def _serialize_tool_response(value) -> str:
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    if len(text) > MAX_RESPONSE_CHARS:
        text = text[:MAX_RESPONSE_CHARS] + f"...[truncated {len(text) - MAX_RESPONSE_CHARS} chars]"
    return text


def _read_transcript(path):
    if not path or not CAPTURE_TRANSCRIPT:
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        if len(text) > TRANSCRIPT_MAX_CHARS:
            text = text[:TRANSCRIPT_MAX_CHARS] + f"...[truncated {len(text) - TRANSCRIPT_MAX_CHARS} chars]"
        return text
    except Exception:
        return None


def ensure_minimal_constraints(tx):
    """PR-F #4: hot-path schema is now minimal — only the UNIQUE constraints
    the MERGE statements depend on. Heavier work (legacy-constraint drops,
    index creation, data backfills) lives in hooks/schema.py and runs from
    `njhook migrate`. Each `CREATE CONSTRAINT IF NOT EXISTS` is a single
    round-trip and a no-op when the constraint already exists.
    """
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Session) REQUIRE s.session_key IS UNIQUE")
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Event) REQUIRE e.event_id IS UNIQUE")


def log_event(data: dict, client: str):
    cwd = data.get("cwd")
    if is_optout(cwd):
        # User has marked this directory off-limits. Drop the event silently.
        return

    session_id = data.get("session_id", "unknown")
    event_name = data.get("hook_event_name", "unknown")
    timestamp = datetime.now(timezone.utc).isoformat()

    # Namespace the event_id by client so different clients can't collide on
    # the same session_id.
    event_id = f"{client}_{session_id}_{timestamp}_{event_name}"

    # Scrub user-content fields before serializing. Structured fields (tool_input
    # is a dict; tool_response is dict-or-string) get scrubbed after stringification.
    tool_input = data.get("tool_input")
    tool_response = data.get("tool_response")
    event_props = {
        "event_id": event_id,
        "event_name": event_name,
        "client": client,
        # Phase B PR-2 (schema v2): app_id (OTel gen_ai.app.id — the multi-tenant
        # key) is a first-class Event property. Defaults to the source client.
        "app_id": data.get("app_id") or client,
        "timestamp": timestamp,
        "cwd": cwd,
        "tool_name": data.get("tool_name"),
        "tool_use_id": data.get("tool_use_id"),
        # M1: empty dicts / strings are falsy in Python but legitimate hook
        # payloads — distinguish "field missing" (None) from "field present but
        # empty" (capture it as-is).
        "tool_input": scrub(json.dumps(tool_input)) if tool_input is not None else None,
        "tool_response": scrub(_serialize_tool_response(tool_response))
        if tool_response is not None
        else None,
        "prompt": scrub(data.get("prompt")),
        "model": data.get("model"),
        "source": data.get("source"),
        "turn_id": data.get("turn_id"),
        "last_assistant_message": scrub(data.get("last_assistant_message")),
        "stop_hook_active": data.get("stop_hook_active"),
        "transcript_path": data.get("transcript_path"),
        "transcript": scrub(_read_transcript(data.get("transcript_path"))),
        # Phase H: tag high-sensitivity events (cwd under HOOKS_SENSITIVE_PATHS) so
        # the dream phase can keep them off remote providers. Only 'high' is stored;
        # 'normal' is the implicit default (stays None → filtered out below).
        "sensitivity": (lambda s: s if s == "high" else None)(sensitivity_for(cwd)),
    }
    event_props = {k: v for k, v in event_props.items() if v is not None}

    # Phase B: in spool mode, append a durable record and return — the ingest
    # worker writes it to Neo4j later. Keeps the hot path bounded and lossless
    # even when Neo4j is unavailable.
    if CAPTURE_MODE == "spool":
        import spool
        spool.append(
            {
                "schema_version": SCHEMA_VERSION,
                "client": client,
                "session_id": session_id,
                "app_id": data.get("app_id") or client,
                "event_props": event_props,
            },
            day=(timestamp or "")[:10] or "unknown",
        )
        return

    driver = get_driver()
    with driver.session() as session:
        # PR-F #4: only the two MERGE-supporting UNIQUE constraints. The full
        # migration (legacy-constraint drops, indexes, data backfills) ran via
        # `njhook migrate`, not here.
        session.execute_write(ensure_minimal_constraints)
        session.execute_write(_append_event, session_id, client, event_props)
    driver.close()


def _append_event(tx, session_id: str, client: str, event_props: dict):
    # H1: MERGE on session_key (composite) so two clients with the same raw
    # session_id don't collide. session_id and client remain as properties.
    session_key = f"{client}:{session_id}"
    tx.run(
        """
        MERGE (s:Session {session_key: $session_key})
        ON CREATE SET s.created_at = $timestamp, s.session_id = $session_id, s.client = $client
        SET s.session_id = coalesce(s.session_id, $session_id),
            s.client = coalesce(s.client, $client)
        WITH s
        CREATE (e:Event $event_props)
        WITH s, e
        OPTIONAL MATCH (s)-[old_latest:LATEST_EVENT]->(prev:Event)
        DELETE old_latest
        WITH s, e, prev
        FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END |
            CREATE (prev)-[:NEXT]->(e)
        )
        FOREACH (_ IN CASE WHEN prev IS NULL THEN [1] ELSE [] END |
            CREATE (s)-[:FIRST_EVENT]->(e)
        )
        CREATE (s)-[:LATEST_EVENT]->(e)
        """,
        session_key=session_key,
        session_id=session_id,
        client=client,
        timestamp=event_props.get("timestamp"),
        event_props=event_props,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True, choices=["claude_code", "codex", "cursor", "gemini"])
    args = parser.parse_args()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        log_event(data, client=args.client)
    except Exception as e:
        print(f"Hook error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
