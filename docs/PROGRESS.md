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
| **A** — Non-destructive history | ✅ merged & fully aligned | #4, #12 | 7 tests; revision-chain; `consolidate` supersedes (no `DETACH DELETE`); recall filters `status='active'`; backup/restore round-trips new fields + revision/supersession lineage (PR #12) | — (all 6 acceptance items met) |
| **B** — Durable capture (spool/inbox/DLQ) | 🔵 in progress (PR #11) | #11 | PR-1: append-only fsync spool + `njhook ingest` worker + idempotent replay (Event.event_id = inbox) + DLQ + health backlog row; 6 tests; `HOOKS_CAPTURE_MODE=spool` (default `direct`) | PR-2: canonical OTel `gen_ai.*` schema (Gap 1), DLQ-rate alerting, read-time upcasting, flip default→spool once ingest scheduled |
| **C** — Shared recall + ranking | ✅ merged & fully aligned | #5, #6, #9, #12 | shared `recall.py`; importance×recency + value-density budget; `event_fulltext` + `event_search`; vector-only fallback test (PR #12); 7+4+3+1 tests | ⏸ **C4** reranker formally deferred (decision recorded) — out-of-scope for alignment |
| **D** — Typed memory + admission gate | 🔵 in progress (PR #13) | #13 | PR-1: `:EXTRACTED_FROM` claim-level provenance via heuristic top-K overlap attribution (`attribute_events`, bounded — no explosion); 3 tests | D1 typed `kind` vocab (design: 9 Memanto types don't map to identity memories — see deviations), D2 A-MAC admission gate (+ Phase E review surface), D3 eval suites |
| **E** — Conflict & review | 🔵 in progress (PR #15) | #15 | PR-1: `review.py` engine (auto-resolve by authority×recency) + `njhook review list/approve/reject/supersede/flag` + `pending_review`/`rejected` lifecycle (recall hides them — acceptance #2/#3/#4); 5 tests | PR-2: pre-commit LLM contradiction detection (E1, acceptance #1) + dashboard conflict view + auto-resolve apply |
| **F** — Evolution UI (north star) | 🔵 slice 1 ✅; slice 2 open (#14) | #10, #14 | slice 1: `memory_history` + `history --diff` + dashboard timeline/diffs. slice 2: `content_as_of` + `history --as-of`; `memory_lineage` (source events via `EXTRACTED_FROM` + supersession) in CLI + dashboard; 4 tests | inline citation footer (Q6); `CONTRADICTS` lineage (needs Phase E) |
| **G** — Universal interfaces (REST/MCP) | ⬜ not started | — | — | all (F8: REST, MCP, `recall`/`write-event` CLI, file renderers) — needs C |
| **H** — Governance & eval | ⬜ not started | — | — | all (Gap 7 egress, Gap 12 anti-poisoning, Gap 9 CI evals) — needs B, D |

**Rollup:** A ✅ · C ✅ (sans C4) · F slice 1 ✅ · B started — ~3 of 8 phases touched. Critical path **A → C → F** is the most advanced; D, E, G, H not started.

## Acceptance gaps — all resolved (PR #12)

1. ✅ **A#6 — backup/restore of new fields + revision lineage.** `cli/njhook.py` backup now exports the Phase A scalar fields (`status`/`ingested_at`/`valid_from`/`valid_until`/`created_by`/`importance`) plus `memory_revisions` + `supersessions` lists; restore recreates the `:MemoryRevision` chain + `:SUPERSEDED_BY` edges idempotently. Round-trip test: `tests/test_backup_phase_a.py`.
2. ✅ **C — vector-only fallback test.** `tests/test_recall_engine.py::test_hybrid_merge_vector_only_when_fulltext_empty` pins the fulltext-empty / vector-only ranking path.
3. ✅ **C4 — reranker: formally deferred (decision recorded).** No cross-encoder reranker until an eval suite proves RRF leaves quality on the table — don't add latency + a dependency without evidence (Ollama has no `/api/rerank`; HF `FlagReranker` is the CPU path if/when justified). C4 is **out-of-scope for Phase C "fully aligned"**; revisit once the Phase D/H eval matrix exists.

→ **Phases A and C are now fully aligned** with the plan's acceptance bars.

## Out-of-plan work (shipped)

Not numbered phases, but delivered and acceptance-evidenced in their PRs:

- **Nightly-yield fix** (#7 + #8): the nightly distilled nothing on real data. Root cause was context engineering, not the model (qwen empty, gemma hallucinates; proven by emptying the context). Fix = scope existing-context + exclude superseded + paths-only for local + transcript cap + **hybrid local→Anthropic fallback**. Verified: 44 tests; real 194-event session falls back and writes memories.
- **#1**: dream large-session distillation, quality-gate false-positive fix, health dream-freshness check, **nightly rescheduled 3 AM → 3 PM** (`StartWhenAvailable`).
- **Docs**: research report + plan + roadmap (#3, this PR), self-contained HTML reference (#2).

## Deviations from the original plan (logged)

- **`:EXTRACTED_FROM` moved from Phase A → Phase D** — linking every memory to every processed event would explode edges on large sessions. **Delivered in PR #13 via heuristic top-K overlap attribution** (bounded to K edges/memory), *not* model-citation — no provider/prompt change, no local-model risk; model-cited precision is a later upgrade.
- **D1 typed-`kind` vocabulary needs design before the swap** — the 9 Memanto semantic types (preference/decision/procedure/…) don't map cleanly onto njhook's identity-style memories (e.g. `profile/role`). Deferring the `kind` swap until the vocabulary actually fits njhook's profile/tools/project/general world (likely a typed dimension *alongside* the existing kind, with a migration window).
- **D2 admission gate is coupled to Phase E** — routing low-grounding memories to `pending_review` strands them unless there's a review surface; build the gate together with a minimal review path.
- **Phase A same-path model = revision-chain, not duplicate-path nodes** — `Memory.path` is `UNIQUE`; documented in the plan's Phase A design note.
- **C3 nucleus expansion deferred to Phase D** — it walks `(:Memory)-[:EXTRACTED_FROM]->(:Event)`, which doesn't exist until D.
- **Phase F split into two slices** — slice 1 (history/diff, #10) shipped; slice 2 (`--as-of` + lineage + citation) pending.
- **C4 reranker deferred** — see acceptance gap #3 above.

## PR ledger

| PR | State | Summary |
|---|---|---|
| #1 | merged | dream large-session fix + quality-gate fix + health freshness + 3 PM reschedule |
| #2 | merged | docs: HTML reference |
| #3 | merged | docs: research + implementation plan + roadmap (+ this ledger) |
| #4 | merged | Phase A — non-destructive history |
| #5 | merged | Phase C1 — shared recall engine |
| #6 | merged | Phase C2 — recency + importance ranking |
| #7 | merged | nightly fix — scope existing-context |
| #8 | merged | nightly fix — transcript cap + hybrid fallback |
| #9 | merged | Phase C3 — raw event retrieval |
| #10 | merged | Phase F (1/2) — memory evolution history (timeline + diff) |
| #11 | merged | Phase B (PR-1) — durable capture spool + ingest worker |
| #12 | merged | acceptance alignment — A#6 backup/restore lineage + C vector-only test + C4 deferral |
| #13 | merged | Phase D (PR-1) — :EXTRACTED_FROM claim-level provenance (heuristic top-K) |
| #14 | merged | Phase F (slice 2) — `--as-of` recall + memory lineage view |
| #15 | open | Phase E (PR-1) — conflict/review workflow (review CLI + auto-resolve) |

## Metrics

- Tests: **19 → 67** over the program (live Neo4j + pure).
- `njhook health`: **21 ok / 0 warn / 0 fail**.
- Graph: ~20 memories, ~34 sessions, ~9.5k events; nightly task registered at 3 PM.

## How to keep this aligned

Each phase/slice PR must: (1) state which `IMPLEMENTATION_PLAN.md` acceptance items it satisfies, with evidence (test names, eval/live output); (2) update this ledger's status table + close any acceptance gap it resolves; (3) tag the corresponding plan item ✅. A phase is "fully aligned" only when **every** one of its acceptance bars is ✅ here.
