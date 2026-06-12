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
import time
from datetime import datetime, timezone, timedelta

from neo4j import GraphDatabase

# Pull in project derivation from the hooks package so dream and capture
# share a single source of truth for "what is the project of this cwd?".
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "hooks"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from project import dominant_project  # noqa: E402
from providers import get_provider, default_model, estimate_cost  # noqa: E402
import embeddings  # noqa: E402
import consolidate as consolidate_mod  # noqa: E402
import quality as quality_mod  # noqa: E402
import review as review_mod  # noqa: E402  (Phase E — contradiction detection/flagging)
import recall as recall_mod  # noqa: E402  (fulltext candidate channel for contradiction detection)
import judge as judge_mod  # noqa: E402  (Phase E PR-3 — LLM contradiction judge)
import critic as critic_mod  # noqa: E402  (item #18 — LLM faithfulness critique pass)
import memory_types  # noqa: E402  (Phase D1 — semantic kind vocabulary)

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


def _derived_transcript_cap(provider_name: str) -> int:
    """Item #19: the local-provider transcript char cap. DREAM_TRANSCRIPT_MAX_CHARS,
    if set, is honored verbatim (back-compat). Otherwise, for llama.cpp, derive it
    from the server's n_ctx so the dream-layer slice stays a comfortable fraction
    (DREAM_TRANSCRIPT_CTX_FRACTION, default 0.7) under the provider-layer trim —
    a bigger server → a bigger slice, no per-window summarization. The max(8000,..)
    floor preserves today's behaviour on a tiny/8192 server; non-llamacpp local
    (e.g. ollama, n_ctx unknown) falls back to the historical 16000."""
    env = os.environ.get("DREAM_TRANSCRIPT_MAX_CHARS")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if provider_name == "llamacpp":
        try:
            from providers import _llamacpp_n_ctx, LLAMACPP_CHAT_URL, _CHARS_PER_TOK
            n_ctx = _llamacpp_n_ctx(LLAMACPP_CHAT_URL.rstrip("/"))
            reserve = int(os.environ.get("LLAMACPP_OUTPUT_RESERVE", "3072"))
            frac = float(os.environ.get("DREAM_TRANSCRIPT_CTX_FRACTION", "0.7"))
            return max(8000, int((n_ctx - reserve - 256) * _CHARS_PER_TOK * frac))
        except Exception:
            pass
    return 16000


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


def _order_targets(targets: list) -> list:
    """Item #24: oldest-first session order so a --max-sessions cap drains the
    backlog FIFO and the next run resumes where this stopped. never-dreamed (wm
    NULL) sessions are the oldest backlog → first; then ascending watermark; then
    session_key as a stable tiebreak. Each target is a Record/dict with wm + sk."""
    return sorted(targets, key=lambda t: (t["wm"] is not None, t["wm"] or "", t["sk"]))


def fetch_events(driver, session_id: str | None, since: datetime | None, max_sessions: int | None = None):
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

        # Item #24: drain the backlog oldest-first so a --max-sessions cap is FIFO
        # and the next run resumes where this one stopped (the watermark is the
        # implicit cursor — no new state).
        targets = _order_targets(targets)

        # 2. Walk only sessions that have something new; filter to the events
        #    past the watermark (and >= --since) in Python. Stop at max_sessions.
        out: list[tuple[str, list[dict]]] = []
        capped = False
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
                if max_sessions is not None and len(out) >= max_sessions:
                    capped = True
                    break
    if capped:
        print(f"--max-sessions: processed {len(out)} session(s) this run; the rest of the "
              f"backlog (oldest-first) will be picked up next run.", file=sys.stderr)
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


def _key_tool_input(ti, limit: int = 200) -> str:
    """The signal-bearing field of a tool_input (command / file_path / path),
    capped. Shared by the verbose and compact renderers."""
    if not ti:
        return ""
    try:
        obj = json.loads(ti) if isinstance(ti, str) else ti
        if isinstance(obj, dict):
            key = obj.get("command") or obj.get("file_path") or obj.get("path") or obj
            return str(key)[:limit]
    except Exception:
        pass
    return str(ti)[:limit]


