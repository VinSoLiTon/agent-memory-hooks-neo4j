#!/usr/bin/env python3
"""Shared recall engine — ONE ranking/retrieval implementation for every surface.

Before Phase C this logic was duplicated three ways: hooks/inject_memory.py
(hybrid + project boost + status filter), dashboard/app.py /search (hybrid, no
status filter), and cli/njhook.py search (fulltext only). They drifted. This
module is the single source of truth; the hook, dashboard, CLI — and future
REST/MCP surfaces — all call it.

Design:
- Functions take an open neo4j `session` and are otherwise side-effect free, so
  any caller (with its own driver) can reuse them.
- Recall is hybrid: Lucene fulltext + (optional) vector ANN, fused by Reciprocal
  Rank Fusion (k=60), with a small in-project boost. Vector silently returns []
  when EMBED_PROVIDER is unset, so recall degrades to fulltext-only.
- Phase C2 ranking signals: the fused score is multiplied by an importance
  factor (LLM-rated 1-10 at dream time, neutral at 5) and a decayed-recency
  factor (exp(-lambda*hours), half-life per path prefix). Session-start budget
  truncation orders by BudgetMem value-density (importance x recency / chars).
  All signals default to neutral, so pre-C2 memories rank exactly as before.
- Lifecycle filter (Phase A): only `coalesce(status,'active')='active'` and
  non-archived memories are ever returned.
- `mode` is a closed vocabulary (RECALL_MODES).

Tunables (env):
  INJECT_PROFILE_LIMIT / INJECT_TOOLS_LIMIT / INJECT_PROJECT_LIMIT  (default 5)
  INJECT_CHAR_BUDGET   session-start total-chars soft cap            (default 4000)
  INJECT_PROJECT_BOOST RRF tie-break for in-project hits             (default 0.5)
"""
from __future__ import annotations

import math
import os
import re
import sys
from datetime import datetime, timezone

import embeddings  # hooks/embeddings.py — on sys.path for every caller

# --- tunables ---------------------------------------------------------------
MAX_PROMPT_HITS = 5
MIN_FULLTEXT_SCORE = 0.5
RRF_K = 60

PROFILE_LIMIT = int(os.environ.get("INJECT_PROFILE_LIMIT", "5"))
TOOLS_LIMIT = int(os.environ.get("INJECT_TOOLS_LIMIT", "5"))
PROJECT_LIMIT = int(os.environ.get("INJECT_PROJECT_LIMIT", "5"))
CHAR_BUDGET = int(os.environ.get("INJECT_CHAR_BUDGET", "4000"))
PROJECT_BOOST = float(os.environ.get("INJECT_PROJECT_BOOST", "0.5"))

# Closed vocabulary of recall modes (mirrors the roadmap; tool_context is a thin
# variant of prompt_context for now and gains a dedicated plan in a later phase).
RECALL_MODES = frozenset({"session_start", "prompt_context", "tool_context"})


def _active(alias: str) -> str:
    """Phase A lifecycle predicate, parameterized by the bound node alias."""
    return (f"coalesce({alias}.archived, false) = false "
            f"AND coalesce({alias}.status, 'active') = 'active'")


# --- C2 ranking signals: importance x decayed recency -----------------------
DEFAULT_IMPORTANCE = 5  # neutral; memories without an importance keep score x1.0

# Recency half-lives by path prefix (days). profile/tools are durable identity →
# decay slowly; project context goes stale faster. Converted to a per-hour lambda.
_HALF_LIFE_DAYS = {"profile/": 180.0, "tools/": 180.0, "project/": 30.0, "general/": 60.0}
_DEFAULT_HALF_LIFE_DAYS = 60.0


def _lambda_for(path: str) -> float:
    for prefix, hl in _HALF_LIFE_DAYS.items():
        if (path or "").startswith(prefix):
            return math.log(2) / (hl * 24.0)
    return math.log(2) / (_DEFAULT_HALF_LIFE_DAYS * 24.0)


