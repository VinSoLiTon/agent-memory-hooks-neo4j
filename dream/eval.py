#!/usr/bin/env python3
"""Minimal dream-phase eval harness.

Seeds a synthetic session into Neo4j, dreams it (--dry-run, no writes), and
checks the output against a small set of structural assertions:

  - JSON is parseable on first try (no `text.find('{')` fallback needed)
  - At least MIN_MEMORIES were produced
  - Every path matches ^(profile|tools|project|general)/.+\\.md$
  - Every memory has YAML frontmatter and non-empty body
  - At least one path contains the project slug (project-discrimination check)

Usage:
    python dream/eval.py --provider ollama --model qwen3.5:latest
    python dream/eval.py --provider anthropic
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HOOK = REPO / "hooks" / "log_event.py"

# Synthetic session designed to exercise discrimination + extraction:
# - Cross-project facts (shell preference for fd over find)
# - Project-specific rule (Rust safety, with rationale)
# - User identity (Rust systems engineer at Acme)
EVAL_SESSION = [
    ("SessionStart", {"model": "test", "source": "startup"}),
    ("UserPromptSubmit", {
        "prompt": "I'm a Rust systems engineer at Acme. We use fd everywhere instead of find — much faster. Be terse."
    }),
    ("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "cargo check"}}),
    ("PostToolUse", {
        "tool_name": "Bash",
        "tool_input": {"command": "cargo check"},
        "tool_response": {"stdout": "Finished `dev` profile in 4.2s", "exit_code": 0},
    }),
    ("UserPromptSubmit", {
        "prompt": "Project rule for acme-router: NO `unsafe` blocks unless I explicitly approve. We had a UAF last sprint that took two days to track."
    }),
    ("PreToolUse", {"tool_name": "Read", "tool_input": {"file_path": "src/router.rs"}}),
    ("PostToolUse", {
        "tool_name": "Read",
        "tool_input": {"file_path": "src/router.rs"},
        "tool_response": {"content": "// router.rs\npub fn route(...)"},
    }),
    ("Stop", {}),
]

PATH_RE = re.compile(r"^(profile|tools|project|general)/[A-Za-z0-9._/-]+\.md$")

MIN_MEMORIES = 2  # minimum count to count as a pass
EXPECTED_TOPIC_KEYWORDS = ["unsafe", "rust", "fd"]  # at least one must appear in some memory


def seed_session() -> str:
    sid = f"eval-{int(time.time())}-{uuid.uuid4().hex[:6]}"
    cwd = str(REPO / "fixtures" / "acme-router")
    for event_name, extra in EVAL_SESSION:
        payload = {"session_id": sid, "hook_event_name": event_name, "cwd": cwd, **extra}
        p = subprocess.run(
            ["python", str(HOOK), "--client", "claude_code"],
            input=json.dumps(payload), capture_output=True, text=True,
        )
        if p.returncode:
            raise RuntimeError(f"seed failed: {p.stderr}")
        time.sleep(0.02)
    return sid


def cleanup(sid: str) -> None:
    """Tidy up so the eval doesn't pollute the graph."""
    from neo4j import GraphDatabase
    d = GraphDatabase.driver(
        os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("HOOKS_NEO4J_USER", "neo4j"),
              os.environ.get("HOOKS_NEO4J_PASSWORD", "password")),
    )
    with d.session() as s:
        s.run(f"MATCH (s:Session {{session_id: '{sid}'}})-[:FIRST_EVENT|NEXT*0..]->(e:Event) DETACH DELETE e")
        s.run(f"MATCH (s:Session {{session_id: '{sid}'}}) DETACH DELETE s")
    d.close()


def parse_dream_output(stdout: str) -> list[dict]:
    """Extract memory blocks from dream.py --dry-run stdout. Each block
    starts with `--- <path> ---` followed by content until the next block
    or end of stream."""
    memories = []
    blocks = re.split(r"^--- (.+?) ---$", stdout, flags=re.MULTILINE)
    # blocks = ['<prelude>', path1, body1, path2, body2, ...]
    for i in range(1, len(blocks), 2):
        path = blocks[i].strip()
        body = blocks[i + 1].strip() if i + 1 < len(blocks) else ""
        # Trim the trailing recap if present
        body = re.split(r"^\s*wrote/updated", body, flags=re.MULTILINE)[0].strip()
        memories.append({"path": path, "content": body})
    return memories


def evaluate(memories: list[dict]) -> tuple[bool, list[str]]:
    failures: list[str] = []

    if len(memories) < MIN_MEMORIES:
        failures.append(f"only {len(memories)} memories returned (need >= {MIN_MEMORIES})")

    for m in memories:
        if not PATH_RE.match(m["path"]):
            failures.append(f"path doesn't match schema: {m['path']!r}")
        body = m["content"]
        if not body.startswith("---"):
            failures.append(f"missing YAML frontmatter on {m['path']}")
        elif "title:" not in body or "kind:" not in body:
            failures.append(f"frontmatter missing title/kind on {m['path']}")
        if len(body) < 30:
            failures.append(f"body suspiciously short ({len(body)} chars) on {m['path']}")

    all_text = "\n".join(m["content"] for m in memories).lower()
    if not any(k in all_text for k in EXPECTED_TOPIC_KEYWORDS):
        failures.append(
            f"none of the expected keywords ({', '.join(EXPECTED_TOPIC_KEYWORDS)}) "
            "appear in any memory body — model missed the session's main topics"
        )

    project_paths = [m["path"] for m in memories if m["path"].startswith("project/")]
    if not project_paths:
        failures.append("no project/* memory created — model didn't produce a project-scoped memory for the Rust rule")

    return (not failures), failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["anthropic", "openai", "ollama"], required=True)
    ap.add_argument("--model", help="override default model")
    args = ap.parse_args()

    sid = seed_session()
    print(f"seeded session: {sid}")
    try:
        env = os.environ.copy()
        cmd = ["python", str(REPO / "dream" / "dream.py"),
               "--session", sid, "--dry-run", "--provider", args.provider]
        if args.model:
            cmd += ["--model", args.model]
        t0 = time.time()
        p = subprocess.run(cmd, capture_output=True, text=True, env=env)
        elapsed = time.time() - t0
        if p.returncode != 0:
            print(f"FAIL: dream.py exited {p.returncode}\nstderr:\n{p.stderr[:1000]}")
            return 1

        memories = parse_dream_output(p.stdout)
        ok, failures = evaluate(memories)
        print(f"\nelapsed: {elapsed:.1f}s")
        print(f"memories produced: {len(memories)}")
        for m in memories:
            print(f"  {m['path']}  ({len(m['content'])} chars)")
        if ok:
            print("\nPASS")
            return 0
        print("\nFAIL")
        for f in failures:
            print(f"  - {f}")
        return 1
    finally:
        cleanup(sid)


if __name__ == "__main__":
    raise SystemExit(main())