def _render_one(e: dict) -> str:
    """PR-C trim render of a single event — full prompt, but tool I/O collapses
    to a one-liner. Signal-bearing fields are what inform memory extraction."""
    lines = [f"[{e.get('timestamp', '?')}] {e.get('event_name', '?')}"
             + (f" tool={e['tool_name']}" if e.get("tool_name") else "")]
    if e.get("prompt"):
        lines.append(f"  prompt: {e['prompt']}")  # highest-signal field — keep full
    if e.get("tool_input"):
        lines.append(f"  input:  {_key_tool_input(e['tool_input'])}")
    if e.get("tool_response"):
        lines.append(f"  output: {_summarize_tool_response(e['tool_response'])}")
    return "\n".join(lines)


# --- compact "dream language" (DREAM_COMPACT_TRANSCRIPT=1) -------------------
# Same signal, less scaffolding: a short index replaces the 32-char ISO timestamp,
# a PreToolUse+PostToolUse pair collapses to ONE `tool(input) -> output` line (so a
# tool call costs one header, not two), and event names shrink to a marker. The
# high-signal prompt body stays full. Lets ~2x more events fit the transcript budget
# for tool-heavy sessions; default-off until the A/B confirms quality holds.

def _merge_tool_pairs(events: list[dict]) -> list[dict]:
    """Collapse a PreToolUse immediately followed by its matching PostToolUse
    (same tool, same tool_use_id when present) into one logical event carrying both
    the input and the response. Unpaired events pass through unchanged."""
    out: list[dict] = []
    i, n = 0, len(events)
    while i < n:
        e = events[i]
        nxt = events[i + 1] if i + 1 < n else None
        if (e.get("event_name") == "PreToolUse" and nxt is not None
                and nxt.get("event_name") == "PostToolUse"
                and e.get("tool_name") == nxt.get("tool_name")
                and (not e.get("tool_use_id") or e.get("tool_use_id") == nxt.get("tool_use_id"))):
            merged = dict(e)
            merged["tool_response"] = nxt.get("tool_response")
            out.append(merged)
            i += 2
        else:
            out.append(e)
            i += 1
    return out


def _render_one_compact(e: dict, idx: int) -> str:
    """Scaffolding-lean single-line render. `idx` is the event's position (order is
    the only temporal signal the model needs); the prompt body is kept verbatim."""
    if e.get("prompt"):
        return f"#{idx} > {e['prompt']}"                       # user intent — full
    if e.get("tool_name"):
        ti = _key_tool_input(e.get("tool_input"))
        out = _summarize_tool_response(e["tool_response"]) if e.get("tool_response") else ""
        s = f"#{idx} {e['tool_name']}" + (f"({ti})" if ti else "")
        return s + (f" -> {out}" if out else "")
    return f"#{idx} {e.get('event_name', '?')}"


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
    if os.environ.get("DREAM_COMPACT_TRANSCRIPT") == "1":
        logical = _merge_tool_pairs(events)
        blocks = [(i, bool(e.get("prompt")), _render_one_compact(e, i))
                  for i, e in enumerate(logical)]
    else:
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
                  system_prompt: str, usage_out: dict | None = None) -> list[dict]:
    """Thin wrapper so call sites don't need to know provider internals. When
    `usage_out` is given (item #13), the provider fills it in place with
    {input_tokens, output_tokens} — additive, never changes the return type."""
    return provider_fn(
        transcript=transcript,
        existing=existing,
        system=system_prompt,
        model=model,
        max_tokens=MAX_TOKENS,
        usage_out=usage_out,
    )


def safe_distil(provider_fn, transcript: str, existing: str, model: str,
                system_prompt: str, provider_name: str = "local",
                usage_out: dict | None = None, error_out: list | None = None) -> list[dict]:
    """Call the primary provider but NEVER raise: on any error (unreachable,
    HTTP 4xx, malformed JSON, timeout) return [] so the run doesn't crash (PR-4).

    A TRANSIENT error is NOT the same as a genuine 0-yield: when the local model is
    busy/unreachable, an empty result means "couldn't assess this session", not
    "nothing to remember". `error_out` (a list, when provided) is appended a truthy
    marker on any exception so the caller can DEFER the session — leave the watermark
    where it is and retry next run — instead of advancing past it (which would drop
    the session forever) or egressing to a remote fallback on a blip. A clean empty
    result leaves `error_out` untouched. On error `usage_out` is left as-is."""
    try:
        return call_provider(provider_fn, transcript, existing, model, system_prompt, usage_out=usage_out)
    except Exception as e:
        print(f"  primary {provider_name} errored ({type(e).__name__}: {str(e)[:140]}); "
              f"deferring this session (will retry next run)", file=sys.stderr)
        if error_out is not None:
            error_out.append(True)
        return []


