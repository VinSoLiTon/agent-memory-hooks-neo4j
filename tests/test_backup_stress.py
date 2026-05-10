"""Backup stress regression — confirm a session with a multi-megabyte
tool_response can be backed up safely.

Previously, `collect(properties(e))` materialized full event payloads in
Neo4j before any Python-side trimming, which OOM'd the DB on real graphs.
The streaming-export rewrite (PR-I) does field projection in Cypher so
oversized fields are either dropped server-side (--no-tool-response) or
substring()'d server-side (--max-field-chars).

This test seeds a 5 MB tool_response, then verifies:

  - backup completes (does not OOM)
  - --no-tool-response: tool_response field absent from output
  - --max-field-chars 1000: tool_response truncated, other fields intact
  - default --with-sessions --session-key produces a usable JSON

Cleans up the seeded session on exit.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path

from neo4j import GraphDatabase

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "log_event.py"
NJHOOK = REPO / "cli" / "njhook.py"

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

SID = f"stress-{int(time.time())}-{uuid.uuid4().hex[:6]}"
HUGE_RESPONSE = "a" * (5 * 1024 * 1024)  # 5 MiB of repeating ASCII


def _driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _fire(event: dict) -> None:
    p = subprocess.run(
        ["python", str(HOOK), "--client", "claude_code"],
        input=json.dumps(event), capture_output=True, text=True,
    )
    if p.returncode:
        raise RuntimeError(f"hook failed: {p.stderr}")
    time.sleep(0.02)


def seed():
    cwd = str(REPO)
    _fire({"session_id": SID, "hook_event_name": "SessionStart", "cwd": cwd})
    _fire({"session_id": SID, "hook_event_name": "PreToolUse", "cwd": cwd,
           "tool_name": "Bash", "tool_input": {"command": "stress test"}})
    # Bypass the hook's truncation cap for tool_response by writing the huge
    # blob directly. The hook caps at MAX_RESPONSE_CHARS; we want to confirm
    # backup is safe even if a future hook change lets large fields through.
    with _driver() as d, d.session() as s:
        ts = "2026-05-10T20:00:00.000+00:00"
        eid = f"claude_code_{SID}_{ts}_PostToolUse"
        s.run(
            "MATCH (sess:Session {session_key: $sk}) "
            "CREATE (e:Event {event_id: $eid, event_name: 'PostToolUse', "
            "                 client: 'claude_code', timestamp: $ts, "
            "                 cwd: $cwd, tool_name: 'Bash', "
            "                 tool_response: $resp}) "
            "WITH sess, e "
            "OPTIONAL MATCH (sess)-[old:LATEST_EVENT]->(prev:Event) "
            "DELETE old "
            "WITH sess, e, prev "
            "FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END | "
            "    CREATE (prev)-[:NEXT]->(e)) "
            "CREATE (sess)-[:LATEST_EVENT]->(e)",
            parameters={
                "sk": f"claude_code:{SID}", "eid": eid, "ts": ts,
                "cwd": cwd, "resp": HUGE_RESPONSE,
            },
        )


def cleanup():
    with _driver() as d, d.session() as s:
        s.run(
            "MATCH (sess:Session {session_key: $sk})-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
            "DETACH DELETE e",
            parameters={"sk": f"claude_code:{SID}"},
        )
        s.run(
            "MATCH (sess:Session {session_key: $sk}) DETACH DELETE sess",
            parameters={"sk": f"claude_code:{SID}"},
        )


def run_backup(out: Path, *extra: str) -> dict:
    cmd = ["python", str(NJHOOK), "backup", "--out", str(out),
           "--with-sessions", "--session-key", f"claude_code:{SID}", *extra]
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, f"backup failed (rc={p.returncode}): {p.stderr}"
    return json.loads(out.read_text(encoding="utf-8"))


def main() -> int:
    failures: list[str] = []
    tmp = Path(os.environ.get("TEMP", "/tmp"))
    no_tr = tmp / f"njhook-stress-no-tr-{SID}.json"
    capped = tmp / f"njhook-stress-capped-{SID}.json"
    full = tmp / f"njhook-stress-full-{SID}.json"

    try:
        cleanup()
        seed()

        # 1) --no-tool-response: tool_response should not appear at all,
        #    AND the file size must be small.
        b1 = run_backup(no_tr, "--no-tool-response")
        sz1 = no_tr.stat().st_size
        sess = next((s for s in b1["sessions"] if s["session_key"] == f"claude_code:{SID}"), None)
        if sess is None:
            failures.append("--no-tool-response: session not in backup")
        else:
            for e in sess["events"]:
                if "tool_response" in e:
                    failures.append(f"--no-tool-response: tool_response leaked into {e.get('event_name')}")
                    break
        if sz1 > 64 * 1024:
            failures.append(f"--no-tool-response: file should be tiny (got {sz1} bytes — tool_response must have leaked)")

        # 2) --max-field-chars 1000: tool_response is truncated, kept fields intact.
        b2 = run_backup(capped, "--max-field-chars", "1000")
        sz2 = capped.stat().st_size
        sess2 = next((s for s in b2["sessions"] if s["session_key"] == f"claude_code:{SID}"), None)
        if sess2 is None:
            failures.append("--max-field-chars: session missing")
        else:
            tr_event = next((e for e in sess2["events"] if e.get("event_name") == "PostToolUse"), None)
            if tr_event is None:
                failures.append("--max-field-chars: PostToolUse event missing")
            else:
                tr = tr_event.get("tool_response") or ""
                if len(tr) > 2000:  # truncated to 1000 + suffix; allow some slack
                    failures.append(f"--max-field-chars 1000: tool_response not truncated server-side (got {len(tr)} chars)")
                if "...[truncated]" not in tr:
                    failures.append(f"--max-field-chars: missing truncation marker on huge field")
        if sz2 > 64 * 1024:
            failures.append(f"--max-field-chars 1000: file too big ({sz2} bytes)")

        # 3) Default --with-sessions --session-key (no trimming knobs):
        #    must still complete (this is the previously-OOMing case).
        b3 = run_backup(full)
        sess3 = next((s for s in b3["sessions"] if s["session_key"] == f"claude_code:{SID}"), None)
        if sess3 is None:
            failures.append("default scoped backup: session missing")
        else:
            tr_event = next((e for e in sess3["events"] if e.get("event_name") == "PostToolUse"), None)
            if tr_event and len(tr_event.get("tool_response") or "") < len(HUGE_RESPONSE) - 1024:
                failures.append("default backup: full tool_response should be present (no trimming requested)")
    finally:
        cleanup()
        for p in (no_tr, capped, full):
            try: p.unlink()
            except FileNotFoundError: pass

    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("All backup-stress tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
