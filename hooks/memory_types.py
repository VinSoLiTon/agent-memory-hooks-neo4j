#!/usr/bin/env python3
"""Phase D1 — typed memory `kind` vocabulary (single source of truth).

Decision (2026-06-02): `kind` IS the semantic type of a memory — replacing the
old storage-bucket labels (profile/tool/project/general), which now live only in
the PATH prefix (recall still buckets by path, so this change doesn't touch
ranking). All 15 types are ACTIVE; there is no reserved tier.

Two axes, deliberately separate:
  - PATH prefix  -> WHERE it's stored  (profile/ tools/ project/ general/) — recall buckets
  - kind         -> WHAT kind of knowledge it is  (the 15 semantic types below)

Migration window (acceptance #4): legacy memories whose `kind` is still a bucket
label (profile/tool/project/general) remain VALID until re-tagged. `VALID_KINDS`
= the 15 semantic types ∪ the 4 legacy labels; `normalize_kind()` maps a legacy
label to its semantic equivalent so the queryable node property is always semantic.
"""
from __future__ import annotations

import re

# The semantic type vocabulary — all 15 ACTIVE (no reserved tier).
MEMORY_KINDS = frozenset({
    "preference", "projectrule", "decision", "procedure", "fact",
    "constraint", "toolpattern", "incident", "openquestion",
    "commitment", "goal", "context", "learning", "observation", "artifact",
})

# Old storage-bucket labels, accepted during the migration window only.
LEGACY_KINDS = frozenset({"profile", "tool", "project", "general"})

# Accepted by the quality gate during the window (semantic ∪ legacy).
VALID_KINDS = MEMORY_KINDS | LEGACY_KINDS

# Legacy bucket label -> semantic type, for normalizing the queryable property.
LEGACY_KIND_MAP = {
    "profile": "preference",
    "tool": "toolpattern",
    "project": "projectrule",
    "general": "context",
}

# Fallback when a memory has no parseable kind (validation should prevent this).
DEFAULT_KIND = "context"

_KIND_RE = re.compile(r"^kind:\s*([A-Za-z]+)\s*$", re.MULTILINE)


def is_valid_kind(kind: str | None) -> bool:
    """True if `kind` is in the accepted vocabulary (semantic or legacy window)."""
    return isinstance(kind, str) and kind.lower() in VALID_KINDS


def normalize_kind(kind: str | None) -> str:
    """Map a kind to its canonical SEMANTIC type: legacy labels are translated,
    valid semantic types pass through, anything unknown falls back to DEFAULT_KIND."""
    k = (kind or "").lower()
    if k in LEGACY_KIND_MAP:
        return LEGACY_KIND_MAP[k]
    if k in MEMORY_KINDS:
        return k
    return DEFAULT_KIND


def parse_kind(content: str | None) -> str | None:
    """Extract the `kind` value from a memory body's YAML frontmatter, or None."""
    m = _KIND_RE.search(content or "")
    return m.group(1).lower() if m else None
