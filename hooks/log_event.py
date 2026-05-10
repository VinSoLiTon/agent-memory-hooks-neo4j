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
from privacy import is_optout, scrub  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


MAX_RESPONSE_CHARS = 4000

# M2: transcript capture is OFF by default. Opt-in via HOOKS_CAPTURE_TRANSCRIPT=1.
# When enabled, transcripts are still capped at HOOKS_TRANSCRIPT_MAX_CHARS to
# prevent multi-MB blobs from bloating the graph. Transcripts are duplicate
# data (every event is already stored individually); the on-by-default capture
# was costing storage and scrub time for ~no marginal value.
CAPTURE_TRANSCRIPT = os.environ.get("HOOKS_CAPTURE_TRANSCRIPT") == "1"
TRANSCRIPT_MAX_CHARS = int(os.environ.get("HOOKS_TRANSCRIPT_MAX_CHARS", "20000"))


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


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


def ensure_constraints(tx):
    """Schema-only setup. Neo4j forbids mixing schema and data ops in one tx,
    so the H1 data backfill happens separately in `_backfill_session_keys`."""
    # H1: drop the legacy single-property UNIQUE constraint on session_id, if
    # present, so different clients can reuse a session_id without collision.
    try:
        for record in tx.run("SHOW CONSTRAINTS YIELD name, labelsOrTypes, properties, type"):
            labels = record.get("labelsOrTypes") or []
            props = record.get("properties") or []
            ctype = (record.get("type") or "").upper()
            if "Session" in labels and props == ["session_id"] and "UNIQUE" in ctype:
                tx.run(f"DROP CONSTRAINT `{record['name']}`")
    except Exception:
        # SHOW CONSTRAINTS not available; harmless on graphs that never had it.
        pass
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Session) REQUIRE s.session_key IS UNIQUE")
    tx.run("CREATE INDEX session_id_lookup IF NOT EXISTS FOR (s:Session) ON (s.session_id)")
    tx.run("CREATE CONSTRAINT IF NOT EXISTS FOR (e:Event) REQUIRE e.event_id IS UNIQUE")
    tx.run("CREATE FULLTEXT INDEX memory_fulltext IF NOT EXISTS FOR (m:Memory) ON EACH [m.content, m.path]")
    tx.run("CREATE INDEX memory_project IF NOT EXISTS FOR (m:Memory) ON (m.project)")


def _backfill_session_keys(tx):
    """H1 data migration: heal pre-PR-B Sessions that lack a session_key.
    Idempotent — no-op once everything is keyed."""
    tx.run(
        "MATCH (s:Session) WHERE s.session_key IS NULL "
        "SET s.session_key = coalesce(s.client, 'unknown') + ':' + coalesce(s.session_id, 'unknown')"
    )


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
    }
    event_props = {k: v for k, v in event_props.items() if v is not None}

    driver = get_driver()
    with driver.session() as session:
        session.execute_write(ensure_constraints)
        # H1 backfill must run in its own tx (Neo4j forbids mixing schema +
        # data writes); it's a no-op once all sessions have session_key set.
        session.execute_write(_backfill_session_keys)
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
