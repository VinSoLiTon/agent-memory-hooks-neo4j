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
    python dream.py                                  # default provider (anthropic)
    python dream.py --session <id>                   # dream over one session
    python dream.py --since 24h                      # only events newer than 24h / 7d / 30m
    python dream.py --dry-run                        # print, don't write
    python dream.py --provider ollama                # use local Ollama (no API key)
    python dream.py --provider openai --model gpt-4o # use OpenAI

Provider precedence: --provider flag > $DREAM_PROVIDER > anthropic.
Default models: see dream/providers.py.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta

from neo4j import GraphDatabase

# Pull in project derivation from the hooks package so dream and capture
# share a single source of truth for "what is the project of this cwd?".
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from project import dominant_project  # noqa: E402
from providers import get_provider, default_model  # noqa: E402
import embeddings  # noqa: E402
import consolidate as consolidate_mod  # noqa: E402
import quality as quality_mod  # noqa: E402

# Windows consoles default to cp1252; memories from Claude routinely include
# em-dashes, arrows, smart quotes, etc. Force UTF-8 so the human-readable
# preview doesn't crash before write_memories runs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

# Output-token ceiling for the LLM's memory JSON. 4096 truncated the response
# mid-string on large sessions (many memories), producing invalid JSON that
# _extract_json_object couldn't parse. Bumped + env-overridable. It's only a
# ceiling — you pay for tokens actually generated, not this number.
MAX_TOKENS = int(os.environ.get("DREAM_MAX_TOKENS", "16384"))

# System prompts now live in dream/prompts.py (per-provider variants).
from prompts import system_prompt_for  # type: ignore  # noqa: E402


def get_driver():
    # PR-G #2: silence harmless "property does not exist" notifications.
    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )


def parse_since(s: str) -> datetime:
    m = re.fullmatch(r"(\d+)([hdm])", s)
    if not m:
        raise ValueError(f"--since must look like '24h', '7d', '30m'; got {s!r}")
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    return datetime.now(timezone.utc) - delta


def _walk_session_events(ses, session_key: str) -> list[dict]:
    """Walk one session's event chain a single NEXT hop at a time.

    Why not `MATCH (s)-[:FIRST_EVENT|NEXT*0..]->(e:Event)`? That unbounded
    variable-length expansion materializes a path for every reachable event and
    blows Neo4j's transaction-memory pool
    (Neo.TransientError.General.MemoryPoolOutOfMemoryError, default 2.7 GiB) on
    long or branched chains — in practice any session past ~150 events fails
    outright. Walking the linked list explicitly is a series of O(1) single-hop
    lookups with bounded memory. A `seen` set guards against corrupted chains
    (duplicate / branching NEXT edges) so a damaged graph can't loop forever.

    Returns the event property dicts in chain order (== append/timestamp order).
    """
    first = ses.run(
        "MATCH (s:Session {session_key: $sk})-[:FIRST_EVENT]->(e:Event) RETURN e",
        sk=session_key,
    ).single()
    if not first:
        return []
    events: list[dict] = []
    seen: set[str] = set()
    node = dict(first["e"])
    while node is not None:
        eid = node.get("event_id")
        if not eid or eid in seen:
            break  # missing id or a cycle/branch — stop rather than loop
        seen.add(eid)
        events.append(node)
        nxt = ses.run(
            "MATCH (:Event {event_id: $eid})-[:NEXT]->(n:Event) RETURN n LIMIT 1",
            eid=eid,
        ).single()
        node = dict(nxt["n"]) if nxt else None
    return events


