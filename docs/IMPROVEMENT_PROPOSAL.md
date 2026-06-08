# njhook Deep-Review Roadmap: Memory Generation, Maintenance & Recall

> **Status:** Proposal only — no code. Every item below survived adversarial, code-grounded critique; already-implemented and false-premise proposals were removed. Effort/impact are corrected against the actual codebase, and overstated claims (invented commands, "free reuse", inflated impact) are flagged in each entry.

---

## Executive Summary

njhook's universal-memory program (Phases A–H) is functionally complete, and the review confirms the core is sound. What survives the critique clusters around one structural theme: **the system extracts well but maintains weakly**, and that weakness is concentrated on (a) the local-model nightly path and (b) maintenance jobs that exist but are never scheduled.

The cheapest, highest-leverage wins are in the **Now** tier: make the most-used recall surface rank-aware, fix the degenerate importance distribution, schedule the dedup/archive jobs that already exist, and turn on the contradiction detector that ships off. Everything heavier is sequenced behind those.

### What's already strong (acknowledged, not re-proposed)
- **Non-destructive bi-temporal supersession** + audit/revision chain.
- **Hybrid recall**: fulltext (with OR-term fallback) + vector, RRF-fused, project-boosted, importance × recency ranked.
- **Admission gates**: token-overlap grounding + anti-poisoning (thin/novel directive quarantine).
- **15-type kind vocabulary** enforced via constrained decoding.
- **Eval harness**: deterministic distillation scorer **and** vector/RRF/recency retrieval regression coverage are **already CI-gated** (the critique explicitly rejected proposals to "add" these — they exist).
- **Local-first provider stack**: llama.cpp primary, Anthropic fallback on 0-yield.

### Honesty caveats threaded through this document
1. **This is a single-user local tool.** Most "scale" and "security" framings are *preventive / defense-in-depth*, not active fires — which is why several superficially-attractive items land lower than their prose implied.
2. **Several proposals oversold reuse or invented integration points.** There is no `njhook prune` command; `:DreamRun` is per-session-write (not per-nightly) and is skipped on zero-yield sessions; the write path already runs a vector-neighbour scan behind a flag. Estimates below correct for this.

---

## Prioritized Table

| # | Proposal | Theme | Effort | Impact | Tier |
|---|----------|-------|:------:|:------:|:----:|
| 1 | Rank-aware session-start bucket fetch | Retrieval | S | med | **Now** |
| 2 | Anchored salience rubric + kind-prior floor | Generation | S | med | **Now** |
| 3 | Schedule consolidate + archive into nightly | Dreaming | S | med | **Now** |
| 4 | Default-on contradiction detection + fulltext channel | Update/Conflict | M | med | **Now** |
| 5 | Event tier-down (blank heavy fields on dreamed sessions) | Storage | M | med | Next |
| 6 | Opt-in write-time dedup on existing vector finder | Generation | M | med | Next |
| 7 | Bi-temporal as-of recall replay | Update/Conflict | M | med | Next |
| 8 | Per-nightly run-ledger node + dream-stats + health | Observability | M | med | Next |
| 9 | Consolidation kind/project stamping (part 1) | Update/Conflict | S | low | Next |
| 10 | Update admission gates (non-destructive) | Update/Conflict | M | med | Later |
| 11 | Distillation precision eval + kind fixtures | Generation | L | med | Later |
| 12 | Decouple recency decay from access freshness | Retrieval | S | low | Later |
| 13 | Token/cost capture into run ledger | Observability | M | med | Later |
| 14 | Recall-effectiveness telemetry | Observability | M | med | Later |
| 15 | Storage-accounting view | Storage | S | med | Later |
| 16 | Per-collision local UPDATE-with-body merge | Dreaming | L | med | Research |
| 17 | Reflection/synthesis layer | Dreaming | L | med | Research |
| 18 | Critique pass (claim verification) | Dreaming | M | med | Research |
| 19 | Hierarchical rolling summarization | Dreaming | L | low | Research |
| 20 | Corroboration promotion + graduated decay | Update/Conflict | L | low | Research |
| 21 | Embedding storage reduction | Storage | L | low | Research |
| 22 | Provider A/B eval matrix | Observability | M | low | Research |
| 23 | MemoryRevision chain compaction | Storage | M | low | Research |
| 24 | Salience/batched dreaming (--max-sessions only) | Dreaming | M | low | Research |

