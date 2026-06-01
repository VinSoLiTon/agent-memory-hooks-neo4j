<!--
Implementation plan derived from docs/UNIVERSAL_MEMORY_RESEARCH.md (§7 prioritized
recommendations, §8 roadmap amendments) and UNIVERSAL_MEMORY_ROADMAP.md.
Drafted 2026-06-01. Phase-gated: each phase ships as one or more PRs and requires
explicit go/no-go before the next. Acceptance bars are per-item and include negative
tests (assert the absence of the deprecated path), per house style.
-->

# njhook Universal Memory — Implementation Plan

North star: *a universal memory layer for LLMs with a human-friendly interface for tracing memory evolutions.*

This plan turns the research findings into a sequenced, dependency-ordered build. It maps every work item to concrete files, gives a numbered acceptance bar (with negative tests), and gates each phase behind explicit sign-off. Research item refs (`Q1`, `F4`, …) point at `docs/UNIVERSAL_MEMORY_RESEARCH.md §7`.

> **Execution status** is tracked in [`docs/PROGRESS.md`](PROGRESS.md) (status table, acceptance evidence, open acceptance gaps, deviations). Per-phase status tags appear under each heading below — ✅ done & merged · 🔵 in progress / open PR · ⏸ deferred · ⬜ not started. **The program's overall acceptance bar is: every phase's acceptance bar below met (tests + live verification) and merged to `main`** — full alignment between this plan and the shipped system.

## Guiding constraints (apply to every phase)

- **Additive migration windows.** New `:Memory` properties default to `null`/`active`; legacy `{path, content}` rows keep working untouched. New node labels (`:MemoryRevision`, `:DreamRun`) and relationships are introduced alongside, never by rewriting existing nodes. Each schema change lands in `hooks/schema.py` behind idempotent `IF NOT EXISTS`.
- **Closed vocabularies at every boundary.** `status`, `kind`, `sensitivity`, `event_type`, `source_client`, `operation` are frozensets in Python, mirrored as the JSON-schema `enum` the dream provider must emit, with round-trip tests at each site (Pydantic/CLI/Cypher/dashboard VM).
- **Vocabulary evolution is guaranteed forward-compatible.** Closed sets that are expected to grow (notably `kind`) live in ONE module (`memory_types.py`) as `MEMORY_KINDS_ALL` (full superset, reserved) and `MEMORY_KINDS_ACTIVE ⊆ ALL` (what we ship now). Rules that make growth a one-line change with no migration: (a) `kind` is stored as a plain string property — validation lives only at the boundary, so **no Neo4j constraint enumerates the active set and none ever needs dropping**; (b) the provider JSON-schema `enum` is *generated from* `MEMORY_KINDS_ACTIVE`, with a test asserting `enum == frozenset` so prompt/validator can't drift; (c) round-trip tests iterate `for kind in MEMORY_KINDS_ACTIVE` so coverage auto-extends, plus a negative test (out-of-vocab rejected) and `assert ACTIVE <= ALL`; (d) legacy path-prefix memories map prefix → `kind` so neither the active set nor its expansion strands existing rows. Promoting a reserved type = moving it from `ALL`-only into `ACTIVE`.
- **Degrade gracefully.** Every new path is wrapped so a failure returns a structured result and never blocks capture, recall, or the agent session — same discipline as the existing hooks.
- **Tests pin invariants, not just behaviour.** Negative tests assert the deprecated path is gone (e.g. `DETACH DELETE` absent from `consolidate.py`; superseded memories never appear in injection output; blind `SET m.content` overwrite no longer reachable).
- **Scope-lock.** If a sub-task expands, document the decision and defer cleanly rather than blur the phase. One phase → one focused PR (or a small, named PR series).

## Program overview