def fetch_events(driver, session_id: str | None, since: datetime | None):
    """Return list of (session_key, [event_props, ...]) ordered chronologically.

    A session is included if it has at least one event newer than its
    `last_dreamed_at` watermark (or has never been dreamed). Events are read by
    walking each session's NEXT chain (see _walk_session_events) rather than via
    an unbounded variable-length path, which OOMs Neo4j's transaction-memory
    pool on large sessions. A cheap LATEST_EVENT timestamp check lets us skip
    walking sessions that have nothing new since their watermark.

    PR-G #5: --session accepts either the composite session_key or a raw
    session_id for ergonomics. If a raw id matches multiple sessions (across
    clients), we DON'T silently process all of them — we exit with a
    candidate list and ask for the explicit session_key. Same disambiguation
    rule as `njhook session <id>`.
    """
    since_iso = since.isoformat() if since else None

    with driver.session() as ses:
        # 1. Resolve the candidate sessions, each with its watermark and the
        #    timestamp of its LATEST_EVENT (cheap — one hop, no chain walk).
        if session_id:
            candidates = list(ses.run(
                "MATCH (s:Session) "
                "WHERE s.session_key = $sid OR s.session_id = $sid "
                "OPTIONAL MATCH (s)-[:LATEST_EVENT]->(last:Event) "
                "RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS sk, "
                "       s.client AS client, s.last_dreamed_at AS wm, last.timestamp AS latest",
                parameters={"sid": session_id},
            ))
            if not candidates:
                print(f"--session: no session matching {session_id!r}", file=sys.stderr)
                return []
            if len(candidates) > 1:
                print(
                    f"--session: raw id {session_id!r} matches {len(candidates)} sessions across clients:",
                    file=sys.stderr,
                )
                for c in candidates:
                    print(f"  {c['sk']}  (client={c['client']})", file=sys.stderr)
                print("\nRe-run with the explicit session_key (e.g. claude_code:<id>).", file=sys.stderr)
                return []
            targets = candidates
        else:
            targets = list(ses.run(
                "MATCH (s:Session) "
                "OPTIONAL MATCH (s)-[:LATEST_EVENT]->(last:Event) "
                "RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS sk, "
                "       s.last_dreamed_at AS wm, last.timestamp AS latest"
            ))

        # 2. Walk only sessions that have something new; filter to the events
        #    past the watermark (and >= --since) in Python.
        out: list[tuple[str, list[dict]]] = []
        for t in targets:
            sk, wm, latest = t["sk"], t["wm"], t["latest"]
            if latest is None:
                continue  # no events (no LATEST_EVENT) — nothing to dream
            if wm is not None and latest <= wm:
                continue  # nothing newer than the watermark
            if since_iso is not None and latest < since_iso:
                continue  # newest event predates the --since window
            events = _walk_session_events(ses, sk)
            qualifying = [
                e for e in events
                if (wm is None or (e.get("timestamp") or "") > wm)
                and (since_iso is None or (e.get("timestamp") or "") >= since_iso)
            ]
            if qualifying:
                qualifying.sort(key=lambda e: e.get("timestamp") or "")
                out.append((sk, qualifying))
    return out


def fetch_existing_memories(driver, project: str | None = None) -> list[dict]:
    """Memories to show the model as merge/dedup context.

    Scoped to what THIS session could legitimately update: cross-project
    `profile/` + `tools/`, plus memories tagged with the session's own project.
    Feeding every project's memories (the old behaviour) bloated the context to
    tens of KB and swamped small local models — they regurgitated unrelated
    existing memories or returned nothing, so the nightly distilled little.
    Superseded/archived memories are excluded (Phase A). A hard char cap
    (`DREAM_EXISTING_MAX_CHARS`, default 12000) is a final backstop, dropping the
    largest memories first.
    """
    cap = int(os.environ.get("DREAM_EXISTING_MAX_CHARS", "12000"))
    with driver.session() as ses:
        if project:
            result = ses.run(
                "MATCH (m:Memory) WHERE coalesce(m.status, 'active') = 'active' "
                "AND coalesce(m.archived, false) = false "
                "AND (m.path STARTS WITH 'profile/' OR m.path STARTS WITH 'tools/' "
                "     OR m.project = $project) "
                "RETURN m.path AS path, m.content AS content ORDER BY m.path",
                project=project,
            )
        else:
            result = ses.run(
                "MATCH (m:Memory) WHERE coalesce(m.status, 'active') = 'active' "
                "AND coalesce(m.archived, false) = false "
                "RETURN m.path AS path, m.content AS content ORDER BY m.path"
            )
        mems = [dict(r) for r in result]
    if sum(len(m["content"] or "") for m in mems) > cap:
        kept, used = [], 0
        for m in sorted(mems, key=lambda x: len(x["content"] or "")):  # smallest first
            if used + len(m["content"] or "") > cap:
                continue
            kept.append(m)
            used += len(m["content"] or "")
        mems = sorted(kept, key=lambda x: x["path"])
    return mems


