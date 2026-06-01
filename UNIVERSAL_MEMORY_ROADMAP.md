# Universal LLM Memory Roadmap

## Purpose

The target state for `njhook` is a unified memory substrate shared by every
attached LLM agent. A Claude Code session, a Codex session, a Cursor session,
a Gemini session, a local model, or a future MCP/REST-integrated tool should
all operate from the same durable memory basis for a project.

The current implementation is a strong local prototype: it captures events
from four agent clients into Neo4j, distills durable memories through a dream
phase, embeds them, and injects relevant context back into later sessions.
That is the right foundation. It is not yet a universal memory system. The
remaining work is mostly about protocol stability, durability, governance,
retrieval quality, and operational guarantees.

## Current State

The system currently has five major parts:

1. Hook capture:
   - `hooks/log_event.py` receives client hook payloads.
   - Events are written into Neo4j as per-session linked lists.
   - Supported clients are `claude_code`, `codex`, `cursor`, and `gemini`.

2. Memory recall:
   - `hooks/inject_memory.py` injects memories at session start and prompt
     time.
   - Recall combines fulltext and vector search, then applies a project boost.

3. Dream phase:
   - `dream/dream.py` reads recent event chains.
   - Provider adapters in `dream/providers.py` support Anthropic, OpenAI, and
     Ollama.
   - Model output is quality-gated before writes.

4. Operations:
   - `cli/njhook.py` exposes list, search, edit, sessions, stats, backup,
     restore, migrate, health, reindex, consolidate, archive, and pattern
     detection.
   - Backup and restore have already been heavily hardened.

5. Dashboard:
   - `dashboard/app.py` gives a local web UI.
   - Write routes are disabled unless `DASHBOARD_WRITE=1`.

Recent verification showed:

- Full test suite passes: `19 passed`.
- `njhook health` reports `21 ok, 0 warn, 0 fail`.
- Live graph has memories, sessions, events, embeddings, a vector index, and a
  working scheduled dream task.

## What Is Already Good

The existing design gets several fundamentals right:

- One backing store for multiple clients.
- Composite `session_key = "<client>:<session_id>"` avoids cross-client raw ID
  collisions.
- Hook capture and memory distillation are separated.
- Recall is already hybrid fulltext/vector rather than single-mode search.
- Project derivation gives memories a useful scope signal.
- Privacy scrubbing is applied before event writes.
- Backup/restore now has explicit shape validation, scoped export, and
  regression tests for malformed input and large payloads.
- Dashboard write gating is the correct default for a sensitive local memory
  system.

These are not cosmetic wins. They mean the system is viable as a base for a
real shared-memory layer.

## Main Gaps

### 1. The System Is Hook-Adapter Driven, Not Protocol Driven

Today each client is integrated through its native hook shape. That is fine
for bootstrapping, but it does not scale to arbitrary LLMs or agent runtimes.

The graph currently stores normalized-enough data, but there is no explicit
canonical event contract that every adapter must satisfy.

Required improvement:

Define a versioned internal event schema:

```text
AgentEvent {
  schema_version
  event_id
  source_client
  source_model
  session_key
  project_key
  event_type
  actor
  timestamp
  cwd
  prompt
  tool_call
  tool_result
  artifacts
  visibility
  sensitivity
  raw_payload
}
```

Each client adapter should map native events into this schema before anything
touches Neo4j.

Acceptance bar:

- Closed vocabulary for `event_type`, `source_client`, `actor`, `visibility`,
  and `sensitivity`.
- Invalid native events are rejected into a dead-letter queue, not silently
  dropped.
- Tests cover Claude/Codex/Cursor/Gemini event fixtures mapping into the same
  normalized shape.
- Schema version is stored on every event.

### 2. Direct Hook-To-Neo4j Writes Are A Durability Risk

The hook currently writes directly to Neo4j. If Neo4j is down, slow,
schema-locked, or temporarily unreachable, capture can fail. Hook scripts
correctly avoid breaking the agent session, but that means loss can be silent.

Required improvement:

Add a local append-only spool:

```text
client hook -> JSONL/WAL spool -> ingest worker -> Neo4j
```

The hook path should do only three things:

1. Parse input.
2. Scrub sensitive fields.
3. Append a normalized event to a local durable queue.

The ingest worker can handle retries, backoff, deduplication, schema writes,
and dead-letter handling.

Acceptance bar:

- Stop Neo4j, run agent sessions, restart Neo4j, and confirm queued events
  replay into the correct session chains.
- Replay is idempotent by `event_id`.
- Queue backlog appears in `njhook health`.
- Malformed queue records are isolated and reported.
- Hook runtime remains bounded even when Neo4j is unavailable.

### 3. Markdown Memories Are Useful, But Too Weak As Canonical State

Current memories are `path + content` markdown blobs with optional metadata.
That works for prompt injection. It is not strong enough as the canonical
memory model for multiple independent LLMs writing into the same store.

Required improvement:

Introduce typed memory records alongside the existing markdown representation.
Markdown becomes a render target, not the only source of truth.

