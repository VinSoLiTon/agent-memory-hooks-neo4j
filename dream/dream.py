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

# Windows consoles default to cp1252; memories from Claude routinely include
# em-dashes, arrows, smart quotes, etc. Force UTF-8 so the human-readable
# preview doesn't crash before write_memories runs.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

MAX_TOKENS = 4096

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


def fetch_events(driver, session_id: str | None, since: datetime | None):
    """Return list of (session_key, [event_props, ...]) ordered chronologically.

    A session is included if it has at least one event newer than its
    `last_dreamed_at` watermark (or has never been dreamed).

    PR-G #5: --session accepts either the composite session_key or a raw
    session_id for ergonomics. If a raw id matches multiple sessions (across
    clients), we DON'T silently process all of them — we exit with a
    candidate list and ask for the explicit session_key. Same disambiguation
    rule as `njhook session <id>`.
    """
    where, params = ["(s.last_dreamed_at IS NULL OR e.timestamp > s.last_dreamed_at)"], {}

    if session_id:
        # Resolve --session to a single session_key first.
        with driver.session() as ses:
            candidates = list(ses.run(
                "MATCH (s:Session) "
                "WHERE s.session_key = $sid OR s.session_id = $sid "
                "RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS sk, "
                "       s.client AS client",
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
        where.append("s.session_key = $session_key")
        params["session_key"] = candidates[0]["sk"]

    if since:
        where.append("e.timestamp >= $since")
        params["since"] = since.isoformat()

    query = f"""
    MATCH (s:Session)-[:FIRST_EVENT|NEXT*0..]->(e:Event)
    WHERE {' AND '.join(where)}
    RETURN coalesce(s.session_key, s.session_id) AS session_key, e
    ORDER BY session_key, e.timestamp
    """
    grouped: dict[str, list] = {}
    with driver.session() as ses:
        for record in ses.run(query, **params):
            grouped.setdefault(record["session_key"], []).append(dict(record["e"]))
    return list(grouped.items())


def fetch_existing_memories(driver) -> list[dict]:
    with driver.session() as ses:
        result = ses.run("MATCH (m:Memory) RETURN m.path AS path, m.content AS content ORDER BY path")
        return [dict(r) for r in result]


def _summarize_tool_response(tr) -> str:
    """One-line summary of a tool response for the dream input. Reduces a
    multi-KB raw tool dump to a signal line: success/failure + a snippet."""
    s = str(tr)
    # Heuristics: pluck out exit_code if present; cap snippet to 80 chars.
    snippet = " ".join(s.split())[:80]
    return snippet


def render_events(events: list[dict]) -> str:
    """PR-C: trim render — keep prompt full, but tool I/O collapses to a
    one-liner. Smaller models drown in raw transcript dumps; signal-bearing
    fields are what actually inform memory extraction.
    """
    lines = []
    for e in events:
        head = f"[{e.get('timestamp', '?')}] {e.get('event_name', '?')}"
        if e.get("tool_name"):
            head += f" tool={e['tool_name']}"
        lines.append(head)
        if e.get("prompt"):
            # Keep the full prompt — it's the highest-signal field.
            lines.append(f"  prompt: {e['prompt']}")
        if e.get("tool_input"):
            ti = e["tool_input"]
            try:
                # Pull just the canonical command/file_path field if present.
                ti_obj = json.loads(ti) if isinstance(ti, str) else ti
                if isinstance(ti_obj, dict):
                    key_field = (
                        ti_obj.get("command")
                        or ti_obj.get("file_path")
                        or ti_obj.get("path")
                        or str(ti_obj)
                    )
                    lines.append(f"  input:  {str(key_field)[:200]}")
                else:
                    lines.append(f"  input:  {str(ti)[:200]}")
            except Exception:
                lines.append(f"  input:  {str(ti)[:200]}")
        if e.get("tool_response"):
            lines.append(f"  output: {_summarize_tool_response(e['tool_response'])}")
    return "\n".join(lines)


def render_existing(memories: list[dict]) -> str:
    if not memories:
        return "(no existing memories)"
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


def write_memories(driver, session_key: str, memories: list[dict], watermark: str, project: str | None = None) -> int:
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
    valid = [m for m in memories if m.get("path") and m.get("content")]

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
        ses.run(
            """
            MATCH (s:Session {session_key: $session_key})
            UNWIND $rows AS row
            MERGE (m:Memory {path: row.path})
            SET m.content = row.content,
                m.updated_at = row.updated_at,
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
            """,
            parameters={"session_key": session_key, "rows": rows},
        )
    return len(rows)


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
        existing = render_existing(fetch_existing_memories(driver))
        system_prompt = system_prompt_for(provider_name, model)
        for session_key, events in sessions:
            project = dominant_project([e.get("cwd") for e in events])
            label = f"{session_key}" + (f"  project={project}" if project else "")
            print(f"\n=== dreaming over {label} ({len(events)} new events) ===")
            memories = call_provider(provider_fn, render_events(events), existing, model, system_prompt)
            for m in memories:
                print(f"\n--- {m.get('path')} ---")
                print(m.get("content", ""))
            if not args.dry_run:
                watermark = events[-1].get("timestamp")
                n = write_memories(driver, session_key, memories, watermark, project=project)
                print(f"\n  wrote/updated {n} memories; watermark -> {watermark}")
    finally:
        driver.close()


if __name__ == "__main__":
    main()