def _summarize_tool_response(tr) -> str:
    """One-line summary of a tool response for the dream input. Reduces a
    multi-KB raw tool dump to a signal line: success/failure + a snippet."""
    s = str(tr)
    # Heuristics: pluck out exit_code if present; cap snippet to 80 chars.
    snippet = " ".join(s.split())[:80]
    return snippet


def _render_one(e: dict) -> str:
    """PR-C trim render of a single event — full prompt, but tool I/O collapses
    to a one-liner. Signal-bearing fields are what inform memory extraction."""
    lines = [f"[{e.get('timestamp', '?')}] {e.get('event_name', '?')}"
             + (f" tool={e['tool_name']}" if e.get("tool_name") else "")]
    if e.get("prompt"):
        lines.append(f"  prompt: {e['prompt']}")  # highest-signal field — keep full
    if e.get("tool_input"):
        ti = e["tool_input"]
        try:
            ti_obj = json.loads(ti) if isinstance(ti, str) else ti
            if isinstance(ti_obj, dict):
                key_field = (ti_obj.get("command") or ti_obj.get("file_path")
                             or ti_obj.get("path") or str(ti_obj))
                lines.append(f"  input:  {str(key_field)[:200]}")
            else:
                lines.append(f"  input:  {str(ti)[:200]}")
        except Exception:
            lines.append(f"  input:  {str(ti)[:200]}")
    if e.get("tool_response"):
        lines.append(f"  output: {_summarize_tool_response(e['tool_response'])}")
    return "\n".join(lines)


def render_events(events: list[dict], max_chars: int | None = None) -> str:
    """Render events to a transcript, optionally bounded to `max_chars`.

    Real sessions run to thousands of events (hundreds of KB); feeding that whole
    transcript to a small local model overflows its context and it returns nothing
    (qwen3.5 produced 0 memories on every real session until this cap). When
    `max_chars` is set we keep a coherent, signal-first slice: the most-recent
    UserPromptSubmit/BeforeAgent prompts first (they carry the session's intent),
    then the most-recent tool events that still fit, re-emitted in chronological
    order with a note of how many were dropped. `max_chars=None` = unbounded
    (frontier models handle the full transcript and distil it better)."""
    blocks = [(i, bool(e.get("prompt")), _render_one(e)) for i, e in enumerate(events)]
    if max_chars is None:
        return "\n".join(text for _, _, text in blocks)

    chosen: dict[int, str] = {}
    used = 0
    # 1) most-recent prompt-bearing events first — the session's intent.
    for i, has_prompt, text in reversed(blocks):
        if not has_prompt:
            continue
        if used + len(text) + 1 > max_chars:
            continue
        chosen[i] = text
        used += len(text) + 1
    # 2) fill the remaining budget with the most-recent tool events.
    for i, has_prompt, text in reversed(blocks):
        if has_prompt or i in chosen:
            continue
        if used + len(text) + 1 > max_chars:
            continue
        chosen[i] = text
        used += len(text) + 1

    out = [chosen[i] for i in sorted(chosen)]
    omitted = len(blocks) - len(chosen)
    if omitted > 0:
        out.append(f"\n[... {omitted} lower-signal events omitted to fit the "
                   f"transcript budget ({max_chars} chars) ...]")
    return "\n".join(out)


def render_existing(memories: list[dict], paths_only: bool = False) -> str:
    if not memories:
        return "(no existing memories)"
    if paths_only:
        # Small local models can't reliably extract the new session through full
        # existing-memory bodies — they regurgitate or stall. Give them just the
        # existing paths so they reuse a path when updating one, without the bloat.
        # Content-level merge for this path is handled later by Phase A
        # supersession + `dream.py --consolidate`.
        return "Existing memory paths (reuse a path to update it):\n" + "\n".join(
            f"- {m['path']}" for m in memories
        )
    parts = []
    for m in memories:
        parts.append(f"### {m['path']}\n```\n{m['content']}\n```")
    return "\n\n".join(parts)


def call_provider(provider_fn, transcript: str, existing: str, model: str,
                  system_prompt: str) -> list[dict]:
    """Thin wrapper so call sites don't need to know provider internals."""
    return provider_fn(
        transcript=transcript,
        existing=existing,
        system=system_prompt,
        model=model,
        max_tokens=MAX_TOKENS,
    )


