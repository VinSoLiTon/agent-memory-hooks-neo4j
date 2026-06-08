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


# --- item #11: anti-noise / count-discipline metric -------------------------

def test_noise_metric_present_and_clean_for_useful_set():
    r = ed.score(_good(), _TRANSCRIPT, _EXPECTED)
    assert "noise" in r and "precision" in r and "negative_hits" in r
    assert r["noise"] == 0.0 and r["precision"] == 1.0   # both candidates cover a fact
    assert r["pass"] is True


def test_padding_inflates_noise_and_fails():
    # 2 useful + 3 valid, grounded, on-topic-but-fact-IRRELEVANT memories (padding).
    # the OLD scorer would pass (grounded=1.0, coverage=1.0); the noise gate fails it.
    padding = [
        _cand("project/p1.md", "context", "deploy via staging pipeline never prod"),
        _cand("project/p2.md", "context", "the user prefers terse descriptions on deploy"),
        _cand("project/p3.md", "context", "staging pipeline is used for deploy steps"),
    ]
    r = ed.score(_good() + padding, _TRANSCRIPT, _EXPECTED)
    assert r["grounded_rate"] == 1.0 and r["coverage"] == 1.0   # old scorer would have passed
    assert r["noise"] > ed.NOISE_MAX and r["pass"] is False     # ...but count-discipline catches it


def test_negative_hit_fails_even_when_facts_covered():
    expected = {"facts": [["rust", "engineer"]], "negatives": [["cargo", "passed"]]}
    cands = [_cand("profile/role.md", "fact", "rust engineer building systems"),
             _cand("general/run.md", "observation", "cargo test passed in 1.2s")]  # ephemera memorialized
    r = ed.score(cands, _TRANSCRIPT, expected)
    assert r["negative_hits"] == 1 and r["pass"] is False


def test_no_facts_declared_means_no_noise_penalty():
    # a fixture with no facts can't judge usefulness → noise must not penalize it
    r = ed.score(_good(), _TRANSCRIPT, {"facts": []})
    assert r["noise"] == 0.0


def test_golden_fixtures_carry_negatives_and_widen_kinds():
    names = {fx["name"] for fx in ed.GOLDEN_SESSIONS}
    assert {"incident-postmortem", "build-procedure"} <= names      # widened beyond the original 2
    for fx in ed.GOLDEN_SESSIONS:
        assert "negatives" in fx                                    # contract: every fixture declares it


# --- item #22: A/B latency matrix + JSONL persistence -----------------------

def test_persist_appends_jsonl(tmp_path):
    import json
    p = str(tmp_path / "matrix.jsonl")
    ed.persist({"runs": [{"provider": "llamacpp"}]}, p)
    ed.persist({"runs": [{"provider": "anthropic"}]}, p)
    lines = open(p, encoding="utf-8").read().splitlines()
    assert len(lines) == 2                                   # append-only history, not overwrite
    for ln in lines:
        rec = json.loads(ln)
        assert "generated_at" in rec and "runs" in rec       # each line round-trips + stamped


def test_run_matrix_shape_with_stub(monkeypatch):
    def fake_run_timed(provider, model=None):
        return {"provider": provider, "model": "m", "pass": True, "total_latency_s": 1.23,
                "sessions": [{"name": "s1", "pass": True, "latency_s": 0.6,
                              "valid_rate": 1.0, "grounded_rate": 1.0, "coverage": 1.0, "noise": 0.0}]}
    monkeypatch.setattr(ed, "run_timed", fake_run_timed)
    rep = ed.run_matrix(["ollama", "llamacpp"])
    assert len(rep["runs"]) == 2 and "generated_at" in rep
    for run in rep["runs"]:
        assert isinstance(run["total_latency_s"], float) and run["total_latency_s"] >= 0
        assert all("latency_s" in s for s in run["sessions"])


def test_print_matrix_has_latency_and_caveat(capsys):
    rep = {"generated_at": "2026-06-08T00:00:00+00:00", "runs": [
        {"provider": "llamacpp", "model": "g", "pass": True, "total_latency_s": 2.0,
         "sessions": [{"name": "s1", "pass": True, "latency_s": 1.0, "valid_rate": 1.0,
                       "grounded_rate": 1.0, "coverage": 1.0, "noise": 0.0}]}]}
    ed.print_matrix(rep)
    out = capsys.readouterr().out
    assert "lat=1.00s" in out and "total_lat=2.00s" in out
    assert "NOT a provider-selection signal" in out          # the n=2 honesty banner


def test_print_report_smoke(capsys):
    rep = {"provider": "ollama", "model": "m", "pass": True,
           "sessions": [{"name": "s1", "pass": True, "valid_rate": 1.0, "path_ok_rate": 1.0,
                         "kind_ok_rate": 1.0, "grounded_rate": 1.0, "coverage": 1.0},
                        {"name": "s2", "error": "boom", "pass": False}]}
    ed.print_report(rep)
    out = capsys.readouterr().out
    assert "PASS" in out and "s1" in out and "ERR" in out and "overall: PASS" in out
