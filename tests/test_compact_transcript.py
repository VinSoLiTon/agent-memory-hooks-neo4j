#!/usr/bin/env python3
"""Compact "dream language" (DREAM_COMPACT_TRANSCRIPT=1) — denser event encoding.

Merges a PreToolUse+PostToolUse pair into one `tool(input) -> output` line, drops
the 32-char ISO timestamp for a short index, and shrinks event names — so more
events fit the transcript budget. Default-off; identical to the verbose render
otherwise. Pure (no Neo4j / no LLM)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as d  # noqa: E402


def _tool_pair(i, tool="Bash", cmd="ls -la /tmp", out="total 12\nfoo bar", tuid="u1"):
    pre = {"event_id": f"e{i}a", "event_name": "PreToolUse", "tool_name": tool,
           "tool_use_id": tuid, "tool_input": f'{{"command": "{cmd}"}}',
           "timestamp": f"2026-06-01T00:00:0{i}+00:00"}
    post = {"event_id": f"e{i}b", "event_name": "PostToolUse", "tool_name": tool,
            "tool_use_id": tuid, "tool_response": out,
            "timestamp": f"2026-06-01T00:00:0{i}+00:00"}
    return pre, post


def _prompt(i, text="implement the dream encoder"):
    return {"event_id": f"p{i}", "event_name": "UserPromptSubmit", "prompt": text,
            "timestamp": f"2026-06-01T00:01:0{i}+00:00"}


# --- merge -------------------------------------------------------------------

def test_merge_collapses_pre_post_pair():
    pre, post = _tool_pair(1)
    merged = d._merge_tool_pairs([pre, post])
    assert len(merged) == 1
    assert merged[0]["tool_name"] == "Bash"
    assert merged[0]["tool_input"] == pre["tool_input"]
    assert merged[0]["tool_response"] == post["tool_response"]


def test_merge_leaves_unpaired_events_alone():
    pre, post = _tool_pair(1)
    p = _prompt(1)
    # prompt between pre and post → not a clean pair, no merge
    merged = d._merge_tool_pairs([pre, p, post])
    assert len(merged) == 3
    # mismatched tool_use_id → not merged
    pre2, post2 = _tool_pair(2, tuid="u2")
    post2["tool_use_id"] = "DIFFERENT"
    assert len(d._merge_tool_pairs([pre2, post2])) == 2


# --- render ------------------------------------------------------------------

def test_compact_render_one_line_per_kind():
    assert d._render_one_compact(_prompt(3, "do X"), 3) == "#3 > do X"
    pre, post = _tool_pair(4, tool="Bash", cmd="pytest -q", out="5 passed")
    merged = d._merge_tool_pairs([pre, post])[0]
    line = d._render_one_compact(merged, 4)
    assert line == "#4 Bash(pytest -q) -> 5 passed"
    assert "\n" not in line                       # one line per event
    assert "2026-" not in line                    # no ISO timestamp


def test_key_tool_input_shared_helper():
    assert d._key_tool_input('{"command": "git status"}') == "git status"
    assert d._key_tool_input('{"file_path": "/a/b.py"}') == "/a/b.py"
    assert d._key_tool_input("") == ""
    assert d._key_tool_input("x" * 500) == "x" * 200       # capped


# --- density + flag dispatch -------------------------------------------------

def _session(n_tools=20):
    evs = [_prompt(0, "start the task about port 5000 and dashboards")]
    for i in range(1, n_tools + 1):
        pre, post = _tool_pair(i, cmd=f"command number {i} --flag", out=f"output line {i} ok")
        evs += [pre, post]
    return evs


def test_compact_is_denser_than_verbose(monkeypatch):
    evs = _session(20)
    monkeypatch.delenv("DREAM_COMPACT_TRANSCRIPT", raising=False)
    verbose = d.render_events(evs, max_chars=None)
    monkeypatch.setenv("DREAM_COMPACT_TRANSCRIPT", "1")
    compact = d.render_events(evs, max_chars=None)
    assert len(compact) < len(verbose)
    # tool-heavy session: expect a solid reduction (merged pairs + no timestamps)
    assert len(compact) <= 0.65 * len(verbose), \
        f"compact {len(compact)} vs verbose {len(verbose)} — under 35% reduction"


def test_flag_off_is_unchanged(monkeypatch):
    evs = _session(5)
    monkeypatch.delenv("DREAM_COMPACT_TRANSCRIPT", raising=False)
    a = d.render_events(evs, max_chars=None)
    assert "[2026-" in a and "PreToolUse" in a       # verbose scaffolding intact


def test_compact_keeps_signal_first_slicing(monkeypatch):
    monkeypatch.setenv("DREAM_COMPACT_TRANSCRIPT", "1")
    evs = _session(40)
    # tiny budget — the prompt (intent) must survive, with a dropped-events note
    out = d.render_events(evs, max_chars=200)
    assert "start the task" in out                    # the prompt is kept first
    assert "omitted to fit" in out                    # truncation noted


def test_compact_more_events_fit_same_budget(monkeypatch):
    evs = _session(60)
    budget = 1500
    monkeypatch.delenv("DREAM_COMPACT_TRANSCRIPT", raising=False)
    v = d.render_events(evs, max_chars=budget)
    monkeypatch.setenv("DREAM_COMPACT_TRANSCRIPT", "1")
    c = d.render_events(evs, max_chars=budget)
    # same budget, compact fits more tool lines (count "->" output markers / "command")
    assert c.count("command number") > v.count("input:"), \
        "compact should fit more events in the same char budget"
