#!/usr/bin/env python3
"""Per-delivery hook idempotency (fix/hook-dedup).

The same hooks registered in BOTH ~/.claude/settings.json and the repo's
.claude/settings.json made Claude Code deliver every dispatch twice: doubled
injection context per prompt and duplicate :Event rows at identical
timestamps. dedup.duplicate_delivery claims a hash of the raw stdin payload
atomically; a byte-identical payload within the window is the same dispatch
delivered again. Fail-open: an error must never suppress capture/recall.

Pure / static — no Neo4j.
"""
import os
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import dedup  # noqa: E402

PAYLOAD = '{"hook_event_name":"UserPromptSubmit","session_id":"s1","prompt":"hello"}'


@pytest.fixture(autouse=True)
def isolated_marker_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(dedup, "_marker_dir", lambda: str(tmp_path / "dedup"))
    monkeypatch.setattr(dedup, "WINDOW_SECS", 30.0)


def test_first_delivery_passes_second_is_duplicate():
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is True


def test_different_payloads_both_pass():
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
    assert dedup.duplicate_delivery(PAYLOAD.replace("hello", "world"), "claude_code") is False


def test_different_clients_are_independent():
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
    assert dedup.duplicate_delivery(PAYLOAD, "codex") is False


def test_stale_marker_counts_as_fresh_delivery(monkeypatch):
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
    # age the marker past the window: a genuine identical resubmission later
    # must still be handled (only same-dispatch double delivery is dropped)
    root = dedup._marker_dir()
    marker = os.path.join(root, os.listdir(root)[0])
    old = time.time() - 3600
    os.utime(marker, (old, old))
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False


def test_window_zero_disables(monkeypatch):
    monkeypatch.setattr(dedup, "WINDOW_SECS", 0.0)
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False


def test_empty_payload_never_deduped():
    assert dedup.duplicate_delivery("", "claude_code") is False
    assert dedup.duplicate_delivery("", "claude_code") is False


def test_fail_open_on_unwritable_dir(monkeypatch):
    # dedup errors must NEVER suppress capture/recall — fail-open to "not dup"
    monkeypatch.setattr(dedup, "_marker_dir", lambda: "\x00invalid\x00path")
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
    assert dedup.duplicate_delivery(PAYLOAD, "claude_code") is False