def _defer_session(errored: bool, memories: list) -> bool:
    """True when a session should be DEFERRED rather than recorded as dreamed.

    A transient provider error (server busy / unreachable / timeout) with nothing
    produced means we couldn't assess the session — the local model was occupied,
    not that the session was empty. Deferring skips the write so the watermark is
    NOT advanced and the next scheduled run retries it (no egress on a blip). A
    clean empty result (no error) is a genuine 0-yield → not deferred, so the
    watermark still advances and we don't re-dream empty sessions forever."""
    return bool(errored) and not memories


# Deterministic importance priors by semantic kind — used as a fallback ONLY when
# the model omits/garbles importance (a recognized model value always wins). Gives
# ranking a real signal even on local models that skip the field: durable identity
# / hard rules sit high; produced artifacts and transient observations sit low.
# Resurrects IMPLEMENTATION_PLAN D2 (content-type prior). Keyed by normalized kind.
DEFAULT_KIND_IMPORTANCE = 4  # floor for an unrecognized kind (normalize_kind → context)
_KIND_IMPORTANCE_PRIOR = {
    "constraint": 8, "preference": 7, "projectrule": 7, "decision": 7,
    "goal": 6, "commitment": 6, "procedure": 6, "fact": 6,
    "toolpattern": 5, "incident": 5, "learning": 5,
    "context": 4, "openquestion": 4, "observation": 3, "artifact": 3,
}


def _coerce_importance(v, kind=None):
    """Clamp a model-supplied importance to an int in [1,10]. A recognized value
    always wins. When the model omits/garbles it, fall back to a deterministic
    per-kind prior (so ranking still has a real signal) instead of None; if no
    kind is given either, return None (recall then treats it as neutral)."""
    try:
        return max(1, min(10, int(v)))
    except (TypeError, ValueError):
        pass
    if kind is not None:
        return _KIND_IMPORTANCE_PRIOR.get(memory_types.normalize_kind(kind), DEFAULT_KIND_IMPORTANCE)
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


def _local_merge_pass(driver, valid: list[dict], provider: str, model: str, provider_fn=None) -> int:
    """Item #16 — for LOCAL providers (llamacpp/ollama) only, when a candidate
    collides with an EXISTING active memory at the same path AND the body differs,
    make ONE LLM merge call (prior body + new body) and replace the candidate's
    content IN PLACE. Local models see existing memories as paths-only, so a
    same-path UPDATE otherwise clobbers the accumulated body (recoverable via
    MemoryRevision, but a fidelity regression). Default-OFF (DREAM_LOCAL_MERGE=1),
    capped (DREAM_LOCAL_MERGE_MAX, default 10). Per-collision failure falls back to
    the raw candidate (today's clobber) — never crashes. NO delete/add/noop, NO
    path-rewrite. Returns the number merged. `provider_fn` is injectable for tests;
    else resolved via get_provider."""
    if os.environ.get("DREAM_LOCAL_MERGE") != "1" or provider not in ("llamacpp", "ollama"):
        return 0
    if provider_fn is None:
        try:
            provider_fn = get_provider(provider)[1]
        except Exception:
            return 0
    cap = int(os.environ.get("DREAM_LOCAL_MERGE_MAX", "10"))
    paths = [m["path"] for m in valid]
    try:
        with driver.session() as _ses:
            stored = {r["p"]: r["c"] for r in _ses.run(
                "MATCH (m:Memory) WHERE m.path IN $paths "
                "AND coalesce(m.status, 'active') = 'active' "
                "RETURN m.path AS p, m.content AS c", paths=paths)}
    except Exception:
        return 0
    merged = 0
    for m in valid:
        if merged >= cap:
            break
        prior = stored.get(m["path"])
        if not prior or prior == m["content"]:
            continue
        try:
            out = consolidate_mod._merge_pair(provider_fn, model, m["path"], prior, m["path"], m["content"])
            cand = {**m, "content": out.get("content") or ""}
            # the merge runs after validate_batch, so re-validate the merged body —
            # a malformed local-model merge must NOT bypass the quality gate; on any
            # problem keep the raw new body (today's clobber).
            if cand["content"] and quality_mod.validate_memory(cand) == []:
                m["content"] = cand["content"]   # keep the path fixed; take merged body only
                merged += 1
            else:
                print(f"  local-merge: {m['path']} merged body invalid; keeping new body", file=sys.stderr)
        except Exception as e:
            print(f"  local-merge: {m['path']} merge failed ({type(e).__name__}); keeping new body",
                  file=sys.stderr)
    if merged:
        print(f"  local-merge: merged {merged} same-path collision(s) into the prior body "
              f"(DREAM_LOCAL_MERGE)", file=sys.stderr)
    return merged


