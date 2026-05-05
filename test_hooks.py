"""
Test that the hook script correctly creates a linked list of events in Neo4j.

Requires a running Neo4j instance. Set NEO4J_URI, NEO4J_USER, NEO4J_PASSWORD env vars.
"""

import json
import subprocess
import os
import time

from neo4j import GraphDatabase

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

HOOK_SCRIPT = os.path.join(os.path.dirname(__file__), ".claude", "hooks", "log_event.py")


def run_hook(event_data: dict):
    result = subprocess.run(
        ["python3", HOOK_SCRIPT],
        input=json.dumps(event_data),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Hook failed: {result.stderr}")


def test_linked_list():
    driver = GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))
    session_id = f"test_session_{int(time.time())}"

    # Clean up any previous test data
    with driver.session() as session:
        session.run("MATCH (s:Session {session_id: $sid}) DETACH DELETE s", sid=session_id)

    # Simulate a sequence of events
    events = [
        {"session_id": session_id, "hook_event_name": "SessionStart", "cwd": "/tmp", "model": "claude-sonnet-4-6", "source": "startup"},
        {"session_id": session_id, "hook_event_name": "UserPromptSubmit", "cwd": "/tmp", "prompt": "hello world"},
        {"session_id": session_id, "hook_event_name": "PreToolUse", "cwd": "/tmp", "tool_name": "Bash", "tool_input": {"command": "ls"}},
        {"session_id": session_id, "hook_event_name": "PostToolUse", "cwd": "/tmp", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_response": {"stdout": "file1.txt\nfile2.txt", "exit_code": 0}},
        {"session_id": session_id, "hook_event_name": "Stop", "cwd": "/tmp"},
    ]

    for event in events:
        run_hook(event)
        time.sleep(0.01)  # small delay so timestamps differ

    # Verify the linked list structure
    with driver.session() as session:
        # Check session exists
        result = session.run(
            "MATCH (s:Session {session_id: $sid}) RETURN s",
            sid=session_id,
        )
        record = result.single()
        assert record is not None, "Session node not created"

        # Check FIRST_EVENT points to SessionStart
        result = session.run(
            "MATCH (s:Session {session_id: $sid})-[:FIRST_EVENT]->(e:Event) RETURN e.event_name AS name",
            sid=session_id,
        )
        record = result.single()
        assert record["name"] == "SessionStart", f"First event should be SessionStart, got {record['name']}"

        # Check LATEST_EVENT points to Stop
        result = session.run(
            "MATCH (s:Session {session_id: $sid})-[:LATEST_EVENT]->(e:Event) RETURN e.event_name AS name",
            sid=session_id,
        )
        record = result.single()
        assert record["name"] == "Stop", f"Latest event should be Stop, got {record['name']}"

        # Walk the full linked list via NEXT relationships
        result = session.run(
            """
            MATCH (s:Session {session_id: $sid})-[:FIRST_EVENT]->(first:Event)
            MATCH path = (first)-[:NEXT*0..]->(end)
            WHERE NOT (end)-[:NEXT]->()
            RETURN [n IN nodes(path) | n.event_name] AS chain
            """,
            sid=session_id,
        )
        record = result.single()
        chain = record["chain"]
        expected = ["SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"]
        assert chain == expected, f"Expected {expected}, got {chain}"

        # Count total events
        result = session.run(
            "MATCH (s:Session {session_id: $sid})-[:FIRST_EVENT]->(first) MATCH (first)-[:NEXT*0..]->(e) RETURN count(e) AS cnt",
            sid=session_id,
        )
        assert result.single()["cnt"] == 5

        # Verify tool_response was stored on the PostToolUse event
        result = session.run(
            """
            MATCH (s:Session {session_id: $sid})-[:FIRST_EVENT]->(:Event)-[:NEXT*0..]->(e:Event {event_name: 'PostToolUse'})
            RETURN e.tool_response AS resp
            """,
            sid=session_id,
        )
        resp = result.single()["resp"]
        assert resp is not None and "file1.txt" in resp, f"tool_response not stored correctly: {resp!r}"

    # Clean up
    with driver.session() as session:
        session.run(
            "MATCH (s:Session {session_id: $sid})-[*]->(e:Event) DETACH DELETE e, s",
            sid=session_id,
        )

    driver.close()
    print("All tests passed! Linked list structure verified.")


if __name__ == "__main__":
    test_linked_list()