| Phase | Status | Goal | Research items | Depends on |
|---|---|---|---|---|
| **A — Non-destructive history** | ✅ merged (#4) | Stop destroying memory state; seed the bi-temporal/provenance schema | Q1, Q2, Q5, F1 (data model) | — |
| **B — Durable capture** | ⬜ not started | No silent event loss when Neo4j is down | F4, Gap 1 canonical schema, Gap 8 metrics | — (parallel to A) |
| **C — Shared recall + ranking** | ✅ merged (#5,#6,#9); ⏸ C4 | One ranking engine; use the signals already stamped | F5, Q3, F7, F9 | A |
| **D — Typed memory + admission gate** | ⬜ not started | Structured records; block ungrounded dream output | F3, Gap 3 (13-type vocab), Gap 9 evals | A, C |
| **E — Conflict & review** | ⬜ not started | Contradictions can't silently become truth | F6 | A, D |
| **F — Evolution UI (north-star payoff)** | ✅ done (#10, #14, #21) | `--as-of` recall + human timeline/diff/lineage | F2, Q6 | A, C |
| **G — Universal interfaces** | ⬜ not started | Attach any LLM, not just hook-capable CLIs | F8, Gap 10 (REST/CLI/renderers) | C |
| **H — Governance & eval** | ⬜ not started | Trustworthy over months | Gap 7 egress, Gap 12 anti-poisoning, Gap 9 CI evals | B, D |

**Critical path to the north star:** A → C → F. Phases B, D, E, G, H hang off that spine. A and B are independent and can run concurrently.

---

## Phase A — Non-destructive history (the cheap north-star unblocker)

**Status:** ✅ Done & fully aligned — PR #4 (acceptance 1–5, 7 tests) + PR #12 (#6: backup/restore round-trips the new fields + `:MemoryRevision`/`:SUPERSEDED_BY` lineage, round-trip test). All 6 acceptance items met.

**Goal:** memory writes stop destroying prior state; the additive schema for time + provenance + revisions exists. This is the foundation the evolution UI (Phase F) renders.

**Design decisions (2026-06-01 review):** (i) Same-path evolution uses a **revision-chain**, not duplicate-path nodes — `Memory.path` is `UNIQUE`, so one node per path stays the "current" view and the prior body is snapshotted into a separate `:MemoryRevision` before each overwrite. `:SUPERSEDED_BY` is used only by `consolidate` (where source and merged paths genuinely differ). (ii) Claim-level `:EXTRACTED_FROM` is **deferred to Phase D** (it needs the dream provider to cite specific source events; linking every memory to every processed event would explode edges on large sessions). Phase A keeps session-granularity `:DERIVED_FROM`.

**Work items**
- **A1 — Additive schema** (`hooks/schema.py`, `cli/njhook.py migrate`). Add `:Memory` props `ingested_at, valid_from, valid_until, status, created_by` (all nullable; `status` defaults `'active'` on read via `coalesce`). New labels `:MemoryRevision`, `:DreamRun`. New rels `:SUPERSEDED_BY`, `:WROTE`, `:VERSION_OF`. New index on `(:Memory) ON (m.status)`. No backfill required.
- **A2 — Retire destructive consolidate** (`dream/consolidate.py`, `Q1`). Replace `DETACH DELETE old` with: `SET old.valid_until=$now, old.status='superseded'` + `MERGE (old)-[:SUPERSEDED_BY]->(merged)`. Keep provenance rewiring. (Sources retain distinct paths, so `path UNIQUE` holds.)
- **A3 — Non-destructive dream write** (`dream/dream.py write_memories`, `Q2`+`Q5`+`F1` data model). On divergent content at an existing path: snapshot the prior body into a `:MemoryRevision` (`:VERSION_OF` the node) and update the node **in place** (no duplicate-path node); set `status='active'`, `ingested_at`, `valid_from` (coalesce-preserve), `created_by`. Create one `(:DreamRun)-[:WROTE]->(:Memory)` per run. Identical content → no-op (no spurious revision).
- **A4 — Recall lifecycle filter** (`hooks/inject_memory.py`). All recall queries gain `coalesce(m.status,'active')='active'` (the `valid_until` window arrives with `--as-of` in Phase F).

**Acceptance bar**
1. Migration is idempotent; re-run is a no-op; existing memories load and inject exactly as before (snapshot test on injection output unchanged for active memories).
2. After a consolidate, both source memories still exist with `status='superseded'` and a `:SUPERSEDED_BY` edge to the merged node; the merged node is `active`. **Negative test:** `DETACH DELETE` string absent from `dream/consolidate.py`.
3. Re-dreaming a path with changed content snapshots the prior body into a `:MemoryRevision` (linked `:VERSION_OF`) and updates the node in place; re-dreaming identical content adds no revision. **Negative test:** no two `:Memory` ever share a path (path-UNIQUE preserved).
4. Each dream run creates one `:DreamRun` with `(:DreamRun)-[:WROTE]->(:Memory)` to every memory it wrote; existing `:DERIVED_FROM` session provenance is preserved. (Claim-level `:EXTRACTED_FROM` is Phase D.)
5. Superseded and `pending_review` memories never appear in `session_start_context` or `prompt_context` output. **Negative test** asserts this explicitly.
6. `backup`/`restore` round-trip the new fields and the `:MemoryRevision`/`:SUPERSEDED_BY` lineage.

---

## Phase B — Durable capture (reliability core)

**Status:** 🔵 In progress (PR #11). **PR-1**: append-only fsync spool (`hooks/spool.py`) + `njhook ingest` worker (`hooks/ingest.py`) with idempotent replay (the `Event.event_id` UNIQUE constraint is the inbox) + DLQ + `health` backlog row; `HOOKS_CAPTURE_MODE=spool` (default `direct`, opt-in until ingest is scheduled). Deferred to **PR-2**: canonical OTel `gen_ai.*` event schema (Gap 1), DLQ-*rate* alerting, read-time upcasting, and flipping the default to `spool`.

**Goal:** an event is never silently lost when Neo4j is unavailable. (Roadmap Gap 1 + 2.)

**Work items**
- **B1 — Canonical event + spool** (`hooks/log_event.py`, new `hooks/spool.py`, `F4`+Gap 1). Hook hot path becomes: parse → scrub → append one normalized `AgentEvent` (with `schema_version`, `app_id` defaulting to `source_client`, OTel `gen_ai.*`-aligned field names) to an append-only daily JSONL with `fsync` per record. No direct Neo4j write on the hot path.
- **B2 — Ingest worker** (new `ingest/worker.py`, `cli/njhook.py ingest`). Reads the spool, conditional-`INSERT`s `event_id` into a `processed_events` inbox before the Neo4j append, applies the existing linked-list write, marks the spool record done. Retry with backoff (5–8, jitter) → DLQ JSONL carrying payload+error+retry-count.
- **B3 — Read-time upcasting** (`ingest/worker.py`). `v1→v2→…` transformer chain applied before write; old spool records never rewritten.
- **B4 — Health + metrics** (`cli/njhook.py health`, Gap 8). Surface spool backlog, inbox size, DLQ count **and rate** (`dlq_events_per_hour`), ingest success/failure. FAIL on rising DLQ rate, not on static nonzero count.

**Acceptance bar**
1. Stop Neo4j, run sessions, restart, run `njhook ingest` → all events replay into the correct session chains in order.
2. Replaying the same spool twice is idempotent (inbox blocks the duplicate). **Negative test:** no duplicate `:Event` after double replay.
3. A malformed spool record lands in the DLQ with its error; the worker continues.
4. Hook hot-path runtime stays bounded with Neo4j down (no connection attempt on the hook path).
5. `health` shows backlog/DLQ; a backlog over threshold WARNs.
6. Legacy direct-write path remains available behind a compatibility flag during migration (roadmap requirement).

---

## Phase C — Shared recall engine + ranking

**Status:** ✅ Done & fully aligned — C1 (#5), C2 (#6), C3 (#9); vector-only fallback test added in PR #12. **C4 (cross-encoder reranker) formally deferred** — no reranker until an eval proves RRF leaves quality on the table; out-of-scope for Phase C alignment.

**Goal:** one ranking implementation reused everywhere; use the recency/importance signals already stamped but unused.

**Work items**
- **C1 — Extract `recall.py`** (`F5`). Move ranking out of `hooks/inject_memory.py` (and the dashboard's parallel logic) into `recall.query(context, mode) -> MemoryHit[]` and `recall.render(hits, budget) -> str`. `mode ∈ {session_start, prompt_context, tool_context}` (closed vocab; modes may start as thin variants).
- **C2 — Richer scoring** (`Q3`). Fuse into the existing RRF: LLM-rated `importance` (1–10, produced at dream time — small `dream.py` change), decayed recency `exp(-λ·hours_since_access)` from the already-stamped `last_accessed_at`/`access_count`, λ per-`kind`. Replace the `updated_at DESC`-then-truncate budget order with `importance × recency_decay / char_length` (BudgetMem, Gap 6/missed-9).
- **C3 — Event retrieval + nucleus expansion** (`F7`). Fulltext index on `Event.prompt`/`tool_response` + optional per-event embeddings; fuse Event hits into recall; given a hit, walk `:EXTRACTED_FROM`→`NEXT`/prev to expand context.
- **C4 — Optional reranker** (`F9`). Gated second-stage cross-encoder via HuggingFace `FlagReranker` CPU path. **Not** via Ollama (no rerank endpoint — see research §9).

**Acceptance bar**
1. `hooks/inject_memory.py`, `dashboard/app.py` search, and `njhook search` all call `recall.py`. **Negative test:** no independent ranking math remains in the dashboard/hook modules.
2. Ranking unit tests pin: project boost, archive/superseded filtering, fulltext-only fallback, vector-only fallback, hybrid fusion, recency-decay ordering, and deterministic budget truncation.
3. With importance+decay enabled, a high-importance recently-accessed memory outranks a stale equal-relevance one (fixture test).
4. Reranker is off by default and, when enabled, changes only ordering — never surfaces a `status!='active'` memory.

---

## Phase D — Typed memory + admission gate

**Status:** 🔵 In progress (#13, #16, #22). **PR-1**: `:EXTRACTED_FROM` claim-level provenance via heuristic top-K overlap attribution (`dream.attribute_events`; bounded, no explosion). **PR-2**: A-MAC grounding admission gate — `quality.grounding_score` (memory body vs source transcript); a NEW memory below `DREAM_GROUNDING_MIN` (0.10) is routed to `pending_review` (recall hides it; `njhook review` adjudicates — Phase E loop), while updates to existing-active memories are never gated (no clobber). **PR-3 (D3 retrieval eval)**: `dream/eval_retrieval.py` — a deterministic golden-set retrieval eval over the shared `recall.prompt_query` reporting hit@k + MRR; coined `qqz*` tokens isolate the fixture from any live graph and a shared-token pair exercises ranking discrimination; exposed as `njhook eval-retrieval` and gated by `tests/test_eval_retrieval.py` (fulltext-only for CI determinism: `hit@5==1.0`, `MRR≥0.75`). Partially closes acceptance #3 (retrieval half). **Remaining**: D1 typed `kind` vocabulary (deferred — the 9 Memanto types don't map cleanly to identity memories; needs design); D3 distillation eval (output-path/type quality across providers); model-cited attribution precision upgrade. (Caveat: grounding catches off-topic fabrication, not subtle factual errors.)

**Goal:** structured records; ungrounded dream output can't enter the graph. (Gap 3, 9.)

**Work items**
- **D1 — Type vocabulary** (new `memory_types.py`, `dream/quality.py`, `dream/prompts.py`). Ship `MEMORY_KINDS_ACTIVE` = the roadmap's 9 (`preference, projectrule, decision, procedure, fact, constraint, toolpattern, incident, openquestion`); reserve the rest (`commitment, goal, context, learning, observation, artifact`) in `MEMORY_KINDS_ALL`. `kind` becomes a first-class validated field (stored as a string; validated at the boundary against `ACTIVE`); markdown stays the render target. The Vocabulary-evolution guarantee above is what makes promoting a reserved type into `ACTIVE` a one-line, test-guarded change with no migration.
- **D2 — A-MAC admission gate** (`dream/quality.py`, `F3`). Before write: utility (1 LLM call) + **ROUGE-L grounding confidence** vs source events + cosine novelty + recency + content-type prior. `confidence < θ` or detected contradiction → `status='pending_review'` (advisory-only injection). The ROUGE-L check is the dream-hallucination guard.
- **D3 — Eval suites** (`dream/eval.py` + new `tests/eval/`, Gap 9). Synthetic fixtures: preference extraction, contradiction pairs (only one survives), update-vs-add fragmentation, stale archival — run across Anthropic/OpenAI/Ollama on output paths + type labels. Separate RAGAS-style retrieval eval (Precision@5/Recall@5 on golden query→path pairs). Wire into CI.

**Acceptance bar**
1. `kind` round-trips Python frozenset ↔ JSON-schema enum ↔ Cypher ↔ dashboard; an out-of-vocab kind is rejected by the quality gate.
2. A memory not grounded in source events (ROUGE-L below θ) is routed to `pending_review`, not `active`. **Negative test:** ungrounded memory absent from injection.
3. Eval matrix reports pass/fail per provider/model; CI fails on a deterministic semantic regression. *(Retrieval half ✅ — `tests/test_eval_retrieval.py` gates hit@5/MRR on a golden set, deterministic in CI; distillation half — output-path/type quality across providers — still open.)*
4. Legacy memories with only a path-prefix kind still validate (migration window).

---

## Phase E — Conflict & review workflow

**Status:** 🔵 In progress (#15, #20). **PR-1**: `hooks/review.py` engine + `njhook review list/approve/reject/supersede/flag` + the `pending_review`/`rejected` lifecycle (acceptance #2/#3/#4). **PR-2**: `detect_contradiction` engine (injected candidate-finder + judge, so the logic is unit-tested without an LLM) + `vector_candidates` (similarity finder) + `auto_resolve_all` + `njhook review auto-resolve` + the dashboard `/review` conflict view (approve/reject/supersede, write-gated). **Remaining (PR-3)**: wire a real LLM judge into the nightly (opt-in) so contradictions auto-flag at write time (acceptance #1 auto-trigger — kept out of the hot path until opted in).

**Goal:** contradictory memories are detected pre-commit and surfaced, not auto-activated. (Gap 4, F6.)

**Work items**
- **E1 — Pre-commit contradiction detection** (`dream/dream.py`/`recall.py`). Compare a new claim against semantically related active memories before writing; on contradiction, create `:CONTRADICTS` and route to `pending_review`.
- **E2 — Auto-resolution heuristic** (`dream/consolidate.py` or new `resolve.py`). For un-reviewed conflicts: `Winner = max(source_authority × recency)`, source hierarchy `user > claude_code > codex > cursor > gemini > ollama`.
- **E3 — Review surfaces** (`cli/njhook.py review list/approve/reject/supersede`; `dashboard` conflict view). Conflict view shows which events support each side via `:EXTRACTED_FROM`, with approve/reject/supersede actions.

**Acceptance bar**
1. A new memory contradicting an active one is flagged `:CONTRADICTS` + `pending_review`; the active one stays active until resolved.
2. `njhook review approve <id>` activates one and supersedes the other; the change affects recall immediately.
3. **Negative test:** `pending_review`/contradicted memories are not injected.
4. Auto-resolution picks the higher-authority/recency memory when no human acts; tested.

---

## Phase F — Evolution UI (north-star payoff)

**Status:** ✅ Done & fully aligned (#10, #14, #21). Memory history + `history --diff` + `history --as-of` point-in-time reconstruction; `memory_lineage` (source events via `:EXTRACTED_FROM` + supersession + `:CONTRADICTS`) in CLI + dashboard `/memory/<path>/history`; inline citation footer in injected context (Q6). All four acceptance bars met. The north-star "trace how a memory came to be" UI is complete.

**Goal:** the human-facing "trace how this memory evolved" experience. (F2, Q6.)

**Work items**
- **F1' — `--as-of` recall** (`recall.py`, `cli/njhook.py recall`). Filter which memories were active at `$T` (`valid_from <= $T AND (valid_until IS NULL OR valid_until > $T)`); reconstruct each one's *content* at `$T` from its `:MemoryRevision` chain (latest snapshot with `ts <= $T`, else current) — since same-path history lives in revisions per the Phase A decision.
- **F2' — Dashboard timeline** (`dashboard/app.py` `/memory/<path>/history`). Time-ordered `:MemoryRevision` + `:SUPERSEDED_BY` rows: operation, `:DreamRun` provider/model, timestamp, one-line summary.
- **F3' — Diff panel.** `difflib.unified_diff` over adjacent revisions, colored.
- **F4' — Lineage graph.** Node-link of `:EXTRACTED_FROM`/`:SUPERSEDED_BY`/`:CONTRADICTS`; click an `:Event` to jump to the raw session excerpt; as-of date picker.
- **F5' — Inline citation footer** (`recall.render`, Q6). Injection output annotates which memory paths were used.

**Acceptance bar**
1. `njhook recall --as-of <ts>` reconstructs the active set at that instant (fixture with a supersession in between).
2. `/memory/<path>/history` lists every revision in order with its `:DreamRun`.
3. Diff between two revisions renders correct +/- lines.
4. Lineage view links a memory to the exact source events; clicking reaches the session excerpt.

---

## Phase G — Universal interfaces

**Status:** 🔵 In progress (#17, #18). **PR-1**: shared `hooks/service.py` + `njhook recall`/`write-event` CLI + REST API (`api/server.py`). **PR-2**: MCP server (`api/mcp_server.py`) — 4 tools (`search_memory`/`get_project_context`/`record_event`/`propose_memory`) over the same `service.py`; `mcp` imported lazily so tools are unit-tested without the package; `propose_memory` is synchronous (not the experimental MCP Tasks primitive). All interfaces route through the same `recall.py` + `log_event` (acceptance #1/#2 — parity test). **Remaining**: G4 file renderers (AGENTS.md/CLAUDE.md/Cursor/Gemini).

**Goal:** attach arbitrary LLM runtimes over the same recall + write core. (Gap 10, F8.)

**Work items**
- **G1 — `njhook recall`/`write-event`/`write-memory`** CLI over `recall.py` + the spool.
- **G2 — REST API** (`POST /events`, `POST /recall`, `POST /memories`, `GET /health`) — thin layer over the same core.
- **G3 — MCP server** — 4-tool minimum (`search_memory`, `get_project_context`, `record_event`, `propose_memory`). `propose_memory` async via MCP Tasks (note: **experimental** spec — compatibility bet).
- **G4 — File renderers** — `AGENTS.md`/`CLAUDE.md`/Cursor rules/Gemini context from the active memory set.

**Acceptance bar**
1. Hook, CLI, REST, and MCP paths all reuse `recall.py` and the same schema validation. **Negative test:** no path bypasses validation.
2. The same query yields equivalent hits through each interface (fixture parity test).
3. `health` covers each enabled interface.

---

## Phase H — Governance & evaluation

**Status:** 🔵 In progress (PR #19). **PR-1 (H1)**: sensitivity tagging — `privacy.sensitivity_for` classifies events by cwd (`HOOKS_SENSITIVE_PATHS` / `~/.njhook/sensitive.txt`), stamped at capture; egress policy — `dream.egress_blocked` keeps high-sensitivity sessions off remote providers (remote primary skipped; remote fallback suppressed) unless `DREAM_ALLOW_SENSITIVE_EGRESS=1`; `health` reports the policy. **Remaining**: H2 audit CLI/dashboard (the `:MemoryRevision`/`:DreamRun` log already records mutations), H3 anti-poisoning / confidence annealing, H4 backup/restore-rehearsal check in health.

**Goal:** trustworthy over months of multi-agent use. (Gap 7, 12.)

**Work items**
- **H1 — Sensitivity + egress policy** (`hooks/privacy.py`, `dream/dream.py`). `sensitivity` on events/memories; dream refuses to send `high`-sensitivity events to remote providers (route to Ollama), keyed on `app_id`.
- **H2 — Audit log.** `:MemoryRevision` (Phase A) already records create/edit/supersede/archive/reject; expose `njhook audit <path>` and a dashboard view.
- **H3 — Anti-poisoning / confidence annealing** (Gap 12). High-novelty + short-source-session + rule/procedure-type candidates route to review regardless of confidence.
- **H4 — Backup/restore rehearsal check** in `health`.

**Acceptance bar**
1. A `high`-sensitivity event is never sent to a remote dream provider unless policy allows; tested with a stub provider asserting it was not called.
2. Every memory mutation is reconstructable from the audit log.
3. `health` reports policy status and restore-rehearsal age.

---

## Sequencing & first slice

- **Recommended first PR: Phase A1–A4** — additive, reversible, ~one focused PR, and it directly unblocks the north star while immediately stopping data loss in `consolidate.py`. Low risk: existing recall is unchanged for active memories.
- **Run Phase B in parallel** if durability is the bigger worry than evolution-tracing; A and B share no files except `health`.
- Then **C → F** is the straight line to the visible "memory timeline" payoff.
- D, E, G, H follow as capacity allows, each behind its own go/no-go.

## Open decisions (need your call)

1. **First slice:** Phase A (history/north-star) vs Phase B (durability) vs both in parallel?
2. **Type vocabulary size:** ~~adopt the full set now, or start with the roadmap's 9 and grow?~~ **DECIDED (2026-06-01): ship the 9 `ACTIVE` types now**, with the forward-compatibility guarantee above ensuring a one-line, test-guarded promotion path to the full reserved set.
3. **`as-of` granularity:** per-memory bi-temporal only, or also reconstruct whole-graph snapshots?
4. **Reranker / event-embedding cost:** enable on this box (16 GB VRAM) by default, or keep gated?
5. **MCP Tasks experimental risk:** build `propose_memory` on it now, or ship synchronous first and migrate?
