"""Quality gates for dream-phase memory output.

Validates each memory dict before it lands in Neo4j so a malformed model
response can't corrupt the graph for future sessions.

Checks:
  - path matches ^(profile|tools|project|general)/.+\\.md$
  - body starts with `---` frontmatter
  - frontmatter contains `title:` and a valid `kind:` field
  - body length is between MIN and MAX chars
  - body, when re-scrubbed via privacy.scrub(), is unchanged (i.e. doesn't
    contain a freshly-pasted secret the capture filter missed because the
    LLM regenerated it from context)

Returns a list of human-readable error strings; empty list = valid.
A separate `validate_batch()` filters and logs in one call so callers
don't have to write the same boilerplate.
"""
from __future__ import annotations

import os
import re
import sys
from typing import Iterable

# Pull in privacy.scrub for the secret-leak check. dream/ is imported with
# hooks/ already on sys.path (set by dream.py before this module loads).
try:
    from privacy import scrub  # type: ignore
except ImportError:
    def scrub(s):  # type: ignore
        return s


PATH_RE = re.compile(r"^(profile|tools|project|general)/[A-Za-z0-9._/-]+\.md$")
VALID_KINDS = {"profile", "tool", "project", "general"}

MIN_BODY_CHARS = int(os.environ.get("DREAM_MEMORY_MIN_CHARS", "30"))
MAX_BODY_CHARS = int(os.environ.get("DREAM_MEMORY_MAX_CHARS", "20000"))

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"^title:\s*\S", re.MULTILINE)
_KIND_RE = re.compile(r"^kind:\s*([A-Za-z]+)\s*$", re.MULTILINE)


def validate_memory(memory: dict) -> list[str]:
    """Return a list of error messages. Empty list means the memory is valid."""
    errors: list[str] = []

    path = memory.get("path")
    content = memory.get("content")

    if not isinstance(path, str) or not path:
        errors.append("missing path")
        return errors  # nothing else to validate without a path
    if not PATH_RE.match(path):
        errors.append(f"path doesn't match schema (^(profile|tools|project|general)/...\\.md$): {path!r}")

    if not isinstance(content, str) or not content:
        errors.append("missing content")
        return errors

    if len(content) < MIN_BODY_CHARS:
        errors.append(f"body too short ({len(content)} < {MIN_BODY_CHARS} chars)")
    if len(content) > MAX_BODY_CHARS:
        errors.append(f"body too long ({len(content)} > {MAX_BODY_CHARS} chars)")

    fm_match = _FRONTMATTER_RE.match(content)
    if not fm_match:
        errors.append("missing YAML frontmatter (must start with `---\\n...\\n---\\n`)")
    else:
        fm = fm_match.group(1)
        if not _TITLE_RE.search(fm):
            errors.append("frontmatter missing required `title:` field")
        kind_match = _KIND_RE.search(fm)
        if not kind_match:
            errors.append("frontmatter missing required `kind:` field")
        elif kind_match.group(1).lower() not in VALID_KINDS:
            errors.append(
                f"frontmatter `kind: {kind_match.group(1)}` invalid "
                f"(allowed: {sorted(VALID_KINDS)})"
            )

    # Defense in depth: a model can hallucinate a secret-shaped string from
    # scrubbed input. Scrub the body and reject if anything matched.
    scrubbed = scrub(content)
    if scrubbed != content:
        errors.append("body contains a secret-shaped string the dream phase generated; rejecting")

    return errors


def validate_batch(memories: Iterable[dict]) -> list[dict]:
    """Filter a list of memories to only the valid ones, logging each
    rejection to stderr. Caller writes only the returned list to Neo4j."""
    valid: list[dict] = []
    for i, m in enumerate(memories):
        errs = validate_memory(m)
        if errs:
            label = m.get("path") or f"memory[{i}]"
            for e in errs:
                print(f"  rejected {label}: {e}", file=sys.stderr)
            continue
        valid.append(m)
    return valid
