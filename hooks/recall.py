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
  RECALL_RECENCY_ANCHOR  recency-decay anchor: access (default) | content
  RECALL_USAGE_BOOST_MAX bounded usage lift in content mode          (default 0.15)
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
# Session-start buckets over-fetch a recency-ordered candidate pool this many
# times the limit, then keep the value-densest `limit` (see _rank_bucket). Mirrors
# the prompt/vector path's `max(limit*3, limit+5)` over-fetch idiom.
BUCKET_OVERFETCH = int(os.environ.get("INJECT_BUCKET_OVERFETCH", "3"))
# Recency-decay anchor (item #12). "access" (default) = today's behaviour: decay
# anchored on last_accessed_at first, which _bump_access resets on every injection
# (a popularity feedback loop — injected memories stay "fresh" regardless of
# content currency). "content" = bi-temporal split: decay anchored on content
# currency (updated_at/ingested_at), with a small bounded usage lift so a
# recently-USED memory can't leapfrog a content-fresher one by more than the cap.
# Read once into a module constant so per-call cost is a branch, not an env read;
# tests monkeypatch RECENCY_ANCHOR_MODE (mirrors EVENT_MIN_SCORE override).
RECENCY_ANCHOR_MODE = os.environ.get("RECALL_RECENCY_ANCHOR", "access")
USAGE_BOOST_MAX = float(os.environ.get("RECALL_USAGE_BOOST_MAX", "0.15"))

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


def _decay_one(now, lam: float, ts):
    """exp(-lambda * hours) since `ts`, or None if `ts` is absent/unparseable."""
    anchor = _parse_ts(ts)
    if anchor is None:
        return None
    hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
    return math.exp(-lam * hours)


def recency_factor(row: dict, now=None) -> float:
    """Decayed recency. A memory with no timestamp at all is treated as fresh
    (1.0) rather than penalized.

    RECALL_RECENCY_ANCHOR=access (default): anchor on last_accessed_at, else
    updated_at, else ingested_at — kept byte-identical to the original so the
    default path and all existing tests are unaffected.

    RECALL_RECENCY_ANCHOR=content (bi-temporal split): decay anchored on CONTENT
    currency (updated_at, else ingested_at, else last_accessed_at) so injecting a
    memory can't keep it "fresh" via _bump_access; a recently-USED memory still
    gets a small lift bounded by (1 + USAGE_BOOST_MAX), which can never leapfrog a
    content-fresher memory by more than that cap."""
    now = now or _now_utc()
    lam = _lambda_for(row.get("path", ""))

    if RECENCY_ANCHOR_MODE == "content":
        base = None
        for field in ("updated_at", "ingested_at", "last_accessed_at"):
            base = _decay_one(now, lam, row.get(field))
            if base is not None:
                break
        if base is None:
            return 1.0
        usage = _decay_one(now, lam, row.get("last_accessed_at"))
        return base if usage is None else base * (1.0 + USAGE_BOOST_MAX * usage)

    # default: access-anchored (unchanged behaviour)
    anchor = (_parse_ts(row.get("last_accessed_at"))
              or _parse_ts(row.get("updated_at"))
              or _parse_ts(row.get("ingested_at")))
    if anchor is None:
        return 1.0
    hours = max(0.0, (now - anchor).total_seconds() / 3600.0)
    return math.exp(-lam * hours)


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

def _rank_bucket(rows: list, limit: int, now=None) -> list:
    """Slice an over-fetched, recency-ordered candidate pool down to the
    value-densest `limit` (importance x recency / chars). The DB cut is recency
    only; this is where importance gets a vote — so a high-importance,
    slightly-older memory surfaces instead of being lost at the DB LIMIT. A pool
    already <= limit short-circuits (order is irrelevant; render re-ranks)."""
    if len(rows) <= limit:
        return rows
    return sorted(rows, key=lambda r: value_density(r, now), reverse=True)[:limit]


