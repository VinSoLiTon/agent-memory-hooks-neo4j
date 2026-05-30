"""Privacy filters for the capture path.

Two responsibilities:

1. **CWD opt-out** — sessions whose `cwd` matches the user-configured blocklist
   never get logged. Configure via either:
     - Env var HOOKS_OPT_OUT_PATHS (semicolon-separated absolute paths;
       semicolon avoids the colon-in-path ambiguity on Windows)
     - File ~/.njhook/optout.txt (one path per line, '#' comments OK)
   Match is case-insensitive prefix match on a normalized form of cwd.

2. **Secret scrubbing** — replace high-confidence secret patterns in any
   captured string (prompt, tool_input, tool_response, transcript) before
   the event is written to Neo4j. Set HOOKS_DISABLE_SCRUB=1 to bypass
   (useful for tests; do not use in production).

Both stages are best-effort: an exception in either path must never block
event capture. Callers should treat these as advisory.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

OPTOUT_FILE = Path.home() / ".njhook" / "optout.txt"


def _normalize(p: str) -> str:
    # Resolve to an absolute, case-folded, forward-slashed string so we can do
    # a stable prefix compare across Windows / POSIX.
    try:
        return str(Path(p).expanduser().resolve()).replace("\\", "/").lower()
    except Exception:
        return p.replace("\\", "/").lower()


def _load_blocklist() -> list[str]:
    paths: list[str] = []
    env = os.environ.get("HOOKS_OPT_OUT_PATHS", "")
    if env:
        paths.extend(s.strip() for s in env.split(";") if s.strip())
    if OPTOUT_FILE.exists():
        try:
            for line in OPTOUT_FILE.read_text(encoding="utf-8").splitlines():
                line = line.split("#", 1)[0].strip()
                if line:
                    paths.append(line)
        except Exception:
            pass
    return [_normalize(p) for p in paths]


def is_optout(cwd: str | None) -> bool:
    """Return True if `cwd` (or any parent) is in the blocklist."""
    if not cwd:
        return False
    norm = _normalize(cwd)
    for bad in _load_blocklist():
        if not bad:
            continue
        if norm == bad or norm.startswith(bad.rstrip("/") + "/"):
            return True
    return False


# --- Secret scrubbing ----------------------------------------------------

# Each entry: (compiled regex, replacement). Replacement may use \g<name> back-refs.
# Order matters — more specific patterns first so general ones don't pre-empt.
# High-confidence patterns: distinctive shapes that are almost certainly a
# real secret wherever they appear. Safe to treat a match as a hard signal
# (e.g. to REJECT a generated memory — see quality.py).
_HIGH_CONFIDENCE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic API keys (sk-ant-api...)
    (re.compile(r"sk-ant-(?:api\d+-|admin-)[A-Za-z0-9_\-]{20,}"), "<REDACTED:anthropic_key>"),
    # OpenAI / generic sk-... keys (after Anthropic so the more specific one wins)
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_\-]{20,}"), "<REDACTED:api_key>"),
    # GitHub tokens (classic, fine-grained, OAuth, server-to-server, refresh)
    (re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{30,}"), "<REDACTED:github_token>"),
    # AWS access key IDs and secret access keys
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "<REDACTED:aws_access_key_id>"),
    (re.compile(r"(?i)(aws_secret_access_key\s*[:=]\s*)['\"]?[A-Za-z0-9/+=]{30,}['\"]?"), r"\1<REDACTED:aws_secret>"),
    # Slack
    (re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}"), "<REDACTED:slack_token>"),
    # Stripe
    (re.compile(r"\b(sk|pk|rk)_(live|test)_[A-Za-z0-9]{20,}"), "<REDACTED:stripe_key>"),
    # JWT (header.payload.signature, base64url segments)
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "<REDACTED:jwt>"),
    # PEM-style private key blocks
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"),
        "<REDACTED:private_key>",
    ),
]

# Heuristic patterns: useful for scrubbing captured events, but too eager to
# use as a hard reject signal — they match ordinary documentation such as
# `HOOKS_NEO4J_PASSWORD=password` or `Authorization: Bearer <token>`. Applied
# by scrub() (capture path) but NOT by scrub_high_confidence().
_HEURISTIC_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # HTTP Bearer tokens (when something like 'Authorization: Bearer xyz' shows up)
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9_.~+/=\-]{20,}"), "Bearer <REDACTED>"),
    # .env-style KEY=VALUE for sensitive-looking names. Keep the name visible
    # but redact the value so debugging context isn't lost.
    (
        re.compile(
            r"(?im)\b(?P<k>(?:[A-Z0-9_]+_)?(?:API[_-]?KEY|SECRET(?:_KEY)?|TOKEN|PASSWORD|PASSWD|PWD|PRIVATE[_-]?KEY))\b"
            r"\s*[:=]\s*['\"]?(?P<v>[^'\"\s\n,;]{6,})['\"]?"
        ),
        r"\g<k>=<REDACTED>",
    ),
]

# Order matters — high-confidence (more specific) first so general ones don't pre-empt.
_PATTERNS: list[tuple[re.Pattern[str], str]] = _HIGH_CONFIDENCE_PATTERNS + _HEURISTIC_PATTERNS


def scrub(text):
    """Apply all secret patterns to `text`. Returns text with redactions.

    Accepts non-string inputs (returned untouched). Errors are swallowed —
    privacy is best-effort, never block the capture.
    """
    if os.environ.get("HOOKS_DISABLE_SCRUB") == "1":
        return text
    if not isinstance(text, str) or not text:
        return text
    try:
        out = text
        for pat, repl in _PATTERNS:
            out = pat.sub(repl, out)
        return out
    except Exception:
        return text


def scrub_high_confidence(text):
    """Like scrub(), but only the high-confidence secret shapes — not the
    KEY=VALUE / Bearer heuristics that also match ordinary documentation.

    Used by the dream-phase quality gate to decide whether a generated memory
    leaked a real secret, without rejecting legitimate config docs like
    `HOOKS_NEO4J_PASSWORD=password`.
    """
    if os.environ.get("HOOKS_DISABLE_SCRUB") == "1":
        return text
    if not isinstance(text, str) or not text:
        return text
    try:
        out = text
        for pat, repl in _HIGH_CONFIDENCE_PATTERNS:
            out = pat.sub(repl, out)
        return out
    except Exception:
        return text
