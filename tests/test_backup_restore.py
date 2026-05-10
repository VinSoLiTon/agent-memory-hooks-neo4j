"""Backup → restore round-trip integration test.

Seeds a small graph through the live capture path (3 events for one Session,
1 Memory), runs `njhook backup --with-sessions`, then DELETEs the seeded
nodes and runs `njhook restore` on the JSON. Asserts that the restored
graph matches: memory upserts cleanly, session re-appears with the right
session_key, events are chronologically ordered.

Requires a running Neo4j (same one the hooks talk to). Run after
test_hooks.py to confirm capture + backup are in sync.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
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

SID = f"backuptest-{int(time.time())}-{uuid.uuid4().hex[:6]}"
MEMORY_PATH = f"general/backuptest-{uuid.uuid4().hex[:6]}.md"
MEMORY_BODY = (
    "---\n"
    "title: Backup round-trip fixture\n"
    "kind: general\n"
    "---\n\n"
    "Synthetic memory used by tests/test_backup_restore.py. Should round-trip "
    "cleanly through `njhook backup` and `njhook restore`.\n"
)


def _driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _fire(event: dict) -> None:
    p = subprocess.run(
        ["python", str(HOOK), "--client", "claude_code"],
        input=json.dumps(event), capture_output=True, text=True,
    )
    if p.returncode:
        raise RuntimeError(f"hook failed: {p.stderr}")
    time.sleep(0.02)  # ensure timestamps are strictly ordered


def seed():
    cwd = str(REPO)
    for name in ("SessionStart", "UserPromptSubmit", "Stop"):
        _fire({
            "session_id": SID, "hook_event_name": name, "cwd": cwd,
            **({"prompt": "round-trip test prompt"} if name == "UserPromptSubmit" else {}),
        })
    # And a Memory the same way write_memories would.
    with _driver() as d, d.session() as s:
        s.run(
            "MERGE (m:Memory {path: $p}) SET m.content = $c, m.updated_at = $t, m.project = 'njhook'",
            parameters={"p": MEMORY_PATH, "c": MEMORY_BODY, "t": "2026-05-10T00:00:00+00:00"},
        )


def cleanup():
    with _driver() as d, d.session() as s:
        s.run("MATCH (s:Session {session_key: $k}) DETACH DELETE s",
              parameters={"k": f"claude_code:{SID}"})
        s.run("MATCH (e:Event) WHERE e.event_id CONTAINS $sid DETACH DELETE e",
              parameters={"sid": SID})
        s.run("MATCH (m:Memory {path: $p}) DETACH DELETE m", parameters={"p": MEMORY_PATH})


def run_backup(out: Path) -> dict:
    p = subprocess.run(
        ["python", str(NJHOOK), "backup",
         "--out", str(out), "--with-sessions",
         "--max-field-chars", "500"],
        capture_output=True, text=True,
    )
    assert p.returncode == 0, f"backup failed: {p.stderr}"
    return json.loads(out.read_text(encoding="utf-8"))


def run_restore(in_: Path) -> None:
    p = subprocess.run(
        ["python", str(NJHOOK), "restore", "--in", str(in_)],
        capture_output=True, text=True,
    )
    assert p.returncode == 0, f"restore failed: {p.stderr}"


def graph_state() -> tuple[dict, dict]:
    """Snapshot the seeded entities to compare pre- vs post-restore."""
    with _driver() as d, d.session() as s:
        m = s.run(
            "MATCH (m:Memory {path: $p}) "
            "RETURN m.path AS path, m.content AS content, m.project AS project",
            parameters={"p": MEMORY_PATH},
        ).single()
        sess_row = s.run(
            "MATCH (s:Session {session_key: $k})-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
            "RETURN s.session_key AS sk, s.session_id AS sid, s.client AS client, "
            "       e.event_name AS name, e.timestamp AS ts "
            "ORDER BY e.timestamp",
            parameters={"k": f"claude_code:{SID}"},
        ).data()
    return (
        dict(m) if m else {},
        {
            "session_key": sess_row[0]["sk"] if sess_row else None,
            "session_id": sess_row[0]["sid"] if sess_row else None,
            "client": sess_row[0]["client"] if sess_row else None,
            "events": [(r["name"], r["ts"]) for r in sess_row],
        }
    )


def main() -> int:
    failures: list[str] = []
    out = Path(os.environ.get("TEMP", "/tmp")) / f"njhook-rt-{SID}.json"
    try:
        cleanup()  # belt + suspenders if a prior run died half-way
        seed()
        before_mem, before_sess = graph_state()
        assert before_mem.get("path") == MEMORY_PATH, "seed: memory not in graph"
        assert before_sess["session_key"] == f"claude_code:{SID}", "seed: session not keyed correctly"
        assert len(before_sess["events"]) == 3, f"seed: expected 3 events, got {len(before_sess['events'])}"
        ts = [e[1] for e in before_sess["events"]]
        assert ts == sorted(ts), "seed: events not chronologically ordered"

        backup = run_backup(out)
        assert any(m["path"] == MEMORY_PATH for m in backup["memories"]), \
            "backup: missing seeded memory"
        sess_in_backup = next((s for s in backup["sessions"]
                               if s["session_key"] == f"claude_code:{SID}"), None)
        assert sess_in_backup is not None, "backup: missing seeded session"
        backup_event_ts = [e["timestamp"] for e in sess_in_backup["events"]]
        assert backup_event_ts == sorted(backup_event_ts), \
            f"backup: events out of order: {backup_event_ts}"

        # Wipe and restore.
        cleanup()
        wiped_mem, wiped_sess = graph_state()
        assert not wiped_mem, "cleanup: memory still present"
        assert not wiped_sess["events"], "cleanup: events still present"

        run_restore(out)
        after_mem, after_sess = graph_state()
        if after_mem.get("path") != MEMORY_PATH:
            failures.append(f"restore: memory missing, got {after_mem!r}")
        if after_mem.get("content") != MEMORY_BODY:
            failures.append("restore: memory content mismatch")
        if after_sess["session_key"] != f"claude_code:{SID}":
            failures.append(f"restore: session_key mismatch, got {after_sess['session_key']!r}")
        if len(after_sess["events"]) != 3:
            failures.append(f"restore: expected 3 events, got {len(after_sess['events'])}")
        ts_after = [e[1] for e in after_sess["events"]]
        if ts_after != sorted(ts_after):
            failures.append(f"restore: events out of order: {ts_after}")
    finally:
        cleanup()
        try:
            out.unlink()
        except FileNotFoundError:
            pass

    if failures:
        for f in failures:
            print(f"  FAIL {f}")
        print(f"\n{len(failures)} failure(s)")
        return 1
    print("All backup/restore round-trip tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