Recommended memory types:

```text
Preference
ProjectRule
Decision
Procedure
Fact
Constraint
ToolPattern
Incident
OpenQuestion
```

Recommended fields:

```text
memory_id
memory_type
scope
project_key
repo_root
path
content
rendered_markdown
status
confidence
source_sessions
created_by
updated_by
valid_from
valid_until
supersedes
sensitivity
review_state
```

Acceptance bar:

- Closed vocabulary for memory type, status, scope, sensitivity, and review
  state.
- Existing `:Memory {path, content}` remains supported during migration.
- Injection can render typed memories back to markdown.
- Recall filters out inactive, rejected, archived, and superseded memories.
- Round-trip tests prove typed memory -> markdown -> recall output.

### 4. Conflict Handling Is Missing

Universal memory means multiple agents will produce overlapping and sometimes
contradictory claims. Blind upsert by path is not enough.

Examples:

- Dream task runs at `3 AM` vs `3 PM`.
- Local dream model should be `qwen3.5` vs `gemma4`.
- Dashboard default port is `5000` vs `5050`.
- A project rule is replaced by a newer decision.

Required improvement:

Add explicit lifecycle and relationship semantics:

```text
(Memory)-[:SUPERCEDES]->(Memory)
(Memory)-[:CONTRADICTS]->(Memory)
(Memory)-[:CONFIRMED_BY]->(Session|UserAction)
(Memory)-[:REJECTED_BY]->(Session|UserAction)
```

Conflicting model-generated memories should enter a review queue instead of
becoming active automatically.

Acceptance bar:

- New memory contradicting active memory is flagged.
- Recall injects only active, non-superseded memories.
- CLI/dashboard expose unresolved conflicts.
- User confirmation can activate one memory and supersede the other.
- Tests cover contradiction, supersession, and recall filtering.

### 5. Recall Logic Needs To Become A Shared Service

Recall is currently embedded in `hooks/inject_memory.py`, while dashboard
search implements similar logic separately. That will drift.

Required improvement:

Move recall into a shared module or service:

```text
recall.query(context) -> ranked MemoryHit[]
recall.render(hits, budget) -> injection text
```

Different callers should use the same engine:

- hook injection
- dashboard search
- CLI search
- future REST API
- future MCP server
- generated agent files

Acceptance bar:

- One ranking implementation.
- Tests pin ranking behavior for project boost, archive filtering,
  superseded filtering, fulltext fallback, vector fallback, and token budget.
- Dashboard, CLI, and hooks call the shared implementation.

### 6. Retrieval Semantics Are Too Simple For Long-Term Use

Current retrieval uses fulltext hits, vector hits, RRF, and project boost.
That is a good baseline. Universal memory needs richer ranking.

Recommended ranking signals:

- project/repo/directory scope match
- memory type priority
- human-confirmed vs model-generated
- confidence
- recency
- access frequency
- source reliability
- sensitivity policy
- supersession status
- token value density

Recommended recall modes:

```text
session_start_context
prompt_context
tool_context
project_bootstrap_context
debug_context
review_context
```

Acceptance bar:

- Prompt recall and session-start recall are separate query plans.
- Tool-call recall can retrieve tool-specific procedures.
- Token budget is enforced deterministically.
- Low-confidence or unreviewed memories can be excluded by policy.

### 7. Privacy Needs Policy, Not Just Regex Scrubbing

The current scrubber is useful and should remain. It is not enough for a
universal memory base.

Required improvement:

Add first-class privacy policy:

- project allowlist mode
- project opt-out mode
- memory sensitivity levels
- raw event retention policy
- transcript capture policy
- secret and PII scanners
- model-provider egress policy
- "do not distill" markers
- audit log for every memory mutation

Hard truth: if all LLMs are attached, this system will eventually see private
code, credentials, personal data, internal decisions, and sensitive prompts.
Regex scrubbing reduces damage but does not define governance.

Acceptance bar:

- Each event and memory carries sensitivity.
- Dream phase refuses to send sensitive events to remote providers unless
  policy allows it.
- Generated memories are scanned before activation.
- `njhook health` reports policy status.
- Audit log records memory creation, edit, archive, supersede, reject, and
  restore.

### 8. Observability Must Become Operational

`njhook health` is already useful. It should become the front door for
operational reliability.

Required metrics:

- hook append count
- hook parse failure count
- queue backlog
- ingest success/failure count
- dead-letter count
- Neo4j write latency
- dream runs by provider/model
- dream generated/rejected memory count
- recall query count
- recall hit count
- average injection token count
- conflict count
- backup age
- restore rehearsal age

Acceptance bar:

- `njhook health` shows queue, dead-letter, backup, restore-rehearsal, and
  dream freshness status.
- Logs are structured enough to parse.
- A failed dream run does not look healthy just because the scheduler fired.
- There is a command to inspect recent failures.

### 9. Distillation Needs Semantic Evaluation

