#!/usr/bin/env python3
"""Phase D3 — distillation eval (acceptance #3, second half).

Scores the QUALITY of dream output: did distillation produce well-formed, on-topic
memories that capture what mattered in a session? Two layers, deliberately split:

1. **Deterministic scoring** (`score`) — the CI-gateable core. Given a candidate
   memory set + the source transcript + the fixture's expectations, it checks each
   memory's STRUCTURE (valid frontmatter, in-vocab path bucket, in-enum `kind`),
   its GROUNDING (token overlap with the transcript — no off-topic fabrication),
   and the set's COVERAGE of the expected facts. No LLM — so the SCORER itself is
   regression-tested deterministically (a model update that starts emitting
   invalid paths/kinds or off-topic memories drops these numbers).

2. **Opt-in model run** (`run`) — `njhook eval-distillation --provider …` actually
   calls a real provider over the golden transcripts, then applies the SAME
   deterministic scorer, printing a per-provider matrix. Non-deterministic and
   needs a provider, so it's a local/manual report, not a CI gate.

This is the one program eval that resists a pure CI gate: generating candidates
needs the LLM, but the scoring of whatever's generated is fully deterministic.
"""
from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality            # noqa: E402  (dream/)
import memory_types as mt  # noqa: E402  (hooks/)

GROUND_MIN = float(os.environ.get("DREAM_EVAL_GROUNDING_MIN", "0.30"))
COVERAGE_MIN = float(os.environ.get("DREAM_EVAL_COVERAGE_MIN", "0.66"))
# Item #11: max tolerated fraction of candidates that are "noise" — valid, grounded,
# on-topic, but covering NONE of the expected facts (padding). The old scorer only
# measured recall (coverage), so a provider could pass by emitting 10 vague memories
# to cover 2 facts; this gates count-discipline ("prefer fewer, sharper memories").
NOISE_MAX = float(os.environ.get("DREAM_EVAL_NOISE_MAX", "0.50"))

_TOKEN_RE = quality._GROUND_TOKEN_RE


# Golden sessions: a transcript + what a good distillation must capture. Each
# expected "fact" is a set of distinctive tokens that should appear together in
# at least one produced memory.
GOLDEN_SESSIONS = [
    {
        "name": "rust-safety",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "I'm a Rust systems engineer. We had a use-after-free incident last sprint, "
                       "so no unsafe blocks unless I explicitly approve them."},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "cargo test"},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "42 passed in 1.2s"},
        ],
        "facts": [["rust", "engineer"], ["unsafe", "approval"], ["incident", "sprint"]],
        # The cargo-test output is ephemera — a good distillation must NOT
        # memorialize "42 passed in 1.2s" as a memory.
        "negatives": [["cargo", "passed"], ["1.2s"]],
    },
    {
        "name": "deploy-pref",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Always deploy via the staging pipeline first, never straight to prod. "
                       "I prefer terse PR descriptions."},
        ],
        "facts": [["staging", "pipeline"], ["terse", "descriptions"]],
        "negatives": [],
    },
    # +2 fixtures covering kinds beyond preference/constraint (#11 descope: a
    # focused widening toward the rest of the 15-type vocabulary, with ephemera
    # negatives, not the full 12-kind expansion).
    {
        "name": "incident-postmortem",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Postmortem: the nightly cron silently failed for a week because the "
                       "Docker container had no restart policy. Lesson: always set "
                       "restart=unless-stopped on long-running services."},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "container exited 137"},
        ],
        "facts": [["restart", "unless-stopped"], ["nightly", "failed"]],
        "negatives": [["exited", "137"]],
    },
    {
        "name": "build-procedure",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "To release: bump the version, run the full pytest suite, then tag and "
                       "push. Never release on a red build."},
        ],
        "facts": [["release", "version"], ["pytest", "suite"]],
        "negatives": [],
    },
]


def _body_tokens(candidate) -> set:
    return set(_TOKEN_RE.findall((candidate.get("content") or "").lower()))


def _matches(candidate, token_set) -> bool:
    """True if the candidate body contains every token of `token_set`."""
    return {t.lower() for t in token_set} <= _body_tokens(candidate)


def _covers(candidates: list, fact_tokens: list) -> bool:
    """True if some candidate body contains every token of `fact_tokens`."""
    return any(_matches(c, fact_tokens) for c in candidates)


