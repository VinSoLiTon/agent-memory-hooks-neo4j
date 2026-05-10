#!/usr/bin/env python3
"""
Dream phase: read recent session events from Neo4j, ask Claude to distill
them into durable memories, write them back.

Memories imitate markdown files: each :Memory node has a `path` (e.g.
"profile/role.md", "tools/bash/grep-flags.md") and a `content` field holding
the full markdown body (frontmatter + prose).

Schema:
    (:Memory {path, content, updated_at})         -- path is unique
    (:Memory)-[:DERIVED_FROM]->(:Session)

Usage:
    python dream.py                  # dream over sessions not yet dreamed
    python dream.py --session <id>   # dream over one session
    python dream.py --since 24h      # only events newer than 24h / 7d / 30m
    python dream.py --dry-run        # print, don't write
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

from anthropic import Anthropic
from neo4j import GraphDatabase

# Pull in project derivation from the hooks package so dream and capture
# share a single source of truth for "what is the project of this cwd?".
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
from project import dominant_project  # noqa: E402

# Windows consoles default to cp1252; memories from Claude routinely include
# em-dashes, arrows, smart quotes, etc. Force UTF-8 so the human-readable
# preview doesn't crash before write_memories runs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

MODEL = os.environ.get("DREAM_MODEL", "claude-opus-4-7")
MAX_TOKENS = 4096

SYSTEM_PROMPT = """You are the "dream phase" for a Claude Code memory system. \
You receive a chronological log of hook events from a Claude Code session \
(SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop) plus the set of \
markdown memories that already exist. Distill the session into durable markdown \
memories that will help future sessions.

Each memory imitates a markdown file: it has a path and a markdown body with \
YAML frontmatter. Organize paths semantically by topic, e.g.:

  profile/role.md
  profile/preferences.md
  tools/bash/common-flags.md
  tools/edit/conventions.md
  project/<short-slug>.md
  general/<short-slug>.md

Output STRICT JSON only, no prose, matching this schema:

{
  "memories": [
    {
      "path": "profile/role.md",
      "content": "---\\ntitle: User role\\nkind: profile\\n---\\n\\n<markdown body>"
    }
  ]
}

Frontmatter must include `title` and `kind` (one of: profile, tool, project, general).
The body should be tight markdown a future agent can read cold.

