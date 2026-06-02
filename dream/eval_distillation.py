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

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import quality            # noqa: E402  (dream/)
import memory_types as mt  # noqa: E402  (hooks/)

GROUND_MIN = float(os.environ.get("DREAM_EVAL_GROUNDING_MIN", "0.30"))
COVERAGE_MIN = float(os.environ.get("DREAM_EVAL_COVERAGE_MIN", "0.66"))

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
    },
    {
        "name": "deploy-pref",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Always deploy via the staging pipeline first, never straight to prod. "
                       "I prefer terse PR descriptions."},
        ],
        "facts": [["staging", "pipeline"], ["terse", "descriptions"]],
    },
]


def _covers(candidates: list, fact_tokens: list) -> bool:
    """True if some candidate body contains every token of `fact_tokens`."""
    want = {t.lower() for t in fact_tokens}
    for c in candidates:
        body = set(_TOKEN_RE.findall((c.get("content") or "").lower()))
        if want <= body:
            return True
    return False


def score(candidates: list, transcript: str, expected: dict,
          ground_min: float = GROUND_MIN, coverage_min: float = COVERAGE_MIN) -> dict:
    """Deterministically score a candidate memory set against a fixture. Returns
    rates for the structural checks + grounding + fact coverage, and an overall
    `pass`. Empty candidate set fails (coverage 0)."""
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
    covered = sum(1 for f in facts if _covers(candidates, f))
    coverage = covered / len(facts) if facts else 1.0

    def rate(x):
        return (x / n) if n else 0.0

    rates = {
        "n": n,
        "valid_rate": rate(valid),
        "path_ok_rate": rate(path_ok),
        "kind_ok_rate": rate(kind_ok),
        "grounded_rate": rate(grounded),
        "coverage": coverage,
    }
    rates["pass"] = bool(
        n > 0
        and rates["valid_rate"] == 1.0
        and rates["path_ok_rate"] == 1.0
        and rates["kind_ok_rate"] == 1.0
        and rates["grounded_rate"] == 1.0
        and coverage >= coverage_min
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
        print(f"  [{mark}] {s['name']:<14} valid={s['valid_rate']:.2f} path={s['path_ok_rate']:.2f} "
              f"kind={s['kind_ok_rate']:.2f} grounded={s['grounded_rate']:.2f} coverage={s['coverage']:.2f}")
    print(f"overall: {'PASS' if rep['pass'] else 'FAIL'}")


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Phase D3 distillation eval (opt-in, calls a real provider).")
    ap.add_argument("--provider", choices=["anthropic", "openai", "ollama"],
                    help="LLM backend (default: $DREAM_PROVIDER or anthropic)")
    ap.add_argument("--model", help="override the provider's default model")
    args = ap.parse_args()
    rep = run(args.provider, args.model)
    print_report(rep)
    return 0 if rep["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