def score(candidates: list, transcript: str, expected: dict,
          ground_min: float = GROUND_MIN, coverage_min: float = COVERAGE_MIN,
          noise_max: float = NOISE_MAX) -> dict:
    """Deterministically score a candidate memory set against a fixture. Returns
    rates for the structural checks + grounding + fact coverage + count-discipline
    (precision / noise / negative_hits), and an overall `pass`. Empty candidate set
    fails (coverage 0)."""
    n = len(candidates)
    valid = path_ok = kind_ok = grounded = 0
    for c in candidates:
        content = c.get("content") or ""
        path = c.get("path") or ""
        if quality.validate_memory(c) == []:
            valid += 1
        if quality.PATH_RE.match(path):
            path_ok += 1
        k = (c.get("kind") or mt.parse_kind(content) or "").lower()
        if k in mt.MEMORY_KINDS:
            kind_ok += 1
        if quality.grounding_score(content, transcript) >= ground_min:
            grounded += 1
    facts = expected.get("facts") or []
    negatives = expected.get("negatives") or []
    covered = sum(1 for f in facts if _covers(candidates, f))
    coverage = covered / len(facts) if facts else 1.0

    # Count-discipline (item #11): a candidate is "useful" if it covers >=1 expected
    # fact. noise = fraction that cover NONE (padding). negative_hits = candidates
    # memorializing declared ephemera. Only judge noise when the fixture declares
    # facts (else there's nothing to be useful against → no penalty).
    useful = sum(1 for c in candidates if any(_matches(c, f) for f in facts))
    negative_hits = sum(1 for c in candidates if any(_matches(c, neg) for neg in negatives))
    noise = (n - useful) / n if (n and facts) else 0.0

    def rate(x):
        return (x / n) if n else 0.0

    rates = {
        "n": n,
        "valid_rate": rate(valid),
        "path_ok_rate": rate(path_ok),
        "kind_ok_rate": rate(kind_ok),
        "grounded_rate": rate(grounded),
        "coverage": coverage,
        "precision": rate(useful),
        "noise": noise,
        "negative_hits": negative_hits,
    }
    rates["pass"] = bool(
        n > 0
        and rates["valid_rate"] == 1.0
        and rates["path_ok_rate"] == 1.0
        and rates["kind_ok_rate"] == 1.0
        and rates["grounded_rate"] == 1.0
        and coverage >= coverage_min
        and noise <= noise_max
        and negative_hits == 0
    )
    return rates


def run(provider: str | None = None, model: str | None = None) -> dict:
    """Opt-in: call a real provider over each golden session and score its output.
    Returns {provider, model, sessions:[{name, ...rates}], pass}. Imports the dream
    stack lazily (needs the provider SDK / Ollama)."""
    import dream as dream_mod
    from providers import get_provider, default_model
    from prompts import system_prompt_for

    name, fn = get_provider(provider)
    model = model or default_model(name)
    system = system_prompt_for(name, model)

    sessions = []
    for fx in GOLDEN_SESSIONS:
        transcript = dream_mod.render_events(fx["events"])
        try:
            candidates = dream_mod.call_provider(fn, transcript, existing="", model=model, system_prompt=system)
        except Exception as e:
            sessions.append({"name": fx["name"], "error": f"{type(e).__name__}: {str(e)[:100]}", "pass": False})
            continue
        r = score(candidates, transcript, fx)
        r["name"] = fx["name"]
        sessions.append(r)
    return {"provider": name, "model": model, "sessions": sessions,
            "pass": all(s.get("pass") for s in sessions)}


def print_report(rep: dict) -> None:
    """Render a run() report as a per-session matrix."""
    print(f"distillation eval — provider={rep['provider']} model={rep['model']}")
    for s in rep["sessions"]:
        if s.get("error"):
            print(f"  [ERR ] {s['name']}: {s['error']}")
            continue
        mark = "PASS" if s["pass"] else "FAIL"
        print(f"  [{mark}] {s['name']:<20} valid={s['valid_rate']:.2f} path={s['path_ok_rate']:.2f} "
              f"kind={s['kind_ok_rate']:.2f} grounded={s['grounded_rate']:.2f} "
              f"coverage={s['coverage']:.2f} noise={s.get('noise', 0.0):.2f} neg={s.get('negative_hits', 0)}")
    print(f"overall: {'PASS' if rep['pass'] else 'FAIL'}")