def _parse_ts(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now_utc():
    return datetime.now(timezone.utc)


def importance_factor(importance) -> float:
    """Map importance in [1,10] to a multiplier, neutral (1.0) at 5. Missing or
    malformed importance is neutral, so pre-C2 memories are unaffected."""
    try:
        imp = int(importance)
    except (TypeError, ValueError):
        imp = DEFAULT_IMPORTANCE
    imp = max(1, min(10, imp))
    return imp / float(DEFAULT_IMPORTANCE)


def recency_factor(row: dict, now=None) -> float:
    """exp(-lambda * hours) since the memory was last touched. Anchor on
    last_accessed_at, else updated_at, else ingested_at; a memory with no
    timestamp is treated as fresh (1.0) rather than penalized."""
    now = now or _now_utc()
    anchor = (_parse_ts(row.get("last_accessed_at"))
              or _parse_ts(row.get("updated_at"))
              or _parse_ts(row.get("ingested_at")))
    if anchor is None:
        return 1.0
    hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
    return math.exp(-_lambda_for(row.get("path", "")) * hours)


def value_density(row: dict, now=None) -> float:
    """importance x recency_decay / char_length — BudgetMem token-value density.
    Orders which memories survive the session-start char budget."""
    content = row.get("content") or ""
    return importance_factor(row.get("importance", DEFAULT_IMPORTANCE)) * recency_factor(row, now) / max(len(content), 1)


STOPWORDS = {
    "this", "that", "with", "from", "have", "what", "when", "where", "which",
    "would", "could", "should", "your", "their", "there", "about", "into",
    "they", "them", "then", "than", "some", "make", "like", "want", "need",
    "just", "only", "also", "still", "very", "much", "more", "most", "ours",
    "please", "thanks", "code", "file", "files",
}

_LUCENE_SPECIAL = re.compile(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)')


def escape_lucene(query: str) -> str:
    """Escape Lucene reserved chars so a user prompt with `:`, `?`, `(`, `-`,
    etc. isn't parsed as Lucene operators (and doesn't raise)."""
    return _LUCENE_SPECIAL.sub(r"\\\1", query)


def extract_terms(prompt: str) -> list[str]:
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9_-]+", prompt.lower())
    return [w for w in words if len(w) >= 3 and w not in STOPWORDS]


def _hit(r) -> dict:
    """Build a hit dict from a search result row (fulltext / vector)."""
    return {
        "path": r["path"], "content": r["content"],
        "project": r["project"], "score": r["score"],
        "importance": r["importance"], "last_accessed_at": r["last_accessed_at"],
        "updated_at": r["updated_at"], "ingested_at": r["ingested_at"],
    }


def _bucket_row(r) -> dict:
    return {
        "path": r["path"], "content": r["content"],
        "importance": r["importance"], "last_accessed_at": r["last_accessed_at"],
        "updated_at": r["updated_at"], "ingested_at": r["ingested_at"],
    }


# --- primitive retrievers ---------------------------------------------------

def fulltext_search(session, query: str, limit: int = MAX_PROMPT_HITS,
                    min_score: float = MIN_FULLTEXT_SCORE) -> list:
    """Lucene fulltext over (m.content, m.path). Active, non-archived only.
    Escapes reserved chars and returns [] on any error so a malformed query
    never blocks the vector fallback."""
    raw_limit = max(limit * 3, limit + 5)
    cypher = f"""
    CALL db.index.fulltext.queryNodes('memory_fulltext', $query)
    YIELD node, score
    WHERE score > $min_score AND {_active('node')}
    RETURN node.path AS path, node.content AS content,
           coalesce(node.project, '') AS project, score,
           coalesce(node.importance, $default_importance) AS importance,
           node.last_accessed_at AS last_accessed_at,
           node.updated_at AS updated_at, node.ingested_at AS ingested_at
    ORDER BY score DESC
    LIMIT $limit
    """
    try:
        rows = list(session.run(cypher, parameters={
            "query": escape_lucene(query), "min_score": min_score,
            "limit": raw_limit, "default_importance": DEFAULT_IMPORTANCE,
        }))
    except Exception as e:
        print(f"recall: fulltext query failed ({e}); falling back to vector only", file=sys.stderr)
        return []
    return [_hit(r) for r in rows]


