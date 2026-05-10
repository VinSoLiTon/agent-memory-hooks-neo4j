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
    # PR-I #3: scope the export to JUST the seeded session via --session-key
    # so the test doesn't OOM on the user's live historical graph. Also cap
    # tool_response fields server-side so a runaway transcript on some other
    # captured session can't poison this test (defense in depth).
    p = subprocess.run(
        ["python", str(NJHOOK), "backup",
         "--out", str(out), "--with-sessions",
         "--session-key", f"claude_code:{SID}",
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

    # ---------------------------------------------------------------------
    # PR-J #1: restore must be safe when the backup is SHORTER than the
    # current state. Seed the session again, run a backup of just 3 events,
    # then add a 4th event directly to Neo4j, then restore the 3-event
    # backup. The 4th event must be GONE — the previous code left it
    # reachable through the old NEXT chain.
    # ---------------------------------------------------------------------
    out = Path(os.environ.get("TEMP", "/tmp")) / f"njhook-rt2-{SID}.json"
    try:
        cleanup()
        seed()
        backup = run_backup(out)  # captures the 3-event session
        # Inject a 4th event directly into the chain.
        with _driver() as d, d.session() as s:
            extra_eid = f"claude_code_{SID}_2026-99_extra"
            s.run(
                "MATCH (sess:Session {session_key: $sk}) "
                "OPTIONAL MATCH (sess)-[old:LATEST_EVENT]->(prev:Event) "
                "DELETE old "
                "WITH sess, prev "
                "CREATE (e:Event {event_id: $eid, event_name: 'Extra', "
                "                 client: 'claude_code', timestamp: '2099-01-01T00:00:00+00:00'}) "
                "FOREACH (_ IN CASE WHEN prev IS NOT NULL THEN [1] ELSE [] END | "
                "    CREATE (prev)-[:NEXT]->(e)) "
                "MERGE (sess)-[:LATEST_EVENT]->(e)",
                parameters={"sk": f"claude_code:{SID}", "eid": extra_eid},
            )
        # Now restore the older 3-event backup.
        run_restore(out)
        with _driver() as d, d.session() as s:
            row = s.run(
                "MATCH (sess:Session {session_key: $sk})-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
                "RETURN count(DISTINCT e) AS n",
                parameters={"sk": f"claude_code:{SID}"},
            ).single()
            still_there = s.run(
                "MATCH (e:Event {event_id: $eid}) RETURN count(e) AS n",
                parameters={"eid": extra_eid},
            ).single()
        if row["n"] != 3:
            failures.append(f"changed-backup restore: expected 3 events, got {row['n']} (stale tail not pruned)")
        if still_there["n"] != 0:
            failures.append("changed-backup restore: extra Event node still exists in graph after restore")
    finally:
        cleanup()
        try:
            out.unlink()
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------------
    # PR-K: restoring a backup whose session has events:[] must clear the
    # existing chain. Previously the wipe sat inside `if events:`, so
    # empty-event restore preserved stale data and the graph didn't match
    # the backup.
    # ---------------------------------------------------------------------
    out2 = Path(os.environ.get("TEMP", "/tmp")) / f"njhook-rt3-{SID}.json"
    try:
        cleanup()
        seed()
        backup = run_backup(out2)  # 3-event session
        # Hand-edit the JSON so this session has no events.
        for sess in backup["sessions"]:
            if sess["session_key"] == f"claude_code:{SID}":
                sess["events"] = []
        out2.write_text(json.dumps(backup, indent=2), encoding="utf-8")
        run_restore(out2)
        with _driver() as d, d.session() as s:
            row = s.run(
                "MATCH (sess:Session {session_key: $sk}) "
                "OPTIONAL MATCH (sess)-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
                "RETURN count(DISTINCT e) AS n",
                parameters={"sk": f"claude_code:{SID}"},
            ).single()
            rels = s.run(
                "MATCH (sess:Session {session_key: $sk})-[r:FIRST_EVENT|LATEST_EVENT]->() "
                "RETURN count(r) AS n",
                parameters={"sk": f"claude_code:{SID}"},
            ).single()
        if row["n"] != 0:
            failures.append(f"empty-events restore: expected 0 events, got {row['n']} (stale chain not pruned)")
        if rels["n"] != 0:
            failures.append(f"empty-events restore: FIRST_EVENT/LATEST_EVENT relationships still attached (got {rels['n']})")
    finally:
        cleanup()
        try:
            out2.unlink()
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------------
    # PR-L: restore must reject a malformed backup up front rather than
    # half-write it. Build a backup whose first event is missing event_id
    # (the worst case — would skip FIRST_EVENT and leave the chain broken)
    # and one whose memory is missing 'path'. Both must abort with rc=2.
    # ---------------------------------------------------------------------
    bad = Path(os.environ.get("TEMP", "/tmp")) / f"njhook-bad-{SID}.json"
    try:
        cleanup()
        seed()
        backup = run_backup(bad)
        # Mutilate: drop event_id from the first event of our session.
        for sess in backup["sessions"]:
            if sess["session_key"] == f"claude_code:{SID}" and sess["events"]:
                sess["events"][0].pop("event_id", None)
                break
        bad.write_text(json.dumps(backup, indent=2), encoding="utf-8")

        p = subprocess.run(
            ["python", str(NJHOOK), "restore", "--in", str(bad)],
            capture_output=True, text=True,
        )
        if p.returncode != 2:
            failures.append(
                f"malformed backup: expected rc=2 abort, got rc={p.returncode}; "
                f"stderr={p.stderr[:200]!r}"
            )
        if "missing 'event_id'" not in p.stderr:
            failures.append(f"malformed backup: missing event_id error not surfaced: {p.stderr[:200]!r}")

        # Same backup with --allow-malformed should succeed (skipping the bad event).
        p2 = subprocess.run(
            ["python", str(NJHOOK), "restore", "--in", str(bad), "--allow-malformed"],
            capture_output=True, text=True,
        )
        if p2.returncode != 0:
            failures.append(f"--allow-malformed: expected rc=0, got {p2.returncode}; stderr={p2.stderr[:200]!r}")
    finally:
        cleanup()
        try:
            bad.unlink()
        except FileNotFoundError:
            pass

    # ---------------------------------------------------------------------
    # PR-M: --allow-malformed must SKIP bad records, not crash on them and
    # not invent `unknown:unknown` sentinel session keys.
    # ---------------------------------------------------------------------
    bad2 = Path(os.environ.get("TEMP", "/tmp")) / f"njhook-bad2-{SID}.json"
    try:
        cleanup()
        seed()
        backup = run_backup(bad2)
        # 1) Inject a memory with no path.
        backup["memories"].append({"content": "orphan memory body without a path"})
        # 2) Inject a session with no session_key, client, OR session_id —
        #    must NOT create unknown:unknown.
        backup["sessions"].append({"events": []})
        bad2.write_text(json.dumps(backup, indent=2), encoding="utf-8")

        # Pre-condition: no Session with key 'unknown:unknown' exists yet.
        with _driver() as d, d.session() as s:
            pre = s.run(
                "MATCH (s:Session {session_key: 'unknown:unknown'}) RETURN count(s) AS n"
            ).single()
        assert pre["n"] == 0, "test setup: unknown:unknown session pre-existed"

        p3 = subprocess.run(
            ["python", str(NJHOOK), "restore", "--in", str(bad2), "--allow-malformed"],
            capture_output=True, text=True,
        )
        # Must not crash with KeyError on 'path'.
        if p3.returncode != 0:
            failures.append(
                f"--allow-malformed crashed on missing path/id: rc={p3.returncode}; "
                f"stderr={p3.stderr[:300]!r}"
            )
        if "KeyError" in p3.stderr:
            failures.append(f"--allow-malformed leaked KeyError: {p3.stderr[:300]!r}")
        if "skipping" not in p3.stderr:
            failures.append(f"--allow-malformed didn't print skip-counts line: {p3.stderr[:200]!r}")
        with _driver() as d, d.session() as s:
            post = s.run(
                "MATCH (s:Session {session_key: 'unknown:unknown'}) RETURN count(s) AS n"
            ).single()
            if post["n"] != 0:
                failures.append("--allow-malformed: unknown:unknown sentinel session was created")
    finally:
        # Cleanup the sentinel if it leaked, plus the test's own session.
        with _driver() as d, d.session() as s:
            s.run("MATCH (s:Session {session_key: 'unknown:unknown'}) DETACH DELETE s")
        cleanup()
        try:
            bad2.unlink()
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
