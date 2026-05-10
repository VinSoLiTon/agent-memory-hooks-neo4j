"""Pattern detection across captured sessions.

Three detectors surface signals that might be worth promoting to memories:

- repeated_commands(): Bash/shell commands run >= min_count times.
- hot_files():        files Read/Edit/Write'd >= min_count times.
- prompt_clusters():  UserPromptSubmit prompts that group semantically
                      via embedding cosine similarity (requires EMBED_PROVIDER).

Each detector returns a list of dicts so callers (CLI, dashboard, future
auto-promotion) can format however they like. Nothing in here writes to the
graph — patterns are advisory.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

# Pick up the embeddings module (lives under hooks/).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import embeddings  # noqa: E402


def _since_clause(since: str | None) -> tuple[str, dict]:
    """Convert a '24h' / '7d' / '30m' string to a Cypher WHERE fragment."""
    if not since:
        return "", {}
    m = re.fullmatch(r"(\d+)([hdm])", since)
    if not m:
        return "", {}
    n, unit = int(m.group(1)), m.group(2)
    delta = {"h": timedelta(hours=n), "d": timedelta(days=n), "m": timedelta(minutes=n)}[unit]
    cutoff = (datetime.now(timezone.utc) - delta).isoformat()
    return "AND e.timestamp >= $since", {"since": cutoff}


def _normalize_command(cmd: str) -> str:
    """Collapse whitespace and trim. Keeps the command exact otherwise so
    'pytest -x' and 'pytest -xvs' don't collide. We deliberately don't strip
    arguments — same flags are part of the pattern."""
    return " ".join(cmd.strip().split())


def repeated_commands(driver, min_count: int = 3, since: str | None = None,
                      limit: int = 20) -> list[dict]:
    extra, params = _since_clause(since)
    cypher = (
        "MATCH (e:Event {event_name: 'PreToolUse'}) "
        "WHERE e.tool_name IN ['Bash', 'BashOutput', 'run_shell_command', 'shell'] "
        "AND e.tool_input IS NOT NULL "
        f"{extra} "
        "RETURN e.tool_input AS ti, e.cwd AS cwd"
    )
    rows: dict[str, dict] = {}
    with driver.session() as s:
        for r in s.run(cypher, parameters=params):
            try:
                ti = json.loads(r["ti"]) if isinstance(r["ti"], str) else r["ti"]
            except Exception:
                continue
            cmd = ti.get("command") if isinstance(ti, dict) else None
            if not cmd or not isinstance(cmd, str):
                continue
            key = _normalize_command(cmd)
            if not key:
                continue
            entry = rows.setdefault(key, {"command": key, "count": 0, "cwds": set()})
            entry["count"] += 1
            if r["cwd"]:
                entry["cwds"].add(r["cwd"])
    matches = [
        {"command": v["command"], "count": v["count"], "cwds": sorted(v["cwds"])}
        for v in rows.values() if v["count"] >= min_count
    ]
    matches.sort(key=lambda x: x["count"], reverse=True)
    return matches[:limit]


def hot_files(driver, min_count: int = 3, since: str | None = None,
              limit: int = 20) -> list[dict]:
    extra, params = _since_clause(since)
    cypher = (
        "MATCH (e:Event {event_name: 'PreToolUse'}) "
        "WHERE e.tool_name IN ['Read', 'Edit', 'Write', 'NotebookEdit', 'MultiEdit', 'edit', 'write_file', 'replace'] "
        "AND e.tool_input IS NOT NULL "
        f"{extra} "
        "RETURN e.tool_input AS ti, e.tool_name AS tool"
    )
    rows: dict[str, dict] = {}
    with driver.session() as s:
        for r in s.run(cypher, parameters=params):
            try:
                ti = json.loads(r["ti"]) if isinstance(r["ti"], str) else r["ti"]
            except Exception:
                continue
            if not isinstance(ti, dict):
                continue
            path = ti.get("file_path") or ti.get("path") or ti.get("notebook_path")
            if not path or not isinstance(path, str):
                continue
            entry = rows.setdefault(path, {"path": path, "count": 0, "tools": {}})
            entry["count"] += 1
            entry["tools"][r["tool"]] = entry["tools"].get(r["tool"], 0) + 1
    matches = [
        {"path": v["path"], "count": v["count"], "tools": v["tools"]}
        for v in rows.values() if v["count"] >= min_count
    ]
    matches.sort(key=lambda x: x["count"], reverse=True)
    return matches[:limit]


def _cosine(a: list[float], b: list[float]) -> float:
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def prompt_clusters(driver, min_cluster_size: int = 3,
                    similarity_threshold: float = 0.8,
                    since: str | None = None, max_prompts: int = 500) -> list[dict]:
    """Greedy clustering of UserPromptSubmit prompts by embedding cosine
    similarity. Skips clusters smaller than min_cluster_size.

    Returns [{"size": int, "exemplar": str, "prompts": [str, ...]}]. Embedding
    provider must be configured (EMBED_PROVIDER).
    """
    if not embeddings.is_enabled():
        return []
    extra, params = _since_clause(since)
    cypher = (
        "MATCH (e:Event) WHERE e.event_name IN ['UserPromptSubmit', 'BeforeAgent'] "
        "AND e.prompt IS NOT NULL "
        f"{extra} "
        f"RETURN e.prompt AS prompt ORDER BY e.timestamp DESC LIMIT {int(max_prompts)}"
    )
    prompts: list[str] = []
    with driver.session() as s:
        for r in s.run(cypher, parameters=params):
            p = (r["prompt"] or "").strip()
            if len(p) >= 8:
                prompts.append(p)
    if len(prompts) < min_cluster_size:
        return []

    # Embed in one batch when possible. If batch is too large for the provider,
    # naive chunking would be a future polish.
    try:
        vecs = embeddings.embed(prompts)
    except Exception:
        return []
    if len(vecs) != len(prompts):
        return []

    # Greedy: each prompt either joins an existing cluster (if cosine > threshold
    # vs the cluster centroid — approximated by the first member) or starts a new one.
    clusters: list[dict] = []
    for prompt, vec in zip(prompts, vecs):
        placed = False
        for cl in clusters:
            if _cosine(vec, cl["seed_vec"]) > similarity_threshold:
                cl["prompts"].append(prompt)
                placed = True
                break
        if not placed:
            clusters.append({"seed_vec": vec, "prompts": [prompt]})

    out = [
        {"size": len(cl["prompts"]), "exemplar": cl["prompts"][0], "prompts": cl["prompts"]}
        for cl in clusters if len(cl["prompts"]) >= min_cluster_size
    ]
    out.sort(key=lambda c: c["size"], reverse=True)
    return out
