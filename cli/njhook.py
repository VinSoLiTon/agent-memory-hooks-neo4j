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
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path

from neo4j import GraphDatabase

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Bring in embeddings module (lives next to hooks/) for the backfill command.
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "hooks"))
import embeddings  # noqa: E402
import recall  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


def driver():
    # PR-G #2: silence the "property X does not exist" notifications. They
    # fire for optional fields (archived, consolidated_from, embedding_model,
    # promoted_from_pattern) on graphs where those properties haven't been
    # set on any node yet — harmless but visually noisy on user-facing output.
    # We deliberately keep DEPRECATION / PERFORMANCE / SECURITY warnings on.
    return GraphDatabase.driver(
        NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
        notifications_disabled_classifications=["UNRECOGNIZED"],
    )


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
    # Phase C: route through the shared recall engine (hooks/recall.py) — same
    # hybrid RRF + lifecycle filtering as the hook and dashboard. This upgrades
    # the CLI from fulltext-only to hybrid (vector hits fold in when
    # EMBED_PROVIDER is set); --min-score still gates the fulltext stage and the
    # printed score is the fused RRF score.
    with driver() as d, d.session() as s:
        rows = recall.prompt_query(
            s, args.query, current_project=None,
            limit=args.limit, min_score=args.min_score,
        )
        events = recall.event_search(s, args.query, limit=args.limit) if getattr(args, "events", False) else []
    if not rows and not events:
        print("(no matches)")
        return 0
    for r in rows:
        print(f"[{r['score']:6.4f}] {r['path']}\n         {_preview(r['content'], 90)}")
    if events:
        print("\nraw events (not yet distilled):")
        for r in events:
            head = r["event_name"] + (f" {r['tool']}" if r["tool"] else "")
            print(f"[{r['score']:6.2f}] {r['ts']} {head}\n         {r['snippet'][:90]}")
    return 0


def cmd_eval_retrieval(args: argparse.Namespace) -> int:
    """Phase D3 — seed a golden fixture and score recall (hit@k + MRR). Regression
    guard for the ranking signals; seeds + cleans up its own fixture."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dream"))
    import eval_retrieval
    return eval_retrieval.main()


def cmd_render(args: argparse.Namespace) -> int:
    """Phase G (PR-3) — render project memory into an agent context file
    (AGENTS.md / CLAUDE.md / GEMINI.md / Cursor rules) as a managed block, so a
    runtime that can't run hooks still gets memory. Same recall core the hook
    injects; content outside the markers is left untouched."""
    import render as rndr
    root = args.root or os.getcwd()
    targets = sorted(rndr.RENDER_TARGETS) if args.target == "all" else [args.target]
    with driver() as d, d.session() as s:
        if args.stdout:
            for t in targets:
                text, _ = rndr.proposed_text(s, t, root, cwd=root)
                if len(targets) > 1:
                    print(f"\n===== {t} -> {rndr.target_path(t, root)} =====")
                print(text)
            return 0
        results = [rndr.render_target(s, t, root, cwd=root) for t in targets]
    for r in results:
        print(f"{r['action']:>9}  {r['target']:<7} {r['path']}")
    return 0


def cmd_recall(args: argparse.Namespace) -> int:
    """Phase G — recall memories for a prompt over the shared core (same ranking
    the hook uses). For programmatic use by non-hook runtimes."""
    import service
    with driver() as d, d.session() as s:
        hits = service.recall_context(s, args.prompt, cwd=args.cwd, limit=args.limit)
    if args.json:
        import json as _json
        print(_json.dumps(hits, indent=2))
    elif not hits:
        print("(no matches)")
    else:
        for h in hits:
            print(f"[{h['score']:.4f}] {h['path']}\n         {_preview(h['content'], 90)}")
    return 0


def cmd_write_event(args: argparse.Namespace) -> int:
    """Phase G — capture an event from JSON (stdin or --json FILE) through the same
    capture path the hooks use (scrub + opt-out + spool/direct)."""
    import json as _json
    import log_event
    if args.json and args.json != "-":
        raw = open(args.json, encoding="utf-8").read()
    else:
        raw = sys.stdin.read()
    try:
        data = _json.loads(raw) if raw.strip() else {}
    except Exception as e:
        print(f"invalid JSON: {e}", file=sys.stderr)
        return 2
    log_event.log_event(data, client=args.client)
    print("event captured")
    return 0


def cmd_review(args: argparse.Namespace) -> int:
    """Phase E review queue: list / approve / reject / supersede / flag conflicts."""
    import review as rv
    with driver() as d, d.session() as s:
        if args.action == "list":
            pend = rv.list_pending(s)
            con = rv.list_contradictions(s)
            if not pend and not con:
                print("(nothing awaiting review)")
                return 0
            if pend:
                print("pending review:")
                for m in pend:
                    print(f"  {m['path']}  (by {m['created_by'] or '?'}, {str(m['updated_at'] or '')[:19]})")
            if con:
                print("contradiction pairs:")
                for c in con:
                    print(f"  {c['a']}  <!>  {c['b']}")
            return 0
        if args.action in ("approve", "reject"):
            if not args.paths:
                print(f"usage: review {args.action} <path>", file=sys.stderr)
                return 2
            n = (rv.approve if args.action == "approve" else rv.reject)(s, args.paths[0])
            print(f"{args.action}: {n} memory updated" if n else f"no memory at {args.paths[0]}")
            return 0 if n else 1
        if args.action == "supersede":
            if len(args.paths) != 2:
                print("usage: review supersede <winner-path> <loser-path>", file=sys.stderr)
                return 2
            rv.supersede(s, args.paths[0], args.paths[1])
            print(f"superseded: {args.paths[1]} -> {args.paths[0]}")
            return 0
        if args.action == "flag":
            if len(args.paths) != 2:
                print("usage: review flag <pathA> <pathB>", file=sys.stderr)
                return 2
            rv.flag_contradiction(s, args.paths[0], args.paths[1])
            print(f"flagged contradiction: {args.paths[0]} <!> {args.paths[1]} (both pending_review)")
            return 0
        if args.action == "auto-resolve":
            n = rv.auto_resolve_all(s)
            print(f"auto-resolved {n} conflict pair(s) by source-authority × recency")
            return 0
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    """Drain the durable event spool into Neo4j (Phase B). Idempotent — safe to
    re-run; events already in the graph are skipped, malformed records dead-lettered."""
    import ingest as ingest_mod
    with driver() as d:
        r = ingest_mod.ingest(d)
    print(f"ingest: {r['processed']} ingested, {r['skipped']} already-present, {r['dlq']} dead-lettered")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    """Show a memory's revision timeline + lineage — how it evolved and came to be."""
    with driver() as d, d.session() as s:
        hist = recall.memory_lineage(s, args.path)
    if hist is None:
        print(f"no memory at path: {args.path}", file=sys.stderr)
        return 1
    vs = hist["versions"]

    # --as-of: reconstruct the body that was current at the given timestamp.
    if getattr(args, "as_of", None):
        body = recall.content_as_of(vs, args.as_of)
        print(f"# {hist['path']} as of {args.as_of}\n")
        print(body or "(no content)")
        return 0

    print(f"{hist['path']}  [{hist['status']}]  ({len(vs)} version(s))")
    for v in vs:
        when = str(v["ts"])[:19].replace("T", " ") if v["ts"] else "?"
        print(f"  {v['label']:<8} {when}  {v['operation'] or ''} by {v['actor'] or '?'}  ({len(v['content'])} chars)")

    # Lineage: where this memory came from / what it superseded.
    if hist.get("supersedes"):
        print("  supersedes:   " + ", ".join(hist["supersedes"]))
    if hist.get("superseded_by"):
        print("  superseded by: " + ", ".join(hist["superseded_by"]))
    if hist.get("contradicts"):
        print("  contradicts:  " + ", ".join(hist["contradicts"]))
    if hist.get("source_events"):
        print(f"  extracted from {len(hist['source_events'])} source event(s):")
        for e in hist["source_events"]:
            when = str(e["ts"])[:19].replace("T", " ") if e["ts"] else "?"
            head = e["event_name"] + (f" {e['tool']}" if e["tool"] else "")
            print(f"    [{when}] {head}: {e['snippet'][:80]}")

    if args.diff and len(vs) > 1:
        import difflib
        for i in range(len(vs) - 1):
            a, b = vs[i], vs[i + 1]
            print(f"\n--- {a['label']} -> {b['label']} ---")
            for line in difflib.unified_diff(
                (a["content"]).splitlines(), (b["content"]).splitlines(), lineterm="", n=2
            ):
                print(line)
    elif args.diff:
        print("\n(only one version — nothing to diff yet)")
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
    import audit
    with driver() as d, d.session() as s:
        # H2 audit: if the memory already existed, snapshot the prior body+status
        # as an `edit` entry before overwriting (a manual edit otherwise left no trace).
        if r:
            prior = s.run(
                "MATCH (m:Memory {path:$p}) RETURN coalesce(m.status,'active') AS st, m.content AS c",
                p=args.path,
            ).single()
            if prior:
                audit.record(s, args.path, "edit", actor="user",
                             status=prior["st"], content_snapshot=prior["c"], ts=now)
        s.run(
            """
            MERGE (m:Memory {path: $path})
            SET m.content = $content, m.updated_at = $now
            """,
            parameters={"path": args.path, "content": new_content, "now": now},
        )
    print(f"saved ({len(new_content)} chars)")
    return 0


