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

# Pull in privacy.scrub_high_confidence for the secret-leak check. dream/ is
# imported with hooks/ already on sys.path (set by dream.py before this loads).
# We use the high-confidence variant (not scrub()) so the gate rejects only
# real key shapes, not config docs like `HOOKS_NEO4J_PASSWORD=password` that
# the KEY=VALUE heuristic would otherwise flag.
try:
    from privacy import scrub_high_confidence  # type: ignore
except ImportError:
    def scrub_high_confidence(s):  # type: ignore
        return s


PATH_RE = re.compile(r"^(profile|tools|project|general)/[A-Za-z0-9._/-]+\.md$")
VALID_KINDS = {"profile", "tool", "project", "general"}

MIN_BODY_CHARS = int(os.environ.get("DREAM_MEMORY_MIN_CHARS", "30"))
MAX_BODY_CHARS = int(os.environ.get("DREAM_MEMORY_MAX_CHARS", "20000"))

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_TITLE_RE = re.compile(r"^title:\s*\S", re.MULTILINE)
_KIND_RE = re.compile(r"^kind:\s*([A-Za-z]+)\s*$", re.MULTILINE)

_GROUND_TOKEN_RE = re.compile(r"[a-z0-9_]{4,}")


def grounding_score(content: str, source_text: str) -> float:
    """Phase D2 (A-MAC) — fraction of the memory body's distinctive tokens that
    appear in the source transcript. A cheap ROUGE-L-ish grounding signal: ~1.0
    when the memory is supported by the session, near 0 when it's about something
    not in the session (fabrication). Frontmatter is stripped first. Returns 1.0
    when there's no source to check against (can't judge → don't gate).

    Note: catches off-topic fabrication, NOT subtle factual errors (a wrong port
    whose tokens still appear in the transcript scores high) — it's defence in
    depth, not a correctness oracle."""
    if not source_text:
        return 1.0
    body = content or ""
    fm = _FRONTMATTER_RE.match(body)
    if fm:
        body = body[fm.end():]
    mem = set(_GROUND_TOKEN_RE.findall(body.lower()))
    if not mem:
        return 1.0
    src = set(_GROUND_TOKEN_RE.findall((source_text or "").lower()))
    return len(mem & src) / len(mem)


# --- Phase H3: anti-poisoning admission signals -----------------------------
#
# A memory layer that ingests agent output has a poisoning surface: one short or
# adversarial session can inject a durable, authoritative-sounding *rule* that
# then steers every future session. The defence is a closed-vocabulary, threshold
# gate (no LLM) over three independent signals — a candidate is quarantined to
# `pending_review` only when ALL THREE fire, so well-corroborated memories pass:
#   1. directive    — the body asserts a rule/command (closed marker set below),
#                     the dangerous-if-wrong kind. (Stands in for the typed
#                     rule/procedure/constraint kinds — D1 typed-kind vocab is
#                     deferred — so we detect directive *content* instead.)
#   2. thin source  — distilled from a session with few events → little
#                     corroboration (`source_event_count < POISON_MIN_EVENTS`).
#   3. novel        — low token overlap with existing trusted memory, so it
#                     can't be cross-checked (`novelty >= POISON_NOVELTY_MIN`).

# Closed vocabulary of directive markers (matched as lowercased substrings of the
# frontmatter-stripped body). Imperative / command shapes, deliberately narrow.
DIRECTIVE_MARKERS = frozenset({
    "always", "never", "must ", "do not", "don't",
    "delete", "remove", "drop", "disable", "enable",
    "overwrite", "the command is", "rm -", "sudo", "run ",
})

POISON_MIN_EVENTS = int(os.environ.get("DREAM_POISON_MIN_EVENTS", "5"))
POISON_NOVELTY_MIN = float(os.environ.get("DREAM_POISON_NOVELTY_MIN", "0.6"))


def _strip_frontmatter(content: str) -> str:
    body = content or ""
    fm = _FRONTMATTER_RE.match(body)
    return body[fm.end():] if fm else body


def is_directive(content: str) -> bool:
    """True if the memory body asserts a rule/command (per DIRECTIVE_MARKERS).
    Frontmatter is stripped first so a `kind:`/`title:` value can't trip it."""
    body = _strip_frontmatter(content).lower()
    return any(marker in body for marker in DIRECTIVE_MARKERS)


def novelty_score(content: str, existing_corpus: str) -> float:
    """1.0 = entirely new (no distinctive-token overlap with existing memory),
    0.0 = fully covered. Empty corpus → 1.0 (nothing to corroborate against =
    maximally novel); a body with no distinctive tokens → 0.0 (can't be novel).
    The inverse of grounding's overlap, but with the opposite empty-default: an
    uncorroborated new claim should read as novel, not as 'can't judge'."""
    mem = set(_GROUND_TOKEN_RE.findall(_strip_frontmatter(content).lower()))
    if not mem:
        return 0.0
    corpus = set(_GROUND_TOKEN_RE.findall((existing_corpus or "").lower()))
    if not corpus:
        return 1.0
    return 1.0 - len(mem & corpus) / len(mem)


def poisoning_risk(content: str, source_event_count: int, novelty: float,
                   min_events: int = POISON_MIN_EVENTS,
                   novelty_min: float = POISON_NOVELTY_MIN) -> bool:
    """True → quarantine to `pending_review`. Fires only when the candidate is a
    directive AND thinly sourced AND novel (all three). Absence of any signal →
    False: a directive that's well-corroborated, or emerges from a rich session,
    or overlaps trusted memory, is NOT held. Defence, not paranoia."""
    return (
        is_directive(content)
        and source_event_count < min_events
        and novelty >= novelty_min
    )


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

    # Defense in depth: a model can hallucinate a real secret from scrubbed
    # input. Reject only on high-confidence key shapes (sk-ant-, AKIA, JWT,
    # PEM, ...) — NOT the KEY=VALUE heuristic, which fires on legitimate
    # config documentation (e.g. `HOOKS_NEO4J_PASSWORD=password`).
    if scrub_high_confidence(content) != content:
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
