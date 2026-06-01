<!--
Execution ledger for docs/IMPLEMENTATION_PLAN.md. Maps every phase/slice to its
status, delivering PR, and acceptance evidence — and honestly logs the acceptance
items still open. Update this whenever a phase/slice changes state. Last updated
2026-06-01.
-->

# njhook Universal Memory — Progress Ledger

## Goal

Execute **Phases A–H** of [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) to completion. The ultimate target is the north star: *a universal memory layer for LLMs with a human-friendly interface for tracing memory evolutions.*

**Definition of done (overall acceptance):** every numbered acceptance bar in `IMPLEMENTATION_PLAN.md`, for every phase A–H, is **met with tests + live verification and merged to `main`** — i.e. full alignment between the plan and the shipped system. This ledger is the single source of truth for that alignment; an item is only "done" when its acceptance evidence is recorded here.

Legend: ✅ done & merged · 🔵 in progress / open PR · ⏸ deferred (with reason) · ⬜ not started · ⚠ acceptance gap (delivered but an acceptance item is unmet)

## Status by phase

| Phase | Status | Delivered by | Acceptance evidence | Open items |
|---|---|---|---|---|
| **A** — Non-destructive history | ✅ merged | #4 | 7 tests; revision-chain on content change; `consolidate` supersedes (negative test: no `DETACH DELETE`); recall filters `status='active'`; `:DreamRun`-`WROTE` provenance | ⚠ **A#6** backup/restore of the new fields + `:MemoryRevision`/`:SUPERSEDED_BY` lineage **not yet verified** |
| **B** — Durable capture (spool/inbox/DLQ) | ⬜ not started | — | — | all (F4, Gap 1 canonical schema, Gap 8 metrics) |
| **C** — Shared recall + ranking | ✅ merged (C1–C3) | #5, #6, #9 | shared `recall.py` (negative test: no surface keeps own ranking math); importance×recency ranking + value-density budget; `event_fulltext` + `event_search`; 7+4+3 tests | ⏸ **C4** cross-encoder reranker deferred; ⚠ explicit **vector-only fallback** ranking test not yet written |
| **D** — Typed memory + admission gate | ⬜ not started | — | — | all (F3 A-MAC gate, 13-type vocab, Gap 9 eval suites); also delivers `:EXTRACTED_FROM` |
| **E** — Conflict & review | ⬜ not started | — | — | all (F6: contradiction detection, review queue) — needs A, D |
| **F** — Evolution UI (north star) | 🔵 slice 1 open | #10 | `memory_history` engine; CLI `history --diff`; dashboard `/memory/<path>/history` timeline + diffs; 2 tests | slice 2: `--as-of` recall (buildable now), lineage graph (needs D `EXTRACTED_FROM` + E `CONTRADICTS`), inline citation footer (Q6) |
| **G** — Universal interfaces (REST/MCP) | ⬜ not started | — | — | all (F8: REST, MCP, `recall`/`write-event` CLI, file renderers) — needs C |
| **H** — Governance & eval | ⬜ not started | — | — | all (Gap 7 egress, Gap 12 anti-poisoning, Gap 9 CI evals) — needs B, D |

**Rollup:** A ✅ · C ✅ (sans C4) · F partial — ~2.5 of 8 phases. Critical path **A → C → F** is the most advanced; B, D, E, G, H not started.

## Acceptance gaps to close for full alignment

These are delivered-but-incomplete items the "full alignment" goal must resolve:

1. **A#6 — backup/restore of new fields + revision lineage.** PR #4 added the schema (`ingested_at`/`valid_from`/`valid_until`/`status`/`created_by`, `:MemoryRevision`, `:SUPERSEDED_BY`, `:DreamRun`) but did **not** update `cli/njhook.py` backup/restore to round-trip them. The event-projection field list and the memory projection need the new fields, and the revision/supersession lineage needs export/restore. **Action:** a small follow-up PR + round-trip test.
2. **C — vector-only fallback test.** Phase C acceptance item 2 lists "vector-only fallback" among the ranking behaviours to pin; `test_recall_engine.py` covers fusion, project boost, budget, modes, importance, recency — but not an explicit fulltext-empty / vector-only case. **Action:** add one test.
3. **C4 — reranker (deferred, not cancelled).** Optional cross-encoder rerank (HF `FlagReranker` CPU path). Deferred in favour of Phase F; revisit before closing Phase C as fully aligned, or explicitly mark C4 out-of-scope in the plan.

## Out-of-plan work (shipped)

Not numbered phases, but delivered and acceptance-evidenced in their PRs:

- **Nightly-yield fix** (#7 + #8): the nightly distilled nothing on real data. Root cause was context engineering, not the model (qwen empty, gemma hallucinates; proven by emptying the context). Fix = scope existing-context + exclude superseded + paths-only for local + transcript cap + **hybrid local→Anthropic fallback**. Verified: 44 tests; real 194-event session falls back and writes memories.
- **#1**: dream large-session distillation, quality-gate false-positive fix, health dream-freshness check, **nightly rescheduled 3 AM → 3 PM** (`StartWhenAvailable`).
- **Docs**: research report + plan + roadmap (#3, this PR), self-contained HTML reference (#2).

## Deviations from the original plan (logged)

- **`:EXTRACTED_FROM` moved from Phase A → Phase D** — linking every memory to every processed event would explode edges on large sessions; claim-level provenance needs the provider to cite source events (a D capability).
- **Phase A same-path model = revision-chain, not duplicate-path nodes** — `Memory.path` is `UNIQUE`; documented in the plan's Phase A design note.
- **C3 nucleus expansion deferred to Phase D** — it walks `(:Memory)-[:EXTRACTED_FROM]->(:Event)`, which doesn't exist until D.
- **Phase F split into two slices** — slice 1 (history/diff, #10) shipped; slice 2 (`--as-of` + lineage + citation) pending.
- **C4 reranker deferred** — see acceptance gap #3 above.

## PR ledger

| PR | State | Summary |
|---|---|---|
| #1 | merged | dream large-session fix + quality-gate fix + health freshness + 3 PM reschedule |
| #2 | open | docs: HTML reference |
| #3 | open | docs: research + implementation plan + roadmap (+ this ledger) |
| #4 | merged | Phase A — non-destructive history |
| #5 | merged | Phase C1 — shared recall engine |
| #6 | merged | Phase C2 — recency + importance ranking |
| #7 | merged | nightly fix — scope existing-context |
| #8 | merged | nightly fix — transcript cap + hybrid fallback |
| #9 | merged | Phase C3 — raw event retrieval |
| #10 | open | Phase F (1/2) — memory evolution history (timeline + diff) |

## Metrics

- Tests: **19 → 49** over the program (live Neo4j + pure).
- `njhook health`: **21 ok / 0 warn / 0 fail**.
- Graph: ~20 memories, ~34 sessions, ~9.5k events; nightly task registered at 3 PM.

## How to keep this aligned

Each phase/slice PR must: (1) state which `IMPLEMENTATION_PLAN.md` acceptance items it satisfies, with evidence (test names, eval/live output); (2) update this ledger's status table + close any acceptance gap it resolves; (3) tag the corresponding plan item ✅. A phase is "fully aligned" only when **every** one of its acceptance bars is ✅ here.
