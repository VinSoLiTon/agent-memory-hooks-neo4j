#!/usr/bin/env python3
"""njhook — CLI to inspect, edit, and curate the memory graph.

Subcommands:
    list      list memories (filter by --kind / --project / --since)
    show      print a single memory's content
    search    fulltext search the memory store
    edit      open a memory in $EDITOR (or notepad on Windows), save back
    delete    remove a memory
    sessions  list captured sessions
    session   walk events of a single session
    stats     counts by client / kind / project

The CLI talks directly to Neo4j via the same env-var defaults as the hooks.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

from neo4j import GraphDatabase

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Bring in embeddings module (lives next to hooks/) for the backfill command.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "hooks"))
import embeddings  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


def driver():
    return GraphDatabase.driver(NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD))


def _parse_since(s: str) -> str:
    """Convert a duration like '24h' / '7d' / '30m' to an ISO timestamp.

    M5: validate the input shape so a typo like '7day' or '24' produces a
    clear error instead of an int(...) ValueError or KeyError.
    """
    import re as _re
    m = _re.fullmatch(r"(\d+)([hdm])", s)
    if not m:
        raise argparse.ArgumentTypeError(
            f"--since must look like '24h', '7d', or '30m'; got {s!r}"
        )
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    return (datetime.now(timezone.utc) - delta).isoformat()


def _short(s: str | None, n: int = 60) -> str:
    if not s:
        return ""
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _preview(content: str | None, n: int = 60) -> str:
    """First meaningful line of a memory body, skipping YAML frontmatter."""
    if not content:
        return ""
    lines = content.splitlines()
    i = 0
    if lines and lines[0].strip() == "---":
        # Skip until matching closing fence
        i = 1
        while i < len(lines) and lines[i].strip() != "---":
            i += 1
        i += 1  # past the closing ---
    while i < len(lines) and not lines[i].strip():
        i += 1
    return _short(lines[i] if i < len(lines) else "", n)


def _kind_of(path: str) -> str:
    return path.split("/", 1)[0] if "/" in path else path


# --- list / show / search / delete -----------------------------------------

def cmd_list(args: argparse.Namespace) -> int:
    where, params = [], {}
    if not args.include_archived:
        where.append("coalesce(m.archived, false) = false")
    if args.kind:
        where.append("m.path STARTS WITH $kind_prefix")
        params["kind_prefix"] = args.kind.rstrip("/") + "/"
    if args.project:
        where.append("m.project = $project")
        params["project"] = args.project
    if args.since:
        where.append("m.updated_at >= $since")
        params["since"] = _parse_since(args.since)
    cypher = (
        "MATCH (m:Memory) "
        + (("WHERE " + " AND ".join(where) + " ") if where else "")
        + "RETURN m.path AS path, m.updated_at AS updated_at, m.content AS content "
        + "ORDER BY m.updated_at DESC, m.path "
        + ("LIMIT $limit" if args.limit else "")
    )
    if args.limit:
        params["limit"] = args.limit

    with driver() as d, d.session() as s:
        rows = list(s.run(cypher, parameters=params))

    if not rows:
        print("(no memories matched)")
        return 0
    width = max(len(r["path"]) for r in rows)
    for r in rows:
        ts = (r["updated_at"] or "")[:19].replace("T", " ")
        print(f"{r['path']:<{width}}  {ts}  {_preview(r['content'], 50)}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    with driver() as d, d.session() as s:
        r = s.run(
            "MATCH (m:Memory {path: $path}) RETURN m.content AS content, m.updated_at AS u",
            parameters={"path": args.path},
        ).single()
    if not r:
        print(f"no memory at path: {args.path}", file=sys.stderr)
        return 1
    print(f"# path: {args.path}")
    print(f"# updated: {r['u']}")
    print()
    print(r["content"] or "")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    # Escape Lucene reserved chars so prompts with `:`, `-`, `(`, etc. work.
    import re as _re
    safe_q = _re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r'\\\1', args.query)
    with driver() as d, d.session() as s:
        rows = list(
            s.run(
                """
                CALL db.index.fulltext.queryNodes('memory_fulltext', $q)
                YIELD node, score
                WHERE score > $min
                RETURN node.path AS path, node.content AS content, score
                ORDER BY score DESC LIMIT $limit
                """,
                parameters={"q": safe_q, "min": args.min_score, "limit": args.limit},
            )
        )
    if not rows:
        print("(no matches)")
        return 0
    for r in rows:
        print(f"[{r['score']:5.2f}] {r['path']}\n         {_preview(r['content'], 90)}")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    if not args.yes:
        ans = input(f"Delete memory '{args.path}'? [y/N] ").strip().lower()
        if ans != "y":
            print("aborted")
            return 1
    with driver() as d, d.session() as s:
        r = s.run(
            "MATCH (m:Memory {path: $path}) DETACH DELETE m RETURN count(*) AS n",
            parameters={"path": args.path},
        ).single()
    print(f"deleted (matched {r['n']})")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    """Open the memory in $EDITOR and write the modified content back."""
    with driver() as d, d.session() as s:
        r = s.run(
            "MATCH (m:Memory {path: $path}) RETURN m.content AS content",
            parameters={"path": args.path},
        ).single()
    if not r and not args.create:
        print(f"no memory at path: {args.path} (use --create to make a new one)", file=sys.stderr)
        return 1
    original = r["content"] if r else ""

    editor = os.environ.get("EDITOR")
    if not editor:
        editor = "notepad" if os.name == "nt" else (shutil.which("vim") or shutil.which("nano") or "vi")

    # Use a temp file with .md so editors syntax-highlight markdown.
    fd, tmp = tempfile.mkstemp(suffix=".md", prefix="njhook-edit-")
    os.close(fd)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(original)
        rc = subprocess.call([editor, tmp])
        if rc != 0:
            print(f"editor exited with rc={rc}; not saving", file=sys.stderr)
            return rc
        with open(tmp, "r", encoding="utf-8") as f:
            new_content = f.read()
    finally:
        try:
            os.unlink(tmp)
        except Exception:
            pass

    if new_content == original:
        print("no changes")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with driver() as d, d.session() as s:
        s.run(
            """
            MERGE (m:Memory {path: $path})
            SET m.content = $content, m.updated_at = $now
            """,
            parameters={"path": args.path, "content": new_content, "now": now},
        )
    print(f"saved ({len(new_content)} chars)")
    return 0


# --- sessions / session / stats --------------------------------------------

def cmd_sessions(args: argparse.Namespace) -> int:
    """List captured sessions.

    PR-F #1: lists by `session_key` (the canonical primary key) so cross-client
    raw-id collisions can't merge views. The session_id column is shown as
    metadata for human readability.
    """
    where, params = [], {}
    if args.client:
        where.append("s.client = $client")
        params["client"] = args.client
    if args.since:
        where.append("s.created_at >= $since")
        params["since"] = _parse_since(args.since)
    cypher = (
        "MATCH (s:Session) "
        + (("WHERE " + " AND ".join(where) + " ") if where else "")
        + "OPTIONAL MATCH (s)-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
        + "WITH s, count(e) AS events "
        + "RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS session_key, "
        + "       s.session_id AS sid, s.client AS client, s.created_at AS created, "
        + "       s.last_dreamed_at AS dreamed, events "
        + "ORDER BY s.created_at DESC LIMIT $limit"
    )
    params["limit"] = args.limit

    with driver() as d, d.session() as s:
        rows = list(s.run(cypher, parameters=params))

    if not rows:
        print("(no sessions)")
        return 0
    print(f"{'session_key':<60}  {'client':<12}  {'created':<19}  {'events':>6}  dreamed")
    for r in rows:
        sk = (r["session_key"] or "?")[:60]
        c = (r["created"] or "")[:19].replace("T", " ")
        d_ = "yes" if r["dreamed"] else "—"
        print(f"{sk:<60}  {r['client'] or '?':<12}  {c:<19}  {r['events']:>6}  {d_}")
    return 0


def cmd_session(args: argparse.Namespace) -> int:
    """Walk events of one session.

    PR-F #1: prefer matching by `session_key` (composite, unique). Accept raw
    `session_id` as a convenience fallback — if it matches multiple sessions
    across clients, list the candidates and ask for the full key.
    """
    sid = args.session_id
    with driver() as d, d.session() as s:
        # Resolve to a single session_key. If the user passed the composite key
        # directly, this matches one session. If they passed a raw id and it
        # collides across clients, we surface the ambiguity instead of merging.
        candidates = list(s.run(
            "MATCH (s:Session) WHERE s.session_key = $sid OR s.session_id = $sid "
            "RETURN s.session_key AS sk, s.client AS client",
            parameters={"sid": sid},
        ))
        if not candidates:
            print(f"no session matching {sid!r}", file=sys.stderr)
            return 1
        if len(candidates) > 1:
            print(f"raw session_id {sid!r} matches {len(candidates)} sessions across clients:", file=sys.stderr)
            for c in candidates:
                print(f"  {c['sk']}  (client={c['client']})", file=sys.stderr)
            print("\nRe-run with the full session_key (e.g. claude_code:<id>).", file=sys.stderr)
            return 1
        session_key = candidates[0]["sk"]

        rows = list(s.run(
            """
            MATCH (s:Session {session_key: $sk})-[:FIRST_EVENT|NEXT*0..]->(e:Event)
            RETURN e.timestamp AS ts, e.event_name AS name, e.tool_name AS tool,
                   e.prompt AS prompt, e.tool_input AS ti, e.tool_response AS tr
            ORDER BY e.timestamp
            """,
            parameters={"sk": session_key},
        ))
    if not rows:
        print(f"no events for session {session_key}", file=sys.stderr)
        return 1
    print(f"# session_key: {session_key}\n")
    for r in rows:
        head = f"[{(r['ts'] or '')[:19].replace('T',' ')}] {r['name'] or '?'}"
        if r["tool"]:
            head += f"  tool={r['tool']}"
        print(head)
        if args.verbose:
            for label, val in (("prompt", r["prompt"]), ("input", r["ti"]), ("output", r["tr"])):
                if val:
                    print(f"    {label}: {_short(val, 200)}")
    print(f"\n({len(rows)} events)")
    return 0


def cmd_embed_backfill(args: argparse.Namespace) -> int:
    """Compute and store embeddings for memories that don't have them yet.

    Requires EMBED_PROVIDER=openai|ollama in the env. Idempotent: re-run after
    adding new memories or switching models (use --force to overwrite).
    """
    if not embeddings.is_enabled():
        print("EMBED_PROVIDER is not set. Export EMBED_PROVIDER=openai or ollama and retry.", file=sys.stderr)
        return 2

    where = "" if args.force else "WHERE m.embedding IS NULL"
    with driver() as d, d.session() as s:
        rows = list(s.run(
            f"MATCH (m:Memory) {where} RETURN m.path AS path, m.content AS content ORDER BY m.path"
        ))
        if not rows:
            print("nothing to backfill")
            return 0
        print(f"backfilling {len(rows)} memories using EMBED_PROVIDER={embeddings.EMBED_PROVIDER} model={embeddings.model()}")

        # Batch in chunks so we don't hit any per-call payload limit.
        batch = max(1, args.batch_size)
        dim_committed = False
        total = 0
        for i in range(0, len(rows), batch):
            chunk = rows[i : i + batch]
            texts = [embeddings.memory_text(r["path"], r["content"]) for r in chunk]
            try:
                embs = embeddings.embed(texts)
            except Exception as e:
                print(f"  batch {i}-{i+len(chunk)}: failed ({e}); aborting", file=sys.stderr)
                return 1
            if not dim_committed and embs:
                d_ = len(embs[0])
                s.run(
                    f"""
                    CREATE VECTOR INDEX memory_embeddings IF NOT EXISTS
                    FOR (m:Memory) ON m.embedding
                    OPTIONS {{ indexConfig: {{
                      `vector.dimensions`: {d_},
                      `vector.similarity_function`: 'cosine'
                    }} }}
                    """
                )
                dim_committed = True
            model_name = embeddings.model()
            dim_value = len(embs[0]) if embs else 0
            payload = [
                {
                    "path": r["path"],
                    "embedding": embs[j],
                    "embedding_model": model_name,
                    "embedding_dim": dim_value,
                }
                for j, r in enumerate(chunk)
                if j < len(embs)
            ]
            s.run(
                """
                UNWIND $rows AS row
                MATCH (m:Memory {path: row.path})
                SET m.embedding = row.embedding,
                    m.embedding_model = row.embedding_model,
                    m.embedding_dim = row.embedding_dim
                """,
                parameters={"rows": payload},
            )
            total += len(payload)
            print(f"  {i+len(chunk):>4}/{len(rows)}  ({chunk[-1]['path']})")

    print(f"\nembedded {total} memories")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """H5: detect embedding model/dim mismatch and rebuild memory_embeddings.

    Compares the active EMBED_PROVIDER's model vs what's stored on existing
    memories. If they disagree (or --force), drops the vector index, clears
    stale embeddings, and re-runs embed-backfill so every memory gets a fresh
    embedding from the current model.
    """
    if not embeddings.is_enabled():
        print("EMBED_PROVIDER is not set; nothing to reindex.", file=sys.stderr)
        return 2

    active_model = embeddings.model()
    try:
        active_dim = embeddings.dim()
    except Exception as e:
        print(f"could not probe active model dim ({e})", file=sys.stderr)
        return 1

    with driver() as d, d.session() as s:
        # What model produced the existing embeddings?
        models_in_graph = list(s.run(
            "MATCH (m:Memory) WHERE m.embedding IS NOT NULL "
            "RETURN coalesce(m.embedding_model, '?') AS model, "
            "       coalesce(m.embedding_dim, 0) AS dim, count(*) AS n "
            "ORDER BY n DESC"
        ))

    if not models_in_graph:
        print(f"no embeddings yet — running embed-backfill against {active_model} ({active_dim}d)")
        backfill_args = argparse.Namespace(force=False, batch_size=16)
        return cmd_embed_backfill(backfill_args)

    print("Embeddings currently in graph:")
    for r in models_in_graph:
        marker = "  (matches active)" if r["model"] == active_model and r["dim"] == active_dim else "  (STALE)"
        print(f"  {r['n']:>4}  model={r['model']:<35}  dim={r['dim']}{marker}")
    print(f"\nActive: model={active_model}  dim={active_dim}")

    needs_reindex = args.force or any(
        r["model"] != active_model or r["dim"] != active_dim for r in models_in_graph
    )
    if not needs_reindex:
        print("\nNothing to do (active model matches stored embeddings). --force to rebuild anyway.")
        return 0

    if args.dry_run:
        print("\n[dry-run] would drop memory_embeddings, clear stale m.embedding, and rerun embed-backfill")
        return 0

    print("\nrebuilding...")
    with driver() as d, d.session() as s:
        try:
            s.run("DROP INDEX memory_embeddings IF EXISTS")
            print("  dropped memory_embeddings index")
        except Exception as e:
            print(f"  warn: drop index failed ({e})", file=sys.stderr)
        s.run(
            "MATCH (m:Memory) WHERE m.embedding IS NOT NULL "
            "REMOVE m.embedding, m.embedding_model, m.embedding_dim"
        )
        print("  cleared stale embeddings on all memories")

    backfill_args = argparse.Namespace(force=True, batch_size=16)
    return cmd_embed_backfill(backfill_args)


def _gather_patterns(drv, args: argparse.Namespace) -> list[dict]:
    """Run all three detectors and return a flat, deduped list with stable IDs."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "detect"))
    import patterns as patterns_mod  # type: ignore

    out: list[dict] = []
    show = args.show or "all"
    if show in ("commands", "all"):
        out.extend(patterns_mod.repeated_commands(drv, min_count=args.min_count, since=args.since))
    if show in ("files", "all"):
        out.extend(patterns_mod.hot_files(drv, min_count=args.min_count, since=args.since))
    if show in ("prompts", "all") and embeddings.is_enabled():
        out.extend(patterns_mod.prompt_clusters(
            drv, min_cluster_size=args.min_count,
            similarity_threshold=args.similarity, since=args.since,
        ))
    return out