Rules:
- If a memory at the same path already exists, return an UPDATED full body that \
merges new evidence with the prior content. Do not duplicate facts. Remove anything \
the new events contradict.
- Skip ephemeral details (one-off filenames, debug output) and anything obvious \
from a fresh repo read (paths, git history).
- Prefer fewer, sharper memories over many vague ones.
- If nothing is worth remembering, return {"memories": []}.
- Each memory must stand alone — a future agent reads it without this transcript."""


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)([hdm])", s)
    if not m:
        raise ValueError(f"--since must look like '24h', '7d', '30m'; got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    return datetime.now(timezone.utc) - delta


def fetch_events(driver, session_id: str | None, since: datetime | None):
    """Return list of (session_id, [event_props, ...]) ordered chronologically.

    A session is included if it has at least one event newer than its
    `last_dreamed_at` watermark (or has never been dreamed).
    """
    where, params = ["(s.last_dreamed_at IS NULL OR e.timestamp > s.last_dreamed_at)"], {}
    if session_id:
        where.append("s.session_id = $session_id")
        params["session_id"] = session_id
    if since:
        where.append("e.timestamp >= $since")
        params["since"] = since.isoformat()

    query = f"""
    MATCH (s:Session)-[:FIRST_EVENT|NEXT*0..]->(e:Event)
    WHERE {' AND '.join(where)}
    RETURN s.session_id AS session_id, e
    ORDER BY s.session_id, e.timestamp
    """
    grouped: dict[str, list] = {}
    with driver.session() as ses:
        for record in ses.run(query, **params):
            grouped.setdefault(record["session_id"], []).append(dict(record["e"]))
    return list(grouped.items())


def fetch_existing_memories(driver) -> list[dict]:
    with driver.session() as ses:
        result = ses.run("MATCH (m:Memory) RETURN m.path AS path, m.content AS content ORDER BY path")
        return [dict(r) for r in result]


def render_events(events: list[dict]) -> str:
    lines = []
    for e in events:
        head = f"[{e.get('timestamp', '?')}] {e.get('event_name', '?')}"
        if e.get("tool_name"):
            head += f" tool={e['tool_name']}"
        lines.append(head)
        if e.get("prompt"):
            lines.append(f"  prompt: {e['prompt'][:500]}")
        if e.get("tool_input"):
            lines.append(f"  input:  {str(e['tool_input'])[:500]}")
        if e.get("tool_response"):
            lines.append(f"  output: {str(e['tool_response'])[:500]}")
    return "\n".join(lines)


def render_existing(memories: list[dict]) -> str:
    if not memories:
        return "(no existing memories)"
    parts = []
    for m in memories:
        parts.append(f"### {m['path']}\n```\n{m['content']}\n```")
    return "\n\n".join(parts)


def call_claude(client: Anthropic, transcript: str, existing: str) -> list[dict]:
    user_msg = (
        f"<existing_memories>\n{existing}\n</existing_memories>\n\n"
        f"<events>\n{transcript}\n</events>"
    )
    msg = client.messages.create(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        system=[
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON in model output: {text[:200]}")
    return json.loads(text[start : end + 1]).get("memories", [])


def write_memories(driver, session_id: str, memories: list[dict], watermark: str, project: str | None = None) -> int:
    """Upsert memories and advance the session's last_dreamed_at watermark.

    `watermark` is the timestamp of the latest event we just dreamed over —
    future runs will only re-dream the session if newer events arrive.

    `project` is the dominant project slug for the session (derived from event
    cwds). Memories whose path starts with profile/ or tools/ are considered
    cross-project and stay untagged so they surface in every session; everything
    else (project/, general/, etc.) is tagged with this project so recall can
    boost in-project hits.
    """
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "path": m["path"],
            "content": m["content"],
            "updated_at": now,
            "project": None
            if m["path"].startswith(("profile/", "tools/")) or not project
            else project,
        }
        for m in memories
        if m.get("path") and m.get("content")
    ]
    with driver.session() as ses:
        ses.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
        ses.run(
            """
            MATCH (s:Session {session_id: $session_id})
            SET s.last_dreamed_at = $watermark
            WITH s
            UNWIND $rows AS row
            MERGE (m:Memory {path: row.path})
            SET m.content = row.content,
                m.updated_at = row.updated_at,
                m.project = coalesce(row.project, m.project)
            MERGE (s)-[:DREAMED]->(m)
            MERGE (m)-[:DERIVED_FROM]->(s)
            """,
            session_id=session_id,
            watermark=watermark,
            rows=rows,
        )
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="dream over a single session_id")
    ap.add_argument("--since", help="only include events newer than e.g. 24h, 7d, 30m")
    ap.add_argument("--dry-run", action="store_true", help="print memories, don't write")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        sys.exit(1)

    since = parse_since(args.since) if args.since else None
    client = Anthropic()
    driver = get_driver()
    try:
        sessions = fetch_events(driver, args.session, since)
        if not sessions:
            print("nothing to dream about.")
            return
        existing = render_existing(fetch_existing_memories(driver))
        for session_id, events in sessions:
            project = dominant_project([e.get("cwd") for e in events])
            label = f"{session_id}" + (f"  project={project}" if project else "")
            print(f"\n=== dreaming over {label} ({len(events)} new events) ===")
            memories = call_claude(client, render_events(events), existing)
            for m in memories:
                print(f"\n--- {m.get('path')} ---")
                print(m.get("content", ""))
            if not args.dry_run:
                watermark = events[-1].get("timestamp")
                n = write_memories(driver, session_id, memories, watermark, project=project)
                print(f"\n  wrote/updated {n} memories; watermark -> {watermark}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
