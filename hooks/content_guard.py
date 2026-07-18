#!/usr/bin/env python3
"""Injection-artifact guard for memory bodies (memory-hygiene fix, 2026-07-18).

Root-caused corruption class: text RENDERED for injection (the `## <path>`
blocks recall emits into hook context, plus its framing markers) was written
back into a memory body by a writer outside the dream gate — the live case was
`project/njhook.md`, which accumulated four stacked `## project/njhook.md`
headers, a 3x size bloat, and regeneration drift. Once a body contains its own
rendered form, every future injection/re-distillation cycle compounds it
(photocopy-of-a-photocopy).

This module detects those artifacts so EVERY sanctioned write path (dream's
`quality.validate_memory`, `service.propose_memory`, `njhook edit`) can refuse
them at the door. Deterministic string checks — no model, no DB.
"""
from __future__ import annotations

import re

# Framing strings only ever produced by the injection renderers
# (recall.render_prompt / recall.render_session_start). A memory BODY that
# contains one of these is echoing rendered output, not recording knowledge.
INJECTION_MARKERS = (
    "# Memory (from prior sessions)",
    "# Relevant memory for this prompt",
    "_memory used:",
    "_(further memories omitted",
    "_(memory truncated",
)

# A rendered memory-block header (`## project/foo.md`) inside a body. Legit
# memory prose has no reason to reproduce another memory's render header on
# its own line; this is the exact residue the njhook.md corruption stacked up.
_RENDERED_HEADER_RE = re.compile(
    r"^## (?:profile|tools|project|general)/\S+\.md\s*$", re.MULTILINE
)


def injection_artifacts(content: str) -> list[str]:
    """Return human-readable violations found in a memory body ([] = clean)."""
    body = content or ""
    found: list[str] = []
    for marker in INJECTION_MARKERS:
        if marker in body:
            found.append(f"injection-render marker in body: {marker!r}")
    headers = _RENDERED_HEADER_RE.findall(body)
    if headers:
        found.append(
            f"rendered memory-block header(s) in body: {', '.join(sorted(set(headers))[:3])}"
            + (f" (+{len(headers) - 3} more)" if len(headers) > 3 else "")
        )
    return found