def cmd_patterns(args: argparse.Namespace) -> int:
    """Surface repeated patterns across captured sessions.

    Three detectors run in series; each is independently filterable. With
    --promote <id> the named pattern is converted into a draft :Memory.
    """
    drv = driver()

    if args.promote:
        return _promote_pattern(drv, args)

    patterns = _gather_patterns(drv, args)
    by_kind: dict[str, list[dict]] = {"command": [], "file": [], "prompt": []}
    for p in patterns:
        by_kind[p["kind"]].append(p)

    if not patterns:
        print("(no patterns above threshold)")
        return 0

    if by_kind["command"]:
        print("\n=== Repeated commands ===")
        for c in by_kind["command"]:
            print(f"  [{c['id']}] {c['count']:>3}×  {_short(c['command'], 90)}")
            if c["cwds"] and len(c["cwds"]) <= 3:
                for cwd in c["cwds"]:
                    print(f"             cwd: {cwd}")
    if by_kind["file"]:
        print("\n=== Hot file paths ===")
        for f in by_kind["file"]:
            tools = " ".join(f"{k}={v}" for k, v in f["tools"].items())
            print(f"  [{f['id']}] {f['count']:>3}×  {f['path']}    [{tools}]")
    if by_kind["prompt"]:
        print("\n=== Recurring prompt clusters ===")
        for cl in by_kind["prompt"]:
            print(f"\n  [{cl['id']}] cluster of {cl['size']}: {_short(cl['exemplar'], 80)}")
            for p in cl["prompts"][1:4]:
                print(f"          - {_short(p, 80)}")
            if cl["size"] > 4:
                print(f"          … and {cl['size']-4} more")

    if "prompt" not in by_kind or not by_kind["prompt"]:
        if not embeddings.is_enabled() and (args.show in (None, "all", "prompts")):
            print("\n(EMBED_PROVIDER not set — prompt clustering disabled)")

    print("\nTo turn one of these into a memory:")
    print("  njhook patterns --promote <id>     (preview by default; -y to write)")
    return 0