def _coerce_importance(v):
    """Clamp a model-supplied importance to an int in [1,10]; None if absent/invalid.
    Phase C2: importance is optional — a missing/garbage value leaves the field
    unset and recall treats it as neutral."""
    if v is None:
        return None
    try:
        return max(1, min(10, int(v)))
    except (TypeError, ValueError):
        return None


_ATTR_TOKEN_RE = re.compile(r"[a-z0-9_]{4,}")


def _attr_tokens(text: str) -> set:
    return set(_ATTR_TOKEN_RE.findall((text or "").lower()))


def attribute_events(content: str, events: list[dict], k: int, min_overlap: int) -> list[str]:
    """Phase D — heuristic claim-level provenance. Link a memory to the top-K source
    events whose text most overlaps it (token-set intersection). Deterministic, no
    model call, bounded to K edges per memory (so no edge explosion on large
    sessions — the reason :EXTRACTED_FROM was deferred). Approximate; a later upgrade
    is model-cited source events for precision."""
    mem = _attr_tokens(content)
    if not mem:
        return []
    scored = []
    for e in events:
        eid = e.get("event_id")
        if not eid:
            continue
        et = _attr_tokens(" ".join(
            str(e.get(f) or "") for f in ("prompt", "tool_input", "tool_response", "tool_name")
        ))
        ov = len(mem & et)
        if ov >= min_overlap:
            scored.append((ov, eid))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [eid for _, eid in scored[:k]]


