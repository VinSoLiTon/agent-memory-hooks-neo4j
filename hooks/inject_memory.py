#!/usr/bin/env python3
"""
Shared hook: inject :Memory nodes into the session as additional context.

- SessionStart: load profile/* and tools/* memories ordered by recency, plus
  any memories tagged with the current cwd's project. Capped by per-bucket
  limits and an overall char budget.
- UserPromptSubmit / BeforeAgent: fulltext search against memory content/path
  with an OR-term fallback. Memories tagged with the current project get a
  recall boost so cross-project hits don't drown out in-project ones.

Tunables (env vars):
  INJECT_PROFILE_LIMIT     max profile/* memories on session start (default 5)
  INJECT_TOOLS_LIMIT       max tools/*  memories on session start (default 5)
  INJECT_PROJECT_LIMIT     max project-tagged memories on session start (default 5)
  INJECT_CHAR_BUDGET       soft cap on total chars emitted on session start (default 4000)
  INJECT_PROJECT_BOOST     score added to fulltext hits whose project matches (default 0.5)

Used by Claude Code, Codex, Cursor, and Gemini. All clients accept the same output:
  {"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}

Requires a fulltext index (create once):
  CREATE FULLTEXT INDEX memory_fulltext IF NOT EXISTS
  FOR (m:Memory) ON EACH [m.content, m.path]
"""

import argparse
import json
import os
import re
import sys

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from project import derive_project  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

MAX_PROMPT_HITS = 5
MIN_FULLTEXT_SCORE = 0.5

PROFILE_LIMIT = int(os.environ.get("INJECT_PROFILE_LIMIT", "5"))
TOOLS_LIMIT = int(os.environ.get("INJECT_TOOLS_LIMIT", "5"))
PROJECT_LIMIT = int(os.environ.get("INJECT_PROJECT_LIMIT", "5"))
CHAR_BUDGET = int(os.environ.get("INJECT_CHAR_BUDGET", "4000"))
PROJECT_BOOST = float(os.environ.get("INJECT_PROJECT_BOOST", "0.5"))

STOPWORDS = {
    "this", "that", "with", "from", "have", "what", "when", "where", "which",
    "would", "could", "should", "your", "their", "there", "about", "into",
    "they", "them", "then", "than", "some", "make", "like", "want", "need",
    "just", "only", "also", "still", "very", "much", "more", "most", "ours",
    "please", "thanks", "code", "file", "files",
}


def get_driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _fulltext_search(session, query: str, limit: int = MAX_PROMPT_HITS, current_project: str | None = None) -> list:
    """Fulltext search with optional in-project boost.

    Pulls more raw hits than `limit` so the post-boost re-rank has room to move
    project-matching memories into the top slots. Score returned to the caller
    is the boosted score.
    """
    raw_limit = max(limit * 3, limit + 5)
    cypher = """
    CALL db.index.fulltext.queryNodes('memory_fulltext', $query)
    YIELD node, score
    WHERE score > $min_score
    RETURN node.path AS path, node.content AS content,
           coalesce(node.project, '') AS project, score
    ORDER BY score DESC
    LIMIT $limit
    """
    # Pass Cypher parameters via the `parameters` dict — using kwargs would
    # collide with neo4j's `Session.run(query, ...)` first positional, since
    # the Cypher parameter happens to be named `$query`.
    rows = list(session.run(
        cypher,
        parameters={"query": query, "min_score": MIN_FULLTEXT_SCORE, "limit": raw_limit},
    ))
    if not rows:
        return rows
    # Re-rank with project boost.
    boosted = []
    for r in rows:
        s = r["score"]
        if current_project and r["project"] == current_project:
            s += PROJECT_BOOST
        boosted.append({"path": r["path"], "content": r["content"], "project": r["project"], "score": s})
    boosted.sort(key=lambda x: x["score"], reverse=True)
    return boosted[:limit]


def _extract_terms(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", prompt.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def _fetch_bucket(s, prefix: str, limit: int) -> list:
    return list(s.run(
        "MATCH (m:Memory) WHERE m.path STARTS WITH $prefix "
        "RETURN m.path AS path, m.content AS content "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $limit",
        parameters={"prefix": prefix, "limit": limit},
    ))


def _fetch_project(s, project: str, limit: int) -> list:
    return list(s.run(
        "MATCH (m:Memory) WHERE m.project = $project "
        "AND NOT (m.path STARTS WITH 'profile/' OR m.path STARTS WITH 'tools/') "
        "RETURN m.path AS path, m.content AS content "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $limit",
        parameters={"project": project, "limit": limit},
    ))


def session_start_context(current_project: str | None = None) -> str:
    with get_driver() as driver, driver.session() as s:
        profile = _fetch_bucket(s, "profile/", PROFILE_LIMIT)
        tools = _fetch_bucket(s, "tools/", TOOLS_LIMIT)
        project_rows = _fetch_project(s, current_project, PROJECT_LIMIT) if current_project else []

    if not profile and not tools and not project_rows:
        return ""

    parts = ["# Memory (from prior sessions)\n"]
    used = len(parts[0])

    def append_section(header: str, rows: list) -> None:
        nonlocal used
        if not rows:
            return
        parts.append(header)
        used += len(header)
        for r in rows:
            entry = f"### {r['path']}\n{r['content']}\n"
            if used + len(entry) > CHAR_BUDGET and len(parts) > 2:
                parts.append(f"_(further memories omitted; CHAR_BUDGET={CHAR_BUDGET} reached)_\n")
                return
            parts.append(entry)
            used += len(entry)

    append_section("## Profile\n", profile)
    if project_rows:
        append_section(f"## Project ({current_project})\n", project_rows)
    append_section("## Tools\n", tools)
    return "\n".join(parts)


def prompt_context(prompt: str, current_project: str | None = None) -> str:
    if not prompt.strip():
        return ""

    with get_driver() as driver, driver.session() as s:
        rows = _fulltext_search(s, prompt, current_project=current_project)

        if not rows:
            terms = _extract_terms(prompt)
            if terms:
                lucene_query = " OR ".join(terms)
                rows = _fulltext_search(s, lucene_query, current_project=current_project)

    if not rows:
        return ""

    parts = ["# Relevant memory for this prompt\n"]
    for r in rows:
        parts.append(f"## {r['path']}\n{r['content']}\n")
    return "\n".join(parts)


def emit(event_name: str, context: str):
    if not context.strip():
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }
    print(json.dumps(out))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--client", required=True, choices=["claude_code", "codex", "cursor", "gemini"])
    parser.parse_args()

    try:
        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        event = data.get("hook_event_name")
        normalized = (event or "").lower()
        current_project = derive_project(data.get("cwd"))
        if normalized in {"sessionstart", "session_start"}:
            emit(event or "sessionStart", session_start_context(current_project))
        # Gemini fires BeforeAgent after the user prompt is submitted but before
        # the agent reasons — analogous to Claude Code's UserPromptSubmit and
        # Cursor's beforeSubmitPrompt. All three carry a `prompt` field.
        elif normalized in {"userpromptsubmit", "beforesubmitprompt", "beforeagent", "before_agent"}:
            emit(event or "beforeAgent", prompt_context(data.get("prompt", ""), current_project))
    except Exception as e:
        print(f"inject_memory error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
