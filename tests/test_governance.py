#!/usr/bin/env python3
"""Phase H (PR-1) — sensitivity + egress policy. Acceptance tests.

sensitivity_for classifies by cwd; egress_blocked encodes the policy (a high-
sensitivity session never goes to a remote provider unless explicitly allowed);
log_event stamps the sensitivity on captured events. No Neo4j needed.
"""
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import privacy        # noqa: E402
import log_event       # noqa: E402
import spool           # noqa: E402
import dream as dream_mod  # noqa: E402


def test_sensitivity_for_classifies_by_cwd(tmp_path, monkeypatch):
    sub = tmp_path / "sub"
    sub.mkdir()
    monkeypatch.setenv("HOOKS_SENSITIVE_PATHS", str(tmp_path))
    assert privacy.sensitivity_for(str(tmp_path)) == "high"
    assert privacy.sensitivity_for(str(sub)) == "high"            # under a sensitive path
    assert privacy.sensitivity_for(str(tmp_path.parent)) == "normal"  # above it
    assert privacy.sensitivity_for(None) == "normal"


def test_egress_blocked_policy():
    assert dream_mod.egress_blocked("anthropic", True, False) is True    # remote + sensitive + not allowed
    assert dream_mod.egress_blocked("openai", True, False) is True
    assert dream_mod.egress_blocked("anthropic", True, True) is False    # explicitly allowed
    assert dream_mod.egress_blocked("anthropic", False, False) is False  # not sensitive
    assert dream_mod.egress_blocked("ollama", True, False) is False      # local is always fine


def test_log_event_stamps_sensitivity(tmp_path, monkeypatch):
    sens_dir = tmp_path / "secret"
    sens_dir.mkdir()
    monkeypatch.setenv("HOOKS_SENSITIVE_PATHS", str(sens_dir))
    monkeypatch.setenv("HOOKS_SPOOL_DIR", str(tmp_path / "spool"))
    monkeypatch.setattr(log_event, "CAPTURE_MODE", "spool")

    log_event.log_event(
        {"session_id": "__gov_s", "hook_event_name": "UserPromptSubmit",
         "cwd": str(sens_dir), "prompt": "secret stuff"},
        client="claude_code",
    )
    log_event.log_event(
        {"session_id": "__gov_n", "hook_event_name": "Stop", "cwd": str(tmp_path), "prompt": "normal"},
        client="claude_code",
    )

    by_sid = {r["session_id"]: r for _, _, r, _ in spool.iter_records() if r}
    assert by_sid["__gov_s"]["event_props"].get("sensitivity") == "high"
    assert "sensitivity" not in by_sid["__gov_n"]["event_props"]   # normal → not stamped