---

## Tier: NOW (high leverage, low/med effort)

### 1. Rank-aware session-start bucket fetch
- **Problem.** Session-start injection is the single most-used recall surface (fires on every SessionStart, before any prompt exists) yet the least intelligent path. `fetch_bucket`/`fetch_project` (`hooks/recall.py:261,277`) do `ORDER BY coalesce(m.updated_at,'') DESC LIMIT $limit` — a pure-recency cut **at the DB**. Only afterward does `render_session_start` re-sort the returned ≤5 rows by value-density (`recall.py:351`). A high-importance, slightly-older memory ranked 6th by recency is never fetched and can never be injected.
- **Proposal.** Adopt **option (b) only**: over-fetch a candidate pool (`raw_limit = max(limit*OVERFETCH, limit+5)`), apply the existing `value_density()` in Python, slice to `limit`. **Reject option (a)** (in-Cypher decay) — it duplicates `_lambda_for`/`recency_factor` and reintroduces the drift the repo deliberately centralizes in Python.
- **Grounding.** `recall.py:261,277` (recency-only DB cut); `recall.py:351` (value_density only on returned rows); `recall.py:171,206,381` (the over-fetch idiom the prompt path already uses — this surface is the lone exception); `docs/UNIVERSAL_MEMORY_RESEARCH.md:134` item 9 lists this as a planned win.
- **Effort/Impact.** **S / med.** Impact is med not high: with `limit=5` and a 4000-char budget, the char budget is often the binding constraint at render, so the loss is a tail case. Honest total scope (env gate + a *new* bucket eval fixture — `eval_retrieval.py` covers only `prompt_query` today) is borderline S/M.
- **Prior art.** BudgetMem token-value-density ordering (already the project's chosen model); standard two-stage retrieve-then-rank.

### 2. Anchored salience rubric + deterministic kind-prior floor
- **Problem.** Importance is a two-place ranking signal (the 0.2x–2.0x `importance_factor` multiplier **and** value-density budget order) generated by a single vague, *optional* prompt line ("an integer 1-10 … Omit if unsure", `prompts.py:70-71`). On the 12B local model this produces a degenerate distribution (everything at 7-8 or omitted → flat default 5). No kind-aware prior exists, even though kind strongly predicts durability.
- **Proposal.** (1) Replace the one-liner with an **anchored rubric** (3-4 band→example mappings) and make importance **required** in `DREAM_JSON_SCHEMA`. (2) Add a deterministic **kind-prior floor** in `_coerce_importance` (constraint/preference→7 … artifact→3) used when the model omits importance, reusing the kind already normalized in the same row dict; model-supplied values always win.
- **Grounding.** `prompts.py:70-71,149,151`; `dream.py:357-366` (clamp-only); `dream.py:512-514` (kind available at coercion site); `recall.py:63,92-121`. Resurrects `IMPLEMENTATION_PLAN.md:117` (D2 content-type prior — designed, never built).
- **Effort/Impact.** **S / med.** "Required" only binds on the schema-constrained local path (the frontier `json_object` path doesn't enforce it) — coherent with the target, since the degenerate distribution is a local-model problem. Add dream-side tests for the new branch (none exist).
- **Prior art.** Anchored-rubric LLM-as-rater calibration; A-MEM/Memanto content-type durability weighting.

### 3. Schedule consolidation + archival into the nightly
- **Problem.** `run_dream.cmd:22` runs only `python dream\dream.py --since 36h`. `consolidate()` and `archive()` are complete, tested, and CLI-exposed, but **no scheduler invokes them** — graph hygiene depends on a human remembering `njhook consolidate`. The proposal correctly identifies that `--consolidate`/`--archive` are *mutually exclusive* with distillation (early `return` in `dream.py`), so they need separate chained invocations.
- **Proposal.** Extend `run_dream.cmd` into a multi-stage nightly: (1) distill; (2) consolidate (conservative threshold, capped rounds); (3) archive — each stage logged separately. **Ship the chaining now (S).** Defer importance-weighted triggering to an M follow-up: it needs a new-memory count `dream.py` doesn't currently emit machine-readably.
- **Grounding.** `run_dream.cmd:22`; `consolidate.py:102` (consolidate), `:191` (archive); `cli/njhook.py:819,841`; health watches only `njhook-dream-nightly`.
- **Effort/Impact.** **S / med.** Impact bounded by present scale (~20 memories / ~34 sessions) — preventive hygiene insurance, not an active fire. Note task registration is a manual README PowerShell snippet (no register-task CLI), so the second-cadence entry is a runbook edit.
- **Prior art.** Letta sleep-time agents; Zep/Graphiti background maintenance; Mem0 async consolidation.

### 4. Default-on contradiction detection + fulltext candidate channel
- **Problem.** Contradiction detection ships **off**: `dream.py:636` gates on `DREAM_CONTRADICTION_CHECK`, which the nightly never sets — so the default schedule **never detects conflicts**. It also only inspects new-vs-existing pairs, and candidates come only from cosine neighbours above 0.85, missing antonym-style contradictions ("deploy via Docker" vs "never containers") that score low on cosine.
- **Proposal.** **(a)** Default-on in the nightly (effectively a one-liner). The judge is conservative-by-failure (`judge.py:8-11`, returns False on any doubt), so enabling can only **miss** contradictions, never wrongly quarantine — bounded downside. **(c, fulltext slice)** Add a fulltext second channel (reuse `recall.fulltext_search`) so low-cosine contradictions still surface as candidates; the LLM judge stays the precision gate. **Defer** the all-pairs full-scan (b) and the 0.78 cosine drop — both multiply LLM calls; ship them as a separate cost-bounded follow-up with an explicit per-run pair cap.
- **Grounding.** `dream.py:636,639`; `run_dream.cmd:22`; `review.py:173`; `judge.py:8-11`. Default provider is local (`DREAM_PROVIDER=llamacpp`), so default-on stays local; the `egress_blocked` gate covers the remote-fallback case.
- **Effort/Impact.** **M / med.** Impact med not high: single low-volume user, manual `njhook review flag`/`auto-resolve` already exist as a backstop — this activates a dormant safety net.
- **Prior art.** Graphiti/Zep continuous edge-invalidation on contradiction.

---

## Tier: NEXT

### 5. Event tier-down: blank heavy text fields on dreamed, aged-out sessions
- **Problem.** Events are the dominant unbounded storage cost — write-once, read-by-dream-once, ~4KB `tool_response` each (`log_event.py:40`), never pruned. After the watermark advances (`dream.py:528-531`), the heavy text is never read again except by opt-in event-fulltext recall.
- **Proposal.** A `njhook prune-events` command (scheduled alongside the nightly) operating only on Events whose Session has `last_dreamed_at` set AND timestamp older than `EVENT_RETENTION_DAYS`. **Tier-1 (default):** REMOVE `tool_response`/`tool_input`/`transcript`/`last_assistant_message`, set `tiered=true`, **keep** the node + ids + `FIRST/NEXT/LATEST` chain so lineage survives. **Tier-2 (opt-in, longer window):** `DETACH DELETE` whole dreamed chains, mirroring the proven backup-wipe Cypher.
- **Grounding.** `log_event.py:40,90-197`; `dream.py:186-190` (re-dream only reads events *past* the watermark — so blanking pre-watermark text is safe); `cli/njhook.py:1286-1291` (wipe pattern to mirror). Roadmap lists "raw event retention policy" as unbuilt.
- **Effort/Impact.** **M / med.** Decide explicitly whether `prompt` is in the blanked set (it feeds `event_fulltext` at `schema.py:52` and lineage snippets) — tier-1 *degrades* event recall, doesn't break it. Update stats to report tiered vs full counts. Preventive (graph is small today), so sequence after the Now tier.
- **Prior art.** Letta/MemGPT recursive summarization + eviction; Zep session-summary-then-evict.

### 6. Opt-in write-time semantic dedup on the existing vector finder
- **Problem.** No semantic dedup at write time: local models see existing memories as **paths only** (`dream.py:316-324,760`), so a duplicate emitted at a different path becomes a new active node until the manual consolidate pass.
- **Proposal.** Add a pre-write near-duplicate check that **reuses the per-candidate vector-neighbour scan already wired into `write_memories`** (`review_mod.vector_candidates`, gated behind `DREAM_CONTRADICTION_CHECK`). On a high-confidence hit, rewrite the candidate's path to the neighbour's so the existing MERGE-on-path turns it into an in-place update. **Honesty corrections baked in:** (1) this is a second consumer of an existing finder + a path-rewrite branch, *not* new infra; (2) it is an **overwrite-redirect, not a merge** (the new body replaces the old, prior snapshotted to MemoryRevision); (3) ship **default OFF**, env-gated like the contradiction check; (4) use a **high** threshold (0.97+ and/or same path-prefix/kind), since this is a blind unsupervised overwrite; (5) de-dupe redirects **within** the write batch to avoid intra-UNWIND clobber.
- **Grounding.** `dream.py:491-495,561,636-644`; `review.py:173-221`; `consolidate.py:102` (offline-only merge, threshold 0.92).
- **Effort/Impact.** **M / med.** Local-provider-only gap (frontier providers already get full bodies + merge instructions). Duplicates only accumulate between consolidations, which the scheduled consolidate (item 3) will also catch.
- **Prior art.** Mem0 ADD/UPDATE/NOOP at write time; Graphiti/Zep ingest-time entity resolution.

### 7. Bi-temporal as-of recall replay
- **Problem.** `valid_from`/`valid_until` are written on supersession (`review.py:108`) and consolidation (`consolidate.py:176`) but **no recall query reads them**; `_active()` filters status only. `content_as_of()` reconstructs from the MemoryRevision chain, not the temporal window. Acceptance bar #1 promised `njhook recall --as-of` but only `njhook history --as-of` shipped.
- **Proposal.** Thread an optional `as_of` timestamp through the recall plan; when set, append a temporal predicate to `_active()`. On the dream-write path, stamp the prior generation's `valid_until` when content materially changes at an existing path. Expose `njhook recall --as-of <ts>` and a dashboard time-slider.
- **Grounding.** `recall.py:56-59,449-458`; `review.py:108`; `consolidate.py:176`; `dream.py:583`. A `valid_until` index is genuinely missing (`schema.py` indexes status only).
- **Effort/Impact.** **M / med** (borderline L). **Sell as a new replay feature, not a correctness fix** — supersession is atomic, so no stale-active node exists today. The dream-write `valid_until` boundary stamping (without disturbing the in-place-update + revision model) is the non-trivial part.
- **Prior art.** Graphiti/Zep four-timestamp bi-temporal model.

### 8. Per-nightly run-ledger node + `njhook dream-stats` + one health signal
- **Problem.** Run health is invisible: `:DreamRun` carries only `{run_id, ts, provider, model}` (`dream.py:557-558`), all counters print to a swallowed text log, and health only greps for `exit=0` (`cli/njhook.py:1699-1705`). A run that distilled 0 memories or fell back on every session looks identical to a perfect run.
- **Proposal.** **Redesign vs the original (which had a fatal grain error).** `:DreamRun` is **per-session-write** and is **skipped entirely on zero-yield sessions** (early return before the MERGE) — so bolting counters onto it cannot see the all-zero-yield failure it's meant to catch. Instead create a **new per-nightly run node**, written **unconditionally** in `main()` (with a `finally`-flush of counts-so-far on mid-loop crash), carrying `sessions_seen/with_yield/fallback_fired/candidates/rejected/routed_grounding/routed_poisoning/written/duration_ms`. Add `njhook dream-stats`, a dashboard tab, and a WARN on zero-yield or high reject-rate.
- **Grounding.** `dream.py:533-534` (zero-yield early return), `:557-558,802-823`; `cli/njhook.py:1699-1705`. Counters live in `validate_batch` / `write_memories` / the main loop — threading them out changes the `write_memories` return signature, so this is **real plumbing, not one SET**.
- **Effort/Impact.** **M / med.** Note health check #10 (`dream freshness`) already partially covers "produced nothing lately" at the graph level.
- **Prior art.** dbt `run_results.json`; Airflow TaskInstance ledger; OpenLLMetry GenAI spans.

### 9. Consolidation kind/project stamping (part 1 only)
- **Problem.** Consolidation's merge Cypher (`consolidate.py:152-162`) never sets `m.kind` or `m.project`; a merged node at a new path is untyped/unscoped.
- **Proposal.** **Part (1) only:** apply the same `normalize_kind(parse_kind(content))` + M3 project CASE the dream writer uses (`dream.py:512-516,587-591`) — pure-additive, low-risk Cypher. Also add the **one-line status guard** to the consolidate candidate query so superseded-but-unarchived nodes aren't re-merged. **Drop part (2)** (path-enum + retry + max-importance) — it over-engineers an edge case off the automated path and needs new plumbing (`_fetch_pair_candidates` doesn't select importance).
- **Grounding.** `consolidate.py:53-58,152-162`; contrast `dream.py:512-516,587-591`. **Rationale correction:** recall has **no kind filter** — `m.kind` is read only cosmetically by `njhook stats`; dashboard/CLI facets bucket by *path prefix*. So the "recall mis-handles" justification is false and null-project (not null-kind) is the real, narrow consequence. `migrate-kinds` already self-heals null kind on next run.
- **Effort/Impact.** **S / low.** Consolidation is manual/opt-in (until item 3 schedules it), and the bug only fires when the LLM invents a third path.
- **Prior art.** Internal consistency with the dream writer's own stamping.

---

## Tier: LATER

### 10. Apply admission gates to UPDATES of existing active memories (non-destructive)
- **Problem.** Both gates exempt updates to existing-active paths (`dream.py:446,464-465`), opening an adversarial-update bypass.
- **Proposal.** Gate updates **non-destructively**: when a candidate targets an existing path AND is BOTH low-overlap AND trips a gate, keep the existing body active and write the suspicious body as a **sibling `pending_review` proposal** (reuse `flag_new_contradiction` asymmetry). Record the gate reason on the revision (audit-reason-on-revision is independently valuable and cheaper).
- **Grounding.** `dream.py:446,464-465,428-429,459-460`; `review.py:134-152`. **Correction:** the headline rm-rf example is partly misdiagnosed — grounding is *not* a malice oracle (`quality.py:60-62`), so the **poison gate's update-exemption is the real, narrow hole**, not grounding.
- **Effort/Impact.** **M / med** (low end). Single-user tool — the realistic threat is a prompt-injected coding session, not a multi-tenant attacker.
- **Prior art.** Anti-memory-poisoning update-channel-attack literature.

### 11. Distillation precision (anti-noise) eval bar + expanded kind fixtures
- **Problem.** The distillation scorer measures only recall-side quality — a provider emitting 10 noisy memories to cover 2 facts scores a perfect pass. The "prefer fewer, sharper memories" rule is unmeasured, and the golden set covers only 3 of 15 kinds.
- **Proposal.** Add a `noise_rate`/count-discipline metric to `score()` operationalizing the unmeasured rule, and expand the 2-session golden set toward the other 12 kinds with **narrow negative expectations** (ephemera the distiller should skip). **Drop** the self-consistency mode (can't be CI-gated; local nondeterminism is already known). **Drop** the "make `score()` CI-gated" framing — it **already is** (`tests/test_eval_distillation.py`).
- **Grounding.** `eval_distillation.py:44-65,104-119`; `prompts.py:93,103-120`.
- **Effort/Impact.** **L / med.** The metric is small; authoring ~8-10 fixtures across 12 kinds with curated expected/negative token sets AND matching candidate fixtures is the substantial work.
- **Prior art.** LoCoMo / LongMemEval precision+recall memory benchmarks.

### 12. Decouple recency decay from access-driven freshness (gated)
- **Problem.** `recency_factor` anchors on `last_accessed_at` first, which `_bump_access` resets on **every injection** — a popularity feedback loop where injected memories stay "fresh" regardless of content currency.
- **Proposal.** Preferred variant: **content-currency anchor + a small bounded usage-recency multiplier** (matches the project's endorsed bi-temporal split), gated behind `RECALL_RECENCY_ANCHOR` until a stale-but-popular eval fixture validates the flip.
- **Grounding.** `recall.py:108-110,113-121`; `inject_memory.py:81-100`. **Note:** `last_accessed_at` is also the **archive cutoff anchor** (`consolidate.py:206`) where access-freshness is arguably correct — keep that path stable.
- **Effort/Impact.** **S / low.** Impact corrected to low: 180d profile/tools half-lives largely absorb the reset, and session-start is pre-ordered by `updated_at` anyway, so real exposure is narrow (prompt_context hybrid recall).
- **Prior art.** Graphiti/Zep transaction-time vs valid-time split; recommender popularity-bias literature.

### 13. Capture provider token usage + estimated cost into the run ledger
- **Problem.** Every provider call discards SDK usage data (`providers.py:53-54,72-73`), so the local-vs-fallback cost tradeoff — the system's explicit design tension — is unmeasurable.
- **Proposal.** Change the provider contract to return `(memories, usage)` parsed from objects already in hand; store on the run-ledger node (item 8); add a static price table and a rolling-cost WARN. Prefer an **additive/backward-compatible** signature to avoid breaking the eval harness in one shot.
- **Grounding.** `providers.py:53-54,72-73,212-270`; `run_dream.cmd:15-19`. Contract change fans out to `call_provider`, `safe_distil`, the per-session loop, the DreamRun write, and `eval_distillation.py:139`.
- **Effort/Impact.** **M / med.** Impact med not high: nightly is local-by-default so the dollar figure is usually $0; the real value is the **prompt-size regression signal**. The claimed sibling "dreamrun-telemetry" proposal is item 8 here.
- **Prior art.** LiteLLM/Helicone cost ledgers; Anthropic SDK usage object.

### 14. Recall-effectiveness telemetry: never-injected cohort + decayed-recency view
- **Problem.** `access_count` is bumped on **return** (`inject_memory.py:81-100`), conflating deterministically-injected with actually-useful; there is no signal for never-surfaced dead-weight memories — the input a pruning decision needs.
- **Proposal.** Ship the two **non-redundant** signals: a never-injected cohort (`access_count=0` AND aged) and an effectively-dead-but-not-archived view (recency below floor). **Skip** rebuilding the pending_review backlog view — the `/review` tab already lists it with approve/reject. Worklist-only, no auto-archive.
- **Grounding.** `inject_memory.py:81-100`; `dashboard/app.py:613-616,628-632`; `recall.py:103-114,305-313`. **Correction:** pending_review is NOT "stranded forever" — `/review` (`app.py:520-570`) already surfaces it.
- **Effort/Impact.** **M / med** (M-L). Adding `injection_count` distinct from `access_count` is a real schema change, not one SET, and must not perturb the `last_accessed_at` ranking anchor. Never-injected cohort is only accurate going forward.
- **Prior art.** Feature-store freshness/usage dashboards; recommender impression-vs-engagement splits.

### 15. Storage-accounting view (`njhook storage` / stats byte breakdown)
- **Problem.** `stats` reports counts only; there is no way to see **where** bytes accumulate, so every pruning decision is blind.
- **Proposal.** Cheap read-only aggregations (text-field length sums per label, embedding-byte estimate, top-N sessions by event bytes, **dreamed-but-un-pruned reclaimable bytes** as the headline trigger metric for item 5). Keep the byte scan in an on-demand subcommand, not on the routinely-invoked health path. Label all numbers as estimates.
- **Grounding.** `cli/njhook.py:1810-1848` (counts only), `:1474-1486` (health watches DLQ rate only); `log_event.py:122-133`.
- **Effort/Impact.** **S / med.** **Explicitly contingent:** reclaims nothing alone — value is entirely as scaffolding for the event tier-down. Adopt as a bundle with item 5.
- **Prior art.** Postgres `pg_total_relation_size`; observability-before-optimization.

---

## Tier: RESEARCH BETS (high ambition / high risk / overlaps existing machinery)

### 16. Per-collision local UPDATE-with-body merge (subset of Mem0 ADD/UPDATE/DELETE/NOOP)
The load-bearing 20%: for local providers, on a path collision, make **one** per-pair LLM call that includes the prior body so the model can actually merge (today a same-path local UPDATE silently clobbers the accumulated body — recoverable via MemoryRevision, so fidelity-regression not data-loss). **Drop DELETE** — a flaky local model auto-retracting an active memory directly contradicts the project's resolved non-destructive design and is the exact poisoning vector the gates resist. NOOP/ADD are near-zero value. `dream.py:760,313-324,561-575`. **L (full pipeline) / med.**

### 17. Reflection/synthesis layer (N→1 abstraction with `:REFLECTS_ON` provenance)
Genuine novelty is LLM synthesis of higher-order insights from memory clusters. But the baseline is misrepresented: `consolidate.py` already does cross-session vector-merge, and `detect/patterns.py` already covers ~2 of the 3 motivating examples deterministically. Real delta is incremental and high-effort (new edge + schema migration + backup round-trip + eval fixtures) with a real platitude/error-compounding drift risk. `dream.py:786-823`. **L / med.**

### 18. Critique pass between extract and write (claim verification)
`grounding_score` is bag-of-tokens overlap that the code itself documents as unable to catch subtle errors (a wrong port whose digits appear in the transcript scores ~1.0; default floor 0.10). An opt-in `DREAM_CRITIQUE` LLM call re-reads candidates against the bounded source, copying `judge.py`'s conservative-by-failure pattern. **Cannot be validated until new accuracy-specific eval fixtures exist** — those are the real prerequisite. `quality.py:60-62`; `dream.py:430-453`. **M / med.**

### 19. Hierarchical rolling summarization for over-budget long sessions
Long sessions are handled by lossy truncation. **But the headline double-truncation conflict is moot in production** (`n_ctx=32768` → ~98K-char budget >> the 16K cap), and the 0-yield fallback already re-dreams worst cases on a frontier model with the full transcript. **The cheap alternative the proposal ignores:** derive `DREAM_TRANSCRIPT_MAX_CHARS` from `n_ctx` (or raise it) — most benefit, no per-window LLM calls. Map-reduce is L with unproven quality benefit. `dream.py:271-310`; `providers.py:243-246`. **L / low.**

### 20. Corroboration-based promotion + graduated decay (decay-on-contradiction slice only)
No auto-exit from pending_review; binary time-only archival. **But** part (b) graduated decay largely **duplicates** the shipped per-kind `recency_factor` + access reinforcement; part (a) annealing **attacks the anti-poisoning gate** it would auto-promote past, on provenance that is approximate/not-persisted. Survivable slice: decay-on-contradiction as a multiplier reusing existing `CONTRADICTS` edges, **without** auto-promotion. `consolidate.py:191-232`; `recall.py:103-114,247`. **L / low.**

### 21. Embedding storage reduction (Matryoshka dimensions; deferred int8)
Embeddings stored as float64 lists, duplicated in the HNSW index, retained on superseded nodes. The clean 3x lever (OpenAI `dimensions`) **does not apply to the default nomic-embed**. int8 needs Neo4j 5.18+ (no version floor in the repo) and is L. **Cheaper independent win: REMOVE `m.embedding` on supersession.** Defer until the corpus is large or OpenAI is in use; gate lever (a) on `EMBED_PROVIDER=openai`. `dream.py:592-596`; `embeddings.py:36-44`. **L / low.**

### 22. Provider A/B eval matrix (latency-only first; cost descoped)
`eval_distillation` runs one provider per invocation. The multi-provider loop + JSON persistence is sound **if cost is descoped** (token capture is unbuilt; latency is standalone). But oversold: the real fallback is **0-yield-triggered, not quality-delta-triggered**, and n=2 toy fixtures cannot answer the central operational question. `eval_distillation.py:123-147`; `providers.py:316-321`. **M / low.**

### 23. MemoryRevision chain compaction
Every content change appends a full prior-body snapshot. **But** impact is overstated: re-dream is idempotent on unchanged content (no revision) and most audit-path revisions carry `content_snapshot=None`. The proposed `njhook prune` host command **does not exist**, and dropping old snapshots defeats `content_as_of()`/`memory_history` — the feature the chain exists to provide. Also interacts with restore's `{ts, content_snapshot}` MERGE key. `dream.py:566-575`; `audit.py:55-64`. **M / low.**

### 24. Salience-triggered + batched dreaming (`--max-sessions` backlog guard only)
`fetch_events` returns all qualifying sessions with no LIMIT/ordering, so a backlog dreams hundreds in one run. The valid, cheap slice is a `--max-sessions` guard with resume-from-remainder. **Drop the salience scoring/floor:** the watermark already advances on 0-yield sessions (trivial sessions dreamed at most once, on a free local model), making the floor near-worthless. **The cited "system map" grounding for this item is fabricated.** `dream.py:167-172,786,528-531`. **M / low.**

---

## Sequencing notes
- **Bundle** item 15 (storage view) with item 5 (event tier-down) — the view is non-actionable alone.
- **Item 13** (token/cost) depends on **item 8** (run ledger) for its storage target.
- **Items 3 + 4 + 6** compound: scheduling consolidate, enabling the contradiction judge, and the opt-in write-time dedup all attack duplicate/conflict accumulation from different angles — ship 3 and 4 first (cheap, default-safe), then 6 as the opt-in finisher.
- **Items 11 + 18 + 22** share a prerequisite: richer eval fixtures (precision/negative/accuracy-specific). Building that fixture corpus once unblocks all three.