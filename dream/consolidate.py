"""Memory consolidation and archival.

Two operations, both invoked from dream.py and the njhook CLI:

- consolidate(): find pairs of memories whose embedding cosine similarity
  exceeds a threshold, ask the LLM to merge each pair, replace both memories
  with the merged one. Greedy: repeats until no pairs above threshold remain
  or a max-rounds cap is hit.

- archive(stale_days): set m.archived=true on memories that have neither been
  retrieved nor updated in stale_days days. Recall queries already filter
  archived=false, so archived memories vanish from sessions but stay queryable
  via the CLI.

Consolidation requires a dream-provider (LLM) AND embeddings to be enabled.
Archival needs neither.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone, timedelta
from typing import Callable

# These modules are picked up via sys.path in dream.py before this is imported.
from providers import get_provider, default_model  # type: ignore  # noqa: E402

CONSOLIDATE_SYSTEM_PROMPT = """You are merging two markdown memories that overlap significantly.

Output ONE consolidated memory in strict JSON:
{
  "path": "the better path for the merged memory (pick whichever is more general or specific, your call)",
  "content": "<full markdown body, including YAML frontmatter (title, kind)>"
}

Rules:
- Preserve every fact and rule from both inputs that still applies. Drop only true duplicates.
- If the two memories contradict, keep the newer claim (right-hand input is newer).
- Output JSON only, no commentary."""


def _fetch_pair_candidates(session, threshold: float, limit: int) -> list[dict]:
    """Find candidate pairs of memories with cosine similarity above threshold.

    Uses the vector index for the right-hand side; m1 < m2 by path so each
    unordered pair appears at most once.
    """
    try:
        return list(session.run(
            """
            MATCH (m1:Memory) WHERE m1.embedding IS NOT NULL
              AND coalesce(m1.archived, false) = false
            CALL db.index.vector.queryNodes('memory_embeddings', 5, m1.embedding)
            YIELD node AS m2, score
            WHERE m1.path < m2.path
              AND coalesce(m2.archived, false) = false
              AND score > $threshold
            RETURN m1.path AS p1, m1.content AS c1,
                   m2.path AS p2, m2.content AS c2,
                   score
            ORDER BY score DESC
            LIMIT $limit
            """,
            parameters={"threshold": threshold, "limit": limit},
        ))
    except Exception:
        return []


def _merge_pair(provider_fn: Callable, model: str, p1: str, c1: str, p2: str, c2: str) -> dict:
    """Ask the LLM to merge two memories. Returns {path, content} dict."""
    user_msg = (
        f"<older_memory>\n## {p1}\n{c1}\n</older_memory>\n\n"
        f"<newer_memory>\n## {p2}\n{c2}\n</newer_memory>"
    )
    # The provider abstraction expects a list-of-memories return shape; we wrap
    # our single-merge prompt to fit that, returning {"memories": [{...}]}.
    provider_prompt = (
        CONSOLIDATE_SYSTEM_PROMPT
        + '\n\nReturn the merged memory wrapped as {"memories": [{...}]} so the caller can reuse the existing JSON shape.'
    )
    out = provider_fn(
        transcript=user_msg, existing="(none)",
        system=provider_prompt, model=model, max_tokens=4096,
    )
    if not out:
        raise ValueError("provider returned no merged memory")
    merged = out[0]
    if not merged.get("path") or not merged.get("content"):
        raise ValueError(f"merged memory missing path/content: {merged}")
    return merged


def consolidate(driver, provider_name: str | None, threshold: float, max_rounds: int,
                dry_run: bool = False, embed_fn: Callable | None = None) -> int:
    """Returns number of pairs merged. Embed_fn is used to embed the merged
    memory if provided (so the new memory immediately participates in future
    consolidation rounds)."""
    pname, pfn = get_provider(provider_name)
    model = default_model(pname)
    print(f"consolidate: provider={pname} model={model} threshold={threshold} max_rounds={max_rounds}")

    rounds = 0
    merges = 0
    while rounds < max_rounds:
        rounds += 1
        with driver.session() as ses:
            pairs = _fetch_pair_candidates(ses, threshold, limit=10)
        if not pairs:
            print(f"  round {rounds}: no pairs above threshold")
            break
        # Take the strongest pair this round.
        p = pairs[0]
        print(f"  round {rounds}: merging '{p['p1']}' + '{p['p2']}' (sim={p['score']:.3f})")
        try:
            merged = _merge_pair(pfn, model, p["p1"], p["c1"], p["p2"], p["c2"])
        except Exception as e:
            print(f"    merge failed: {e}; skipping pair")
            continue

        if dry_run:
            print(f"    [dry-run] would write: {merged['path']}")
            merges += 1
            # Don't actually write — but we'd loop forever on the same pair.
            break

        new_path = merged["path"]
        new_content = merged["content"]
        new_embedding = None
        if embed_fn:
            try:
                vec = embed_fn([f"{new_path}\n\n{new_content}"])
                if vec and vec[0]:
                    new_embedding = vec[0]
            except Exception as e:
                print(f"    warn: embedding failed for merged memory: {e}")

        now = datetime.now(timezone.utc).isoformat()
        with driver.session() as ses:
            # Re-parent provenance: every Session that DREAMED p1 or p2 also
            # DREAMED the merged memory. Then delete the originals.
            ses.run(
                """
                MERGE (m:Memory {path: $new_path})
                SET m.content = $new_content,
                    m.updated_at = $now,
                    m.consolidated_from = coalesce(m.consolidated_from, []) + [$p1, $p2]
                FOREACH (_ IN CASE WHEN $emb IS NOT NULL THEN [1] ELSE [] END |
                    SET m.embedding = $emb
                )
                WITH m
                MATCH (old:Memory) WHERE old.path IN [$p1, $p2] AND old.path <> $new_path
                OPTIONAL MATCH (s:Session)-[r1:DREAMED]->(old)
                FOREACH (_ IN CASE WHEN s IS NOT NULL THEN [1] ELSE [] END |
                    MERGE (s)-[:DREAMED]->(m)
                    MERGE (m)-[:DERIVED_FROM]->(s)
                )
                WITH old
                DETACH DELETE old
                """,
                parameters={
                    "new_path": new_path, "new_content": new_content, "now": now,
                    "p1": p["p1"], "p2": p["p2"], "emb": new_embedding,
                },
            )
        merges += 1
        print(f"    merged into '{new_path}'; originals removed")

    print(f"\nconsolidate: {merges} merge(s) across {rounds} round(s)")
    return merges


def archive(driver, stale_days: int, dry_run: bool = False) -> int:
    """Flag memories that haven't been read OR updated in `stale_days` days
    as archived (m.archived = true). Profile memories are exempt — they're
    foundational context that should never be archived.

    Returns the number of memories archived.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=stale_days)).isoformat()
    print(f"archive: cutoff={cutoff} dry_run={dry_run}")
    with driver.session() as ses:
        rows = list(ses.run(
            """
            MATCH (m:Memory)
            WHERE coalesce(m.archived, false) = false
              AND NOT m.path STARTS WITH 'profile/'
              AND coalesce(m.last_accessed_at, m.updated_at, '') < $cutoff
              AND coalesce(m.updated_at, '') < $cutoff
            RETURN m.path AS path,
                   m.last_accessed_at AS last_accessed,
                   m.updated_at AS updated
            ORDER BY m.path
            """,
            parameters={"cutoff": cutoff},
        ))
        if not rows:
            print("  nothing to archive")
            return 0
        for r in rows:
            print(f"  - {r['path']:<40}  last_accessed={r['last_accessed']}  updated={r['updated']}")
        if dry_run:
            print(f"  [dry-run] would archive {len(rows)} memories")
            return len(rows)
        ses.run(
            """
            UNWIND $paths AS p
            MATCH (m:Memory {path: p})
            SET m.archived = true, m.archived_at = $now
            """,
            parameters={"paths": [r["path"] for r in rows], "now": datetime.now(timezone.utc).isoformat()},
        )
    print(f"  archived {len(rows)} memories")
    return len(rows)
