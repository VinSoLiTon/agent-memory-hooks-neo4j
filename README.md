# Agent Memory Hooks (Claude Code · Codex · Cursor · Gemini)

A two-stage memory layer for [Claude Code](https://claude.com/claude-code),
[Codex](https://developers.openai.com/codex/hooks),
[Cursor](https://www.cursor.com/), and
[Gemini CLI](https://github.com/google-gemini/gemini-cli), backed by Neo4j.

1. **Hooks (online)** — capture every session event from any of the four CLIs
   into a shared graph as it happens, with privacy filters and per-CWD opt-out.
2. **Dream phase (offline)** — periodically read those events through any of
   three LLM providers (Anthropic / OpenAI / local Ollama) and distill durable
   markdown memories that future sessions automatically receive.

The hooks record *what happened*. The dream phase decides *what's worth
remembering*. All four CLIs write into the same Neo4j instance; nodes carry
`client: "claude_code" | "codex" | "cursor" | "gemini"`. Memory recall
combines fulltext and vector (embedding) search and boosts in-project hits.

## Repo layout

```
hooks/
  log_event.py              # shared writer (takes --client)
  inject_memory.py          # shared memory injector — hybrid (fulltext + vector)
  privacy.py                # CWD opt-out + secret scrubbing
  project.py                # derive project slug from cwd
  embeddings.py             # embedding providers (openai / ollama)
.claude/  .codex/  .cursor/  .gemini/
  hooks/                    # .cmd wrappers (Windows) / .sh (POSIX)
  settings.json | hooks.json
dream/
  dream.py                  # offline consolidation
  providers.py              # anthropic / openai / ollama adapters
  prompts.py                # per-provider system prompts (Ollama gets few-shot)
  consolidate.py            # LLM-merge near-duplicate memories + archive
  eval.py                   # tiny pass/fail harness for prompt regressions
  run_dream.cmd             # Windows Task Scheduler wrapper
cli/njhook.py               # the njhook CLI (see "Subcommands" below)
njhook.cmd                  # Windows launcher for the CLI
dashboard/app.py            # local Flask UI on http://localhost:5000
detect/patterns.py          # cross-session pattern detection
tests/                      # unit tests (privacy, project)
test_hooks.py               # integration test against a live Neo4j
```

## Stage 1 — Hooks

Each session becomes a linked list of events:

```
(Session {session_key, session_id, client}) -[:FIRST_EVENT]->  (Event) -[:NEXT]-> (Event) -> ...
                                            -[:LATEST_EVENT]-> (last Event)
```

`session_key = "<client>:<session_id>"` so two clients can use the same raw
`session_id` without colliding into one chain.

Events captured: `SessionStart`, `UserPromptSubmit` / `BeforeAgent`,
`PreToolUse` / `BeforeTool`, `PostToolUse` / `AfterTool`, `Stop` /
`SessionEnd`. Each `:Event` stores the relevant hook payload — prompt, tool
name, tool input, tool response — with secrets scrubbed before write.
Transcripts are NOT captured by default (set `HOOKS_CAPTURE_TRANSCRIPT=1` to
opt in; cap with `HOOKS_TRANSCRIPT_MAX_CHARS`).

### Setup

```bash
pip install -r requirements.txt
# defaults assume bolt://localhost:7687 with neo4j/password
export HOOKS_NEO4J_URI=bolt://localhost:7687
export HOOKS_NEO4J_USER=neo4j
export HOOKS_NEO4J_PASSWORD=password

# Run schema migration once (idempotent; rerun after pulling upgrades).
# Hooks themselves only ensure the two MERGE-supporting UNIQUE constraints
# at runtime — the rest of the schema (legacy-constraint drops, indexes,
# data backfills) lives behind this command so hot-path events stay cheap.
./njhook.cmd migrate

# Optional: enable semantic recall + dream-phase Ollama
export EMBED_PROVIDER=ollama          # or openai
ollama pull nomic-embed-text          # if using Ollama
```

The hooks are already wired up at the project level — open this directory in
your CLI of choice and they fire automatically:

| CLI | File | Notes |
|---|---|---|
| Claude Code | `.claude/settings.json` | Just run Claude Code in this dir. |
| Codex | `.codex/hooks.json` | Set `[features] codex_hooks = true` in `~/.codex/config.toml`. |
| Cursor | `.cursor/hooks.json` (modern) or `.cursor/settings.json` (legacy) | Open the folder. |
| Gemini CLI | `.gemini/settings.json` | Run from this dir; uses `BeforeAgent` as user-prompt analog. |

For **global capture** (any project, no per-repo glue) merge a `hooks` block
into your user-level config (`~/.claude/settings.json`,
`~/.gemini/settings.json`, `~/.codex/hooks.json`) pointing at the absolute
path of the wrappers in this repo's `.claude/hooks/` etc.

### Privacy

`hooks/privacy.py` runs on every captured event:

- **CWD opt-out** — drop the event entirely if `cwd` is in
  `HOOKS_OPT_OUT_PATHS` (semicolon-separated) or `~/.njhook/optout.txt`.
- **Secret scrubbing** — regex-redact API keys (Anthropic, OpenAI, GitHub,
  AWS, Slack, Stripe), JWTs, Bearer tokens, .env-style assignments, and PEM
  private key blocks before write. `HOOKS_DISABLE_SCRUB=1` to bypass.

### Tests

```bash
python tests/test_privacy.py       # 11 unit tests, no Neo4j required
python tests/test_project.py       # 6 unit tests, no Neo4j required
python test_hooks.py               # integration; needs a live Neo4j
```

## Stage 2 — Dream phase

Reads sessions newer than their `last_dreamed_at` watermark, distills durable
markdown memories via your chosen LLM, upserts them as `:Memory` nodes (with
embeddings if `EMBED_PROVIDER` is set).

```bash
pip install -r dream/requirements.txt

python dream/dream.py                              # default provider (anthropic)
python dream/dream.py --since 24h                  # only recent events
python dream/dream.py --dry-run                    # preview, don't write
python dream/dream.py --provider ollama            # local, no API key
python dream/dream.py --provider openai --model gpt-4o-mini
python dream/dream.py --consolidate                # LLM-merge near-duplicates
python dream/dream.py --archive --stale-days 60    # flag cold memories
```

Provider precedence: `--provider` flag > `$DREAM_PROVIDER` > anthropic.
Default models: `claude-opus-4-7`, `gpt-4o-mini`, `qwen3.5:latest`.

### Scheduled (Windows Task Scheduler)

The repo ships `dream/run_dream.cmd` (a wrapper that defaults to ollama +
gemma4) and was registered via:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Projects\njhook\dream\run_dream.cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At 3pm
Register-ScheduledTask -TaskName "njhook-dream-nightly" -Action $action -Trigger $trigger
```

Logs at `dream/logs/dream_YYYY-MM-DD.log`. See `dream/README.md` for details.

### Eval harness

```bash
python dream/eval.py --provider ollama --model qwen3.5:latest
```

Seeds a synthetic Rust-engineer-at-Acme session, runs dream `--dry-run`,
asserts JSON validity, ≥2 memories, path schema, project discrimination, and
expected-keyword coverage. Cleans up after itself. Use it as a regression
gate when tuning prompts or swapping models.

## The `njhook` CLI

Full reference with every flag and example workflows lives in
[cli/README.md](cli/README.md). Quick-glance summary below.

```bash
./njhook.cmd <subcommand>
```

| Subcommand | Purpose |
|---|---|
| `list [--kind X] [--project Y] [--include-archived]` | Tabular list of memories. |
| `show <path>` | Print a memory's full content. |
| `search <query>` | Fulltext-rank search (Lucene-escaped). |
| `edit <path> [--create]` | Open in `$EDITOR` / Notepad, save back. |
| `delete <path> [-y]` | Remove a memory. |
| `sessions [--client X] [--since 7d]` | Captured sessions with event counts. |
| `session <id> [-v]` | Walk events of one session. |
| `stats` | Memory counts by bucket AND by semantic `kind`; sessions by client; events. |
| `embed-backfill [--force]` | Compute embeddings for memories missing them. |
| `reindex [--force] [--dry-run]` | Rebuild vector index when embedding model changes. |
| `consolidate [--threshold 0.92] [--rounds 10]` | LLM-merge near-duplicates. |
| `archive [--stale-days 60]` | Flag stale memories so they vanish from recall. |
| `unarchive <path>` | Restore an archived memory. |
| `patterns [--show ...] [--since 7d]` | Surface repeated commands / hot files / prompt clusters. |
| `patterns --promote <id> [-y]` | Convert a detected pattern into a draft memory. |
| `review <list\|approve\|reject\|supersede\|flag\|auto-resolve>` | Adjudicate the `pending_review` / `:CONTRADICTS` queue (Phase E). |
| `audit <path> \| --recent [N]` | A memory's full mutation log, or a graph-wide view (Phase H2). |
| `ingest` | Drain the durable event spool into Neo4j; idempotent replay + DLQ (Phase B). |
| `migrate-kinds [--dry-run]` | Re-tag legacy `kind` labels to the semantic vocabulary + backfill `m.kind` (Phase D1). |
| `recall <prompt> [--cwd D] [--json]` | Ranked recall over the shared engine, for non-hook runtimes (Phase G). |
| `write-event --client X [--json F]` | Capture an event via the shared scrub/spool path (Phase G). |
| `render --target {agents,claude,gemini,cursor,all}` | Render memory into a runtime's context file as a managed block (Phase G). |
| `rehearse-restore` | Prove backups are restorable on a disposable marker; records a `:RehearsalRun` (Phase H4). |
| `eval-retrieval` | Golden-set recall eval (hit@k + MRR) — ranking regression guard (Phase D3). |
| `eval-distillation [--provider X]` | Score dream output quality over golden sessions via a real provider (Phase D3). |
| `backup [--out F] [--with-embeddings] [--with-sessions] [--since 7d] [--no-tool-response] [--max-field-chars N]` | JSON dump. Default = memories only (~10KB). `--with-sessions` is unbounded; the size flags are essential. |
| `restore --in F [--with-embeddings] [--dry-run] [--allow-malformed]` | Idempotent upsert from a backup (round-trips revision/supersession/audit lineage). |
| `migrate` | Run full schema migration (idempotent). Run once after install or upgrade. |
| `health` | 13-category readiness check: Neo4j, schema, hooks, configs, Ollama, scheduled task, dream log + freshness, **event spool / DLQ rate, egress policy, restore-rehearsal age**. |

## Web dashboard

```bash
pip install -r dashboard/requirements.txt
python dashboard/app.py                  # http://localhost:5000  (read-only by default)
DASHBOARD_WRITE=1 python dashboard/app.py # enable edit / delete / archive
```

View memories, walk sessions, hybrid search. Edit / archive / delete are
**off by default** — set `DASHBOARD_WRITE=1` to enable. The read-only
default is intentional: even though the server binds to 127.0.0.1 only,
any local process can hit it and we don't want a casual click to mutate
the graph. A "read-only" pill renders in the header when writes are
disabled. Override the bind interface with `DASHBOARD_HOST=0.0.0.0`.

## Memory paths (conventions)

```
profile/role.md                 # who the user is (cross-project)
profile/preferences.md          # communication / workflow preferences (cross-project)
tools/<binary>/usage.md         # tool conventions (cross-project)
project/<slug>.md               # per-project rules and architecture
general/<slug>.md               # cross-cutting notes
```

`profile/*` and `tools/*` are cross-project (surface in every session).
`project/*` and `general/*` are tagged with a project slug (derived from the
cwd's nearest `.git` ancestor) and recall boosts in-project hits.

## Full graph schema

```
(:Session {session_key, session_id, client, created_at, last_dreamed_at})
  -[:FIRST_EVENT]->  (:Event)
  -[:LATEST_EVENT]-> (:Event)
  -[:DREAMED]->      (:Memory)

(:Event {event_id, event_name, client, app_id, timestamp, tool_name, tool_input,
         tool_use_id, tool_response, prompt, model, source, turn_id,
         last_assistant_message, stop_hook_active, transcript_path,
         transcript, cwd, sensitivity})            # app_id = OTel gen_ai.app.id (Phase B)
  -[:NEXT]-> (:Event)

(:Memory {path, content, kind, status, importance, updated_at, ingested_at,
          valid_from, valid_until, created_by, reviewed_at, project, archived,
          access_count, last_accessed_at, embedding, embedding_model, embedding_dim,
          consolidated_from, promoted_from_pattern})
  -[:DERIVED_FROM]->  (:Session)
  -[:EXTRACTED_FROM]->(:Event)                      # claim-level provenance (Phase D)
  -[:SUPERSEDED_BY]-> (:Memory)                     # consolidation/conflict (Phase A/E)
  -[:CONTRADICTS]-    (:Memory)                     # detected conflict (Phase E)

# kind  = semantic type (15-vocab: preference/decision/constraint/...; Phase D1)
# status = active | pending_review | rejected | superseded | archived  (recall = active only)

(:MemoryRevision {ts, operation, actor, status, content_snapshot})
  -[:VERSION_OF]->(:Memory)                         # immutable history + audit log (Phase A/H2)
(:DreamRun {run_id, ts, provider, model}) -[:WROTE]->(:Memory)
(:RehearsalRun {ts, ok, detail})                    # backup/restore drill record (Phase H4)

constraints: Session.session_key UNIQUE, Event.event_id UNIQUE, Memory.path UNIQUE
indexes:     fulltext on (Memory.content, Memory.path) + on Event prompt/response,
             vector on Memory.embedding, Memory.project, Session.session_id
```

## Reliability, governance & evolution (Phases A–H)

Beyond capture + dream + recall, the system carries a full engineering program
(see [`docs/IMPLEMENTATION_PLAN.md`](docs/IMPLEMENTATION_PLAN.md) for the phased
plan with acceptance bars and [`docs/PROGRESS.md`](docs/PROGRESS.md) for the
per-PR execution ledger):

- **A — Non-destructive history**: every write snapshots prior state into an
  immutable `:MemoryRevision`; nothing is destroyed (path-UNIQUE revision chain).
- **B — Durable capture**: optional `spool` mode (`HOOKS_CAPTURE_MODE=spool`) +
  `njhook ingest` with idempotent replay + DLQ, so events survive Neo4j downtime.
  Versioned event schema with read-time upcasting + OTel `gen_ai.*` alignment.
- **C — Shared recall engine**: one ranker (fulltext + vector + RRF + recency +
  importance) reused by hook, dashboard, CLI, REST, MCP.
- **D — Typed memory + admission gate**: semantic `kind` vocabulary; A-MAC
  grounding gate routes ungrounded dream output to `pending_review`; retrieval +
  distillation evals.
- **E — Conflict & review**: contradiction detection (opt-in LLM judge in the
  nightly) + `njhook review` queue + auto-resolution by authority×recency.
- **F — Evolution UI**: `njhook history --diff --as-of` + dashboard timeline /
  lineage; the north-star "trace how a memory came to be".
- **G — Universal interfaces**: CLI `recall`/`write-event`, REST (`api/server.py`),
  MCP (`api/mcp_server.py`), and file renderers (`njhook render`) — attach any LLM.
- **H — Governance**: sensitivity tagging + egress policy, anti-poisoning
  admission gate, audit log (`njhook audit`), and restore-rehearsal
  (`njhook rehearse-restore`).

## Suggested workflow

1. Use any of the four CLIs as normal — hooks capture everything (with
   secrets scrubbed; opt out of specific projects via
   `HOOKS_OPT_OUT_PATHS`).
2. Let `njhook-dream-nightly` run at 3 PM, or trigger ad-hoc with
   `python dream/dream.py --since 24h`.
3. Future sessions automatically receive distilled memories on
   `SessionStart` (profile + tools + current-project sections) and
   `UserPromptSubmit` / `BeforeAgent` (hybrid recall ranked by RRF +
   in-project boost).
4. Curate with `njhook list / show / edit / delete`, surface candidates with
   `njhook patterns --promote`, dedupe with `njhook consolidate`, prune cold
   entries with `njhook archive`.