def write_memories(driver, session_key: str, memories: list[dict], watermark: str, project: str | None = None, provider: str = "unknown", model: str = "unknown", events: list[dict] | None = None) -> int:
    """Upsert memories and advance the session's last_dreamed_at watermark.

    `watermark` is the timestamp of the latest event we just dreamed over —
    future runs will only re-dream the session if newer events arrive.

    `project` is the dominant project slug for the session (derived from event
    cwds). Memories whose path starts with profile/ or tools/ are considered
    cross-project and stay untagged so they surface in every session; everything
    else (project/, general/, etc.) is tagged with this project so recall can
    boost in-project hits.

    If EMBED_PROVIDER is set, embeddings are computed in one batch call and
    written alongside the memory. Failures fall back gracefully — content is
    still saved without embedding.
    """
    now = datetime.now(timezone.utc).isoformat()
    # PR-H #2: quality gate before any DB write — reject malformed paths,
    # missing/invalid frontmatter, oversize bodies, and any body that contains
    # a secret-shaped string the model regenerated. Each rejection is logged
    # to stderr; the dream run continues with the remaining valid memories.
    valid = quality_mod.validate_batch(
        m for m in memories if m.get("path") and m.get("content")
    )

    # Phase D2: A-MAC grounding admission gate. Score each memory's overlap with
    # the source transcript; a NEW memory below threshold is routed to
    # 'pending_review' (recall hides it; `njhook review` adjudicates) instead of
    # going straight to 'active'. Updates to an EXISTING active memory are NOT
    # gated — we never hide a previously-good memory behind a suspicious update.
    source_text = " ".join(
        str(e.get(f) or "") for e in (events or [])
        for f in ("prompt", "tool_input", "tool_response")
    )
    ground_min = float(os.environ.get("DREAM_GROUNDING_MIN", "0.10"))
    existing_active: set = set()
    if valid and source_text:
        with driver.session() as _ses:
            existing_active = {r["p"] for r in _ses.run(
                "MATCH (m:Memory) WHERE m.path IN $paths "
                "AND coalesce(m.status, 'active') = 'active' RETURN m.path AS p",
                paths=[m["path"] for m in valid])}
    mem_status: dict = {}
    held = 0
    for m in valid:
        g = quality_mod.grounding_score(m["content"], source_text) if source_text else 1.0
        if g < ground_min and m["path"] not in existing_active:
            mem_status[m["path"]] = "pending_review"
            held += 1
        else:
            mem_status[m["path"]] = "active"
    if held:
        print(f"  grounding gate: {held} low-grounding memory(ies) → pending_review "
              f"(adjudicate with `njhook review`)", file=sys.stderr)

    embeds: list[list[float]] = []
    embed_dim: int | None = None
    if valid and embeddings.is_enabled():
        try:
            texts = [embeddings.memory_text(m["path"], m["content"]) for m in valid]
            embeds = embeddings.embed(texts)
            embed_dim = len(embeds[0]) if embeds and embeds[0] else None
        except Exception as e:
            print(f"  warn: embedding failed, writing memories without vectors: {e}", file=sys.stderr)
            embeds = []

    embed_model_name = embeddings.model() if (valid and embeddings.is_enabled() and embeds) else None
    rows = []
    for i, m in enumerate(valid):
        rows.append({
            "path": m["path"],
            "content": m["content"],
            "updated_at": now,
            "created_by": f"dream_{provider}",
            "status": mem_status[m["path"]],
            "importance": _coerce_importance(m.get("importance")),
            "project": None
            if m["path"].startswith(("profile/", "tools/")) or not project
            else project,
            "embedding": embeds[i] if embeds and i < len(embeds) else None,
            # H5: track which model produced the embedding and at what dimension.
            # Lets `njhook reindex` detect mismatches when the embedding model changes.
            "embedding_model": embed_model_name if embeds and i < len(embeds) else None,
            "embedding_dim": embed_dim if embeds and i < len(embeds) else None,
        })

    with driver.session() as ses:
        # H2: always advance the watermark, even when no memories were produced.
        # Otherwise low-signal sessions get re-dreamed every run forever.
        ses.run(
            "MATCH (s:Session {session_key: $session_key}) SET s.last_dreamed_at = $watermark",
            parameters={"session_key": session_key, "watermark": watermark},
        )

        if not rows:
            return 0

        ses.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
        if embed_dim:
            ses.run(
                f"""
                CREATE VECTOR INDEX memory_embeddings IF NOT EXISTS
                FOR (m:Memory) ON m.embedding
                OPTIONS {{ indexConfig: {{
                  `vector.dimensions`: {embed_dim},
                  `vector.similarity_function`: 'cosine'
                }} }}
                """
            )
        # Phase A: non-destructive write. A :DreamRun records this run's provenance;
        # WROTE edges link it to every memory it touched. On a content change at an
        # existing path we snapshot the prior body into an immutable :MemoryRevision
        # (path is UNIQUE — one node per path stays the "current" view) so a memory's
        # evolution is fully traceable. An identical-content write produces no revision.
        run_id = f"{session_key}@{now}"
        ses.run(
            """
            MATCH (s:Session {session_key: $session_key})
            MERGE (dr:DreamRun {run_id: $run_id})
              ON CREATE SET dr.ts = $now, dr.provider = $provider, dr.model = $model
            WITH s, dr
            UNWIND $rows AS row
            MERGE (m:Memory {path: row.path})
            WITH s, dr, row, m,
                 m.content AS prior_content,
                 coalesce(m.status, 'active') AS prior_status,
                 (m.content IS NOT NULL AND m.content <> row.content) AS changed
            FOREACH (_ IN CASE WHEN changed THEN [1] ELSE [] END |
                CREATE (rev:MemoryRevision {
                    content_snapshot: prior_content,
                    status: prior_status,
                    operation: 'dream_update',
                    actor: row.created_by,
                    ts: $now
                })
                MERGE (rev)-[:VERSION_OF]->(m)
            )
            SET m.content = row.content,
                m.updated_at = row.updated_at,
                m.ingested_at = $now,
                m.status = row.status,
                m.created_by = row.created_by,
                m.importance = coalesce(row.importance, m.importance),
                m.valid_from = coalesce(m.valid_from, $now),
                // M3: cross-project paths (profile/, tools/) ALWAYS clear any
                // stale project tag. Project-scoped paths get the new project
                // when supplied, else preserve the existing tag.
                m.project = CASE
                  WHEN row.path STARTS WITH 'profile/' OR row.path STARTS WITH 'tools/' THEN null
                  WHEN row.project IS NOT NULL THEN row.project
                  ELSE m.project
                END
            FOREACH (_ IN CASE WHEN row.embedding IS NOT NULL THEN [1] ELSE [] END |
                SET m.embedding = row.embedding,
                    m.embedding_model = row.embedding_model,
                    m.embedding_dim = row.embedding_dim
            )
            MERGE (s)-[:DREAMED]->(m)
            MERGE (m)-[:DERIVED_FROM]->(s)
            MERGE (dr)-[:WROTE]->(m)
            """,
            parameters={
                "session_key": session_key, "rows": rows,
                "run_id": run_id, "now": now,
                "provider": provider, "model": model,
            },
        )

        # Phase D: claim-level provenance — link each memory to its top-K most
        # textually-overlapping source events (heuristic; bounded, no explosion).
        # This is what the Phase F lineage view and C3 nucleus expansion walk.
        if events:
            topk = int(os.environ.get("DREAM_EXTRACT_TOPK", "3"))
            min_ov = int(os.environ.get("DREAM_EXTRACT_MIN_OVERLAP", "2"))
            links = []
            for m in valid:
                for eid in attribute_events(m["content"], events, topk, min_ov):
                    links.append({"path": m["path"], "eid": eid})
            if links:
                ses.run(
                    """
                    UNWIND $links AS lnk
                    MATCH (m:Memory {path: lnk.path})
                    MATCH (e:Event {event_id: lnk.eid})
                    MERGE (m)-[:EXTRACTED_FROM]->(e)
                    """,
                    parameters={"links": links},
                )
    return len(rows)