def write_memories(driver, session_key: str, memories: list[dict], watermark: str, project: str | None = None, provider: str = "unknown", model: str = "unknown", events: list[dict] | None = None, contradiction_judge=None, find_candidates=None, usage: dict | None = None, cost: float | None = None, provider_fn=None, critic_fn=None) -> int:
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

    # Item #16 — local same-path body merge (default-OFF, local providers only).
    # Runs BEFORE the gates/embeds/write so the merged body flows through grounding,
    # embeddings and the MemoryRevision snapshot. Replaces the candidate content in
    # place; never deletes/renames.
    if valid:
        _local_merge_pass(driver, valid, provider, model, provider_fn)

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

    # Phase H3 — anti-poisoning quarantine. A NEW directive memory (asserts a
    # rule/command) from a THIN session (few events → little corroboration) that
    # is also UNSUPPORTED by existing memory (high novelty) is the poisoning
    # vector: one short session injecting a durable, unverifiable rule. Route it
    # to pending_review regardless of grounding. Updates to an existing-active
    # memory are never quarantined (consistent with the grounding gate). Skipped
    # when no events were provided — we can't assess provenance, so don't gate.
    held_updates: dict = {}   # item #10: existing-active paths whose suspicious update is held
    if valid and events:
        ev_count = len(events)
        new_paths = [m["path"] for m in valid
                     if m["path"] not in existing_active and mem_status.get(m["path"]) == "active"]
        # Item #10: also gate UPDATES to existing-active paths (default-ON; the
        # exemption was the real, narrow anti-poisoning hole per the triage).
        gate_updates = os.environ.get("DREAM_GATE_UPDATES", "1") == "1"
        update_paths = [m["path"] for m in valid if m["path"] in existing_active] if gate_updates else []
        corpus_by_prefix: dict = {}
        if new_paths or update_paths:
            cand_paths = [m["path"] for m in valid]
            with driver.session() as _ses:
                for r in _ses.run(
                    "MATCH (m:Memory) WHERE coalesce(m.status, 'active') = 'active' "
                    "AND m.content IS NOT NULL AND NOT m.path IN $exclude "
                    "RETURN m.path AS p, m.content AS c LIMIT 1000",
                    exclude=cand_paths):
                    corpus_by_prefix.setdefault(r["p"].split("/", 1)[0], []).append(r["c"])

        def _poison(path: str) -> bool:
            content = next(m["content"] for m in valid if m["path"] == path)
            corpus = " ".join(corpus_by_prefix.get(path.split("/", 1)[0], []))
            return quality_mod.poisoning_risk(content, ev_count, quality_mod.novelty_score(content, corpus))

        quarantined = 0
        for path in new_paths:
            if _poison(path):
                mem_status[path] = "pending_review"   # NEW directive → quarantine the new node
                quarantined += 1
        if quarantined:
            print(f"  anti-poisoning gate: {quarantined} directive memory(ies) from a "
                  f"thin/novel session → pending_review (adjudicate with `njhook review`)",
                  file=sys.stderr)

        # A suspicious UPDATE to an existing-active path is HELD (not pending_review —
        # that would still clobber the established body via the unconditional SET
        # m.content). Keep the established body active; record the rejected incoming
        # body as a non-status-changing quarantine revision (rejected_content + reason)
        # for audit/recovery.
        for path in update_paths:
            if _poison(path):
                held_updates[path] = next(m["content"] for m in valid if m["path"] == path)
        if held_updates:
            print(f"  update gate: {len(held_updates)} suspicious update(s) HELD — prior body "
                  f"kept active, incoming body quarantined as a revision "
                  f"(DREAM_GATE_UPDATES=0 to disable)", file=sys.stderr)
            with driver.session() as _ses:
                for path, rejected in held_updates.items():
                    _ses.run(
                        "MATCH (m:Memory {path:$p}) "
                        "CREATE (rev:MemoryRevision {ts:$ts, operation:'update_held', actor:'dream', "
                        "    status:coalesce(m.status,'active'), content_snapshot:null, "
                        "    rejected_content:$rc, reason:'anti-poisoning: suspicious update held'}) "
                        "MERGE (rev)-[:VERSION_OF]->(m)",
                        p=path, ts=now, rc=rejected)

    # Item #18 — optional LLM critique / faithfulness pass. Opt-in
    # (DREAM_CRITIQUE=1). Re-reads each NEW candidate against the bounded source
    # transcript and routes any the critic judges UNFAITHFUL (a fabricated /
    # unsupported / contradicted claim) to pending_review. This is the semantic
    # complement to the grounding gate: grounding catches a memory that shares no
    # vocabulary with the transcript; the critic catches a fluent hallucination
    # that reuses the session's words but inverts a value. NEW-only — an UPDATE to
    # an existing-active memory is never touched (same exemption as the grounding /
    # anti-poisoning gates). Lenient by construction: the critic returns True
    # (faithful) on any error/ambiguity, so a flaky model can only miss a
    # hallucination, never quarantine a good memory. `critic_fn` is injectable for
    # tests; else resolved via get_critic for the winning provider.
    if (os.environ.get("DREAM_CRITIQUE") == "1" and valid and source_text
            and any(mem_status.get(m["path"]) == "active" and m["path"] not in existing_active
                    for m in valid)):
        critic = critic_fn or critic_mod.get_critic(provider, model)
        cap = int(os.environ.get("DREAM_CRITIQUE_MAX_CHARS", "12000"))
        transcript = source_text[:cap]
        critiqued = 0
        for m in valid:
            # skip anything already held (grounding/poison) or any update — NEW-only
            if mem_status.get(m["path"]) != "active" or m["path"] in existing_active:
                continue
            try:
                faithful = critic(m["content"], transcript)
            except Exception:
                faithful = True   # lenient — never quarantine on a critic crash
            if not faithful:
                mem_status[m["path"]] = "pending_review"
                critiqued += 1
        if critiqued:
            print(f"  critique gate: {critiqued} candidate(s) failed faithfulness review "
                  f"→ pending_review (DREAM_CRITIQUE; adjudicate with `njhook review`)",
                  file=sys.stderr)

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
        # Phase D1: the semantic `kind` (queryable node property). Prefer the
        # model's top-level field, fall back to body frontmatter, normalize legacy
        # bucket labels → semantic types. Computed once: also feeds the importance
        # prior below.
        row_kind = memory_types.normalize_kind(
            m.get("kind") or memory_types.parse_kind(m["content"]) or memory_types.DEFAULT_KIND)
        rows.append({
            "path": m["path"],
            "content": m["content"],
            "updated_at": now,
            "created_by": f"dream_{provider}",
            "status": mem_status[m["path"]],
            "kind": row_kind,
            # importance: the model's value when present; else a per-kind prior so
            # ranking has a real signal even when a local model skips the field.
            "importance": _coerce_importance(m.get("importance"), row_kind),
            "project": None
            if m["path"].startswith(("profile/", "tools/")) or not project
            else project,
            "embedding": embeds[i] if embeds and i < len(embeds) else None,
            # H5: track which model produced the embedding and at what dimension.
            # Lets `njhook reindex` detect mismatches when the embedding model changes.
            "embedding_model": embed_model_name if embeds and i < len(embeds) else None,
            "embedding_dim": embed_dim if embeds and i < len(embeds) else None,
        })

    # Item #10: held updates must NOT overwrite the established active body — drop
    # them from the write set (each row already carries its own embedding, so this
    # is index-safe). The watermark still advances; the rejection is recorded above.
    if held_updates:
        rows = [r for r in rows if r["path"] not in held_updates]

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
              ON CREATE SET dr.ts = $now, dr.provider = $provider, dr.model = $model,
                            dr.input_tokens = $input_tokens, dr.output_tokens = $output_tokens,
                            dr.est_cost_usd = $est_cost
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
                m.kind = row.kind,
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
                # item #13: per-DreamRun token usage + estimated cost (additive;
                # None when usage wasn't captured — e.g. a hand-built test write).
                "input_tokens": (usage or {}).get("input_tokens"),
                "output_tokens": (usage or {}).get("output_tokens"),
                "est_cost": cost,
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

    # Phase E (PR-3) — opt-in pre-commit contradiction detection. Kept OUT of the
    # hot path unless DREAM_CONTRADICTION_CHECK=1 (it costs one LLM call per
    # candidate pair). Runs after the write so the new memories + embeddings are
    # in the index for the vector candidate-finder. On a hit, the NEW memory is
    # linked :CONTRADICTS and routed to pending_review while the established active
    # memory STAYS ACTIVE (acceptance #1) — neither silently overwrites the other.
    # judge + find_candidates are injectable so the wiring is tested without an LLM.
    if rows and (contradiction_judge is not None or os.environ.get("DREAM_CONTRADICTION_CHECK") == "1"):
        judge = contradiction_judge or judge_mod.get_judge(provider, model)
        # Two candidate channels, unioned: vector neighbours (semantic) + fulltext
        # overlap (lexical) — the latter catches antonym-style contradictions that
        # score low on cosine. The LLM judge stays the precision gate. Injectable
        # `find_candidates` (tests) still overrides both.
        finder = find_candidates or review_mod.union_candidates(
            review_mod.vector_candidates(embeddings.embed),
            review_mod.fulltext_candidates(recall_mod.fulltext_search),
        )
        new_active = [(r["path"], r["content"]) for r in rows if mem_status.get(r["path"]) == "active"]
        flagged = check_contradictions(driver, new_active, judge, finder)
        if flagged:
            print(f"  contradiction check: {len(flagged)} new memory(ies) contradicted an "
                  f"active one → pending_review (resolve with `njhook review`)", file=sys.stderr)
    return len(rows)