def vector_search(session, query: str, limit: int = MAX_PROMPT_HITS) -> list:
    """ANN over the memory vector index. [] if embeddings are disabled or the
    index isn't populated yet."""
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
            f"""
            CALL db.index.vector.queryNodes('memory_embeddings', $k, $qvec)
            YIELD node, score
            WHERE {_active('node')}
            RETURN node.path AS path, node.content AS content,
                   coalesce(node.project, '') AS project, score,
                   coalesce(node.importance, $default_importance) AS importance,
                   node.last_accessed_at AS last_accessed_at,
                   node.updated_at AS updated_at, node.ingested_at AS ingested_at
            """,
            parameters={"qvec": qvec[0], "k": raw_limit, "default_importance": DEFAULT_IMPORTANCE},
        ))
    except Exception:
        return []
    return [_hit(r) for r in rows]


def hybrid_merge(fulltext: list, vector: list, current_project: str | None, limit: int, now=None) -> list:
    """Fuse fulltext + vector with Reciprocal Rank Fusion (k=60), apply the
    in-project boost, then the C2 ranking signals (importance x decayed recency).
    Returns rows whose `score` is the final, comparable score."""
    now = now or _now_utc()
    scores: dict[str, float] = {}
    by_path: dict[str, dict] = {}
    for rank, r in enumerate(fulltext):
        scores[r["path"]] = scores.get(r["path"], 0.0) + 1.0 / (RRF_K + rank + 1)
        by_path[r["path"]] = r
    for rank, r in enumerate(vector):
        scores[r["path"]] = scores.get(r["path"], 0.0) + 1.0 / (RRF_K + rank + 1)
        by_path.setdefault(r["path"], r)
    if current_project:
        for p in scores:
            if by_path[p].get("project") == current_project:
                scores[p] += PROJECT_BOOST * 0.05  # RRF scores are O(1/60) — boost in the same range
    # C2: importance x decayed recency. Both default to neutral (1.0) when the
    # fields are absent, so this is a no-op for pre-C2 memories / hand-built rows.
    for p in scores:
        row = by_path[p]
        scores[p] *= importance_factor(row.get("importance", DEFAULT_IMPORTANCE)) * recency_factor(row, now)
    ordered = sorted(by_path.keys(), key=lambda p: scores[p], reverse=True)
    return [{**by_path[p], "score": scores[p]} for p in ordered][:limit]


# --- bucket fetch (session-start) -------------------------------------------

def fetch_bucket(session, prefix: str, limit: int) -> list:
    rows = session.run(
        f"MATCH (m:Memory) WHERE m.path STARTS WITH $prefix AND {_active('m')} "
        "RETURN m.path AS path, m.content AS content, "
        "       coalesce(m.importance, $default_importance) AS importance, "
        "       m.last_accessed_at AS last_accessed_at, "
        "       m.updated_at AS updated_at, m.ingested_at AS ingested_at "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $limit",
        parameters={"prefix": prefix, "limit": limit, "default_importance": DEFAULT_IMPORTANCE},
    )
    return [_bucket_row(r) for r in rows]


def fetch_project(session, project: str, limit: int) -> list:
    rows = session.run(
        f"MATCH (m:Memory) WHERE m.project = $project "
        "AND NOT (m.path STARTS WITH 'profile/' OR m.path STARTS WITH 'tools/') "
        f"AND {_active('m')} "
        "RETURN m.path AS path, m.content AS content, "
        "       coalesce(m.importance, $default_importance) AS importance, "
        "       m.last_accessed_at AS last_accessed_at, "
        "       m.updated_at AS updated_at, m.ingested_at AS ingested_at "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $limit",
        parameters={"project": project, "limit": limit, "default_importance": DEFAULT_IMPORTANCE},
    )
    return [_bucket_row(r) for r in rows]


