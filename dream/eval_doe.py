#!/usr/bin/env python3
"""Distillation DOE — compare prompt/output-format factors with replicates.

The CI eval (eval_distillation) runs 4 tiny fixtures ONCE — too noisy to tell a
real change from the 12B's run-to-run variance. This harness runs a factorial over

    output_format ∈ {json, delim}        # JSON (schema-enforced) vs delimited raw body
    prompt_variant ∈ {current, ...}      # extensible

across the extended corpus (eval_corpus.CORPUS) × R replicates, and reports, per
cell, mean±std of:
    parse_ok      — did the output parse to >=1 valid memory (no exception/truncation)?  <-- the format-reliability signal
    grounded      — fraction of memories grounded in the transcript
    coverage      — fraction of expected facts captured
    noise         — fraction of padding memories (cover no fact)
    neg_hits      — memories memorializing ephemera (lower better)
    out_chars     — emitted output size (the ~4% token angle)
    latency_s

The two output specs share ONE base-rules block, so a cell difference is the
FACTOR, not incidental prompt drift. Parsers are pure (unit-tested in
tests/test_eval_doe_parsers.py); the model calls are manual (needs the local server).

    python dream/eval_doe.py --replicates 3 --formats json,delim
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import memory_types as mt           # noqa: E402
import eval_distillation as ed      # noqa: E402  (score() + the CI fixtures)
from eval_corpus import CORPUS      # noqa: E402
import dream as dream_mod           # noqa: E402

LLAMACPP_CHAT_URL = os.environ.get("LLAMACPP_CHAT_URL", "http://127.0.0.1:8090/v1")


# --- shared rules (one block; the ONLY thing that varies across cells is OUTPUT) ---

# Shared preamble — held CONSTANT across prompt variants (kind vocab + path scoping
# + importance). Only the extraction FRAMING (_EXTRACT) varies, so a variant cell
# difference is the framing, not incidental drift.
_PREAMBLE = """\
You distill an agent's coding session into durable markdown memories that will help \
future sessions. Each memory has a `path` (e.g. profile/role.md, tools/bash/grep.md, \
project/deploy.md), a short `title`, a semantic `kind`, a markdown `content` body, \
and an `importance` 1-10.

kind is one of: """ + ", ".join(sorted(mt.MEMORY_KINDS)) + """.

