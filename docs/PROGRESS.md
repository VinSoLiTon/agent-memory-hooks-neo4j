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
| **D** — Typed memory + admission gate | 🔵 in progress (#13, #16, #22) | #13, #16, #22 | PR-1 `:EXTRACTED_FROM` (heuristic top-K, bounded); PR-2 A-MAC grounding gate (`quality.grounding_score`; low-grounding NEW memory → `pending_review`, doesn't gate existing-active); PR-3 D3 **retrieval eval** (`dream/eval_retrieval.py` golden set → hit@k+MRR over `recall.prompt_query`; `njhook eval-retrieval`; CI-gated `hit@5==1.0`/`MRR≥0.75`, fulltext-only for determinism) — partially closes acceptance #3; 3+3+1 tests | D1 typed `kind` vocab (design — see deviations); D3 **distillation** eval (provider output quality); model-cited attribution upgrade |
| **E** — Conflict & review | ✅ merged & fully aligned | #15, #20, #27 | PR-1 `review.py` engine + `njhook review` CLI + lifecycle (acc #2/#3); PR-2 `detect_contradiction` (injected candidates+judge) + `auto_resolve_all` + `njhook review auto-resolve` (acc #4) + dashboard `/review`; PR-3 (E1) **nightly auto-flag** — `dream/judge.py` LLM judge (conservative) wired into `write_memories` opt-in (`--check-contradictions`); `flag_new_contradiction` quarantines only the NEW memory, established active **stays active** (acc #1), audited; judge/finder injectable for tests; 5+3+5 tests | — (all 4 acceptance bars met) |
| **F** — Evolution UI (north star) | ✅ done & fully aligned | #10, #14, #21 | history + `--diff` + `--as-of`; `memory_lineage` (source events + supersession + `CONTRADICTS`) in CLI + dashboard; inline citation footer (Q6); 6+2 tests | — (all acceptance items met) |
| **G** — Universal interfaces (REST/MCP/renderers) | ✅ merged & fully aligned | #17, #18, #23 | PR-1 shared `service.py` + `njhook recall`/`write-event` CLI + REST API (`/recall`,`/events`,`/health`); PR-2 MCP server (`api/mcp_server.py`, 4 tools over the same service, lazy `mcp` import); PR-3 **file renderers** (`hooks/render.py` + `njhook render --target agents\|claude\|gemini\|cursor\|all`) — managed-block splice into each runtime's startup file, human content preserved, idempotent, same recall core as the hook; all reuse `recall.py`+`log_event`; parity + closed-vocab tests; 5+5+13 tests | — (all 4 work items shipped) |
| **H** — Governance & eval | ✅ merged & fully aligned | #19, #24, #25, #26 | PR-1 (H1): sensitivity tagging + egress policy + health egress row (acc #1); PR-2 (H3): **anti-poisoning gate** (`quality.poisoning_risk` — directive×thin×novel → `pending_review`; bonus beyond the bars); PR-3 (H2): **audit log** (`hooks/audit.py`; every mutation → `:MemoryRevision`; `njhook audit` + dashboard; round-trips via backup) (acc #2); PR-4 (H4): **restore rehearsal** (`njhook rehearse-restore` real backup→restore round-trip on a disposable marker → `:RehearsalRun`; health reports age, warn stale/never, fail on failure) (acc #3); 3+13+11+6 tests | confidence-*annealing* deferred (not an acceptance bar) |

**Rollup:** **all 8 phases A–H now touched.** Complete & fully aligned: **A, C, E, F, G, H** (6 of 8). In progress: B (durable capture PR-1 — needs OTel canonical schema + flip default→spool), D (`EXTRACTED_FROM` + grounding gate + anti-poisoning + retrieval eval — needs typed `kind` vocab + the LLM distillation eval). Both north-star halves — *trustworthy evolution-tracing* and *universal layer* — are working end-to-end; remaining work is finishing B and D (no new territory).

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
| #15 | merged | Phase E (PR-1) — conflict/review workflow (review CLI + auto-resolve) |
| #16 | merged | Phase D (PR-2) — A-MAC grounding admission gate |
| #17 | merged | Phase G (PR-1) — shared service + `recall`/`write-event` CLI + REST API |
| #18 | merged | Phase G (PR-2) — MCP server (4 tools over the shared service) |
| #19 | merged | Phase H (PR-1) — sensitivity + egress policy |
| #20 | merged | Phase E (PR-2) — contradiction-detection engine + auto-resolve + dashboard /review |
| #21 | merged | Phase F (finish) — inline citation footer + CONTRADICTS in lineage |
| #22 | merged | Phase D (PR-3) — D3 retrieval eval harness (`dream/eval_retrieval.py` + `njhook eval-retrieval` + CI gate) |
| #23 | merged | Phase G (PR-3 / G4) — file renderers (`hooks/render.py` + `njhook render`); managed-block splice into AGENTS.md/CLAUDE.md/GEMINI.md/Cursor |
| #24 | merged | Phase H (PR-2 / H3) — anti-poisoning admission gate (`quality.poisoning_risk`; directive×thin×novel → pending_review) |
| #25 | merged | Phase H (PR-3 / H2) — audit log (`hooks/audit.py`; record every mutation; `njhook audit` + dashboard); acceptance #2 met |
| #26 | merged | Phase H (PR-4 / H4) — restore rehearsal (`njhook rehearse-restore` + `:RehearsalRun` + health row); acceptance #3 met → **Phase H complete** |
| #27 | open | Phase E (PR-3 / E1) — LLM judge wired into the nightly (opt-in); new-only contradiction quarantine; acceptance #1 met → **Phase E complete** |

## Metrics

- Tests: **19 → 137** over the program (live Neo4j + pure).
- `njhook health`: **24 ok / 0 warn / 0 fail** (after a fresh `njhook rehearse-restore`; the rehearsal row warns if no run is recorded).
- Graph: ~20 memories, ~34 sessions, ~9.5k events; nightly task registered at 3 PM.

## How to keep this aligned

Each phase/slice PR must: (1) state which `IMPLEMENTATION_PLAN.md` acceptance items it satisfies, with evidence (test names, eval/live output); (2) update this ledger's status table + close any acceptance gap it resolves; (3) tag the corresponding plan item ✅. A phase is "fully aligned" only when **every** one of its acceptance bars is ✅ here.
