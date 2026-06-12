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

_BASE_RULES = """\
You distill an agent's coding session into a few durable markdown memories that \
will help future sessions. Each memory has a `path` (e.g. profile/role.md, \
tools/bash/grep.md, project/deploy.md), a short `title`, a semantic `kind`, a \
markdown `content` body, and an `importance` 1-10.

kind is one of: """ + ", ".join(sorted(mt.MEMORY_KINDS)) + """.

Rules:
- Capture durable facts, preferences, decisions, rules, procedures, and lessons — \
NOT ephemera (tool outputs, exit codes, timings, PIDs, one-off command results).
- profile/* and tools/* are cross-project; project/* and general/* are scoped.
- Prefer FEWER, SHARPER memories over many vague ones. If nothing is worth \
remembering, output an empty set.
- Pick `kind` by what the memory IS; spread `importance` across the 1-10 range."""

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
    return _BASE_RULES + "\n\n" + out


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
    ap.add_argument("--variant", default="current")
    ap.add_argument("--persist", action="store_true")
    args = ap.parse_args(argv)
    formats = args.formats.split(",")
    metrics = ["parse_ok", "grounded", "coverage", "noise", "neg_hits", "out_chars", "latency_s", "n"]

    all_recs = []
    for fmt in formats:
        cell = []
        for rep in range(args.replicates):
            for fx in CORPUS:
                rec = run_one(fx, fmt, args.variant)
                cell.append(rec); all_recs.append(rec)
                tag = "ok" if rec["parse_ok"] else (rec.get("error") or "no-parse")
                print(f"  [{fmt:5} r{rep}] {rec['fixture']:<22} parse={tag:<14} "
                      f"n={rec['n']} grnd={rec['grounded']:.2f} cov={rec['coverage']:.2f} "
                      f"noise={rec['noise']:.2f} {rec['out_chars']}ch {rec['latency_s']}s", file=sys.stderr)
        a = _agg(cell, metrics)
        runs = len(cell)
        print(f"\n=== format={fmt} variant={args.variant} | {runs} runs "
              f"({len(CORPUS)} fixtures × {args.replicates} reps) ===")
        for k in metrics:
            m, sd = a[k]
            print(f"  {k:<10} {m:>8.3f} ± {sd:.3f}")

    if args.persist:
        p = Path(__file__).resolve().parents[1] / ".eval" / "doe.jsonl"
        p.parent.mkdir(exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps({"records": all_recs}) + "\n")
        print(f"\npersisted {len(all_recs)} records -> {p}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
