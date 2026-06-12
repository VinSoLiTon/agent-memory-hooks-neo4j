#!/usr/bin/env python3
"""Lean output contract: the model emits `content` as the prose BODY ONLY plus
top-level title/kind; the YAML frontmatter is synthesized at write-time. Saves the
~30-40 boilerplate tokens/memory that double-encoded `kind` and pushed big-session
output past the slot (truncation → defer). The STORED format is unchanged. Pure
(no Neo4j / no LLM)."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as d        # noqa: E402
import prompts           # noqa: E402
import quality           # noqa: E402
import memory_types as mt  # noqa: E402


# --- synthesis ---------------------------------------------------------------

def test_body_only_gets_frontmatter_synthesized():
    mem = {"path": "profile/role.md", "title": "User role", "kind": "preference",
           "content": "Rust systems engineer.", "importance": 9}
    out = d._normalize_memory_content(mem)
    assert out["content"] == "---\ntitle: User role\nkind: preference\n---\n\nRust systems engineer."
    # the synthesized frontmatter is what the rest of the pipeline reads
    assert mt.parse_kind(out["content"]) == "preference"
    assert quality.validate_memory(out) == []          # passes the quality gate


def test_idempotent_passthrough_when_frontmatter_present():
    body = "---\ntitle: X\nkind: fact\n---\n\nalready wrapped"
    mem = {"path": "general/x.md", "kind": "fact", "content": body, "importance": 5}
    out = d._normalize_memory_content(mem)
    assert out["content"] == body                       # untouched, no double-wrap


def test_title_derived_from_path_when_missing():
    mem = {"path": "tools/bash/grep-flags.md", "kind": "toolpattern",
           "content": "use -n for line numbers", "importance": 4}
    out = d._normalize_memory_content(mem)
    assert "title: Grep flags" in out["content"]
    assert "kind: toolpattern" in out["content"]


def test_kind_defaults_when_missing():
    out = d._normalize_memory_content({"path": "general/x.md", "content": "note"})
    assert f"kind: {mt.DEFAULT_KIND}" in out["content"]


def test_title_sanitized_so_frontmatter_stays_well_formed():
    out = d._normalize_memory_content(
        {"path": "general/x.md", "title": "bad: title\nwith newline", "kind": "fact", "content": "b"})
    fm = out["content"].split("\n\n", 1)[0]
    assert fm.count("\n") == 3                          # ---, title, kind, --- = 3 internal newlines
    assert "title:" in fm and "\nwith newline" not in out["content"].split("body", 1)[0]
    # still parses to a valid memory
    assert mt.parse_kind(out["content"]) == "fact"
    assert quality.validate_memory(out) == []


def test_title_from_path_helper():
    assert d._title_from_path("tools/bash/grep-flags.md") == "Grep flags"
    assert d._title_from_path("profile/role.md") == "Role"
    assert d._title_from_path("") == "Memory"


# --- call_provider applies it at the chokepoint ------------------------------

def test_call_provider_normalizes_each_memory():
    # stub provider returns body-only memories (the new contract)
    def stub(**kw):
        return [{"path": "general/a.md", "title": "A", "kind": "fact", "content": "body a", "importance": 6},
                {"path": "general/b.md", "title": "B", "kind": "learning", "content": "body b", "importance": 5}]
    mems = d.call_provider(stub, "transcript", "", "m", "sys")
    assert all(m["content"].startswith("---\ntitle:") for m in mems)
    assert mt.parse_kind(mems[0]["content"]) == "fact"
    assert "body a" in mems[0]["content"]


def test_call_provider_drops_non_dicts_and_handles_none():
    def stub(**kw):
        return [{"path": "general/a.md", "kind": "fact", "content": "x", "importance": 6}, None, "junk"]
    mems = d.call_provider(stub, "t", "", "m", "s")
    assert len(mems) == 1 and mems[0]["content"].startswith("---")


# --- the prompt contract actually changed ------------------------------------

def test_prompt_asks_for_body_only_and_schema_requires_title():
    fp = prompts.system_prompt_for("anthropic")
    assert "BODY ONLY" in fp                              # instruction present
    assert "title" in prompts.DREAM_JSON_SCHEMA["properties"]["memories"]["items"]["properties"]
    assert "title" in prompts.DREAM_JSON_SCHEMA["properties"]["memories"]["items"]["required"]
