<!--
Generated 2026-06-01 by a deep-research workflow (njhook-universal-memory-research):
6 parallel web-research agents → 18 independently fact-checked claims → 1 synthesis.
25 agents, ~1.2M tokens, 735 web/tool calls. Findings grounded against the live
codebase and UNIVERSAL_MEMORY_ROADMAP.md. Refuted/uncertain claims are quarantined
in §9 and not relied on elsewhere.
-->

# njhook vs. the LLM-memory state of the art: gap analysis & recommendations

*Lead analyst review for the njhook author. Scope: njhook as documented + verified competitive research (mid-2026). Every benchmark or paid-tier claim that a verifier REFUTED or rated UNCERTAIN is explicitly flagged in §9 and is not relied on elsewhere. Code paths referenced below were confirmed directly against the repo: `dream/dream.py` (blind `MERGE (m:Memory {path})` + `SET m.content`, `DERIVED_FROM`/`DREAMED` at Session granularity), `dream/consolidate.py` (`DETACH DELETE old`), and `hooks/inject_memory.py` (`_hybrid_merge` RRF k=60 + `PROJECT_BOOST`, `_bump_access` stamping `access_count`/`last_accessed_at`).*

---

## 1. Executive summary

- **njhook's foundation is sound and, in several axes, ahead of the field.** Its event-sourced linked list (`(:Session)-[:FIRST_EVENT]->(:Event)-[:NEXT]->…`) is structurally the *immutable episodic ledger* that the SSGM governance framework calls the gold standard for rollback and drift correction ([arxiv.org/html/2603.11768v1](https://arxiv.org/html/2603.11768v1)). Hybrid RRF (k=60) recall, pre-write secret scrubbing, per-cwd opt-out, and four-client unification are genuine differentiators no surveyed system matches together.

- **The single biggest liability is the memory layer's destructiveness.** `dream.py` does a blind `MERGE` by path with `SET m.content`, and `consolidate.py` does `DETACH DELETE` on merged sources. Prior memory state is gone — only `consolidated_from` *path strings* survive. This is the opposite of the SOTA pattern (Graphiti, MemOS, Kumiho, Memanto), all of which **invalidate, never delete**, retaining history with validity windows ([arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1)). For a project whose north star is *"tracing memory evolutions,"* this is the defining gap.

- **Highest-leverage foundational bet: bi-temporal validity + non-destructive supersession on `:Memory` nodes, implemented natively in the Neo4j you already run.** Graphiti's four-timestamp model is verified as Apache-2.0 and self-hostable against local Neo4j 5.26+ with *no* mandatory cloud dependency ([github.com/getzep/graphiti/blob/main/LICENSE](https://github.com/getzep/graphiti/blob/main/LICENSE), [pypi.org/project/graphiti-core](https://pypi.org/project/graphiti-core/)). You can adopt the *pattern* without adopting the dependency.

- **Highest-leverage quick wins (low effort, high impact):** (a) replace `DETACH DELETE` with version closure; (b) add `EXTRACTED_FROM` edges from Memory to source Events; (c) add recency + LLM-rated importance to `_hybrid_merge` (the data — `last_accessed_at`, `access_count` — is already stamped, just unused in ranking); (d) trigger near-real-time distillation on the `Stop` hook instead of waiting for cron.

- **The roadmap's 10 gaps are well-chosen but under-specified in three places that matter:** Gap 2 says "idempotent by event_id" without naming the inbox/outbox mechanism; Gap 3 conflates *intent* fields (`valid_from`/`valid_until`) with *transaction-time* state and omits `ingested_at`; Gap 9 names "semantic eval" without a contradiction/duplicate test suite or admission gate.

- **The roadmap MISSES several concrete, implementable SOTA ideas:** bi-temporal "as-of" recall, edge-level invalidation, an A-MAC-style five-dimension admission gate (ROUGE-L grounding catches dream hallucinations), cross-encoder reranking, decay-weighted scoring, claim-level provenance, a `MemoryRevision` audit log, and OTel `gen_ai.*` schema alignment.

- **Build-vs-buy is decisively "build the patterns, on your own Neo4j."** Zep Community Edition is discontinued; Graphiti OSS is an *engine, not a platform* (no auth/dashboard/health) and its headline 18.5% benchmark gain is measured only against full-context and session-summary baselines — a well-tuned hybrid like njhook's plausibly matches it ([arxiv.org/html/2601.01280](https://arxiv.org/html/2601.01280), [emergence.ai/blog/sota-on-longmemeval-with-rag](https://www.emergence.ai/blog/sota-on-longmemeval-with-rag)). The graph-substrate overhead is not self-justifying; njhook's retrieval foundation is already competitive.