- profile/* and tools/* are cross-project; project/* and general/* are scoped.
- Pick `kind` by what the memory IS; spread `importance` across the 1-10 range."""

# The prompt FACTOR: three extraction framings targeting the coverage↔noise tradeoff
# the corpus exposed (coverage ~0.4, noise ~0.3 under `current`).
_EXTRACT = {
    # control: today's "prefer fewer, sharper" framing (suspected to suppress coverage)
    "current": """\
Rules:
- Capture durable facts, preferences, decisions, rules, procedures, and lessons — \
NOT ephemera (tool outputs, exit codes, timings, PIDs, one-off command results).
- Prefer FEWER, SHARPER memories over many vague ones. If nothing is worth \
remembering, output an empty set.""",
    # coverage push: one memory per distinct fact, aim for completeness
    "coverage": """\
Rules:
- Extract EACH distinct durable fact, preference, decision, rule, procedure, or \
lesson as its OWN memory — do not merge two unrelated takeaways into one. Aim for \
COMPLETENESS: capture every durable takeaway in the session.
- Skip ephemera (tool outputs, exit codes, timings, PIDs, one-off command results).""",
    # structured: identify-then-emit + explicit ephemera exclusion (coverage AND precision)
    "structured": """\
Work in two steps:
1) Identify each DISTINCT durable takeaway — a fact, preference, decision, rule, \
procedure, or lesson that would help a FUTURE session.
2) Emit exactly ONE memory per takeaway.
Ephemera — tool outputs, exit codes, timings, PIDs, one-off command results — are \
NEVER takeaways. Don't pad with vague memories, and don't merge two takeaways into one.""",
}
_BASE_RULES = _PREAMBLE   # back-compat alias for the shared-preamble invariant test

_OUTPUT_JSON = """\
Output STRICT JSON only, no prose:
{"memories":[{"path":"...","title":"...","kind":"...","content":"<body only>","importance":7}]}"""

_OUTPUT_DELIM = """\
Output ONLY memory blocks in this exact format, nothing else. Start each block with \
a line `@@ path | kind | importance | title` then the raw markdown body on the \
following lines (real newlines, no escaping). End each body with a line `@@end`.

@@ profile/role.md | preference | 8 | User role
Prefers async/await over callbacks. 2-space indent.
@@end
@@ project/db.md | decision | 7 | Database choice
Chose Postgres over DynamoDB — needs multi-row transactions.
@@end

If nothing is worth remembering, output nothing."""


def system_prompt(fmt: str, variant: str = "current") -> str:
    out = _OUTPUT_DELIM if fmt == "delim" else _OUTPUT_JSON
    rules = _EXTRACT.get(variant, _EXTRACT["current"])
    return _PREAMBLE + "\n\n" + rules + "\n\n" + out


# --- pure parsers (unit-tested) ---------------------------------------------

def parse_json_output(text: str) -> list[dict]:
    """Parse a JSON `{memories:[...]}` blob, tolerating leading/trailing prose and
    code fences. Each memory's body-only content is wrapped to frontmatter via the
    same normalizer the production path uses. Raises on unrecoverable JSON."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t).strip()
    try:
        obj = json.loads(t)
    except Exception:
        m = re.search(r"\{.*\}", t, re.S)            # salvage the outermost object
        if not m:
            raise
        obj = json.loads(m.group(0))
    mems = obj.get("memories", []) if isinstance(obj, dict) else []
    return [dream_mod._normalize_memory_content(m) for m in mems if isinstance(m, dict)]


_DELIM_HDR = re.compile(r"^@@\s*(?P<path>[^|]+?)\s*\|\s*(?P<kind>[^|]+?)\s*\|\s*(?P<imp>\d+)\s*\|\s*(?P<title>.+?)\s*$")


def parse_delim_output(text: str) -> list[dict]:
    """Parse the `@@ path | kind | importance | title` / body / `@@end` format into
    memory dicts (with synthesized frontmatter). Forgiving: a missing `@@end` ends
    the body at the next `@@ ` header or EOF; malformed headers are skipped."""
    mems, cur, body = [], None, []

    def _flush():
        if cur is not None:
            b = "\n".join(body).strip()
            mems.append(dream_mod._normalize_memory_content(
                {"path": cur["path"], "title": cur["title"], "kind": cur["kind"],
                 "content": b, "importance": int(cur["imp"])}))

    for line in text.splitlines():
        if line.strip() == "@@end":
            _flush(); cur, body = None, []
            continue
        h = _DELIM_HDR.match(line.strip())
        if h:
            _flush()                                  # implicit close of a prior block
            cur, body = h.groupdict(), []
            continue
        if cur is not None:
            body.append(line)
    _flush()
    return mems


# --- one model call + score -------------------------------------------------

def _chat(system: str, user: str, fmt: str, max_tokens: int = 4096) -> tuple[str, float]:
    payload = {"model": os.environ.get("DREAM_LLAMACPP_MODEL", "gemma-4-12B-it-Q4_K_M.gguf"),
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
               "max_tokens": max_tokens, "temperature": 0.0}
    if fmt == "json":          # production-faithful: schema-constrained (not loose json_object)
        from prompts import DREAM_JSON_SCHEMA
        payload["response_format"] = {"type": "json_schema",
                                      "json_schema": {"name": "memories", "schema": DREAM_JSON_SCHEMA}}
    elif fmt == "json_object":  # the looser mode, kept for comparison
        payload["response_format"] = {"type": "json_object"}
    req = urllib.request.Request(f"{LLAMACPP_CHAT_URL.rstrip('/')}/chat/completions",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer x"},
                                 method="POST")
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=300) as r:
        body = json.loads(r.read())
    return body["choices"][0]["message"]["content"] or "", time.perf_counter() - t0


def run_one(fx: dict, fmt: str, variant: str) -> dict:
    """One model call over one fixture; returns a metrics record (parse failure is a
    record with parse_ok=0, not an exception — that IS the format-reliability data)."""
    transcript = dream_mod.render_events(fx["events"])
    sysp = system_prompt(fmt, variant)
    rec = {"fixture": fx["name"], "format": fmt, "variant": variant,
           "parse_ok": 0, "n": 0, "grounded": 0.0, "coverage": 0.0, "noise": 0.0,
           "neg_hits": 0, "out_chars": 0, "latency_s": 0.0}
    try:
        text, dt = _chat(sysp, transcript, fmt)
        rec["out_chars"] = len(text); rec["latency_s"] = round(dt, 2)
        mems = parse_json_output(text) if fmt == "json" else parse_delim_output(text)
        rec["parse_ok"] = 1 if mems else 0
        if mems:
            sc = ed.score(mems, transcript, fx)
            rec.update(n=sc["n"], grounded=sc["grounded_rate"], coverage=sc["coverage"],
                       noise=sc["noise"], neg_hits=sc["negative_hits"])
    except Exception as e:
        rec["error"] = f"{type(e).__name__}: {str(e)[:80]}"
    return rec


def _agg(records: list[dict], keys) -> dict:
    import statistics as st
    out = {}
    for k in keys:
        vals = [r[k] for r in records if k in r]
        out[k] = (round(st.mean(vals), 3), round(st.pstdev(vals), 3)) if vals else (0.0, 0.0)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Distillation DOE (manual; calls the local model).")
    ap.add_argument("--replicates", type=int, default=3)
    ap.add_argument("--formats", default="json,delim")
    ap.add_argument("--variants", default="current", help="comma list of prompt variants to sweep")
    ap.add_argument("--persist", action="store_true")
    args = ap.parse_args(argv)
    formats = args.formats.split(",")
    variants = args.variants.split(",")
    metrics = ["parse_ok", "grounded", "coverage", "noise", "neg_hits", "out_chars", "latency_s", "n"]

    all_recs = []
    summary = []
    for fmt in formats:
        for variant in variants:
            cell = []
            for rep in range(args.replicates):
                for fx in CORPUS:
                    rec = run_one(fx, fmt, variant)
                    cell.append(rec); all_recs.append(rec)
                    tag = "ok" if rec["parse_ok"] else (rec.get("error") or "no-parse")
                    print(f"  [{fmt:5}/{variant:<10} r{rep}] {rec['fixture']:<22} parse={tag:<12} "
                          f"n={rec['n']} cov={rec['coverage']:.2f} noise={rec['noise']:.2f}", file=sys.stderr)
            a = _agg(cell, metrics)
            summary.append((fmt, variant, a))
            print(f"\n=== format={fmt} variant={variant} | {len(cell)} runs "
                  f"({len(CORPUS)} fixtures × {args.replicates} reps) ===")
            for k in metrics:
                m, sd = a[k]
                print(f"  {k:<10} {m:>8.3f} ± {sd:.3f}")

    # compact comparison table across cells (the DOE payoff)
    print(f"\n{'format/variant':<22}{'parse':>7}{'cover':>7}{'noise':>7}{'neg':>6}{'n':>6}{'chars':>8}{'lat':>7}")
    for fmt, variant, a in summary:
        print(f"{fmt+'/'+variant:<22}{a['parse_ok'][0]:>7.2f}{a['coverage'][0]:>7.2f}"
              f"{a['noise'][0]:>7.2f}{a['neg_hits'][0]:>6.2f}{a['n'][0]:>6.1f}"
              f"{a['out_chars'][0]:>8.0f}{a['latency_s'][0]:>7.2f}")

    if args.persist:
        p = Path(__file__).resolve().parents[1] / ".eval" / "doe.jsonl"
        p.parent.mkdir(exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"records": all_recs}) + "\n")
        print(f"\npersisted {len(all_recs)} records -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