def cmd_audit(args: argparse.Namespace) -> int:
    """Phase H2 — a memory's full mutation log (every dream write / edit / review
    transition, time-ordered with actor + status), or `--recent` for a graph-wide
    governance view. Reconstructed from the :MemoryRevision audit chain."""
    import audit
    with driver() as d, d.session() as s:
        if args.recent is not None:
            rows = audit.recent(s, args.recent)
            if not rows:
                print("(no audit entries)")
                return 0
            for r in rows:
                print(f"{str(r['ts'])[:19]}  {r['operation']:<17} {(r['actor'] or '?'):<18} {r['path']}")
            return 0
        if not args.path:
            print("usage: audit <path>   |   audit --recent [N]", file=sys.stderr)
            return 2
        t = audit.trail(s, args.path)
    if not t:
        print(f"no memory at path: {args.path}", file=sys.stderr)
        return 1
    print(f"{t['path']}  (created {str(t['created_at'] or '?')[:19]} by "
          f"{t['created_by'] or '?'}; now {t['current_status']})")
    for e in t["entries"]:
        op = e["operation"]
        res = f" → {e['result_status']}" if e.get("result_status") and op != "current" else ""
        was = f" (was {e['prior_status']})" if e.get("prior_status") else ""
        size = f"  [{e['snapshot_len']}c]" if e.get("snapshot_len") is not None else ""
        print(f"  {str(e['ts'] or '?')[:19]}  {op}{res}{was}  by {e['actor'] or '?'}{size}")
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
        + "WITH s, count(DISTINCT e) AS events "
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
            WITH DISTINCT e
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
    """Dump memories (and optionally sessions+events) to JSON.

    PR-I #1+#2+#4 — streaming backup that's safe on large graphs:
    - Only memories are exported by default (small, bounded).
    - --with-sessions REQUIRES at least one scope flag: --since,
      --session-key, --limit, OR the explicit --all-sessions opt-in.
    - Events are streamed one row per event from Neo4j with field
      projection done in Cypher (no `collect(properties(e))`, no
      `properties(e)`); --no-tool-response drops those fields server-side
      so they're never materialized; --max-field-chars uses substring()
      in Cypher rather than slicing in Python after the data has already
      crossed the wire.
    - JSON is assembled incrementally in Python so we never hold the
      whole graph in memory.
    """
    import json as _json
    from datetime import timedelta as _td
    import re as _re

    out_path = Path(args.out) if args.out else Path(
        f"njhook-backup-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.json"
    )

    # --- scope guard for --with-sessions ----------------------------------
    if args.with_sessions:
        scoped = bool(args.since or args.session_key or (args.limit and args.limit > 0)
                      or args.all_sessions)
        if not scoped:
            print(
                "--with-sessions needs an explicit scope: pass --since 7d, "
                "--session-key <key>, --limit N, or --all-sessions to opt into "
                "the unbounded export.",
                file=sys.stderr,
            )
            return 2
        # PR-J #2: --all-sessions still streams from Neo4j (the OOM fix
        # holds), but the Python-side payload accumulates everything before
        # writing. On a graph with many MB of tool_response across hundreds
        # of sessions, that consumes a lot of process memory. Require an
        # explicit trimming knob so we don't silently turn a "back up
        # everything" command into a multi-GB JSON file.
        if args.all_sessions and not (args.no_tool_response or args.max_field_chars > 0):
            print(
                "--all-sessions requires either --no-tool-response or "
                "--max-field-chars N to bound per-event field sizes. The Neo4j "
                "side streams safely, but the JSON payload is still assembled "
                "in Python memory before write.",
                file=sys.stderr,
            )
            return 2

    payload: dict = {
        "version": 2,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "memories": [],
        "memory_revisions": [],   # Phase A: evolution history
        "supersessions": [],      # Phase A: (old)-[:SUPERSEDED_BY]->(new) edges
        "sessions": [],
    }

    with driver() as d, d.session() as s:
        # --- memories (small, project explicit fields) ---------------------
        emb_clause = (
            "m.embedding AS embedding, m.embedding_model AS embedding_model, "
            "m.embedding_dim AS embedding_dim, "
            if args.with_embeddings else ""
        )
        for r in s.run(
            "MATCH (m:Memory) "
            "RETURN m.path AS path, m.content AS content, m.project AS project, "
            "       m.updated_at AS updated_at, "
            "       coalesce(m.archived,false) AS archived, "
            "       coalesce(m.access_count,0) AS access_count, "
            "       m.last_accessed_at AS last_accessed_at, "
            "       m.consolidated_from AS consolidated_from, "
            "       m.promoted_from_pattern AS promoted_from_pattern, "
            "       m.status AS status, m.ingested_at AS ingested_at, "
            "       m.valid_from AS valid_from, m.valid_until AS valid_until, "
            "       m.created_by AS created_by, m.importance AS importance, "
            f"      {emb_clause}"
            "       null AS _end "
            "ORDER BY m.path"
        ):
            d_ = {k: r[k] for k in r.keys() if k != "_end" and r[k] is not None}
            payload["memories"].append(d_)

        # --- Phase A lineage: revision chain + supersession edges (small; always) ---
        for r in s.run(
            "MATCH (rev:MemoryRevision)-[:VERSION_OF]->(m:Memory) "
            "RETURN m.path AS path, rev.content_snapshot AS content_snapshot, "
            "       rev.status AS status, rev.operation AS operation, "
            "       rev.actor AS actor, rev.ts AS ts "
            "ORDER BY m.path, rev.ts"
        ):
            payload["memory_revisions"].append({k: r[k] for k in r.keys() if r[k] is not None})
        for r in s.run(
            "MATCH (old:Memory)-[:SUPERSEDED_BY]->(new:Memory) "
            "RETURN old.path AS from_path, new.path AS to_path"
        ):
            payload["supersessions"].append({"from_path": r["from_path"], "to_path": r["to_path"]})

        # --- sessions (streaming, scoped) ---------------------------------
        if args.with_sessions:
            sess_where: list[str] = []
            params: dict = {}
            if args.since:
                m = _re.fullmatch(r"(\d+)([hdm])", args.since)
                if not m:
                    print(f"--since must be like 24h / 7d / 30m; got {args.since!r}", file=sys.stderr)
                    return 2
                n, unit = int(m.group(1)), m.group(2)
                delta = {"h": _td(hours=n), "d": _td(days=n), "m": _td(minutes=n)}[unit]
                params["since"] = (datetime.now(timezone.utc) - delta).isoformat()
                sess_where.append("coalesce(s.created_at, '') >= $since")
            if args.session_key:
                sess_where.append("s.session_key = $session_key")
                params["session_key"] = args.session_key

            sess_query = (
                "MATCH (s:Session) "
                + (("WHERE " + " AND ".join(sess_where) + " ") if sess_where else "")
                + "RETURN s.session_key AS session_key, s.session_id AS session_id, "
                  "       s.client AS client, s.created_at AS created_at, "
                  "       s.last_dreamed_at AS last_dreamed_at "
                  "ORDER BY s.created_at DESC"
            )
            if args.limit and args.limit > 0:
                sess_query += " LIMIT $limit"
                params["limit"] = args.limit

            session_rows = list(s.run(sess_query, parameters=params))

            # Field-projection knobs: omit tool_response/transcript entirely
            # when --no-tool-response, and substring() any kept long fields
            # to --max-field-chars at the DB so we never transfer the full
            # blob.
            cap = args.max_field_chars or 0
            if args.no_tool_response:
                tr_clause = "null AS tool_response, null AS transcript"
            elif cap > 0:
                tr_clause = (
                    "CASE WHEN size(coalesce(e.tool_response, '')) > $cap "
                    "THEN substring(e.tool_response, 0, $cap) + '...[truncated]' "
                    "ELSE e.tool_response END AS tool_response, "
                    "CASE WHEN size(coalesce(e.transcript, '')) > $cap "
                    "THEN substring(e.transcript, 0, $cap) + '...[truncated]' "
                    "ELSE e.transcript END AS transcript"
                )
            else:
                tr_clause = "e.tool_response AS tool_response, e.transcript AS transcript"

            if cap > 0:
                prompt_clause = (
                    "CASE WHEN size(coalesce(e.prompt, '')) > $cap "
                    "THEN substring(e.prompt, 0, $cap) + '...[truncated]' "
                    "ELSE e.prompt END AS prompt"
                )
                input_clause = (
                    "CASE WHEN size(coalesce(e.tool_input, '')) > $cap "
                    "THEN substring(e.tool_input, 0, $cap) + '...[truncated]' "
                    "ELSE e.tool_input END AS tool_input"
                )
                last_msg_clause = (
                    "CASE WHEN size(coalesce(e.last_assistant_message, '')) > $cap "
                    "THEN substring(e.last_assistant_message, 0, $cap) + '...[truncated]' "
                    "ELSE e.last_assistant_message END AS last_assistant_message"
                )
            else:
                prompt_clause = "e.prompt AS prompt"
                input_clause = "e.tool_input AS tool_input"
                last_msg_clause = "e.last_assistant_message AS last_assistant_message"

            event_query = (
                "MATCH (s:Session {session_key: $sk})-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
                # PR-J #3: DISTINCT defends against duplicate rows when the
                # NEXT chain is corrupted (multiple branches from a node);
                # without this, a damaged graph could double-count events.
                "WITH DISTINCT e ORDER BY e.timestamp "
                "RETURN e.event_id AS event_id, e.event_name AS event_name, "
                "       e.client AS client, e.timestamp AS timestamp, "
                "       e.cwd AS cwd, e.tool_name AS tool_name, "
                "       e.tool_use_id AS tool_use_id, "
                "       e.model AS model, e.source AS source, "
                "       e.turn_id AS turn_id, "
                "       e.stop_hook_active AS stop_hook_active, "
                "       e.transcript_path AS transcript_path, "
                f"      {prompt_clause}, "
                f"      {input_clause}, "
                f"      {last_msg_clause}, "
                f"      {tr_clause}"
            )

            for sess in session_rows:
                events: list[dict] = []
                ev_params = {"sk": sess["session_key"]}
                if cap > 0:
                    ev_params["cap"] = cap
                # Stream — never materialize the full event list in Neo4j.
                for er in s.run(event_query, parameters=ev_params):
                    ev = {k: er[k] for k in er.keys() if er[k] is not None}
                    events.append(ev)
                payload["sessions"].append({
                    "session_key": sess["session_key"],
                    "session_id": sess["session_id"],
                    "client": sess["client"],
                    "created_at": sess["created_at"],
                    "last_dreamed_at": sess["last_dreamed_at"],
                    "events": events,
                })

    out_path.write_text(_json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(
        f"wrote {out_path} — {len(payload['memories'])} memories, "
        f"{len(payload['sessions'])} sessions ({out_path.stat().st_size} bytes)"
    )
    return 0


def _validate_backup(memories: list[dict], sessions: list[dict]) -> list[str]:
    """PR-L: pre-flight backup shape check. Returns a list of error strings;
    empty list means safe to restore. Catches malformed backups up front so
    we don't half-restore and leave the graph in a wedged state.
    """
    errors: list[str] = []
    for i, m in enumerate(memories):
        if not isinstance(m, dict):
            errors.append(f"memories[{i}] is not an object")
            continue
        if not m.get("path"):
            errors.append(f"memories[{i}] missing 'path'")
        if "content" not in m:
            errors.append(f"memories[{i}] (path={m.get('path')!r}) missing 'content'")
    for i, sess in enumerate(sessions):
        if not isinstance(sess, dict):
            errors.append(f"sessions[{i}] is not an object")
            continue
        if not sess.get("session_key") and not (sess.get("client") and sess.get("session_id")):
            errors.append(
                f"sessions[{i}] missing 'session_key' (or 'client'+'session_id' to derive one)"
            )
        # PR-N: check the raw value, not the coalesced one — `events: ""` or
        # `events: 42` would otherwise slip past as []. Missing key is fine
        # (treated as []); present-but-not-list is a malformed signal.
        events_raw = sess.get("events", [])
        if not isinstance(events_raw, list):
            errors.append(f"sessions[{i}].events is not a list (got {type(events_raw).__name__})")
            continue
        events = events_raw
        for j, e in enumerate(events):
            if not isinstance(e, dict):
                errors.append(f"sessions[{i}].events[{j}] is not an object")
                continue
            if not e.get("event_id"):
                errors.append(
                    f"sessions[{i}].events[{j}] missing 'event_id' "
                    f"(session_key={sess.get('session_key')!r}); restore would silently "
                    "skip this event and could leave a broken chain"
                )
    return errors


def cmd_restore(args: argparse.Namespace) -> int:
    """Load a backup file. Memories upsert by path; sessions upsert by session_key.
    Embeddings restored only when present in the backup AND --with-embeddings is set.

    PR-L: validates backup shape up front. Aborts before any DB write if the
    file is malformed (missing memory.path/content, session.session_key, or
    event.event_id). This prevents partial restores from silently skipping
    events and leaving the chain wedged.
    """
    import json as _json
    in_path = Path(args.in_)
    if not in_path.exists():
        print(f"file not found: {in_path}", file=sys.stderr)
        return 1
    payload = _json.loads(in_path.read_text(encoding="utf-8"))
    memories = payload.get("memories") or []
    sessions = payload.get("sessions") or []
    revisions = payload.get("memory_revisions") or []     # Phase A lineage
    supersessions = payload.get("supersessions") or []

    validation_errors = _validate_backup(memories, sessions)
    if validation_errors:
        print(f"backup at {in_path} has {len(validation_errors)} malformed entries:", file=sys.stderr)
        for e in validation_errors[:20]:
            print(f"  - {e}", file=sys.stderr)
        if len(validation_errors) > 20:
            print(f"  ... and {len(validation_errors) - 20} more", file=sys.stderr)
        if not args.allow_malformed:
            print("\naborting; nothing was written. Fix the backup or pass --allow-malformed.",
                  file=sys.stderr)
            return 2
        # PR-M: --allow-malformed used to print errors and then proceed into
        # write code that ASSUMED the bad fields existed (KeyError on
        # m['path']) or invented sentinel keys like "unknown:unknown" for
        # sessions missing identifiers. Both were silent corruption modes.
        # Now: filter the bad records out, log a single skip-counts line,
        # and only write the salvageable subset.
        skipped_memories = 0
        kept_memories = []
        for m in memories:
            if isinstance(m, dict) and m.get("path") and "content" in m:
                kept_memories.append(m)
            else:
                skipped_memories += 1

        skipped_sessions = 0
        kept_sessions = []
        skipped_events_total = 0
        for sess in sessions:
            if not isinstance(sess, dict):
                skipped_sessions += 1
                continue
            sk = sess.get("session_key")
            if not sk and sess.get("client") and sess.get("session_id"):
                sk = f"{sess['client']}:{sess['session_id']}"
            if not sk:
                # No identifier and no derivation — never invent unknown:unknown.
                skipped_sessions += 1
                continue
            sess = dict(sess)
            sess["session_key"] = sk
            # PR-N: distinguish "events key missing" (treat as []) from
            # "events present but not a list" (malformed — skip the whole
            # session). Coercing the latter to [] would, when combined with
            # restore's always-wipe-existing-chain semantics (PR-K), DELETE
            # the real events of a same-keyed session in the graph. That's
            # a data-loss path; refuse rather than ride.
            if "events" in sess and not isinstance(sess["events"], list):
                skipped_sessions += 1
                continue
            events = sess.get("events") or []
            kept_events = []
            for e in events:
                if isinstance(e, dict) and e.get("event_id"):
                    kept_events.append(e)
                else:
                    skipped_events_total += 1
            sess["events"] = kept_events
            kept_sessions.append(sess)

        print(
            f"--allow-malformed: skipping {skipped_memories} memories, "
            f"{skipped_sessions} sessions, {skipped_events_total} events with missing required fields",
            file=sys.stderr,
        )
        memories = kept_memories
        sessions = kept_sessions

    print(f"restoring from {in_path}: {len(memories)} memories, {len(sessions)} sessions")

    if args.dry_run:
        for m in memories[:5]:
            print(f"  would write Memory {m['path']}")
        for s in sessions[:5]:
            print(f"  would write Session {s.get('session_key') or s.get('session_id')}")
        print("[dry-run] no writes")
        return 0

    with driver() as d, d.session() as s:
        # Memories — explicit row-by-row upsert so we don't depend on APOC.
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (m:Memory) REQUIRE m.path IS UNIQUE")
        for m in memories:
            props = {k: v for k, v in m.items() if k != "path"}
            if not args.with_embeddings:
                for k in ("embedding", "embedding_model", "embedding_dim"):
                    props.pop(k, None)
            s.run(
                "MERGE (m:Memory {path: $path}) SET m += $props",
                parameters={"path": m["path"], "props": props},
            )

        # Phase A lineage — recreate the :MemoryRevision chain + :SUPERSEDED_BY
        # edges idempotently (MERGE by target-path + ts + snapshot / by path pair).
        for rev in revisions:
            p = rev.get("path")
            if not p:
                continue
            s.run(
                "MATCH (m:Memory {path: $path}) "
                "MERGE (m)<-[:VERSION_OF]-(r:MemoryRevision {ts: $ts, content_snapshot: $cs}) "
                "SET r.status = $st, r.operation = $op, r.actor = $ac",
                parameters={"path": p, "ts": rev.get("ts"), "cs": rev.get("content_snapshot"),
                            "st": rev.get("status"), "op": rev.get("operation"), "ac": rev.get("actor")},
            )
        for sup in supersessions:
            fr, to = sup.get("from_path"), sup.get("to_path")
            if not fr or not to:
                continue
            s.run(
                "MATCH (a:Memory {path: $fr}), (b:Memory {path: $to}) MERGE (a)-[:SUPERSEDED_BY]->(b)",
                parameters={"fr": fr, "to": to},
            )

        # Sessions
        s.run("CREATE CONSTRAINT IF NOT EXISTS FOR (s:Session) REQUIRE s.session_key IS UNIQUE")
        for sess in sessions:
            # PR-M: NEVER fabricate `unknown:unknown`. By this point either
            # _validate_backup() has accepted the session (session_key is
            # present or derivable from client+session_id), or
            # --allow-malformed already filtered out unidentifiable sessions
            # and stamped session_key on the survivors. Defensively skip if
            # somehow we got here without one.
            sk = sess.get("session_key")
            if not sk and sess.get("client") and sess.get("session_id"):
                sk = f"{sess['client']}:{sess['session_id']}"
            if not sk:
                print(f"  skip: session has no identifier ({sess!r:.80})", file=sys.stderr)
                continue
            sess_props = {k: v for k, v in sess.items() if k not in ("events", "session_key") and v is not None}
            sess_props["session_key"] = sk
            s.run(
                "MERGE (s:Session {session_key: $sk}) SET s += $props",
                parameters={"sk": sk, "props": sess_props},
            )
            # PR-K: ALWAYS wipe the existing reachable chain — even when the
            # backup's events list is empty. Previously the wipe was inside
            # `if events:`, so restoring a backup whose session has zero
            # events left the old FIRST_EVENT/NEXT/LATEST_EVENT chain intact,
            # and the restored graph didn't match the backup. Restore should
            # match the backup; if the backup says "this session has no
            # events," the graph must reflect that.
            events = sess.get("events") or []
            s.run(
                "MATCH (s:Session {session_key: $sk}) "
                "OPTIONAL MATCH (s)-[:FIRST_EVENT|NEXT*0..]->(e:Event) "
                "DETACH DELETE e",
                parameters={"sk": sk},
            )
            if events:
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


# --- Phase H4: backup/restore rehearsal -------------------------------------

def _rehearsal_backup_args(out: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        out=out, with_embeddings=False, with_sessions=False, since=None,
        session_key=None, limit=0, all_sessions=False, no_tool_response=False, max_field_chars=0,
    )


def run_rehearsal() -> dict:
    """Phase H4 — prove the backup→restore pipeline works end-to-end on a
    DISPOSABLE marker subgraph, without touching real data: seed a marker memory
    + revision, run the real `cmd_backup`, confirm the marker is in the backup,
    filter the backup to an isolated payload, delete the marker, restore it FROM
    THE BACKUP'S OWN FORMAT via the real `cmd_restore`, and verify content +
    lineage came back. Records a `:RehearsalRun {ts, ok, detail}` either way and
    always cleans up the marker. Returns {ts, ok, detail}.

    'Untested backups aren't backups': this is the recurring restore drill, and
    `njhook health` flags when it's gone stale or last failed."""
    seed_ts = datetime.now(timezone.utc).isoformat()
    marker = f"general/__rehearsal_{seed_ts.replace(':', '').replace('-', '').replace('.', '')}.md"
    content = ("---\ntitle: restore rehearsal\nkind: general\n---\n\n"
               "Disposable backup/restore rehearsal marker — safe to delete.")
    ok, detail = False, ""
    tmpdir = tempfile.mkdtemp(prefix="njhook-rehearsal-")
    try:
        with driver() as d, d.session() as s:
            s.run(
                "MERGE (m:Memory {path:$p}) "
                "SET m.content=$c, m.status='active', m.created_by='rehearsal', "
                "    m.updated_at=$now, m.valid_from=$now, m.ingested_at=$now "
                "MERGE (rev:MemoryRevision {ts:$rt, content_snapshot:$cs}) "
                "SET rev.operation='dream_update', rev.actor='rehearsal', rev.status='active' "
                "MERGE (rev)-[:VERSION_OF]->(m)",
                p=marker, c=content, now=seed_ts, rt=seed_ts, cs="prior rehearsal body",
            )
        full = os.path.join(tmpdir, "full.json")
        if cmd_backup(_rehearsal_backup_args(full)) != 0:
            raise RuntimeError("backup returned non-zero")
        import json as _json
        data = _json.loads(open(full, encoding="utf-8").read())
        mems = [m for m in data.get("memories", []) if m.get("path") == marker]
        revs = [r for r in data.get("memory_revisions", []) if r.get("path") == marker]
        if not mems:
            raise RuntimeError("marker absent from backup — backup did not capture live data")
        mini = os.path.join(tmpdir, "mini.json")
        with open(mini, "w", encoding="utf-8") as f:
            _json.dump({"version": data.get("version", 2), "memories": mems,
                        "memory_revisions": revs, "supersessions": [], "sessions": []}, f)
        with driver() as d, d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) DETACH DELETE r", p=marker)
            s.run("MATCH (m:Memory {path:$p}) DETACH DELETE m", p=marker)
        if cmd_restore(types.SimpleNamespace(
                in_=mini, with_embeddings=False, dry_run=False, allow_malformed=False)) != 0:
            raise RuntimeError("restore returned non-zero")
        with driver() as d, d.session() as s:
            chk = s.run(
                "MATCH (m:Memory {path:$p}) "
                "OPTIONAL MATCH (rev:MemoryRevision)-[:VERSION_OF]->(m) "
                "RETURN m.content AS c, count(rev) AS nrev", p=marker).single()
        if not chk or chk["c"] != content:
            raise RuntimeError("restored marker content mismatch")
        if chk["nrev"] < 1:
            raise RuntimeError("revision lineage not restored")
        ok = True
        detail = f"backup→restore round-trip verified ({chk['nrev']} revision(s))"
    except Exception as e:
        detail = f"{type(e).__name__}: {str(e)[:120]}"
    finally:
        try:
            with driver() as d, d.session() as s:
                s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory {path:$p}) DETACH DELETE r", p=marker)
                s.run("MATCH (m:Memory {path:$p}) DETACH DELETE m", p=marker)
        except Exception:
            pass
        shutil.rmtree(tmpdir, ignore_errors=True)

    rec_ts = datetime.now(timezone.utc).isoformat()
    try:
        with driver() as d, d.session() as s:
            s.run("CREATE (:RehearsalRun {ts:$ts, ok:$ok, detail:$detail})",
                  ts=rec_ts, ok=ok, detail=detail)
    except Exception:
        pass
    return {"ts": rec_ts, "ok": ok, "detail": detail}


