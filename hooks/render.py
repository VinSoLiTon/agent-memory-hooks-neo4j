#!/usr/bin/env python3
"""Phase G (PR-3) — file renderers.

Render the project's memory into the static context file each agent runtime reads
at startup (AGENTS.md, CLAUDE.md, GEMINI.md, Cursor rules) so a runtime that ISN'T
hook-capable still gets memory — the "attach any LLM" half of the north star.

Memory is written into a delimited *managed block* (BEGIN/END markers); everything
outside the block is human-authored and is never touched. Re-rendering replaces
only the block, so it's idempotent and safe to run on a file you also hand-edit.
Content comes from the SAME recall core (`session_start_buckets` + render) the
hook injects, so the rendered file and the live hook can't disagree.
"""
from __future__ import annotations

from pathlib import Path

import recall
from project import derive_project

BEGIN = "<!-- BEGIN njhook memory (managed — edits inside this block are overwritten) -->"
END = "<!-- END njhook memory -->"

# Closed vocabulary of render targets. `path` is relative to the chosen root;
# `preamble` seeds a brand-new file (Cursor `.mdc` rules need YAML frontmatter at
# the very top — kept OUTSIDE the managed block so re-renders never disturb it).
RENDER_TARGETS = {
    "agents": {"path": "AGENTS.md", "preamble": ""},
    "claude": {"path": "CLAUDE.md", "preamble": ""},
    "gemini": {"path": "GEMINI.md", "preamble": ""},
    "cursor": {"path": ".cursor/rules/njhook-memory.mdc",
               "preamble": "---\ndescription: Project memory distilled by njhook\nalwaysApply: true\n---\n\n"},
}


def _require_target(target: str) -> dict:
    if target not in RENDER_TARGETS:
        raise ValueError(f"unknown render target {target!r}; choices: {sorted(RENDER_TARGETS)}")
    return RENDER_TARGETS[target]


def target_path(target: str, root: str) -> Path:
    return Path(root) / _require_target(target)["path"]


def memory_markdown(session, cwd: str | None = None,
                    char_budget: int = recall.CHAR_BUDGET) -> str:
    """The session-start memory injection (profile + tools + project buckets,
    budget-rendered) as markdown — identical to what the hook emits."""
    project = derive_project(cwd) if cwd else None
    buckets = recall.session_start_buckets(session, project)
    md, _paths = recall.render_session_start(buckets, project, char_budget=char_budget)
    return md


def build_block(memory_md: str) -> str:
    """Wrap rendered memory markdown in the managed-block markers."""
    body = (memory_md or "").strip() or "_(no memories distilled yet)_"
    return f"{BEGIN}\n{body}\n{END}"


def splice(existing: str, block: str) -> str:
    """Return `existing` with its managed block replaced by `block`, preserving
    every byte outside the markers. If the markers aren't present, append the
    block after the existing content. Idempotent: splice(splice(x)) == splice(x).
    A truncated file with BEGIN but no END is treated as marker-less (appended),
    so a half-written file can't swallow the rest of the document."""
    if BEGIN in existing and END in existing and existing.index(BEGIN) < existing.index(END):
        pre = existing.split(BEGIN, 1)[0]
        post = existing.split(END, 1)[1]
        return f"{pre}{block}{post}"
    if not existing:
        return f"{block}\n"
    return f"{existing.rstrip(chr(10))}\n\n{block}\n"


def proposed_text(session, target: str, root: str, cwd: str | None = None,
                  char_budget: int = recall.CHAR_BUDGET) -> tuple[str, bool]:
    """Compute the file contents render WOULD write, without writing. Returns
    (text, existed). Used for both `--stdout` preview and the writer below."""
    spec = _require_target(target)
    out = target_path(target, root)
    existed = out.exists()
    existing = out.read_text(encoding="utf-8") if existed else spec["preamble"]
    block = build_block(memory_markdown(session, cwd=cwd or root, char_budget=char_budget))
    return splice(existing, block), existed


def render_target(session, target: str, root: str, cwd: str | None = None,
                  char_budget: int = recall.CHAR_BUDGET) -> dict:
    """Write the managed block into the target file. Returns
    {target, path, action} where action ∈ {created, updated, unchanged}."""
    out = target_path(target, root)
    new_text, existed = proposed_text(session, target, root, cwd=cwd, char_budget=char_budget)
    if not existed:
        action = "created"
    else:
        action = "unchanged" if out.read_text(encoding="utf-8") == new_text else "updated"
    if action != "unchanged":
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(new_text, encoding="utf-8")
    return {"target": target, "path": str(out), "action": action}