# --- high-level query plans -------------------------------------------------

def prompt_query(session, prompt: str, current_project: str | None = None,
                 limit: int = MAX_PROMPT_HITS, min_score: float = MIN_FULLTEXT_SCORE,
                 now=None) -> list:
    """Hybrid recall for a prompt: fulltext (with OR-term fallback) + vector,
    fused, project-boosted, and ranked by importance x recency. Returns ranked
    hit dicts."""
    if not (prompt or "").strip():
        return []
    ft = fulltext_search(session, prompt, limit=limit, min_score=min_score)
    if not ft:
        terms = extract_terms(prompt)
        if terms:
            ft = fulltext_search(session, " OR ".join(terms), limit=limit, min_score=min_score)
    vec = vector_search(session, prompt, limit=limit)
    if not ft and not vec:
        return []
    return hybrid_merge(ft, vec, current_project, limit, now=now)


def session_start_buckets(session, current_project: str | None = None,
                          profile_limit: int = PROFILE_LIMIT,
                          tools_limit: int = TOOLS_LIMIT,
                          project_limit: int = PROJECT_LIMIT) -> dict:
    return {
        "profile": fetch_bucket(session, "profile/", profile_limit),
        "tools": fetch_bucket(session, "tools/", tools_limit),
        "project": fetch_project(session, current_project, project_limit) if current_project else [],
    }


def query(session, mode: str, *, prompt: str | None = None,
          current_project: str | None = None, limit: int = MAX_PROMPT_HITS,
          min_score: float = MIN_FULLTEXT_SCORE):
    """Dispatch over the closed mode vocabulary. session_start returns bucket
    dicts; prompt_context / tool_context return ranked hit lists."""
    if mode not in RECALL_MODES:
        raise ValueError(f"unknown recall mode {mode!r}; choices: {sorted(RECALL_MODES)}")
    if mode == "session_start":
        return session_start_buckets(session, current_project)
    return prompt_query(session, prompt or "", current_project, limit, min_score)


# --- renderers (pure) -------------------------------------------------------

def render_session_start(buckets: dict, current_project: str | None = None,
                         char_budget: int = CHAR_BUDGET, now=None) -> tuple[str, list[str]]:
    """Render session-start buckets to injection markdown under a char budget.
    Within each bucket, memories are ordered by BudgetMem value-density
    (importance x recency / chars) so the most valuable, concise memories
    survive truncation. Returns (markdown, emitted_paths)."""
    now = now or _now_utc()
    profile = buckets.get("profile") or []
    tools = buckets.get("tools") or []
    project_rows = buckets.get("project") or []
    if not profile and not tools and not project_rows:
        return "", []

    parts = ["# Memory (from prior sessions)\n"]
    used = len(parts[0])
    emitted_paths: list[str] = []

    def append_section(header: str, rows: list) -> None:
        nonlocal used
        if not rows:
            return
        rows = sorted(rows, key=lambda r: value_density(r, now), reverse=True)
        parts.append(header)
        used += len(header)
        for r in rows:
            entry = f"### {r['path']}\n{r['content']}\n"
            if used + len(entry) > char_budget and len(parts) > 2:
                parts.append(f"_(further memories omitted; CHAR_BUDGET={char_budget} reached)_\n")
                return
            parts.append(entry)
            used += len(entry)
            emitted_paths.append(r["path"])

    append_section("## Profile\n", profile)
    if project_rows:
        append_section(f"## Project ({current_project})\n", project_rows)
    append_section("## Tools\n", tools)
    return "\n".join(parts), emitted_paths


def render_prompt(rows: list) -> tuple[str, list[str]]:
    """Render hybrid hits to injection markdown. Returns (markdown, paths)."""
    if not rows:
        return "", []
    parts = ["# Relevant memory for this prompt\n"]
    paths: list[str] = []
    for r in rows:
        parts.append(f"## {r['path']}\n{r['content']}\n")
        paths.append(r["path"])
    return "\n".join(parts), paths