The current quality gate validates shape, frontmatter, path schema, size, and
secret-shaped strings. It does not validate whether the memory is true,
non-duplicative, properly scoped, or non-contradictory.

Required improvement:

Add deterministic eval suites:

- preference extraction
- project rule extraction
- decision extraction
- procedure extraction
- contradiction detection
- duplicate detection
- secret rejection
- project scoping
- cross-client event normalization
- empty/low-signal session handling

Acceptance bar:

- Evals run against synthetic sessions with known expected memories.
- Provider/model matrix reports pass/fail.
- CI fails on deterministic semantic regressions.
- Local model regressions are visible before scheduler adoption.

### 10. Interfaces Need To Be Universal

Not every LLM runtime will support hooks. A universal memory system must offer
several integration paths over the same recall and write core.

Recommended interfaces:

1. Existing hooks for current CLIs.
2. CLI:
   - `njhook recall --cwd <path> --prompt <text>`
   - `njhook write-event --json <file>`
   - `njhook write-memory --type ...`
3. REST API:
   - `POST /events`
   - `POST /recall`
   - `POST /memories`
   - `GET /health`
4. MCP server:
   - `search_memory`
   - `get_project_context`
   - `record_event`
   - `propose_memory`
5. File renderers:
   - `AGENTS.md`
   - `CLAUDE.md`
   - Cursor rules
   - Gemini context files

Acceptance bar:

- Every interface uses the same schema validation.
- Every recall interface uses the same ranking engine.
- Interfaces are tested with the same fixture events and expected memory hits.

## Recommended Development Phases

### Phase 1: Reliability Core

Goal: no event loss under normal local failures.

Scope:

- Add canonical event schema.
- Add local append-only spool.
- Add ingest worker.
- Add dead-letter handling.
- Add queue/backlog health checks.

Acceptance:

- Neo4j-down replay test passes.
- Duplicate event replay is idempotent.
- Malformed event goes to dead-letter.
- Existing direct hook behavior remains available behind a compatibility flag
  during migration.

### Phase 2: Shared Recall Engine

Goal: one retrieval implementation for all surfaces.

Scope:

- Extract recall ranking from hook/dashboard code.
- Add recall modes.
- Add deterministic token-budget rendering.
- Add lifecycle filtering hooks for future typed memories.

Acceptance:

- Hook, CLI, and dashboard use the same recall module.
- Tests cover fulltext-only, vector-only, hybrid, archived, project boost, and
  budget truncation behavior.

### Phase 3: Typed Memory Model

Goal: make memory state structured enough for multiple agents to safely share.

Scope:

- Add typed memory schema.
- Preserve legacy markdown memory compatibility.
- Add memory status and scope.
- Add migration/backfill command.
- Add markdown renderer for injection.

Acceptance:

- Existing memories migrate without loss.
- New typed memories render into current injection format.
- Recall excludes inactive/superseded/rejected memories.
- Backup/restore includes typed fields explicitly.

### Phase 4: Conflict And Review Workflow

Goal: prevent model-generated contradictions from silently becoming truth.

Scope:

- Add contradiction and supersession relationships.
- Add review queue.
- Add CLI/dashboard views for unresolved memory conflicts.
- Add activate/reject/supersede commands.

Acceptance:

- Contradictory memory is flagged.
- User can resolve conflict.
- Resolved memory affects recall immediately.
- Tests prove inactive conflicting memories are not injected.

### Phase 5: Universal Interfaces

Goal: attach arbitrary agents, not only hook-capable CLIs.

Scope:

- Add REST API.
- Add MCP server.
- Add `njhook recall` and `njhook write-event`.
- Add file renderers for static-context agents.

Acceptance:

- REST, MCP, CLI, and hook paths all share schema validation.
- Same recall query produces equivalent hits through each interface.
- Health covers every enabled interface.

### Phase 6: Governance And Evaluation

Goal: make memory trustworthy over months of use.

Scope:

- Add sensitivity policy.
- Add audit log.
- Add raw event retention policy.
- Add semantic eval harness.
- Add scheduled backup and restore rehearsal checks.

Acceptance:

- Sensitive events are not sent to remote dream providers unless allowed.
- Memory mutations are auditable.
- Restore rehearsal status appears in health.
- Provider/model eval matrix is reproducible.

## Priority Recommendation

Do not prioritize more client adapters yet. More adapters will multiply
inconsistent inputs unless the internal contract is hardened first.

The next serious milestone should be:

1. Canonical event schema.
2. Local durable spool and ingest worker.
3. Shared recall engine.
4. Typed memory lifecycle.
5. Conflict/review workflow.

After that, adding REST, MCP, and more LLM runtimes becomes straightforward.

## Non-Goals For The Next Milestone

These should be explicitly deferred:

- Multi-user cloud deployment.
- Team ACLs.
- Remote sync across machines.
- Fully automated conflict resolution.
- Replacing Neo4j.
- Rewriting the dashboard.

The current single-user local graph is enough. The urgent issue is not scale
in number of users; it is trustworthiness of shared memory state across many
agents and models.

