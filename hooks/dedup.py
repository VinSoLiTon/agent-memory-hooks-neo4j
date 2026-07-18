#!/usr/bin/env python3
"""Per-delivery hook idempotency (double-registration fix, 2026-07-18).

The same hooks can legitimately be registered in MORE than one place — this
machine has them in both `~/.claude/settings.json` (global, powers
cross-project capture) and the repo's `.claude/settings.json` (shipped glue).
Claude Code then delivers every hook dispatch to BOTH registrations: doubled
injection context on every prompt and duplicate `:Event` rows at identical
timestamps (verified live).

Rather than policing registration topology (fragile — the bug returns with any
config change, and removing repo-level entries breaks clone-and-it-works),
each delivery is deduplicated here: a hash of the RAW stdin payload is claimed
atomically in a temp-dir marker; a byte-identical payload arriving within a
short window is the same dispatch delivered again and is dropped. A genuine
repeat (same prompt resubmitted later, same tool re-run later) arrives after
the window and passes.

Fail-open by design: any error — temp dir unwritable, race on a stale marker —
means "not a duplicate", because a lost dedup duplicates one injection/event
(today's status quo) while a false positive would silently drop capture, which
the spool/DLQ discipline forbids.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
import time

# Seconds within which a byte-identical payload counts as the same delivery.
# Double-registration deliveries land within the same event dispatch
# (milliseconds apart); 30s is generous for that and short enough that a
# genuinely resubmitted identical prompt still gets its injection. 0 disables.
WINDOW_SECS = float(os.environ.get("HOOKS_DEDUP_WINDOW_SECS", "30"))

_DIR_NAME = "njhook-hook-dedup"


def _marker_dir() -> str:
    return os.path.join(tempfile.gettempdir(), _DIR_NAME)


def _prune(root: str, now: float) -> None:
    """Opportunistic, bounded cleanup so the marker dir can't grow unbounded."""
    try:
        for name in os.listdir(root)[:200]:
            p = os.path.join(root, name)
            if now - os.path.getmtime(p) > max(WINDOW_SECS * 10, 300):
                os.unlink(p)
    except OSError:
        pass


def duplicate_delivery(raw_stdin: str, client: str) -> bool:
    """True if a byte-identical hook payload for `client` was already handled
    within WINDOW_SECS — i.e. the same dispatch delivered via a second hook
    registration. First delivery claims the marker atomically (O_CREAT|O_EXCL)
    and returns False."""
    if WINDOW_SECS <= 0 or not raw_stdin:
        return False
    try:
        key = hashlib.sha256(f"{client}\n{raw_stdin}".encode("utf-8")).hexdigest()[:32]
        root = _marker_dir()
        os.makedirs(root, exist_ok=True)
        now = time.time()
        _prune(root, now)
        marker = os.path.join(root, key)
        try:
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return False                       # won the claim — first delivery
        except FileExistsError:
            if now - os.path.getmtime(marker) <= WINDOW_SECS:
                return True                    # duplicate within the window
            os.utime(marker, None)             # stale marker → fresh delivery
            return False
    except Exception:
        return False                           # fail-open: never block capture/recall