# --- item #22: provider A/B latency matrix (latency-only; cost is item #13) ---

def run_timed(provider: str | None = None, model: str | None = None) -> dict:
    """Like run(), but brackets each per-session call_provider with a wall-clock
    timer — attaches `latency_s` to each session + a provider-level
    `total_latency_s`. Latency is the decision-grade signal here (cost is #13)."""
    import dream as dream_mod
    from providers import get_provider, default_model
    from prompts import system_prompt_for

    name, fn = get_provider(provider)
    model = model or default_model(name)
    system = system_prompt_for(name, model)

    sessions, total = [], 0.0
    for fx in GOLDEN_SESSIONS:
        transcript = dream_mod.render_events(fx["events"])
        t0 = time.perf_counter()
        try:
            candidates = dream_mod.call_provider(fn, transcript, existing="", model=model, system_prompt=system)
        except Exception as e:
            dt = time.perf_counter() - t0
            total += dt
            sessions.append({"name": fx["name"], "error": f"{type(e).__name__}: {str(e)[:100]}",
                             "pass": False, "latency_s": round(dt, 3)})
            continue
        dt = time.perf_counter() - t0
        total += dt
        r = score(candidates, transcript, fx)
        r["name"] = fx["name"]
        r["latency_s"] = round(dt, 3)
        sessions.append(r)
    return {"provider": name, "model": model, "sessions": sessions,
            "pass": all(s.get("pass") for s in sessions), "total_latency_s": round(total, 3)}


def run_matrix(providers: list[str], model_overrides: dict | None = None,
               persist_path: str | None = None) -> dict:
    """Run the timed eval across several providers, returning {runs, generated_at}.
    Optionally append the report to a JSONL history (persist)."""
    model_overrides = model_overrides or {}
    runs = [run_timed(p, model_overrides.get(p)) for p in providers]
    report = {"runs": runs, "generated_at": datetime.now(timezone.utc).isoformat()}
    if persist_path is not None:
        persist(report, persist_path)
    return report


def _default_report_path() -> str:
    return (os.environ.get("DREAM_EVAL_REPORT_PATH")
            or str(Path(__file__).resolve().parents[1] / ".eval" / "distillation_matrix.jsonl"))


def persist(report: dict, path: str | None = None) -> str:
    """Append ONE JSON line to a history file (never rewrite — a record accretes,
    matching the non-destructive ethos). Creates the parent dir. Returns the path."""
    path = path or _default_report_path()
    report = {**report, "generated_at": report.get("generated_at") or datetime.now(timezone.utc).isoformat()}
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(report) + "\n")
    return path


def print_matrix(report: dict) -> None:
    """Render the A/B matrix: one block per provider with a latency column, plus a
    banner that the quality rates are NOT decision-grade at this small fixture n."""
    for run in report["runs"]:
        print(f"\nprovider={run['provider']} model={run['model']} "
              f"total_lat={run.get('total_latency_s', 0.0):.2f}s")
        for s in run["sessions"]:
            if s.get("error"):
                print(f"  [ERR ] {s['name']}: {s['error']}  lat={s.get('latency_s', 0.0):.2f}s")
                continue
            mark = "PASS" if s["pass"] else "FAIL"
            print(f"  [{mark}] {s['name']:<20} valid={s['valid_rate']:.2f} grounded={s['grounded_rate']:.2f} "
                  f"coverage={s['coverage']:.2f} noise={s.get('noise', 0.0):.2f} "
                  f"lat={s.get('latency_s', 0.0):.2f}s")
    print(f"\nNOTE: n={len(GOLDEN_SESSIONS)} fixtures — latency is decision-grade; the quality rates "
          "are a regression sanity check, NOT a provider-selection signal.")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Phase D3 distillation eval (opt-in, calls a real provider).")
    ap.add_argument("--provider", choices=["anthropic", "openai", "ollama", "llamacpp"],
                    help="LLM backend (default: $DREAM_PROVIDER or anthropic)")
    ap.add_argument("--model", help="override the provider's default model")
    args = ap.parse_args()
    rep = run(args.provider, args.model)
    print_report(rep)
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