def check_contradictions(driver, candidates: list, judge, find_candidates) -> list:
    """Phase E PR-3 — for each just-written active `(path, content)`, run
    `review.detect_contradiction` with the new-only flagger so a contradicting new
    memory is quarantined while the established active one keeps serving recall.
    Returns the list of (new_path, existing_path) pairs flagged."""
    flagged: list = []
    with driver.session() as s:
        for path, content in candidates:
            for existing in review_mod.detect_contradiction(
                s, path, content, judge, find_candidates,
                on_contradiction=review_mod.flag_new_contradiction,
            ):
                flagged.append((path, existing))
    return flagged


def _write_nightly_run(driver, run_id, stats, provider, model, duration_ms):
    """Item #8: persist one per-nightly ledger node. Written unconditionally (even
    on a zero-yield run or a mid-loop crash, via the caller's finally) so a run
    that distilled nothing or fell back on every session is VISIBLE — the per-write
    :DreamRun can't show that, since it's skipped on zero-yield sessions. Errors are
    swallowed (like the rehearsal-run writer) so observability never crashes the
    nightly."""
    try:
        with driver.session() as s:
            s.run(
                "CREATE (:NightlyRun {run_id:$rid, ts:$ts, provider:$p, model:$m, "
                "sessions_seen:$ss, with_yield:$wy, fallback_fired:$ff, written:$w, "
                "skipped_sensitive:$sk, deferred:$df, skipped_short:$short, duration_ms:$d})",
                rid=run_id, ts=datetime.now(timezone.utc).isoformat(), p=provider, m=model,
                ss=stats["sessions_seen"], wy=stats["with_yield"], ff=stats["fallback_fired"],
                w=stats["written"], sk=stats["skipped_sensitive"], df=stats.get("deferred", 0),
                short=stats.get("skipped_short", 0), d=duration_ms,
            )
    except Exception:
        pass