def egress_blocked(provider_name: str, session_sensitive: bool, allow_egress: bool) -> bool:
    """Phase H egress policy: a high-sensitivity session must not be sent to a
    remote dream provider (anthropic/openai) unless DREAM_ALLOW_SENSITIVE_EGRESS=1.
    Local (ollama) is always allowed. Returns True when the call must be skipped."""
    return provider_name in ("anthropic", "openai") and session_sensitive and not allow_egress


def resolve_fallback(primary_name: str, fallback_name: str | None, has_key) -> str | None:
    """Which provider to retry a 0-yield session on, or None if hybrid fallback is
    off/unavailable. `has_key(name)` reports whether that provider's API key is set.
    Returns None when the fallback is disabled ('none'/empty), equals the primary,
    or is a hosted provider whose key isn't configured (degrade to local-only)."""
    fb = (fallback_name or "").strip().lower()
    if not fb or fb in ("none", "off", primary_name):
        return None
    if fb in ("anthropic", "openai") and not has_key(fb):
        return None
    return fb


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--session", help="dream over a single session_id")
    ap.add_argument("--since", help="only include events newer than e.g. 24h, 7d, 30m")
    ap.add_argument("--dry-run", action="store_true", help="print memories, don't write")
    ap.add_argument(
        "--provider",
        choices=["anthropic", "openai", "ollama"],
        help="LLM backend (default: $DREAM_PROVIDER or anthropic)",
    )
    ap.add_argument("--model", help="override the provider's default model")
    # Consolidation / archival modes (mutually exclusive with the per-session
    # distillation that's the default behavior).
    ap.add_argument("--consolidate", action="store_true",
                    help="merge near-duplicate memories instead of distilling sessions")
    ap.add_argument("--consolidate-threshold", type=float, default=0.92,
                    help="cosine similarity above which memories are candidates to merge")
    ap.add_argument("--consolidate-rounds", type=int, default=10,
                    help="max merge rounds before exiting")
    ap.add_argument("--archive", action="store_true",
                    help="flag stale memories as archived (excluded from recall)")
    ap.add_argument("--stale-days", type=int, default=60,
                    help="memories untouched for this many days are archive-eligible")
    args = ap.parse_args()

    provider_name, provider_fn = get_provider(args.provider)
    model = args.model or default_model(provider_name)
    if not (args.consolidate or args.archive):
        print(f"provider={provider_name} model={model}")

    # Provider-specific preflight: only Anthropic and OpenAI need a key in env;
    # Ollama just needs a reachable local server (checked at first call).
    needs_llm = not args.archive  # archive doesn't call any LLM
    if needs_llm:
        if provider_name == "anthropic" and not os.environ.get("ANTHROPIC_API_KEY"):
            print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
            sys.exit(1)
        if provider_name == "openai" and not os.environ.get("OPENAI_API_KEY"):
            print("OPENAI_API_KEY is not set", file=sys.stderr)
            sys.exit(1)

    since = parse_since(args.since) if args.since else None
    driver = get_driver()
    try:
        if args.archive:
            consolidate_mod.archive(driver, stale_days=args.stale_days, dry_run=args.dry_run)
            return
        if args.consolidate:
            embed_fn = embeddings.embed if embeddings.is_enabled() else None
            consolidate_mod.consolidate(
                driver,
                provider_name=args.provider,
                threshold=args.consolidate_threshold,
                max_rounds=args.consolidate_rounds,
                dry_run=args.dry_run,
                embed_fn=embed_fn,
            )
            return

        sessions = fetch_events(driver, args.session, since)
        if not sessions:
            print("nothing to dream about.")
            return
        system_prompt = system_prompt_for(provider_name, model)
        # Frontier models handle full existing-memory bodies for inline merge; small
        # local models can't, so they get a paths-only context (proven: full bodies
        # make qwen3.5/gemma4 stall or regurgitate). Existing is also scoped to this
        # session's project + cross-project profile/tools so a growing graph never
        # swamps the model.
        paths_only = provider_name not in ("anthropic", "openai")
        # Local models also get a bounded transcript (large real sessions overflow
        # their context → 0 memories); frontier models get the full transcript.
        transcript_cap = int(os.environ.get("DREAM_TRANSCRIPT_MAX_CHARS", "16000")) if paths_only else None

        # Hybrid fallback: small local models reliably fail to distil large, real
        # sessions (qwen returns empty, gemma hallucinates). When the local primary
        # yields 0 for a session, retry just that session on a frontier fallback
        # (default Anthropic) with the full transcript + full existing context.
        # Only the sessions the local model can't handle egress; the rest stay local.
        fallback = None
        if paths_only:
            fb_name = resolve_fallback(
                provider_name, os.environ.get("DREAM_FALLBACK_PROVIDER", "anthropic"),
                lambda n: bool(os.environ.get("ANTHROPIC_API_KEY")) if n == "anthropic"
                else bool(os.environ.get("OPENAI_API_KEY")),
            )
            if fb_name:
                _, fb_fn = get_provider(fb_name)
                fb_model = default_model(fb_name)
                fallback = (fb_name, fb_fn, fb_model, system_prompt_for(fb_name, fb_model))
                print(f"hybrid: primary={provider_name}/{model}, fallback={fb_name}/{fb_model} on 0-yield sessions")

        # Phase H egress policy: high-sensitivity sessions stay off remote providers.
        allow_egress = os.environ.get("DREAM_ALLOW_SENSITIVE_EGRESS") == "1"

        for session_key, events in sessions:
            project = dominant_project([e.get("cwd") for e in events])
            session_sensitive = any(e.get("sensitivity") == "high" for e in events)
            # Primary provider is remote + session is sensitive → don't egress; skip.
            if egress_blocked(provider_name, session_sensitive, allow_egress):
                print(f"\n=== skipping {session_key}: sensitive session, remote egress blocked "
                      f"(DREAM_ALLOW_SENSITIVE_EGRESS=1 to allow) ===")
                continue
            existing = render_existing(fetch_existing_memories(driver, project), paths_only=paths_only)
            label = f"{session_key}" + (f"  project={project}" if project else "")
            print(f"\n=== dreaming over {label} ({len(events)} new events"
                  + ("; SENSITIVE" if session_sensitive else "") + ") ===")
            used_name, used_model = provider_name, model
            memories = call_provider(provider_fn, render_events(events, max_chars=transcript_cap), existing, model, system_prompt)
            # Fall back only if it won't egress a sensitive session to a remote provider.
            if not memories and fallback and not egress_blocked(fallback[0], session_sensitive, allow_egress):
                fb_name, fb_fn, fb_model, fb_system = fallback
                print(f"  local yielded 0 — falling back to {fb_name}/{fb_model} for this session")
                try:
                    fb_existing = render_existing(fetch_existing_memories(driver, project), paths_only=False)
                    fb_mems = call_provider(fb_fn, render_events(events), fb_existing, fb_model, fb_system)
                    if fb_mems:
                        memories, used_name, used_model = fb_mems, fb_name, fb_model
                except Exception as e:
                    print(f"  fallback failed: {e}", file=sys.stderr)
            elif not memories and fallback and session_sensitive:
                print(f"  local yielded 0; {fallback[0]} fallback skipped (sensitive session, egress blocked)",
                      file=sys.stderr)
            for m in memories:
                print(f"\n--- {m.get('path')} ---")
                print(m.get("content", ""))
            if not args.dry_run:
                watermark = events[-1].get("timestamp")
                n = write_memories(driver, session_key, memories, watermark, project=project, provider=used_name, model=used_model, events=events)
                print(f"\n  wrote/updated {n} memories (via {used_name}); watermark -> {watermark}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