def fetch_bucket(session, prefix: str, limit: int, now=None) -> list:
    raw_limit = max(limit * BUCKET_OVERFETCH, limit + 5)
    rows = session.run(
        f"MATCH (m:Memory) WHERE m.path STARTS WITH $prefix AND {_active('m')} "
        "RETURN m.path AS path, m.content AS content, "
        "       coalesce(m.importance, $default_importance) AS importance, "
        "       m.last_accessed_at AS last_accessed_at, "
        "       m.updated_at AS updated_at, m.ingested_at AS ingested_at "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $raw_limit",
        parameters={"prefix": prefix, "raw_limit": raw_limit, "default_importance": DEFAULT_IMPORTANCE},
    )
    return _rank_bucket([_bucket_row(r) for r in rows], limit, now)


def fetch_project(session, project: str, limit: int, now=None) -> list:
    raw_limit = max(limit * BUCKET_OVERFETCH, limit + 5)
    rows = session.run(
        f"MATCH (m:Memory) WHERE m.project = $project "
        "AND NOT (m.path STARTS WITH 'profile/' OR m.path STARTS WITH 'tools/') "
        f"AND {_active('m')} "
        "RETURN m.path AS path, m.content AS content, "
        "       coalesce(m.importance, $default_importance) AS importance, "
        "       m.last_accessed_at AS last_accessed_at, "
        "       m.updated_at AS updated_at, m.ingested_at AS ingested_at "
        "ORDER BY coalesce(m.updated_at, '') DESC, m.path "
        "LIMIT $raw_limit",
        parameters={"project": project, "raw_limit": raw_limit, "default_importance": DEFAULT_IMPORTANCE},
    )
    return _rank_bucket([_bucket_row(r) for r in rows], limit, now)


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
                          project_limit: int = PROJECT_LIMIT, now=None) -> dict:
    return {
        "profile": fetch_bucket(session, "profile/", profile_limit, now),
        "tools": fetch_bucket(session, "tools/", tools_limit, now),
        "project": fetch_project(session, current_project, project_limit, now) if current_project else [],
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


EVENT_MIN_SCORE = float(os.environ.get("RECALL_EVENT_MIN_SCORE", "1.0"))


def event_search(session, query: str, limit: int = 3) -> list:
    """Fulltext over raw :Event prompt/tool_response — surfaces relevant session
    activity that hasn't been distilled into a memory yet (MemMachine: the
    episodic record is a first-class retrieval target). Returns event snippets
    (no path — events aren't memories). [] on any error or if the index is absent.
    A min-score gate keeps raw-event noise out."""
    if not (query or "").strip():
        return []
    raw_limit = max(limit * 3, limit + 5)
    try:
        rows = list(session.run(
            """
            CALL db.index.fulltext.queryNodes('event_fulltext', $q) YIELD node, score
            WHERE score > $min_score
            RETURN coalesce(node.prompt, node.tool_response, '') AS text,
                   node.event_name AS event_name, coalesce(node.tool_name, '') AS tool,
                   node.timestamp AS ts, score
            ORDER BY score DESC
            LIMIT $limit
            """,
            parameters={"q": escape_lucene(query), "min_score": EVENT_MIN_SCORE, "limit": raw_limit},
        ))
    except Exception:
        return []
    out = []
    for r in rows:
        snip = " ".join((r["text"] or "").split())[:200]
        if snip:
            out.append({"event_name": r["event_name"], "tool": r["tool"],
                        "ts": r["ts"], "snippet": snip, "score": r["score"]})
    return out[:limit]


def render_event_context(ev_rows: list) -> str:
    """Render raw-event hits as a clearly-labelled, separate section (kept apart
    from curated memories so the two are never confused)."""
    if not ev_rows:
        return ""
    lines = ["# Relevant prior activity (raw events, not yet distilled)\n"]
    for r in ev_rows:
        head = r["event_name"] + (f" {r['tool']}" if r.get("tool") else "")
        lines.append(f"- [{r.get('ts', '?')}] {head}: {r['snippet']}")
    return "\n".join(lines)


def memory_history(session, path: str):
    """A memory's version timeline (oldest → newest) for tracing how it evolved.

    Reconstructed from the Phase A :MemoryRevision chain: each revision is a
    snapshot of the body that was REPLACED (ordered by its `ts`), so chronological
    content = [revision snapshots oldest-first] + [the current node]. Returns None
    if the path doesn't exist."""
    rec = session.run(
        """
        MATCH (m:Memory {path: $path})
        OPTIONAL MATCH (r:MemoryRevision)-[:VERSION_OF]->(m)
        WITH m, r ORDER BY r.ts
        RETURN m.content AS current, m.updated_at AS updated_at,
               coalesce(m.status, 'active') AS status, m.created_by AS created_by,
               collect(r {.ts, .operation, .actor, content: r.content_snapshot}) AS revs
        """,
        path=path,
    ).single()
    if rec is None:
        return None
    revs = [rv for rv in (rec["revs"] or []) if rv and rv.get("ts") is not None]
    versions = []
    for i, rv in enumerate(revs):
        versions.append({"label": f"v{i + 1}", "ts": rv.get("ts"),
                         "operation": rv.get("operation"), "actor": rv.get("actor"),
                         "content": rv.get("content") or ""})
    versions.append({"label": "current", "ts": rec["updated_at"], "operation": "current",
                     "actor": rec["created_by"], "content": rec["current"] or ""})
    return {"path": path, "status": rec["status"], "versions": versions}


def content_as_of(versions: list, ts: str):
    """Reconstruct the body that was current at `ts` from the version timeline
    (oldest → newest; each revision's `ts` is when it was *replaced*, the current
    node's `ts` is when it became current). Returns the first version whose `ts`
    is after `ts` (it was the live body just before that replacement), else the
    current body. ISO timestamps compare lexicographically (all UTC)."""
    for v in versions:
        if v.get("ts") and str(v["ts"]) > str(ts):
            return v["content"]
    return versions[-1]["content"] if versions else None


def memory_lineage(session, path: str):
    """memory_history + provenance: the source events the memory was extracted
    from (Phase D :EXTRACTED_FROM) and its supersession links (Phase A). The
    human-facing 'how did this memory come to be?' view. None if path is absent."""
    hist = memory_history(session, path)
    if hist is None:
        return None
    events = []
    for r in session.run(
        "MATCH (m:Memory {path: $p})-[:EXTRACTED_FROM]->(e:Event) "
        "RETURN e.event_id AS event_id, e.event_name AS event_name, "
        "       coalesce(e.tool_name, '') AS tool, e.timestamp AS ts, "
        "       coalesce(e.prompt, e.tool_response, '') AS text "
        "ORDER BY e.timestamp LIMIT 25",
        p=path,
    ):
        events.append({"event_id": r["event_id"], "event_name": r["event_name"],
                       "tool": r["tool"], "ts": r["ts"],
                       "snippet": " ".join((r["text"] or "").split())[:160]})
    hist["source_events"] = events
    hist["superseded_by"] = [r["p"] for r in session.run(
        "MATCH (:Memory {path: $p})-[:SUPERSEDED_BY]->(n:Memory) RETURN n.path AS p", p=path)]
    hist["supersedes"] = [r["p"] for r in session.run(
        "MATCH (o:Memory)-[:SUPERSEDED_BY]->(:Memory {path: $p}) RETURN o.path AS p", p=path)]
    hist["contradicts"] = [r["p"] for r in session.run(
        "MATCH (:Memory {path: $p})-[:CONTRADICTS]-(c:Memory) RETURN DISTINCT c.path AS p", p=path)]
    return hist


def render_prompt(rows: list) -> tuple[str, list[str]]:
    """Render hybrid hits to injection markdown. Returns (markdown, paths)."""
    if not rows:
        return "", []
    parts = ["# Relevant memory for this prompt\n"]
    paths: list[str] = []
    for r in rows:
        parts.append(f"## {r['path']}\n{r['content']}\n")
        paths.append(r["path"])
    # Q6 inline citation footer: name the sources so the agent (and a human reading
    # the transcript) can see exactly which memories informed the context.
    parts.append(f"_memory used: {', '.join(paths)}_")
    return "\n".join(parts), paths