def egress_blocked(provider_name: str, session_sensitive: bool, allow_egress: bool) -> bool:
    """Phase H egress policy: a high-sensitivity session must not be sent to a
    remote dream provider (anthropic/openai) unless DREAM_ALLOW_SENSITIVE_EGRESS=1.
    Local providers (ollama/llamacpp) are always allowed. Returns True when the
    call must be skipped."""
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
    ap.add_argument("--max-sessions", type=int, dest="max_sessions",
                    help="cap sessions processed this run (oldest-first; the rest resume next run) "
                         "— a backlog guard so one run can't dream hundreds of sessions")
    ap.add_argument("--dry-run", action="store_true", help="print memories, don't write")
    ap.add_argument(
        "--provider",
        choices=["anthropic", "openai", "ollama", "llamacpp"],
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
    ap.add_argument("--check-contradictions", action="store_true",
                    help="Phase E: after writing, ask the LLM whether each new memory contradicts an "
                         "active one; on a hit the new memory is flagged :CONTRADICTS + pending_review "
                         "(the active one stays active). Opt-in — one extra LLM call per candidate pair.")
    args = ap.parse_args()
    # The flag is a convenience over DREAM_CONTRADICTION_CHECK=1, which write_memories reads.
    if args.check_contradictions:
        os.environ["DREAM_CONTRADICTION_CHECK"] = "1"

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

        sessions = fetch_events(driver, args.session, since, max_sessions=getattr(args, "max_sessions", None))
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
        # their context → 0 memories); frontier models get the full transcript. The
        # cap is DERIVED from the server's n_ctx (item #19) so a bigger server uses a
        # bigger slice and the provider-layer trim never has to fire — no per-window
        # summarization needed.
        transcript_cap = _derived_transcript_cap(provider_name) if paths_only else None
        if paths_only:
            src = "DREAM_TRANSCRIPT_MAX_CHARS" if os.environ.get("DREAM_TRANSCRIPT_MAX_CHARS") else "derived from n_ctx"
            print(f"transcript budget: {transcript_cap} chars ({src})", file=sys.stderr)

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

        # Skip trivially-short sessions: a session with fewer than DREAM_MIN_EVENTS
        # events (default 2) is a lone SessionStart / single prompt with no cross-event
        # pattern to distill — ~89% of sessions in practice. Sending it to the LLM is a
        # wasted call that always yields nothing. We skip it and still advance the
        # watermark so it's retired (re-dreamed only if it later grows past the
        # threshold). Set DREAM_MIN_EVENTS=1 to disable (skips nothing).
        try:
            min_events = int(os.environ.get("DREAM_MIN_EVENTS", "2"))
        except ValueError:
            min_events = 2

        # Item #8: one per-nightly run-ledger node, written UNCONDITIONALLY in the
        # finally below (even on a mid-loop crash) so a zero-yield / all-fallback
        # run is visible — unlike the per-write :DreamRun, which is skipped on
        # zero-yield sessions. Distinct :NightlyRun label.
        stats = {"sessions_seen": 0, "with_yield": 0, "fallback_fired": 0,
                 "written": 0, "skipped_sensitive": 0, "deferred": 0, "skipped_short": 0}
        run_id = f"nightly@{datetime.now(timezone.utc).isoformat()}"
        run_t0 = time.monotonic()
        try:
            for session_key, events in sessions:
                stats["sessions_seen"] += 1
                # Trivially-short session → skip the LLM call, retire it via the
                # watermark (so it's not re-scanned every run). It re-qualifies only if
                # more events arrive past this watermark, at which point it may clear
                # the threshold. DREAM_MIN_EVENTS=1 disables (len(events) is always >=1).
                if len(events) < min_events:
                    stats["skipped_short"] += 1
                    print(f"\n=== skipping {session_key}: {len(events)} event(s) "
                          f"< DREAM_MIN_EVENTS={min_events} (nothing to distill) ===")
                    if not args.dry_run:
                        wm = events[-1].get("timestamp")
                        with driver.session() as _ses:
                            _ses.run(
                                "MATCH (s:Session {session_key: $sk}) SET s.last_dreamed_at = $wm",
                                sk=session_key, wm=wm)
                    continue
                project = dominant_project([e.get("cwd") for e in events])
                session_sensitive = any(e.get("sensitivity") == "high" for e in events)
                # Primary provider is remote + session is sensitive → don't egress; skip.
                if egress_blocked(provider_name, session_sensitive, allow_egress):
                    stats["skipped_sensitive"] += 1
                    print(f"\n=== skipping {session_key}: sensitive session, remote egress blocked "
                          f"(DREAM_ALLOW_SENSITIVE_EGRESS=1 to allow) ===")
                    continue
                existing = render_existing(fetch_existing_memories(driver, project), paths_only=paths_only)
                label = f"{session_key}" + (f"  project={project}" if project else "")
                print(f"\n=== dreaming over {label} ({len(events)} new events"
                      + ("; SENSITIVE" if session_sensitive else "") + ") ===")
                used_name, used_model = provider_name, model
                # item #13: capture token usage of whichever provider produced the
                # kept memories (no-op alloc when DREAM_USAGE_CAPTURE=0).
                capture = os.environ.get("DREAM_USAGE_CAPTURE", "1") != "0"
                prim_usage: dict = {}
                used_usage = prim_usage if capture else None
                # PR-4: safe_distil never raises — a hard provider error (e.g. llama.cpp
                # down / 4xx / malformed) becomes [] so the fallback below fires instead
                # of crashing the run.
                prim_error: list = []
                memories = safe_distil(provider_fn, render_events(events, max_chars=transcript_cap), existing, model, system_prompt, provider_name, usage_out=used_usage, error_out=prim_error)
                # Transient primary failure (model busy/unreachable/timeout): defer this
                # session — do NOT fall back (no egress on a blip) and do NOT advance the
                # watermark; the next scheduled run retries it. A clean 0-yield (no error)
                # falls through to the optional fallback / watermark advance below.
                if _defer_session(bool(prim_error), memories) and not args.dry_run:
                    stats["deferred"] += 1
                    print(f"  → deferred {session_key}: primary unavailable, watermark NOT advanced "
                          f"(retries next run)", file=sys.stderr)
                    continue
                # Fall back only if it won't egress a sensitive session to a remote provider.
                if not memories and fallback and not egress_blocked(fallback[0], session_sensitive, allow_egress):
                    fb_name, fb_fn, fb_model, fb_system = fallback
                    print(f"  local yielded 0 — falling back to {fb_name}/{fb_model} for this session")
                    try:
                        fb_existing = render_existing(fetch_existing_memories(driver, project), paths_only=False)
                        fb_usage: dict = {}
                        fb_mems = call_provider(fb_fn, render_events(events), fb_existing, fb_model, fb_system, usage_out=(fb_usage if capture else None))
                        if fb_mems:
                            memories, used_name, used_model = fb_mems, fb_name, fb_model
                            used_usage = fb_usage if capture else None  # bill the fallback that won
                    except Exception as e:
                        print(f"  fallback failed: {e}", file=sys.stderr)
                elif not memories and fallback and session_sensitive:
                    print(f"  local yielded 0; {fallback[0]} fallback skipped (sensitive session, egress blocked)",
                          file=sys.stderr)
                if memories:
                    stats["with_yield"] += 1
                if used_name != provider_name:
                    stats["fallback_fired"] += 1
                for m in memories:
                    print(f"\n--- {m.get('path')} ---")
                    print(m.get("content", ""))
                if not args.dry_run:
                    watermark = events[-1].get("timestamp")
                    est = estimate_cost(used_name, used_model, used_usage)
                    # item #16: pass the primary provider_fn for the local same-path
                    # merge. It only fires when used_name is the local primary (the
                    # gate skips remote providers), so the fn matches the provider.
                    n = write_memories(driver, session_key, memories, watermark, project=project, provider=used_name, model=used_model, events=events, usage=used_usage, cost=est, provider_fn=(provider_fn if used_name == provider_name else None))
                    stats["written"] += n
                    cost_note = f", est ${est:.4f}" if est else ""
                    print(f"\n  wrote/updated {n} memories (via {used_name}{cost_note}); watermark -> {watermark}")
        finally:
            if not args.dry_run:
                _write_nightly_run(driver, run_id, stats, provider_name, model,
                                   duration_ms=int((time.monotonic() - run_t0) * 1000))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