def cmd_rehearse_restore(args: argparse.Namespace) -> int:
    """Phase H4 — run a backup/restore rehearsal and record the result."""
    res = run_rehearsal()
    if res["ok"]:
        print(f"restore rehearsal OK ({res['ts'][:19]}): {res['detail']}")
        return 0
    print(f"restore rehearsal FAILED: {res['detail']}", file=sys.stderr)
    return 1


def _rehearsal_health_row(latest: dict | None, rehearsal_days: int, now=None):
    """Compute the `health` row for the restore rehearsal from the latest
    :RehearsalRun ({ts, ok, detail} or None). Pure so it's unit-testable."""
    if not latest:
        return ("warn", "restore rehearsal",
                "never run — `njhook rehearse-restore` to verify backups are restorable")
    now = now or datetime.now(timezone.utc)
    ts = latest.get("ts")
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (now - dt).days
    except Exception:
        age_days = None
    if not latest.get("ok"):
        return ("fail", "restore rehearsal",
                f"last rehearsal FAILED ({str(ts)[:10]}): {latest.get('detail')}")
    if age_days is not None and age_days > rehearsal_days:
        return ("warn", "restore rehearsal",
                f"last ok {str(ts)[:10]} (>{rehearsal_days}d ago) — re-run `njhook rehearse-restore`")
    age_str = f"{age_days}d ago" if age_days is not None else str(ts)[:19]
    return ("ok", "restore rehearsal", f"last ok {str(ts)[:10]} ({age_str})")


