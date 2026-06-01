#!/usr/bin/env python3
"""Shared hook: inject :Memory nodes into the session as additional context.

- SessionStart: load profile/* and tools/* memories ordered by recency, plus
  any memories tagged with the current cwd's project. Capped by per-bucket
  limits and an overall char budget.
- UserPromptSubmit / BeforeAgent: hybrid recall (fulltext + vector, RRF-fused)
  with an OR-term fallback and an in-project boost.

Phase C: all ranking/retrieval now lives in the shared `recall` engine
(hooks/recall.py), which the dashboard and CLI also call — one implementation,
no drift. This module is the hook-specific glue: open a driver, derive the
project, call recall, emit the hook output, and track access. The `_`-prefixed
names below are thin re-exports of the engine for backward compatibility.

Used by Claude Code, Codex, Cursor, and Gemini. All clients accept the same output:
  {"hookSpecificOutput": {"hookEventName": "...", "additionalContext": "..."}}
"""

import argparse
import json
import os
import sys

from neo4j import GraphDatabase

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from project import derive_project  # noqa: E402
import recall  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

# Backward-compatible re-exports: the recall engine is the single source of
# truth, but these names kept their old call sites (and tests).
MAX_PROMPT_HITS = recall.MAX_PROMPT_HITS
MIN_FULLTEXT_SCORE = recall.MIN_FULLTEXT_SCORE
_escape_lucene = recall.escape_lucene
_extract_terms = recall.extract_terms
_fulltext_search = recall.fulltext_search
_vector_search = recall.vector_search
_hybrid_merge = recall.hybrid_merge
_fetch_bucket = recall.fetch_bucket
_fetch_project = recall.fetch_project


def get_driver():
    # PR-G #2: silence harmless "property does not exist" notifications.
    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )


def session_start_context(current_project: str | None = None) -> tuple[str, list[str]]:
    """Returns (markdown_context, paths_emitted). Paths are used for access tracking."""
    with get_driver() as driver, driver.session() as s:
        buckets = recall.session_start_buckets(s, current_project)
    return recall.render_session_start(buckets, current_project)


def prompt_context(prompt: str, current_project: str | None = None) -> tuple[str, list[str]]:
    if not prompt.strip():
        return "", []
    with get_driver() as driver, driver.session() as s:
        rows = recall.prompt_query(s, prompt, current_project)
    return recall.render_prompt(rows)


def _bump_access(paths: list[str]) -> None:
    """Best-effort: increment access_count and stamp last_accessed_at on each
    memory just returned. Failures are swallowed so recall is never blocked."""
    if not paths:
        return
    try:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        with get_driver() as driver, driver.session() as s:
            s.run(
                """
                UNWIND $paths AS p
                MATCH (m:Memory {path: p})
                SET m.last_accessed_at = $now,
                    m.access_count = coalesce(m.access_count, 0) + 1
                """,
                parameters={"paths": paths, "now": now},
            )
    except Exception:
        pass


def emit(event_name: str, context: str, accessed_paths: list[str] | None = None):
    if not context.strip():
        return
    out = {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": context,
        }
    }
    print(json.dumps(out))
    _bump_access(accessed_paths or [])


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
            ctx, paths = session_start_context(current_project)
            emit(event or "sessionStart", ctx, paths)
        # Gemini fires BeforeAgent after the user prompt is submitted but before
        # the agent reasons — analogous to Claude Code's UserPromptSubmit and
        # Cursor's beforeSubmitPrompt. All three carry a `prompt` field.
        elif normalized in {"userpromptsubmit", "beforesubmitprompt", "beforeagent", "before_agent"}:
            ctx, paths = prompt_context(data.get("prompt", ""), current_project)
            emit(event or "beforeAgent", ctx, paths)
    except Exception as e:
        print(f"inject_memory error: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
