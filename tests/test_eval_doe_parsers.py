#!/usr/bin/env python3
"""Pure parser tests for the distillation DOE harness (dream/eval_doe.py). The model
calls are manual; the JSON/delim parsers are deterministic and unit-tested here —
parse robustness is the whole point of the output-format factor."""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import eval_doe as doe   # noqa: E402
import memory_types as mt  # noqa: E402


def test_parse_json_clean():
    txt = '{"memories":[{"path":"profile/role.md","title":"Role","kind":"fact","content":"Rust dev.","importance":8}]}'
    mems = doe.parse_json_output(txt)
    assert len(mems) == 1
    assert mems[0]["content"].startswith("---\ntitle: Role")   # frontmatter synthesized
    assert mt.parse_kind(mems[0]["content"]) == "fact"


def test_parse_json_tolerates_fences_and_prose():
    txt = 'Here you go:\n```json\n{"memories":[{"path":"general/x.md","kind":"context","content":"note","importance":3}]}\n```\n'
    mems = doe.parse_json_output(txt)
    assert len(mems) == 1 and "note" in mems[0]["content"]


def test_parse_json_empty_memories():
    assert doe.parse_json_output('{"memories":[]}') == []


def test_parse_delim_clean():
    txt = ("@@ profile/role.md | preference | 8 | User role\n"
           "Prefers async/await.\n2-space indent.\n@@end\n"
           "@@ project/db.md | decision | 7 | DB choice\n"
           "Chose Postgres.\n@@end")
    mems = doe.parse_delim_output(txt)
    assert len(mems) == 2
    assert mt.parse_kind(mems[0]["content"]) == "preference"
    assert "async/await" in mems[0]["content"] and "2-space indent" in mems[0]["content"]
    assert mems[1]["path"] == "project/db.md" and "Postgres" in mems[1]["content"]


def test_parse_delim_missing_end_marker_closes_at_next_header():
    txt = ("@@ a/x.md | fact | 5 | X\nbody one\n"        # no @@end
           "@@ a/y.md | fact | 6 | Y\nbody two\n")
    mems = doe.parse_delim_output(txt)
    assert len(mems) == 2
    assert "body one" in mems[0]["content"] and "body two" in mems[1]["content"]


def test_parse_delim_skips_malformed_header_and_prose():
    txt = ("Sure, here are the memories:\n"
           "@@ malformed header without pipes\n"
           "@@ general/ok.md | learning | 4 | OK\nthe body\n@@end")
    mems = doe.parse_delim_output(txt)
    assert len(mems) == 1 and "the body" in mems[0]["content"]


def test_both_prompts_share_base_rules_differ_only_in_output():
    j = doe.system_prompt("json")
    d = doe.system_prompt("delim")
    assert doe._BASE_RULES in j and doe._BASE_RULES in d   # shared preamble
    assert "STRICT JSON" in j and "STRICT JSON" not in d
    assert "@@end" in d and "@@end" not in j


def test_prompt_variants_share_preamble_differ_in_framing():
    cur = doe.system_prompt("json", "current")
    cov = doe.system_prompt("json", "coverage")
    st = doe.system_prompt("json", "structured")
    for p in (cur, cov, st):
        assert doe._PREAMBLE in p                          # kind vocab held constant
    assert "FEWER, SHARPER" in cur and "FEWER, SHARPER" not in cov
    assert "COMPLETENESS" in cov and "COMPLETENESS" not in cur
    assert "two steps" in st
    assert cur != cov and cov != st
    # unknown variant falls back to current
    assert doe.system_prompt("json", "nope") == cur