def cmd_health(args: argparse.Namespace) -> int:
    """Run a series of stack-readiness checks. Exit 0 if all OK or only WARN;
    exit 1 if any FAIL.

    Designed so a fresh user can run `njhook health` and see, at a glance,
    whether the whole pipeline (Neo4j, hook wrappers, user-level configs,
    Ollama, scheduled task, recent dream success) is operational.
    """
    import json as _json
    import urllib.request as _ureq
    import urllib.error as _uerr
    import subprocess as _sp
    repo = Path(__file__).resolve().parents[1]
    home = Path.home()
    sys.path.insert(0, str(repo / "hooks"))
    import embeddings as _embeddings  # type: ignore

    OK, WARN, FAIL = "ok", "warn", "fail"
    rows: list[tuple[str, str, str]] = []  # (status, name, message)

    # --- 1. Neo4j reachable ---
    try:
        with driver() as d, d.session() as s:
            s.run("RETURN 1").single()
        rows.append((OK, "neo4j", f"reachable at {NEO4J_URI}"))
    except Exception as e:
        rows.append((FAIL, "neo4j", f"unreachable: {type(e).__name__}: {str(e)[:80]}"))
        # If Neo4j is down, schema/index/dream-history checks are pointless.
        return _print_health(rows)

    # --- 2. Required constraints ---
    expected_constraints = [
        ("Session", ["session_key"]),
        ("Event", ["event_id"]),
        ("Memory", ["path"]),
    ]
    try:
        with driver() as d, d.session() as s:
            existing = list(s.run("SHOW CONSTRAINTS YIELD labelsOrTypes, properties, type"))
        present = {(r["labelsOrTypes"][0], tuple(r["properties"]))
                   for r in existing if "UNIQUE" in (r["type"] or "").upper()}
        missing = [(lbl, props) for lbl, props in expected_constraints
                   if (lbl, tuple(props)) not in present]
        if missing:
            mlist = ", ".join(f"{l}.{p[0]}" for l, p in missing)
            rows.append((FAIL, "constraints", f"missing UNIQUE constraints: {mlist} — run `njhook migrate`"))
        else:
            rows.append((OK, "constraints", f"{len(expected_constraints)} required UNIQUE constraints present"))
    except Exception as e:
        rows.append((WARN, "constraints", f"could not list: {e}"))

    # --- 3. Indexes (informational) ---
    try:
        with driver() as d, d.session() as s:
            indexes = list(s.run("SHOW INDEXES YIELD name, type"))
        names = {r["name"] for r in indexes}
        wanted = {
            "memory_fulltext": "fulltext",
            "memory_project": "btree/range",
            "session_id_lookup": "btree/range",
        }
        missing = [n for n in wanted if n not in names]
        if missing:
            rows.append((WARN, "indexes", f"missing: {', '.join(missing)} — run `njhook migrate`"))
        else:
            rows.append((OK, "indexes", f"{len(wanted)} required indexes present"))
        if "memory_embeddings" in names:
            rows.append((OK, "vector_index", "memory_embeddings present"))
        else:
            rows.append((WARN, "vector_index",
                         "memory_embeddings not yet created — run `njhook embed-backfill`"))
    except Exception as e:
        rows.append((WARN, "indexes", f"could not list: {e}"))

    # --- 4. Hook wrappers (project-level) ---
    for client_dir in (".claude", ".codex", ".cursor", ".gemini"):
        log_event = repo / client_dir / "hooks" / "log_event.cmd"
        inject = repo / client_dir / "hooks" / "inject_memory.cmd"
        if log_event.exists() and inject.exists():
            rows.append((OK, f"hooks {client_dir}", "wrappers present"))
        else:
            rows.append((WARN, f"hooks {client_dir}",
                         f"missing {log_event.name if not log_event.exists() else inject.name}"))

    # --- 5. User-level configs ---
    user_configs = [
        (home / ".claude" / "settings.json", "hooks", "claude"),
        (home / ".codex" / "hooks.json", None, "codex"),
        (home / ".gemini" / "settings.json", "hooks", "gemini"),
    ]
    for path, required_key, label in user_configs:
        if not path.exists():
            rows.append((WARN, f"user config {label}", f"{path} not found — global capture disabled for this client"))
            continue
        try:
            data = _json.loads(path.read_text(encoding="utf-8"))
        except Exception as e:
            rows.append((WARN, f"user config {label}", f"unreadable: {e}"))
            continue
        if required_key and required_key not in data:
            rows.append((WARN, f"user config {label}", f"{path.name} missing '{required_key}' key"))
        else:
            rows.append((OK, f"user config {label}", str(path)))

    # --- 6. Env vars ---
    for var in ("HOOKS_NEO4J_URI", "HOOKS_NEO4J_USER", "HOOKS_NEO4J_PASSWORD"):
        if os.environ.get(var):
            rows.append((OK, f"env {var}", "set"))
        else:
            rows.append((WARN, f"env {var}", "unset (using default)"))
    if os.environ.get("EMBED_PROVIDER"):
        rows.append((OK, "env EMBED_PROVIDER", os.environ["EMBED_PROVIDER"]))
    else:
        rows.append((WARN, "env EMBED_PROVIDER", "unset — semantic recall disabled, fulltext only"))
    if os.environ.get("ANTHROPIC_API_KEY"):
        rows.append((OK, "env ANTHROPIC_API_KEY", "set"))
    else:
        rows.append((WARN, "env ANTHROPIC_API_KEY", "unset — only ollama dream provider available"))

    # --- 7. Ollama (only if EMBED_PROVIDER=ollama or DREAM_PROVIDER=ollama) ---
    needs_ollama = os.environ.get("EMBED_PROVIDER") == "ollama" or os.environ.get("DREAM_PROVIDER") == "ollama"
    if needs_ollama:
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        try:
            with _ureq.urlopen(f"{host}/api/tags", timeout=3) as resp:
                tags = _json.loads(resp.read().decode("utf-8"))
            models = [m["name"] for m in tags.get("models", [])]
            rows.append((OK, "ollama daemon", f"reachable at {host} ({len(models)} models)"))
            if _embeddings.is_enabled():
                want = _embeddings.model()
                if want in models or any(m.split(":")[0] == want.split(":")[0] for m in models):
                    rows.append((OK, "ollama embed model", want))
                else:
                    rows.append((FAIL, "ollama embed model",
                                 f"{want} not pulled — run `ollama pull {want.split(':')[0]}`"))
        except Exception as e:
            rows.append((FAIL, "ollama daemon", f"unreachable at {host}: {e}"))

    # --- 8. Scheduled task ---
    try:
        p = _sp.run(["schtasks.exe", "/Query", "/TN", "njhook-dream-nightly", "/FO", "LIST"],
                    capture_output=True, text=True, timeout=5)
        if p.returncode == 0:
            next_run = "?"
            for line in p.stdout.splitlines():
                if line.strip().startswith("Next Run Time:"):
                    next_run = line.split(":", 1)[1].strip()
                    break
            rows.append((OK, "scheduled task", f"njhook-dream-nightly registered, next run {next_run}"))
        else:
            rows.append((WARN, "scheduled task", "njhook-dream-nightly not registered — see README"))
    except FileNotFoundError:
        rows.append((WARN, "scheduled task", "schtasks.exe not available (non-Windows host?)"))
    except Exception as e:
        rows.append((WARN, "scheduled task", f"check failed: {e}"))

    # --- 9. Last dream log ---
    log_dir = repo / "dream" / "logs"
    if not log_dir.exists():
        rows.append((WARN, "dream log", f"{log_dir} not yet created (no dream has run)"))
    else:
        logs = sorted(log_dir.glob("dream_*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
        if not logs:
            rows.append((WARN, "dream log", "no dream logs yet"))
        else:
            latest = logs[0]
            tail = latest.read_text(encoding="utf-8", errors="replace").splitlines()[-3:]
            tail_text = " | ".join(t.strip() for t in tail if t.strip())[:140]
            mtime = datetime.fromtimestamp(latest.stat().st_mtime, tz=timezone.utc).isoformat()[:19]
            if "exit=0" in tail_text:
                rows.append((OK, "dream log", f"{latest.name} latest exit=0  ({mtime})"))
            else:
                rows.append((WARN, "dream log", f"{latest.name} latest didn't end exit=0: ...{tail_text[-80:]}"))

    # --- 10. Dream freshness ---
    # The dream-log check above only inspects the last log *line*; a nightly
    # that "ran" but distilled nothing (or hasn't run in weeks) still looks
    # fine there. This checks the graph itself: how stale is the newest memory,
    # and how many sessions are still awaiting their first dream.
    try:
        stale_days = int(os.environ.get("NJHOOK_FRESHNESS_DAYS", "7"))
        cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
        with driver() as d, d.session() as s:
            newest_mem = s.run("MATCH (m:Memory) RETURN max(m.updated_at) AS t").single()["t"]
            undreamed = s.run(
                "MATCH (s:Session)-[:LATEST_EVENT]->(:Event) WHERE s.last_dreamed_at IS NULL "
                "RETURN count(DISTINCT s) AS n"
            ).single()["n"]
        if not newest_mem:
            rows.append((WARN, "dream freshness", "no memories in graph — has the dream phase ever produced output?"))
        elif newest_mem < cutoff:
            rows.append((WARN, "dream freshness",
                         f"newest memory updated {newest_mem[:10]} (>{stale_days}d ago); "
                         f"{undreamed} session(s) awaiting first dream — distillation may be stalled"))
        else:
            rows.append((OK, "dream freshness",
                         f"newest memory {newest_mem[:10]}; {undreamed} session(s) awaiting first dream"))
    except Exception as e:
        rows.append((WARN, "dream freshness", f"check failed: {e}"))

    # --- 11. Event spool / ingest (Phase B) ---
    try:
        import spool as _spool
        backlog = _spool.backlog_count()
        dlq = _spool.dlq_count()
        if dlq > 0:
            rows.append((WARN, "event spool", f"{backlog} spooled; {dlq} dead-lettered — inspect {_spool._dlq_file()}"))
        elif backlog > 0:
            rows.append((OK, "event spool", f"{backlog} event(s) spooled awaiting `njhook ingest`"))
        else:
            rows.append((OK, "event spool", "empty (no backlog, no dead-letters)"))
    except Exception as e:
        rows.append((WARN, "event spool", f"check failed: {e}"))

    # --- 12. Egress policy (Phase H) ---
    try:
        import privacy as _privacy
        sensitive = _privacy._load_paths("HOOKS_SENSITIVE_PATHS", _privacy.SENSITIVE_FILE)
        allow = os.environ.get("DREAM_ALLOW_SENSITIVE_EGRESS") == "1"
        if allow:
            rows.append((WARN, "egress policy",
                         f"{len(sensitive)} sensitive path(s); remote egress ALLOWED for sensitive sessions"))
        else:
            rows.append((OK, "egress policy",
                         f"{len(sensitive)} sensitive path(s); sensitive sessions kept off remote dream providers"))
    except Exception as e:
        rows.append((WARN, "egress policy", f"check failed: {e}"))

    # --- 13. Restore rehearsal (Phase H4) ---
    try:
        rehearsal_days = int(os.environ.get("NJHOOK_REHEARSAL_DAYS", "30"))
        with driver() as d, d.session() as s:
            rr = s.run("MATCH (rr:RehearsalRun) RETURN rr.ts AS ts, rr.ok AS ok, rr.detail AS detail "
                       "ORDER BY rr.ts DESC LIMIT 1").single()
        rows.append(_rehearsal_health_row(dict(rr) if rr else None, rehearsal_days))
    except Exception as e:
        rows.append((WARN, "restore rehearsal", f"check failed: {e}"))

    return _print_health(rows)


def _print_health(rows: list[tuple[str, str, str]]) -> int:
    glyph = {"ok": " OK ", "warn": "WARN", "fail": "FAIL"}
    width = max(len(name) for _, name, _ in rows)
    fail_count = 0
    for status, name, msg in rows:
        if status == "fail":
            fail_count += 1
        print(f"[{glyph[status]}]  {name:<{width}}  {msg}")
    print()
    counts = {s: 0 for s in ("ok", "warn", "fail")}
    for status, _, _ in rows:
        counts[status] += 1
    print(f"summary: {counts['ok']} ok, {counts['warn']} warn, {counts['fail']} fail")
    return 1 if fail_count else 0


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

    psr = sub.add_parser("search", help="hybrid search memories (and optionally raw events)")
    psr.add_argument("query")
    psr.add_argument("--min-score", type=float, default=0.5, dest="min_score")
    psr.add_argument("--limit", type=int, default=10)
    psr.add_argument("--events", action="store_true", help="also search raw session events (not yet distilled)")
    psr.set_defaults(fn=cmd_search)

    phi = sub.add_parser("history", help="show a memory's revision timeline (how it evolved)")
    phi.add_argument("path")
    phi.add_argument("--diff", action="store_true", help="show unified diffs between consecutive versions")
    phi.add_argument("--as-of", dest="as_of", help="reconstruct the body that was current at this ISO timestamp")
    phi.set_defaults(fn=cmd_history)

    pau = sub.add_parser("audit", help="show a memory's full mutation log (Phase H2); --recent for a graph-wide view")
    pau.add_argument("path", nargs="?", help="memory path to audit (omit when using --recent)")
    pau.add_argument("--recent", nargs="?", type=int, const=20, default=None,
                     metavar="N", help="show the N most recent mutations across all memories (default 20)")
    pau.set_defaults(fn=cmd_audit)

    pin = sub.add_parser("ingest", help="drain the durable event spool into Neo4j (Phase B; idempotent)")
    pin.set_defaults(fn=cmd_ingest)

    prr = sub.add_parser("rehearse-restore", help="verify backups are restorable: backup→restore a disposable marker and record the result (Phase H4)")
    prr.set_defaults(fn=cmd_rehearse_restore)

    prv = sub.add_parser("review", help="conflict/review queue (Phase E): list/approve/reject/supersede/flag")
    prv.add_argument("action", choices=["list", "approve", "reject", "supersede", "flag", "auto-resolve"])
    prv.add_argument("paths", nargs="*", help="memory path(s) — 1 for approve/reject, 2 for supersede/flag")
    prv.set_defaults(fn=cmd_review)

    prc = sub.add_parser("recall", help="recall memories for a prompt (Phase G; same core as the hook)")
    prc.add_argument("prompt")
    prc.add_argument("--cwd", help="project scope — derive the project from this cwd")
    prc.add_argument("--limit", type=int, default=5)
    prc.add_argument("--json", action="store_true", help="machine-readable JSON output")
    prc.set_defaults(fn=cmd_recall)

    pwe = sub.add_parser("write-event", help="capture an event from JSON (stdin or --json FILE) via the shared capture path")
    pwe.add_argument("--client", required=True, choices=["claude_code", "codex", "cursor", "gemini"])
    pwe.add_argument("--json", help="path to a JSON file; '-' or omit reads stdin")
    pwe.set_defaults(fn=cmd_write_event)

    pev = sub.add_parser("eval-retrieval", help="seed a golden set and score recall (hit@k + MRR) — ranking regression guard")
    pev.set_defaults(fn=cmd_eval_retrieval)

    prn = sub.add_parser("render", help="render project memory into an agent context file (AGENTS.md/CLAUDE.md/GEMINI.md/Cursor) as a managed block")
    prn.add_argument("--target", default="agents", choices=["agents", "claude", "gemini", "cursor", "all"],
                     help="which context file to write (default: agents); 'all' writes every target")
    prn.add_argument("--root", help="directory to write into (default: cwd); also the project scope")
    prn.add_argument("--stdout", action="store_true", help="print the rendered file instead of writing it")
    prn.set_defaults(fn=cmd_render)

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

    phl = sub.add_parser("health", help="check Neo4j, schema, hook wrappers, configs, Ollama, scheduled task, last dream")
    phl.set_defaults(fn=cmd_health)

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
    pbk.add_argument("--with-sessions", action="store_true",
                     help="include sessions+events; REQUIRES one of --since / --session-key / --limit / --all-sessions")
    pbk.add_argument("--since", help="(with --with-sessions) only sessions created within this window, e.g. 7d / 24h")
    pbk.add_argument("--session-key", help="(with --with-sessions) export only the named session (e.g. claude_code:abc...)")
    pbk.add_argument("--limit", type=int, default=0, help="(with --with-sessions) cap to N most-recent sessions")
    pbk.add_argument("--all-sessions", action="store_true",
                     help="(with --with-sessions) explicit opt-in to unbounded export — can be huge")
    pbk.add_argument("--no-tool-response", action="store_true",
                     help="(with --with-sessions) drop tool_response and transcript server-side (never fetched)")
    pbk.add_argument("--max-field-chars", type=int, default=0,
                     help="(with --with-sessions) truncate kept string fields to N chars in Cypher (0 = unlimited)")
    pbk.set_defaults(fn=cmd_backup)

    prs = sub.add_parser("restore", help="load a backup file (idempotent upsert by path / session_key)")
    prs.add_argument("--in", dest="in_", required=True, help="input JSON file")
    prs.add_argument("--with-embeddings", action="store_true", help="restore m.embedding when present")
    prs.add_argument("--dry-run", action="store_true")
    prs.add_argument("--allow-malformed", action="store_true",
                     help="proceed even if pre-flight finds missing event_id / path / session_key")
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