- **Treat all cross-vendor benchmark rankings as noise.** There is no neutral leaderboard for LongMemEval, LoCoMo, or ConvoMem; at least five vendors each self-report "#1" using different backbone models, making rankings incomparable ([vectorize.io/articles/mempalace-benchmarks](https://vectorize.io/articles/mempalace-benchmarks)). Adopt *architectural* lessons, not score claims.

---

## 2. Competitive landscape

| System | Substrate | Memory model | Temporal / versioning | Conflict handling | Human UI | Licensing |
|---|---|---|---|---|---|---|
| **njhook** | Neo4j (event linked list + path-keyed Memory) | Markdown blobs, type implicit in path prefix | None on memories (`updated_at`/`archived_at` only); event chain is append-only | **Blind MERGE by path; `DETACH DELETE` on consolidate** | Read-only Flask dashboard (session→memory provenance, RRF scores, access counts) | OSS, local, single-user |
| Graphiti / Zep | Neo4j / FalkorDB / Kuzu / Neptune | Typed entity + relationship edges; 3 subgraphs (episodic/semantic/community) | **True bi-temporal: `valid_at`/`invalid_at` + `created_at`/`expired_at` per edge** | LLM contradiction detection at ingest; non-destructive invalidation | Graphiti: none (engine). Zep cloud: graph viz + debug logs | Graphiti Apache-2.0 (self-host); Zep cloud Flex $125/mo¹ |
| Mem0 / OpenMemory | Vector + KV (Qdrant/pgvector/…); **graph removed in OSS v3**² | Episodic/Semantic/Procedural; smart-dedup | Temporal Reasoning + Decay = **platform-only**³ | ADD/UPDATE/DELETE/NOOP LLM classifier; no review queue | Platform dashboard (CRUD, filters); **no diff/history** | OSS Apache-2.0; cloud free/$19/$249⁴ |
| Letta (MemGPT) | Agent framework + archival vector store | Core blocks (typed slots, 2k chars), recall, archival | None documented (agent self-edits) | None — agent overwrites blindly via `memory_replace` | **ADE: context-window viewer + block editor** (no diff/history) | Apache-2.0; cloud from $20/mo |
| LangMem | LangGraph BaseStore (Postgres/in-mem) | Semantic (Profile=Pydantic / Collection), Episodic, Procedural | None bi-temporal | Cold-path manager removes contradictions as separate tool calls | None native | MIT; runs on LangGraph Platform |
| Cognee | Kuzu+LanceDB+SQLite → Neo4j/pgvector | Entity/relationship graph; ECL+Cognify | None bi-temporal; Memify reweights by usage | Content-hash dedup; resolution undocumented | None native | Apache-2.0; cloud managed |
| Memobase | Postgres + Redis | **Schema-enforced user profile** + event timeline | Event timestamps (timeline) | None documented | Cloud | Apache-2.0 |
| MemOS | SQLite (local) / cloud | MemCube abstraction + governance attrs | **MemLifecycle: version rollback + freeze; Provenance/LogQuery API** | MemGovernance lifecycle policies | OpenClaw plugin Memory Viewer | OSS (MemTensor/MemOS) |
| Memanto | Moorcheh ITS engine | **13 typed categories** | **Non-destructive supersession; as-of/changed-since queries** | Supersede/Retain/Annotate options + human review | Daily markdown reports | Research; Moorcheh backend proprietary |
| Redis/pgvector pattern | Redis + Postgres/pgvector | Hot working / cold semantic | Timestamps | Episodic→semantic consolidation | None | Mixed (agent-memory-server Apache-2.0) |

¹ Zep Flex is **$125/mo**, not $25 (verifier-corrected; the $25 figure is the per-10k-credit overage rate). ² OSS Mem0 graph store was **removed in v3 (~Apr 2026)**, replaced by spaCy entity linking — no multi-hop traversal (verifier-corrected). ³ Platform-only confirmed; but the base token-efficient algorithm — which carries most of the temporal gain — *is* in OSS. ⁴ The $249 Pro "graph" gate is sourced from secondary/marketing pages, not Mem0's own per-tier pricing matrix (UNCERTAIN — see §9).

---

## 3. Where njhook already leads or is on par

Be fair: njhook is not a toy, and several design choices are correct by current evidence.

- **Event sourcing is the right substrate.** The `(:Session)→FIRST_EVENT→(:Event)→NEXT→…` chain is structurally the immutable episodic ledger SSGM identifies as the foundation for rollback and drift correction ([arxiv.org/html/2603.11768v1](https://arxiv.org/html/2603.11768v1)). MemMachine's central finding — that the *raw episodic record* should be a first-class retrieval target, not just an extraction source — validates that this data is your most valuable asset ([arxiv.org/html/2604.04853v1](https://arxiv.org/html/2604.04853v1)). You already store it; you simply don't index or retrieve it yet.

- **Hybrid RRF (k=60) recall is the industry-standard first stage, implemented correctly.** k=60 is the confirmed widely-used default ([arxiv.org/html/2508.01405v2](https://arxiv.org/html/2508.01405v2)); Mem0's 2026 multi-signal retrieval (semantic+BM25+entity) is the same family ([mem0.ai/blog/state-of-ai-agent-memory-2026](https://mem0.ai/blog/state-of-ai-agent-memory-2026)). Your OR-term fallback on fulltext failure is a resilience pattern not universal in production systems.

- **Pre-write secret scrubbing is genuinely ahead of the field.** None of the surveyed frameworks (Mem0, Graphiti, Memanto, the reference MCP server) document any pre-write PII/secret filtering. The mnemonic-sovereignty survey explicitly names *write-gate validation* a **universal blind spot** ([arxiv.org/html/2604.16548v1](https://arxiv.org/html/2604.16548v1)) — your two-tier scrub plus secret-in-output rejection in the quality gate is real defence-in-depth.

- **Per-cwd opt-out and cwd→project derivation** are concrete privacy/scoping mechanisms absent from *every* surveyed framework.

- **Four-client unification under a composite `session_key`** is more advanced than any single-agent framework; Mem0/Graphiti/Memanto all assume one agent and push normalization to the caller.

- **Offline two-stage (online capture + offline distillation) with a quality gate** keeps the hot path fast and is more robust than Letta's in-loop self-editing (which can silently overwrite correct state) and the reference MCP server's direct tool-call storage.

- **Dashboard provenance is already better than the commercial pack.** Your `/memory/<path>` view shows the `DERIVED_FROM` sessions and `consolidated_from` merge sources, RRF score per result, and `access_count`/`last_accessed_at`. Mem0, Letta ADE, Augment Code, and Claude/ChatGPT native UIs surface *none* of this derivation data ([docs.mem0.ai/platform/advanced-memory-operations](https://docs.mem0.ai/platform/advanced-memory-operations), [docs.letta.com/guides/ade/core-memory/](https://docs.letta.com/guides/ade/core-memory/)).

- **`event_id` uniqueness constraint** gives implicit event-level dedup the reference MCP server's flat JSONL entirely lacks ([github.com/modelcontextprotocol/servers/tree/main/src/memory](https://github.com/modelcontextprotocol/servers/tree/main/src/memory)).

- **Local-only Ollama path** matches no SaaS competitor — zero embedding cost, full offline operation.

---

## 4. Gaps vs. SOTA, mapped to the existing roadmap

For each roadmap gap: is the plan adequate vs. what SOTA actually does, and how to sharpen it.

**Gap 1 — Canonical versioned `AgentEvent` schema + dead-letter.**
*Adequate in spirit, under-specified.* SOTA event-sourcing canon adds two things the roadmap omits: (a) **upcasting at read time** — never rewrite old JSONL; carry a v1→v2→v3 transformer chain applied in the ingest worker before the Neo4j write ([event-driven.io/en/simple_events_versioning_patterns/](https://event-driven.io/en/simple_events_versioning_patterns/)); (b) **OTel `gen_ai.*` alignment** — `gen_ai.conversation.id`↔`session_key`, `gen_ai.provider.name`↔`source_client`, `gen_ai.request.model`↔`source_model`, `gen_ai.operation.name`↔`event_type`, `gen_ai.usage.*_tokens` ([opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-events/](https://opentelemetry.io/docs/specs/semconv/gen-ai/gen-ai-events/)). Naming canonical fields after the CNCF standard Google/AWS/Azure already adopt makes future APM export (Gap 8) free. **Add `app_id`/`caller_identity`** now (default to `source_client`) — without it, Gap 7's egress policy can only be per-client, never per-project.

**Gap 2 — Local append-only spool + idempotent replay.**
*Correct, but the mechanism is unnamed.* The production-grade implementation is the **outbox + inbox** pattern: spool is the outbox (one JSONL/day, `fsync` per record); the ingest worker does a conditional `INSERT` into a `processed_events(event_id PRIMARY KEY)` table (inbox) *before* applying the Neo4j write ([theburningmonk.com/2026/05/inbox-outbox-patterns-for-reliable-event-processing/](https://theburningmonk.com/2026/05/inbox-outbox-patterns-for-reliable-event-processing/)). Without the inbox, a crash after the Neo4j write but before marking the spool record processed yields a duplicate on replay — "idempotent by event_id" alone doesn't cover this. Add retry backoff (5–8 attempts, jitter) → DLQ carrying full payload+error+retry-count ([baxchain.com](https://baxchain.com/blogs/resilient-event-driven-architecture-idempotency-retries-and-dead-letter-queues/)). This is the single highest-leverage reliability change: it closes the **silent-loss-when-Neo4j-down** mode that exists today (hooks correctly suppress errors, which means losses are silent).

**Gap 3 — Typed memory records with status/confidence/scope/`valid_from`/`valid_until`/`supersedes`.**
*Direction right; two corrections.* (a) The roadmap's `valid_from`/`valid_until` are *intent* fields (event/domain time). True bi-temporal needs the **second timeline — `ingested_at` (transaction time)** — to answer "what did the system *believe* on date X regardless of when the events happened" (the retroactive-correction query). Both timelines are required for full Graphiti-style semantics ([arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1)). (b) **Adopt the Memanto 13-type vocabulary** to extend the roadmap's 9 — it adds `commitment`, `goal`, `context`, `learning`, `observation`, `artifact`, all of which appear in coding-agent sessions, and the Adaptive Memory Admission Control paper shows *content-type prior is the single strongest admission signal* ([arxiv.org/pdf/2603.04549](https://arxiv.org/pdf/2603.04549)). Caveat: Memanto's "beats all hybrid systems" claim is REFUTED — Hindsight (MIT, open) outscores it on both benchmarks (§9) — so cite the *schema design*, not the score.

**Gap 4 — SUPERSEDES/CONTRADICTS/CONFIRMED_BY/REJECTED_BY + review queue.**
*Good, but two SOTA refinements missing.* (a) **Run contradiction detection PRE-commit**, not post-hoc. Graphiti compares a new edge against semantically related existing edges *before* writing ([arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1)); njhook's `consolidate()` only finds cosine near-duplicates *after* a blind write and only *merges* — it never detects outright contradictions. (b) **Default auto-resolution heuristic** for when the human doesn't review: MemArchitect's `Winner = max(SourceAuth(M) × Recency(M))` with a source hierarchy (user > claude_code > codex > cursor > gemini > ollama) reduces review burden without RL ([arxiv.org/html/2603.18330v1](https://arxiv.org/html/2603.18330v1)). The production UI proof-of-concept to copy is **Augment Code's in-session pending button** (approve/edit/discard in-chat) ([augmentcode.com/changelog/memory-review](https://www.augmentcode.com/changelog/memory-review)).

**Gap 5 — One shared `recall.query`/`recall.render` engine.**
*Adequate and necessary.* This is a hard prerequisite for Gap 10 — you cannot ship an MCP/REST memory server while `inject_memory.py` and `dashboard/app.py` each carry their own ranking. LangMem's stateless functional core reused by every surface is the cleanest reference ([langchain-ai.github.io/langmem/concepts/conceptual_guide/](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)). Sharpen by giving it a `mode` parameter (`session_start`/`prompt_context`/`tool_context`) from day one, even if modes start as stubs.

**Gap 6 — Richer ranking signals + named recall modes.**
*Right list, but missing the highest-ROI item you can ship today.* `_hybrid_merge` currently uses only RRF rank + a flat `PROJECT_BOOST*0.05` nudge — it ignores two of the three signals every serious system uses. The Generative Agents formula (`recency × relevance × importance`) has been SOTA since 2023 ([pmc.ncbi.nlm.nih.gov/articles/PMC12092450/](https://pmc.ncbi.nlm.nih.gov/articles/PMC12092450/)). You already stamp `last_accessed_at`/`access_count` via `_bump_access` — they're just unused in ranking. Add: (1) **importance** (LLM-rated 1–10 at dream time, ~1 JSON field), (2) **decayed recency** `exp(-λ·hours_since_access)` as a multiplier, λ tunable per type (profile decays slowly, project fast). Then add a **cross-encoder reranker** as an optional gated second stage. *Verifier caveat:* BGE-reranker-v2-m3 is pullable via Ollama but Ollama has **no `/api/rerank` endpoint** as of mid-2026 — use the HuggingFace `FlagReranker` CPU path, not Ollama, for true cross-encoder scoring (§9).

**Gap 7 — First-class privacy policy (sensitivity, egress, PII, audit log).**
*Adequate framing; the field's open frontier.* The mnemonic-sovereignty survey defines nine governance primitives and finds **no published system implements all nine** ([arxiv.org/html/2604.16548v1](https://arxiv.org/html/2604.16548v1)) — so this is greenfield where njhook can lead. Two missing specifics: an **audit log of every mutation** (the `MemoryRevision`/`DreamRun` log in §6 doubles as this) and **egress enforcement** keyed on the `app_id` from Gap 1 — block sensitive events from remote Anthropic/OpenAI dream calls, route them to Ollama only.

**Gap 8 — Operational metrics in health.**
*Good; one operational sharpening.* Alert on **DLQ *rate*, not presence** — a static nonzero `dlq_count` is normal (transient Neo4j hiccups); a rising `dlq_events_per_hour` is the systemic-failure signal (schema mismatch, credential rotation). Two-line addition once the DLQ exists.

**Gap 9 — Deterministic semantic eval suites across a provider/model matrix.**
*Right goal; needs a concrete spec and an admission gate.* (a) **A-MAC five-dimension admission gate before write** — utility (1 LLM call) + **ROUGE-L grounding confidence** + cosine novelty + recency + content-type-prior — yields F1=0.583 vs 0.324 for MemGPT-style and runs 31% faster ([arxiv.org/abs/2603.04549](https://arxiv.org/abs/2603.04549)). The ROUGE-L check specifically catches *dream hallucinations not grounded in the source events* — a real risk with your trimmed-log-to-LLM design. (b) **Synthetic eval suite:** 10–20 fixture sessions covering preference extraction, contradiction pairs (verify only one survives), update-vs-add (the "two dogs" fragmentation case from Memory-R1, [arxiv.org/html/2508.19828v4](https://arxiv.org/html/2508.19828v4)), and stale-archival, run across Anthropic/OpenAI/Ollama in CI on output paths + type labels. (c) Separately, a **RAGAS-style retrieval eval** (Precision@5/Recall@5 on golden query→path pairs) — distinct from distillation eval and currently absent.

**Gap 10 — REST + MCP + CLI + file renderers on one core.**
*Adequate; one spec-precision note.* MCP's **Tasks primitive** (call-now/fetch-later, states `working/input_required/completed/failed/cancelled`) maps exactly onto async dream triggering — `propose_memory` returns a `taskId` immediately. But Tasks are marked **experimental** in the 2025-11-25 spec ([modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)) — implementable today, but treat as subject to breaking change (§9). A 4-tool minimum surface (`search_memory`, `get_project_context`, `record_event`, `propose_memory`) mirrors the reference server's proven 8-tool pattern.

---

## 5. Gaps the roadmap MISSES

Concrete, citable SOTA ideas not in any of the 10 gaps:

1. **Bi-temporal "as-of" recall mode.** `njhook recall --as-of <ISO-ts>` → `WHERE m.valid_from <= $T AND (m.valid_until IS NULL OR m.valid_until > $T)`. This is the *direct user-facing implementation of the north star* — "what did the system know last Tuesday?" — yet appears nowhere in the 10 gaps. Trivial once §6's schema exists. Prior art: Memanto's as-of/changed-since/current-only modalities ([arxiv.org/html/2604.22085v1](https://arxiv.org/html/2604.22085v1)), Kumiho's `kref://…?r=N` revision pinning ([arxiv.org/html/2603.17244v1](https://arxiv.org/html/2603.17244v1)).

2. **Non-destructive edge invalidation (retire `DETACH DELETE`).** Every conflict-aware system retains superseded state. njhook's `consolidate.py` physically deletes. Set `valid_until=now`, `status='superseded'` and add `:SUPERSEDED_BY` instead ([arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1)).

3. **Claim-level provenance (`Memory→Event`, not just `Memory→Session`).** After consolidation even the Session link is re-parented, truncating the chain. `(:Memory)-[:EXTRACTED_FROM]->(:Event)` for the specific events that contributed signal enables "show me the conversation excerpt that produced this rule" — the literal north-star query. `dream.py` already knows the processed `event_id`s.

4. **`MemoryRevision` append-only audit log.** On every dream/consolidate/archive/edit, append an immutable `(:MemoryRevision {content_snapshot, status, operation, actor, timestamp})` with `:VERSION_OF`. Enables diff, rollback of a bad dream run without full backup restore, and the Gap 7 mutation audit. SSGM's dual-track model and MemOS's version rollback are the prior art ([arxiv.org/html/2603.11768v1](https://arxiv.org/html/2603.11768v1), [arxiv.org/html/2505.22101v1](https://arxiv.org/html/2505.22101v1)).

5. **Nucleus expansion at retrieval.** MemMachine's +4.2% accuracy gain (vs +0.8% from write-time extraction) by expanding an ANN hit to neighboring sentences/events ([arxiv.org/html/2604.04853v1](https://arxiv.org/html/2604.04853v1)). Your `Event` linked list is *already* the right structure — given a hit, walk `[:EXTRACTED_FROM]` to source events then `NEXT`/`PREV` to expand. Bigger gain than improving the prompt.

6. **Index `Event` nodes and fuse Event hits into RRF.** MemMachine shows raw episodes as a primary retrieval target can outperform full extraction. Add a Lucene fulltext index on `Event.prompt`/`tool_result` + per-event embeddings, fuse into `_hybrid_merge`. Makes session context retrievable *before* the next dream run.

7. **Decay-weighted scoring at query time (graceful, not the binary archival cliff).** SSGM Weibull / Mem0 exponential decay applied as a multiplier on the fused score ([arxiv.org/html/2603.11768v1](https://arxiv.org/html/2603.11768v1)). FSRS retrievability `R(t)=(1+19/9·t/S)^{-1}` with `S` growing on each access maps onto your `access_count` and gives a principled three-state Keep/Consolidate/Archive band ([arxiv.org/html/2603.18330v1](https://arxiv.org/html/2603.18330v1)).

8. **Memory-poisoning defense / confidence annealing.** MemoryGraft/MINJA show >95% injection success in naive systems. A new model-generated memory should *not* immediately reach active authority — route high-novelty + short-source-session + Procedure/Rule-type candidates to review ([arxiv.org/html/2604.16548v1](https://arxiv.org/html/2604.16548v1)). Regex scrubbing alone doesn't cover semantic poisoning.

9. **Token-value-density injection ordering (BudgetMem).** Replace the `updated_at DESC` order before `CHAR_BUDGET` truncation with `importance × recency_decay / char_length` — 72.4% storage reduction at 1.0% F1 loss ([arxiv.org/pdf/2511.04919](https://arxiv.org/pdf/2511.04919)).

10. **Embedding upgrade.** Default local `nomic-embed-text` (768d) is beaten by BGE-M3 (MIT, Ollama-pullable) and Qwen3-Embedding-8B (Apache-2.0). `EMBED_MODEL` already supports override. *Cite Qwen3-8B's retrieval sub-score 86.40, not "70.58 MTEB retrieval"* — 70.58 is the multilingual *mean* (§9).

11. **A-MEM-style keywords/tags at write time.** Generating structured attributes during distillation enriches the Lucene index beyond free-form markdown ([arxiv.org/abs/2502.12110](https://arxiv.org/abs/2502.12110), NeurIPS 2025).

12. **Human-UI gaps:** inline memory citation in `inject_memory.py` output (Claude's key differentiator over ChatGPT — [simonwillison.net/2025/Sep/12/claude-memory/](https://simonwillison.net/2025/Sep/12/claude-memory/)); sensitivity blur in dashboard lists; a GitHub-style 30-day activity heat-map ([github.com/amd/gaia/issues/575](https://github.com/amd/gaia/issues/575)); a pending-review badge.

---

## 6. Deep dive: tracing memory evolution (the north star)

The north star — *"a universal memory layer for LLMs with a human-friendly interface for tracing memory evolutions"* — requires three things njhook lacks: **(a) time on memories, (b) provenance at claim granularity, (c) non-destructive supersession**. Today the opposite holds: `dream.py` overwrites content in place; `consolidate.py` `DETACH DELETE`s sources; provenance survives only as `consolidated_from` *path strings*. You cannot answer "how did this memory come to be?" or "what did the system believe last week?".

### Verified prior art to model on

- **Graphiti/Zep bi-temporal:** four timestamps per edge (`valid_at`/`invalid_at` event time; `created_at`/`expired_at` transaction time); supersession sets `invalid_at` to the new fact's `valid_at` — **never deletes** ([arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1)). Apache-2.0, runs on the Neo4j 5.26+ you already use (verified). *Do not* cite its 18.5% benchmark gain as justification — that's measured against weak baselines (§9); cite the *model*.
- **Kumiho:** immutable append-only revision snapshots + mutable "current" tag pointers; typed `Supersedes`/`Derived_From`/`Depends_On` edges; `kref://project/space/item.kind?r=N` addressable URIs; a browseable interactive graph UI ([arxiv.org/html/2603.17244v1](https://arxiv.org/html/2603.17244v1)).
- **Letta ADE:** the bar for *current-state* legibility (context-window viewer, per-block char counts) — but explicitly **no diff/history** ([docs.letta.com/guides/ade/overview/](https://docs.letta.com/guides/ade/overview/)). njhook can leapfrog by shipping the history dimension ADE lacks.
- **Claude Code (verified):** stores memory as plain markdown at `~/.claude/projects/<project>/memory/`, directly editable — validates njhook's markdown-as-render-target direction. (The "Claude.ai chat uses local markdown files" claim is REFUTED — that's Claude *Code* only; §9.)

### Proposed Neo4j schema

Additive — no migration of existing nodes required; new properties default to null/active.

```
(:Memory {
   path,                       // unchanged: stable identity / current-view key
   content, updated_at,        // existing
   // --- bi-temporal (NEW) ---
   ingested_at,                // transaction time: when THIS dream run wrote it
   valid_from,                 // event time: earliest source-event timestamp
   valid_until,                // null = currently true
   status,                     // 'active' | 'pending_review' | 'superseded' | 'rejected' | 'archived'
   // --- ranking / governance (NEW) ---
   importance,                 // LLM-rated 1-10 at dream time
   confidence,                 // ROUGE-L grounding score (A-MAC)
   kind,                       // typed: preference|projectrule|decision|... (13-type vocab)
   sensitivity,                // none|low|high  (Gap 7)
   created_by                  // 'dream_<provider>' | 'user' | 'consolidate'
})

// --- provenance (NEW) ---
(:Memory)-[:EXTRACTED_FROM]->(:Event)     // claim-level: the exact events
(:Memory)-[:DERIVED_FROM]->(:Session)     // existing, keep
(:DreamRun {run_id, ts, provider, model})-[:WROTE]->(:Memory)   // mutation log
(:Memory)-[:SUPERSEDED_BY]->(:Memory)     // non-destructive lineage
(:Memory)-[:CONTRADICTS]->(:Memory)       // flagged, pre-commit
(:MemoryRevision {content_snapshot, status, operation, actor, ts})-[:VERSION_OF]->(:Memory)
```

### Write-path changes (concrete)

1. **`dream.py` `write_memories()`** — replace blind `MERGE … SET m.content` with: find the active memory at `path`; if none, create with `status` per admission gate; if one exists and the new content semantically diverges (cosine + optional LLM), **set `valid_until=now`, `status='superseded'` on the old**, create a **new** node, add `[:SUPERSEDED_BY]`. Always append a `:MemoryRevision` of the prior content and a `:DreamRun-[:WROTE]->` edge. Add `MERGE (m)-[:EXTRACTED_FROM]->(e)` for each processed `event_id` (one `UNWIND`).
2. **`consolidate.py`** — retire `DETACH DELETE old`; instead close the sources (`valid_until`, `status='superseded'`) and point `:SUPERSEDED_BY` at the merged node. Recall already needs a `status` filter, so retaining old nodes costs almost nothing.
3. **Admission gate** — before any write, compute A-MAC's five dimensions; `confidence < θ` or a detected contradiction → `status='pending_review'` (injected as advisory only, never authoritative).

### Recall changes

- Default queries filter `status='active' AND (valid_until IS NULL OR valid_until > now)`.
- New `--as-of <T>` mode: `valid_from <= $T AND (valid_until IS NULL OR valid_until > $T)`.
- Fuse importance + decayed recency into `_hybrid_merge`.

### Human-friendly UI sketch (extends the Flask dashboard)

- **Memory timeline (`/memory/<path>/history`):** vertical time-ordered list of `MemoryRevision`s and `SUPERSEDED_BY` hops — each row = operation, `DreamRun` (provider/model), timestamp, and a one-line content summary. This is the literal "tracing memory evolution" view no surveyed UI ships.
- **Diff panel:** `difflib.unified_diff` over adjacent revisions, +/- colored in a `<pre>` (~30 lines). The single feature that most clearly demonstrates evolution to a human.
- **Lineage graph:** render `EXTRACTED_FROM`/`SUPERSEDED_BY`/`CONTRADICTS` as a small node-link diagram; click an `Event` to jump to the raw session excerpt. Click "as-of" date picker to reconstruct state.
- **Conflict view:** for a `CONTRADICTS` pair, show *which events support each side* (via `EXTRACTED_FROM`) side-by-side — Memanto's "Annotate" model, with approve/reject/supersede actions.
- **Pending badge + sensitivity blur** in the header and lists.

---

## 7. Prioritized recommendations

Ranked. **Quick wins** are low-effort/high-value and unblock nothing else; **foundational bets** change the schema/architecture and gate later work.

| # | Recommendation | Roadmap gap | Effort | Impact | Why now |
|---|---|---|---|---|---|
| **Quick wins** |
| Q1 | Retire `DETACH DELETE` in `consolidate.py`; close + `SUPERSEDED_BY` instead | 4 | Low | High | One Cypher change; immediately stops destroying history; prereq for any evolution UI |
| Q2 | Add `EXTRACTED_FROM` edges Memory→Event during `write_memories()` | 3 | Low | High | `event_id`s already known; unlocks the literal north-star "show source excerpt" query |
| Q3 | Add `importance` (1–10) + decayed recency to `_hybrid_merge` | 6 | Low | High | `last_accessed_at`/`access_count` already stamped but unused; ~50-line ranking upgrade |
| Q4 | Stop-hook-triggered near-real-time dream (debounced 60s) | 2,8 | Low | High | Eliminates the multi-hour "ran at 2pm, distilled at 3am" gap; cron becomes catch-up |
| Q5 | `MemoryRevision` snapshot before every content write/edit | 3,7 | Low | High | Foundation for diff/rollback/audit; no migration |
| Q6 | Inline memory citation footer in `inject_memory.py` output | (new) | Low | Med | Claude's key transparency edge; zero token cost |
| Q7 | `schema_version` + `app_id` on every spool record; OTel `gen_ai.*` field names | 1,7,8 | Low | Med | Must be decided before first spool record is written; free APM path later |
| **Foundational bets** |
| F1 | Bi-temporal `ingested_at`/`valid_from`/`valid_until` + non-destructive supersession on `:Memory` | 3,4 | Med | High | The defining north-star capability; enables F2; Graphiti pattern on existing Neo4j |
| F2 | `--as-of` recall mode + dashboard timeline/diff/lineage UI | 6,(new) | Med | High | Direct user-facing payoff of F1; the differentiator no competitor ships |
| F3 | A-MAC five-dimension admission gate (ROUGE-L grounding) replacing shape-only gate | 9,4 | Med | High | Blocks dream hallucinations from entering the graph; feeds `confidence`/`pending_review` |
| F4 | JSONL outbox + inbox dedup table + DLQ (fsync per record) | 2,1 | Med | High | Closes the silent-loss-when-Neo4j-down mode; the core reliability fix |
| F5 | Extract recall into shared `recall.py` with `mode` param | 5 | Low–Med | Med | Hard prerequisite for MCP/REST (Gap 10) |
| F6 | Pre-write contradiction check + CLI review queue (`review list/approve/reject`) | 4 | Med | High | Stops "3am vs 3pm" memories silently becoming truth; Augment Code pattern |
| F7 | Index `Event` nodes + fuse Event hits into RRF; nucleus expansion | 6,5 | Med | High | MemMachine: retrieval investment > extraction investment; data already stored |
| F8 | Minimal 4-tool MCP server (`propose_memory` via Tasks) | 10 | Med | High | Makes njhook universal; depends on F5 |
| F9 | Cross-encoder reranker (HF FlagReranker CPU path, gated) | 6 | Med | Med | +precision; *not* via Ollama (no rerank endpoint) |

---

## 8. Suggested roadmap amendments to `UNIVERSAL_MEMORY_ROADMAP.md`

Specific edits/additions:

- **Gap 1:** Add "(a) read-time upcasting chain (`v1→v2→v3`), never rewrite past records; (b) name canonical fields after OTel `gen_ai.*` where they overlap; (c) include `app_id`/`caller_identity` (default `source_client`) as the egress-policy key for Gap 7."
- **Gap 2:** Replace "idempotent replay" with the **explicit outbox+inbox spec**: append-only JSONL outbox with `fsync` per record; `processed_events(event_id PK)` inbox table; conditional-insert before Neo4j write; exponential-backoff retry → DLQ carrying payload+error+retry-count.
- **Gap 3:** Split `valid_from`/`valid_until` (event/intent time) from a **new `ingested_at` (transaction time)** — both required for "as-of." Extend the 9-type vocabulary to the **Memanto 13** (add `commitment`, `goal`, `context`, `learning`, `observation`, `artifact`). Add `importance`, `confidence`, `created_by`, `status` as first-class fields.
- **Gap 4:** Specify contradiction detection runs **pre-commit**; add a default auto-resolution heuristic (`source_authority × recency`) for un-reviewed conflicts; require **non-destructive supersession** (retire `DETACH DELETE`).
- **Gap 6:** Make the highest-ROI item explicit — **recency-decay + importance multipliers in `_hybrid_merge`** (data already present). Add nucleus expansion and an optional HF cross-encoder reranker (note: **not** via Ollama).
- **Gap 9:** Add the **A-MAC admission gate** and a named **synthetic eval suite** (preference/contradiction/update-vs-add/stale fixtures) + a separate **RAGAS retrieval eval** (Precision@5/Recall@5).
- **New section "Gap 11 — Memory evolution & provenance (north star)":** bi-temporal model, `MemoryRevision` audit log, `EXTRACTED_FROM` claim-level provenance, `--as-of` recall, and the timeline/diff/lineage UI. *(This is currently scattered implicitly across 3, 4, and the dashboard; it deserves its own phase since it is the stated north star.)*
- **New "Gap 12 — Memory integrity & anti-poisoning":** confidence annealing, novelty-outlier routing to review, egress policy on `app_id`. Cite the mnemonic-sovereignty nine-primitive taxonomy as the governance scorecard.
- **Phasing note:** keep the existing order (reliability core → shared recall → typed memory → conflict/review → universal interfaces → governance/eval), but pull **Q1/Q2/Q5 (non-destructive history)** forward into the reliability core — they are cheap and unblock the north-star UI early.

---

## 9. Risks, unknowns & refuted claims

Do **not** rely on the following in any external write-up or design justification:

**REFUTED:**
- **"Supermemory is #1 on LongMemEval, LoCoMo, and ConvoMem."** Supermemory's own page only reports LongMemEval-S (81.6%, GPT-4o), dismisses LoCoMo, and never mentions ConvoMem; the ConvoMem paper never evaluated it; no neutral leaderboard exists; ≥4 other systems self-report higher LongMemEval ([supermemory.ai/research](https://supermemory.ai/research/), [arxiv.org/html/2511.10523](https://arxiv.org/html/2511.10523)).
- **"Mem0 OSS includes graph features / multi-hop traversal."** Graph store was **removed in OSS v3 (~Apr 2026)**, replaced by spaCy entity linking with no queryable graph or multi-hop ([docs.mem0.ai/migration/oss-v2-to-v3](https://docs.mem0.ai/migration/oss-v2-to-v3)). It was true for v2. If njhook wants true graph traversal, build it natively on Neo4j — do not plan around OSS Mem0 graph.
- **"Memanto surpasses all hybrid/vector systems."** Hindsight (MIT, open-source) scores higher on *both* benchmarks (91.4% LME / 89.6% LoCoMo vs 89.8% / 87.1%) by Memanto's *own* tables ([arxiv.org/html/2604.22085v1](https://arxiv.org/html/2604.22085v1)). Cite Memanto's schema, not its ranking.
- **"Claude.ai chat stores memory as local editable markdown files."** True only for Claude **Code** (`~/.claude/projects/<project>/memory/`). Claude.ai chat memory is server-side, UI-only ([code.claude.com/docs/en/memory](https://code.claude.com/docs/en/memory), [support.claude.com](https://support.claude.com/en/articles/11817273)). The cited Willison article makes no markdown-file claim.

**PARTLY SUPPORTED / use with the correction:**
- **Zep pricing:** Flex is **$125/mo**, not $25 ($25 is the per-10k-credit overage). Community Edition **is** discontinued (Apr 2025), but a third path exists: enterprise **BYOC** (own AWS VPC) — not a free self-host route ([getzep.com/pricing](https://www.getzep.com/pricing/), [help.getzep.com/faq](https://help.getzep.com/faq)).
- **Graphiti "three separate systems" to self-host:** misleading — it needs Graphiti + **one** graph DB (Neo4j *or* FalkorDB *or* Kuzu) + an LLM key. For njhook (already on Neo4j 5.26+), **no extra DB**. Apache-2.0, no cloud dependency — *fully verified and safe to adopt* ([github.com/getzep/graphiti/blob/main/LICENSE](https://github.com/getzep/graphiti/blob/main/LICENSE)).
- **Graphiti 18.5% / 115k→1.6k token gains:** real, but measured **only vs full-context and session-summary baselines** — no BM25/dense/hybrid comparator. A pure-RAG approach hit 86% vs Zep's 71.2%; "Does Memory Need Graphs?" shows flat methods match graphs under common conditions ([emergence.ai/blog/sota-on-longmemeval-with-rag](https://www.emergence.ai/blog/sota-on-longmemeval-with-rag), [arxiv.org/html/2601.01280](https://arxiv.org/html/2601.01280)). **The bi-temporal graph's accuracy uplift over a well-tuned hybrid like njhook's is unproven.** Adopt the model for *evolution-tracing*, not for a promised accuracy jump.
- **"Zep 63.8% vs Mem0 49.0% (15-pt gap from bitemporal)":** 63.8% is GPT-4o-**mini** (GPT-4o = 71.2%); the 49.0% Mem0 figure is from a different paper, not a controlled head-to-head; no experiment isolates the bitemporal variable. Architecture is sound; the gap claim is not ([arxiv.org/html/2501.13956v1](https://arxiv.org/html/2501.13956v1), [blog.getzep.com/state-of-the-art-agent-memory/](https://blog.getzep.com/state-of-the-art-agent-memory/)).
- **Mem0 "+29.6 pts temporal, platform-only":** platform-only is correct, but +29.6 is a **LoCoMo** gain from the **base algorithm that IS in OSS**; the platform-only Temporal Reasoning layer adds only +3.8 (93.2→97.0% LongMemEval). Don't attribute the big number to the paid tier.
- **BGE-reranker-v2-m3 "local via Ollama":** pullable, but Ollama has **no `/api/rerank` endpoint** as of mid-2026 — true cross-encoder scoring needs the HuggingFace `FlagReranker` CPU path. Revise the reranker recommendation accordingly ([github.com/ollama/ollama/issues/16076](https://github.com/ollama/ollama/issues/16076)).
- **Qwen3-Embedding-8B "70.58 MTEB retrieval":** 70.58 is the multilingual **mean**; the retrieval sub-score is **86.40**. Apache-2.0 is fully correct. Cite the right number.
- **Cohere Rerank 4 Pro:** $2.50/1k (not $2.40). Voyage rerank-2.5 free 200M tokens is a **one-time** credit, not monthly.
- **MCP Tasks primitive:** real and fully specified, but **experimental** — building `propose_memory` on it is a calculated compatibility bet, not a stability guarantee.
- **MemClaw "full audit diffs + complete diff/lifecycle UI":** audit *log* (`/audit-log`) is real in OSS; "audit **diffs**" is marketing-only (docs 404'd), exact 8-status set unconfirmed, and **no human-facing diff UI is documented**. Do **not** treat MemClaw as a reference implementation for the Gap-4 UI without independent verification.
- **Graphiti MCP "hundreds of thousands of weekly users":** vendor-asserted, unaudited, no third-party corroboration. The Nov-2025 v1.0 launch date is supported; the user count is not.

**Genuine unknowns (no verifier signal):** exact mechanism of Mem0's "smart dedup" (undocumented in OSS); Cognee's conflict-resolution algorithm (undocumented); whether njhook's current hybrid actually matches Graphiti on *your* data — recommend running the §9 retrieval eval before committing to any graph-overhead investment.

**Net build-vs-buy verdict:** Build the patterns natively on your existing Neo4j. Zep is cloud-gated and not turnkey self-hostable for free; Graphiti is an engine you'd have to wrap (auth, dashboard, health) and whose accuracy edge is unproven against your hybrid; OSS Mem0 lost its graph. njhook's substrate is already capable of everything Graphiti does — the missing pieces are the temporal edge schema, non-destructive supersession, claim-level provenance, and the evolution UI, all of which are additive Cypher + Flask work, not a new dependency.
