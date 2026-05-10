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
import embeddings  # noqa: E402

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
    # PR-G #2: silence harmless "property does not exist" notifications.
    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )


_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def _escape_lucene(query: str) -> str:
    """Escape Lucene reserved characters so a user prompt with `:`, `?`, `(`,
    `-`, etc. doesn't get parsed as Lucene operators (and doesn't raise).

    Reserved (per Lucene query syntax): + - && || ! ( ) { } [ ] ^ " ~ * ? : \\ /
    """
    return _LUCENE_SPECIAL.sub(r"\\\1", query)


def _fulltext_search(session, query: str, limit: int = MAX_PROMPT_HITS) -> list:
    """Raw fulltext hits — Lucene over (m.content, m.path). Skips archived.

    H4 belt + suspenders: escape Lucene reserved chars before submission AND
    return [] on any exception, so a malformed query never blocks the vector
    fallback. Without this, a prompt like "what does -x mean?" raises a
    parser error that bubbles up and skips _vector_search entirely.
    """
    raw_limit = max(limit * 3, limit + 5)
    safe_query = _escape_lucene(query)
    cypher = """
    CALL db.index.fulltext.queryNodes('memory_fulltext', $query)
    YIELD node, score
    WHERE score > $min_score AND coalesce(node.archived, false) = false
    RETURN node.path AS path, node.content AS content,
           coalesce(node.project, '') AS project, score
    ORDER BY score DESC
    LIMIT $limit
    """
    try:
        rows = list(session.run(
            cypher,
            parameters={"query": safe_query, "min_score": MIN_FULLTEXT_SCORE, "limit": raw_limit},
        ))
    except Exception as e:
        # Don't let fulltext failures kill recall; vector fallback still runs.
        print(f"inject_memory: fulltext query failed ({e}); falling back to vector only", file=sys.stderr)
        return []
    return [{"path": r["path"], "content": r["content"], "project": r["project"], "score": r["score"]} for r in rows]


def _vector_search(session, query: str, limit: int = MAX_PROMPT_HITS) -> list:
    """Approximate-nearest-neighbor over the memory vector index. Returns []
    if embeddings are disabled or the index isn't populated yet."""
    if not embeddings.is_enabled():
        return []
    try:
        qvec = embeddings.embed([query])
        if not qvec:
            return []
    except Exception:
        return []
    raw_limit = max(limit * 3, limit + 5)
    try:
        rows = list(session.run(
            """
            CALL db.index.vector.queryNodes('memory_embeddings', $k, $qvec)
            YIELD node, score
            WHERE coalesce(node.archived, false) = false
            RETURN node.path AS path, node.content AS content,
                   coalesce(node.project, '') AS project, score
            """,
            parameters={"qvec": qvec[0], "k": raw_limit},
        ))
    except Exception:
        # No vector index yet, or unsupported on this Neo4j version. Silently fall back.
        return []
    return [{"path": r["path"], "content": r["content"], "project": r["project"], "score": r["score"]} for r in rows]


def _hybrid_merge(fulltext: list, vector: list, current_project: str | None, limit: int) -> list:
    """Combine fulltext and vector hits with Reciprocal Rank Fusion (k=60),
    then apply the project-match boost as a tie-break / score nudge."""
    k = 60
    scores: dict[str, float] = {}
    by_path: dict[str, dict] = {}
    for rank, r in enumerate(fulltext):
        scores[r["path"]] = scores.get(r["path"], 0.0) + 1.0 / (k + rank + 1)
        by_path[r["path"]] = r
    for rank, r in enumerate(vector):
        scores[r["path"]] = scores.get(r["path"], 0.0) + 1.0 / (k + rank + 1)
        by_path.setdefault(r["path"], r)
    if current_project:
        for p, _ in scores.items():
            if by_path[p].get("project") == current_project:
                scores[p] += PROJECT_BOOST * 0.05  # RRF scores are O(1/60) — boost in the same range
    ordered = sorted(by_path.values(), key=lambda r: scores[r["path"]], reverse=True)
    return ordered[:limit]


def _extract_terms(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", prompt.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def _fetch_bucket(s, prefix: str, limit: int) -> list:
    return list(s.run(
        "MATCH (m:Memory) WHERE m.path STARTS WITH $prefix "
        "AND coalesce(m.archived, false) = false "
        "RETURN m.path AS path, m.content AS content "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $limit",
        parameters={"prefix": prefix, "limit": limit},
    ))


def _fetch_project(s, project: str, limit: int) -> list:
    return list(s.run(
        "MATCH (m:Memory) WHERE m.project = $project "
        "AND NOT (m.path STARTS WITH 'profile/' OR m.path STARTS WITH 'tools/') "
        "AND coalesce(m.archived, false) = false "
        "RETURN m.path AS path, m.content AS content "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $limit",
        parameters={"project": project, "limit": limit},
    ))


def session_start_context(current_project: str | None = None) -> tuple[str, list[str]]:
    """Returns (markdown_context, paths_emitted). Paths are used for access tracking."""
    with get_driver() as driver, driver.session() as s:
        profile = _fetch_bucket(s, "profile/", PROFILE_LIMIT)
        tools = _fetch_bucket(s, "tools/", TOOLS_LIMIT)
        project_rows = _fetch_project(s, current_project, PROJECT_LIMIT) if current_project else []

    if not profile and not tools and not project_rows:
        return "", []

    parts = ["# Memory (from prior sessions)\n"]
    used = len(parts[0])
    emitted_paths: list[str] = []

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
            emitted_paths.append(r["path"])

    append_section("## Profile\n", profile)
    if project_rows:
        append_section(f"## Project ({current_project})\n", project_rows)
    append_section("## Tools\n", tools)
    return "\n".join(parts), emitted_paths


def prompt_context(prompt: str, current_project: str | None = None) -> tuple[str, list[str]]:
    if not prompt.strip():
        return "", []

    with get_driver() as driver, driver.session() as s:
        ft_rows = _fulltext_search(s, prompt)
        if not ft_rows:
            terms = _extract_terms(prompt)
            if terms:
                ft_rows = _fulltext_search(s, " OR ".join(terms))
        # Vector search runs against the verbatim prompt regardless — embeddings
        # don't need stopword filtering. Returns [] when EMBED_PROVIDER is unset
        # or the vector index isn't populated, so behavior degrades gracefully
        # to fulltext-only.
        vec_rows = _vector_search(s, prompt)

    if not ft_rows and not vec_rows:
        return "", []

    rows = _hybrid_merge(ft_rows, vec_rows, current_project, MAX_PROMPT_HITS)

    parts = ["# Relevant memory for this prompt\n"]
    paths: list[str] = []
    for r in rows:
        parts.append(f"## {r['path']}\n{r['content']}\n")
        paths.append(r["path"])
    return "\n".join(parts), paths


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
