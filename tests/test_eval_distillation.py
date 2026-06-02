#!/usr/bin/env python3
"""Phase D3 — distillation eval scorer (deterministic CI gate, acceptance #3).

Pins the SCORING logic with no LLM: a good candidate set passes (structure +
grounding + fact coverage all clean), and each degradation — missing fact,
off-topic (ungrounded) memory, out-of-enum kind, bad path, malformed body, empty
set — drops the right metric and fails the gate. (Generating candidates needs a
real model; that's the opt-in `njhook eval-distillation`. Scoring is deterministic
so it can guard against regressions in CI.)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "hooks"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dream"))

import eval_distillation as ed  # noqa: E402

_TRANSCRIPT = ("the user is a rust engineer; no unsafe code without approval after the "
               "use-after-free incident last sprint; deploy via staging pipeline never prod")
_EXPECTED = {"facts": [["rust", "engineer"], ["unsafe", "approval"], ["incident", "sprint"]]}


def _cand(path, kind, body):
    return {"path": path, "kind": kind, "content": f"---\ntitle: t\nkind: {kind}\n---\n\n{body}"}


def _good():
    return [
        _cand("profile/role.md", "fact", "rust engineer building backend systems"),
        _cand("project/safety.md", "constraint",
              "no unsafe code without explicit approval after the incident last sprint"),
    ]


def test_covers_helper():
    cands = _good()
    assert ed._covers(cands, ["rust", "engineer"])
    assert ed._covers(cands, ["unsafe", "approval"])
    assert not ed._covers(cands, ["kubernetes", "helm"])   # absent


def test_good_set_passes():
    r = ed.score(_good(), _TRANSCRIPT, _EXPECTED)
    assert r["valid_rate"] == 1.0 and r["path_ok_rate"] == 1.0
    assert r["kind_ok_rate"] == 1.0 and r["grounded_rate"] == 1.0
    assert r["coverage"] == 1.0 and r["pass"] is True


def test_missing_fact_drops_coverage_and_fails():
    # drop the safety memory → only the rust fact is covered
    r = ed.score([_good()[0]], _TRANSCRIPT, _EXPECTED)
    assert r["coverage"] < 0.66 and r["pass"] is False


def test_off_topic_memory_fails_grounding():
    cands = _good() + [_cand("general/x.md", "observation",
                             "quarterly revenue marketing projections fiscal roadmap")]
    r = ed.score(cands, _TRANSCRIPT, _EXPECTED)
    assert r["grounded_rate"] < 1.0 and r["pass"] is False


def test_out_of_enum_kind_fails():
    # legacy label 'profile' validates (window) but is NOT an accepted semantic kind
    cands = [_cand("profile/role.md", "profile", "rust engineer building systems"),
             _good()[1]]
    r = ed.score(cands, _TRANSCRIPT, _EXPECTED)
    assert r["kind_ok_rate"] < 1.0 and r["pass"] is False


def test_bad_path_fails():
    cands = [_cand("notes/role.md", "fact", "rust engineer"), _good()[1]]
    r = ed.score(cands, _TRANSCRIPT, _EXPECTED)
    assert r["path_ok_rate"] < 1.0 and r["pass"] is False


def test_malformed_body_fails_validity():
    cands = [{"path": "profile/role.md", "content": "no frontmatter rust engineer"}, _good()[1]]
    r = ed.score(cands, _TRANSCRIPT, _EXPECTED)
    assert r["valid_rate"] < 1.0 and r["pass"] is False


def test_empty_candidates_fail():
    r = ed.score([], _TRANSCRIPT, _EXPECTED)
    assert r["n"] == 0 and r["coverage"] == 0.0 and r["pass"] is False


def test_print_report_smoke(capsys):
    rep = {"provider": "ollama", "model": "m", "pass": True,
           "sessions": [{"name": "s1", "pass": True, "valid_rate": 1.0, "path_ok_rate": 1.0,
                         "kind_ok_rate": 1.0, "grounded_rate": 1.0, "coverage": 1.0},
                        {"name": "s2", "error": "boom", "pass": False}]}
    ed.print_report(rep)
    out = capsys.readouterr().out
    assert "PASS" in out and "s1" in out and "ERR" in out and "overall: PASS" in out