def _promote_pattern(drv, args: argparse.Namespace) -> int:
    """Locate the pattern by ID across all detectors and write a draft memory."""
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "detect"))
    import patterns as patterns_mod  # type: ignore

    patterns = _gather_patterns(drv, args)
    target = next((p for p in patterns if p["id"] == args.promote), None)
    if not target:
        print(f"no pattern with id {args.promote!r} found in current detection (try `njhook patterns` first)", file=sys.stderr)
        return 1

    draft = patterns_mod.draft_memory_from_pattern(target)

    print(f"--- Draft memory: {draft['path']} ---\n")
    print(draft["content"])

    if args.dry_run or not args.yes:
        if args.dry_run:
            print("\n[dry-run] not writing.")
            return 0
        print("\nRun again with -y to write this memory, or pipe through `njhook edit` to refine first.")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    with drv.session() as s:
        s.run(
            "MERGE (m:Memory {path: $path}) "
            "SET m.content = $content, m.updated_at = $now, "
            "    m.promoted_from_pattern = $pid",
            parameters={"path": draft["path"], "content": draft["content"],
                        "now": now, "pid": target["id"]},
        )
    print(f"\nwrote {draft['path']} (promoted_from_pattern={target['id']})")
    return 0


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Delegate to dream/consolidate.py — LLM-merge near-duplicate memories."""
    # The consolidate module lives under dream/, which isn't on sys.path by default
    # for the CLI. Add it.
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dream"))
    import consolidate as consolidate_mod  # type: ignore
    if not embeddings.is_enabled():
        print("EMBED_PROVIDER is not set. Consolidation needs vector similarity to find pair candidates.", file=sys.stderr)
        return 2
    with driver() as d:
        consolidate_mod.consolidate(
            d,
            provider_name=args.provider,
            threshold=args.threshold,
            max_rounds=args.rounds,
            dry_run=args.dry_run,
            embed_fn=embeddings.embed,
        )
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dream"))
    import consolidate as consolidate_mod  # type: ignore
    with driver() as d:
        consolidate_mod.archive(d, stale_days=args.stale_days, dry_run=args.dry_run)
    return 0


def cmd_unarchive(args: argparse.Namespace) -> int:
    with driver() as d, d.session() as s:
        r = s.run(
            "MATCH (m:Memory {path: $path}) "
            "SET m.archived = false, m.unarchived_at = $now "
            "RETURN count(*) AS n",
            parameters={"path": args.path, "now": datetime.now(timezone.utc).isoformat()},
        ).single()
    print(f"unarchived (matched {r['n']})")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    """Dump memories (and optionally sessions+events) to a JSON file.

    Default skips embeddings (large). Use --with-embeddings if you intend
    to restore on a machine without re-running embed-backfill.
    """
    import json as _json
    out_path = Path(args.out) if args.out else Path(f"njhook-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json")
    payload: dict = {"version": 1, "exported_at": datetime.now(timezone.utc).isoformat(), "memories": [], "sessions": []}

    with driver() as d, d.session() as s:
        emb_clause = "m.embedding AS embedding, m.embedding_model AS embedding_model, m.embedding_dim AS embedding_dim, " if args.with_embeddings else ""
        rows = list(s.run(
            f"MATCH (m:Memory) "
            f"RETURN m.path AS path, m.content AS content, m.project AS project, "
            f"       m.updated_at AS updated_at, "
            f"       coalesce(m.archived,false) AS archived, "
            f"       coalesce(m.access_count,0) AS access_count, "
            f"       m.last_accessed_at AS last_accessed_at, "
            f"       m.consolidated_from AS consolidated_from, "
            f"       m.promoted_from_pattern AS promoted_from_pattern, "
            f"       {emb_clause}"
            f"       null AS _end "
            f"ORDER BY m.path"
        ))
        for r in rows:
            d_ = {k: r[k] for k in r.keys() if k != "_end" and r[k] is not None}
            payload["memories"].append(d_)

        if args.with_sessions:
            # PR-F #2: ORDER BY e.timestamp before collect; otherwise the
            # variable-length traversal returns events in arbitrary order and
            # restore rethreads the linked list incorrectly.
            sess_rows = list(s.run(
                "MATCH (s:Session) "
                "OPTIONAL MATCH (s)-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
                "WITH s, e ORDER BY e.timestamp "
                "WITH s, collect(properties(e)) AS events "
                "RETURN s.session_key AS session_key, s.session_id AS session_id, "
                "       s.client AS client, s.created_at AS created_at, "
                "       s.last_dreamed_at AS last_dreamed_at, events"
            ))
            for r in sess_rows:
                payload["sessions"].append({
                    "session_key": r["session_key"], "session_id": r["session_id"],
                    "client": r["client"], "created_at": r["created_at"],
                    "last_dreamed_at": r["last_dreamed_at"],
                    "events": [e for e in r["events"] if e],
                })

    out_path.write_text(_json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"wrote {out_path} — {len(payload['memories'])} memories, {len(payload['sessions'])} sessions ({out_path.stat().st_size} bytes)")
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    """Load a backup file. Memories upsert by path; sessions upsert by session_key.
    Embeddings restored only when present in the backup AND --with-embeddings is set.
    """
    import json as _json
    in_path = Path(args.in_)
    if not in_path.exists():
        print(f"file not found: {in_path}", file=sys.stderr)
        return 1
    payload = _json.loads(in_path.read_text(encoding="utf-8"))
    memories = payload.get("memories") or []
    sessions = payload.get("sessions") or []
    print(f"restoring from {in_path}: {len(memories)} memories, {len(sessions)} sessions")

    if args.dry_run:
        for m in memories[:5]:
            print(f"  would write Memory {m['path']}")
        for s in sessions[:5]:
            print(f"  would write Session {s.get('session_key') or s.get('session_id')}")
        print("[dry-run] no writes")
        return 0

    with driver() as d, d.session() as s:
        # Memories
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
        s.run(
            """
            UNWIND $rows AS row
            MERGE (m:Memory {path: row.path})
            SET m += apoc.map.removeKey(row, 'path')
            """,
            parameters={"rows": memories},
        ) if False else None  # APOC may not be available; use the explicit form below

        for m in memories:
            props = {k: v for k, v in m.items() if k != "path"}
            if not args.with_embeddings:
                for k in ("embedding", "embedding_model", "embedding_dim"):
                    props.pop(k, None)
            s.run(
                "MERGE (m:Memory {path: $path}) SET m += $props",
                parameters={"path": m["path"], "props": props},
            )

        # Sessions
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Session) REQUIRE s.session_key IS UNIQUE")
        for sess in sessions:
            sk = sess.get("session_key") or f"{sess.get('client','unknown')}:{sess.get('session_id','unknown')}"
            sess_props = {k: v for k, v in sess.items() if k not in ("events", "session_key") and v is not None}
            sess_props["session_key"] = sk
            s.run(
                "MERGE (s:Session {session_key: $sk}) SET s += $props",
                parameters={"sk": sk, "props": sess_props},
            )
            # Restore events: append in order, threading FIRST_EVENT / NEXT / LATEST_EVENT.
            events = sess.get("events") or []
            if events:
                # Wipe any existing chain on this session before re-threading.
                s.run(
                    "MATCH (s:Session {session_key: $sk}) "
                    "OPTIONAL MATCH (s)-[r1:FIRST_EVENT|LATEST_EVENT]->() DELETE r1",
                    parameters={"sk": sk},
                )
                prev = None
                for i, e in enumerate(events):
                    eid = e.get("event_id")
                    if not eid:
                        continue
                    s.run(
                        "MERGE (e:Event {event_id: $eid}) SET e += $props",
                        parameters={"eid": eid, "props": e},
                    )
                    if i == 0:
                        s.run(
                            "MATCH (s:Session {session_key: $sk}), (e:Event {event_id: $eid}) "
                            "MERGE (s)-[:FIRST_EVENT]->(e)",
                            parameters={"sk": sk, "eid": eid},
                        )
                    if prev:
                        s.run(
                            "MATCH (a:Event {event_id: $prev}), (b:Event {event_id: $eid}) "
                            "MERGE (a)-[:NEXT]->(b)",
                            parameters={"prev": prev, "eid": eid},
                        )
                    prev = eid
                if prev:
                    s.run(
                        "MATCH (s:Session {session_key: $sk}), (e:Event {event_id: $eid}) "
                        "MERGE (s)-[:LATEST_EVENT]->(e)",
                        parameters={"sk": sk, "eid": prev},
                    )
    print(f"restored {len(memories)} memories, {len(sessions)} sessions")
    return 0


def cmd_migrate(_: argparse.Namespace) -> int:
    """Run the full schema migration (drop legacy constraints, create the
    canonical set, backfill session_key on pre-PR-B sessions). Idempotent.

    PR-F #4: this used to run on every hook event, which made every event
    pay for `SHOW CONSTRAINTS` plus several CREATE round-trips. Hooks now
    only ensure the two MERGE-supporting UNIQUE constraints; everything
    else lives here.
    """
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
    import schema as schema_mod  # type: ignore
    with driver() as d:
        report = schema_mod.run_full_migration(d)
    print(f"dropped legacy constraints: {report['dropped_constraints'] or 'none'}")
    print(f"backfilled session_key on:   {report['session_keys_backfilled']} session(s)")
    print("created canonical constraints/indexes (idempotent)")
    return 0


def cmd_stats(_: argparse.Namespace) -> int:
    with driver() as d, d.session() as s:
        m_total = s.run("MATCH (m:Memory) RETURN count(m) AS n").single()["n"]
        m_archived = s.run(
            "MATCH (m:Memory) WHERE coalesce(m.archived,false)=true RETURN count(m) AS n"
        ).single()["n"]
        m_with_emb = s.run(
            "MATCH (m:Memory) WHERE m.embedding IS NOT NULL RETURN count(m) AS n"
        ).single()["n"]
        m_by_kind = list(s.run(
            """
            MATCH (m:Memory)
            WITH split(m.path, '/')[0] AS kind, count(*) AS n
            RETURN kind, n ORDER BY n DESC
            """
        ))
        s_total = s.run("MATCH (s:Session) RETURN count(s) AS n").single()["n"]
        s_by_client = list(s.run(
            "MATCH (s:Session) RETURN s.client AS client, count(*) AS n ORDER BY n DESC"
        ))
        e_total = s.run("MATCH (e:Event) RETURN count(e) AS n").single()["n"]

    print(f"Memories: {m_total}  ({m_archived} archived, {m_with_emb} embedded)")
    for r in m_by_kind:
        print(f"  {r['kind']:<10} {r['n']}")
    print(f"\nSessions: {s_total}")
    for r in s_by_client:
        print(f"  {r['client'] or '?':<12} {r['n']}")
    print(f"\nEvents: {e_total}")
    return 0


# --- argparse --------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="njhook", description="Inspect and curate the agent-memory graph.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="list memories")
    pl.add_argument("--kind", help="filter by top-level path component (profile, tools, project, general)")
    pl.add_argument("--project", help="filter by project tag")
    pl.add_argument("--since", help="only memories updated since e.g. 24h, 7d, 30m")
    pl.add_argument("--limit", type=int, default=0, help="max rows (0 = no limit)")
    pl.add_argument("--include-archived", action="store_true", help="show archived memories too")
    pl.set_defaults(fn=cmd_list)

    ps = sub.add_parser("show", help="print a memory's full content")
    ps.add_argument("path")
    ps.set_defaults(fn=cmd_show)

    psr = sub.add_parser("search", help="fulltext search memories")
    psr.add_argument("query")
    psr.add_argument("--min-score", type=float, default=0.5, dest="min_score")
    psr.add_argument("--limit", type=int, default=10)
    psr.set_defaults(fn=cmd_search)

    pe = sub.add_parser("edit", help="open a memory in $EDITOR (notepad on Windows)")
    pe.add_argument("path")
    pe.add_argument("--create", action="store_true", help="allow creating a new memory at this path")
    pe.set_defaults(fn=cmd_edit)

    pd = sub.add_parser("delete", help="remove a memory")
    pd.add_argument("path")
    pd.add_argument("-y", "--yes", action="store_true", help="skip confirmation prompt")
    pd.set_defaults(fn=cmd_delete)

    pss = sub.add_parser("sessions", help="list captured sessions")
    pss.add_argument("--client", choices=["claude_code", "codex", "cursor", "gemini"])
    pss.add_argument("--since", help="only sessions started since e.g. 24h, 7d")
    pss.add_argument("--limit", type=int, default=20)
    pss.set_defaults(fn=cmd_sessions)

    psn = sub.add_parser("session", help="show events of one session")
    psn.add_argument("session_id")
    psn.add_argument("-v", "--verbose", action="store_true", help="include prompt / input / output snippets")
    psn.set_defaults(fn=cmd_session)

    pst = sub.add_parser("stats", help="counts by client / kind")
    pst.set_defaults(fn=cmd_stats)

    pmg = sub.add_parser("migrate", help="run full schema migration (idempotent; run after install or upgrade)")
    pmg.set_defaults(fn=cmd_migrate)

    pem = sub.add_parser(
        "embed-backfill",
        help="compute embeddings for memories missing them (requires EMBED_PROVIDER)",
    )
    pem.add_argument("--force", action="store_true", help="re-embed all memories, not just those missing embeddings")
    pem.add_argument("--batch-size", type=int, default=16)
    pem.set_defaults(fn=cmd_embed_backfill)

    pri = sub.add_parser(
        "reindex",
        help="rebuild memory_embeddings when EMBED_MODEL/dim changes (or --force)",
    )
    pri.add_argument("--force", action="store_true", help="rebuild even when active model matches stored embeddings")
    pri.add_argument("--dry-run", action="store_true")
    pri.set_defaults(fn=cmd_reindex)

    pco = sub.add_parser(
        "consolidate",
        help="LLM-merge near-duplicate memories (requires EMBED_PROVIDER and a dream provider)",
    )
    pco.add_argument("--threshold", type=float, default=0.92, help="cosine similarity threshold (default 0.92)")
    pco.add_argument("--rounds", type=int, default=10, help="max merge rounds (default 10)")
    pco.add_argument("--provider", choices=["anthropic", "openai", "ollama"])
    pco.add_argument("--dry-run", action="store_true")
    pco.set_defaults(fn=cmd_consolidate)

    par = sub.add_parser(
        "archive",
        help="flag stale memories as archived (excluded from recall)",
    )
    par.add_argument("--stale-days", type=int, default=60)
    par.add_argument("--dry-run", action="store_true")
    par.set_defaults(fn=cmd_archive)

    pun = sub.add_parser("unarchive", help="restore an archived memory by path")
    pun.add_argument("path")
    pun.set_defaults(fn=cmd_unarchive)

    pbk = sub.add_parser("backup", help="dump memories (and optionally sessions) to JSON")
    pbk.add_argument("--out", help="output file (default: njhook-backup-<timestamp>.json)")
    pbk.add_argument("--with-embeddings", action="store_true", help="include m.embedding vectors (large)")
    pbk.add_argument("--with-sessions", action="store_true", help="include captured sessions and their events")
    pbk.set_defaults(fn=cmd_backup)

    prs = sub.add_parser("restore", help="load a backup file (idempotent upsert by path / session_key)")
    prs.add_argument("--in", dest="in_", required=True, help="input JSON file")
    prs.add_argument("--with-embeddings", action="store_true", help="restore m.embedding when present")
    prs.add_argument("--dry-run", action="store_true")
    prs.set_defaults(fn=cmd_restore)

    ppat = sub.add_parser("patterns", help="surface repeated commands, hot files, and recurring prompt clusters")
    ppat.add_argument("--show", choices=["commands", "files", "prompts", "all"], default="all")
    ppat.add_argument("--min-count", type=int, default=3, help="threshold for a pattern to surface")
    ppat.add_argument("--since", help="only events newer than e.g. 7d, 24h, 30m")
    ppat.add_argument("--similarity", type=float, default=0.8, help="prompt-cluster cosine threshold")
    ppat.add_argument("--promote", metavar="ID", help="convert the pattern with this id into a draft memory")
    ppat.add_argument("--dry-run", action="store_true", help="(with --promote) print draft, don't write")
    ppat.add_argument("-y", "--yes", action="store_true", help="(with --promote) skip preview-only mode and actually write")
    ppat.set_defaults(fn=cmd_patterns)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
