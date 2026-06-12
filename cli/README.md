# `njhook` CLI reference

The CLI is the day-to-day interface for inspecting, curating, and operating
the agent-memory graph. Every command runs against the same Neo4j instance
the hooks talk to.

## Global

```bash
./njhook.cmd <subcommand> [args]
```

The `njhook.cmd` launcher just forwards args to `cli/njhook.py`. On POSIX
hosts, run `python cli/njhook.py <subcommand>` directly.

### Env vars consulted

Defaults in parentheses.

| Var | Default | Purpose |
|---|---|---|
| `HOOKS_NEO4J_URI` | `bolt://localhost:7687` | Neo4j Bolt endpoint |
| `HOOKS_NEO4J_USER` | `neo4j` | |
| `HOOKS_NEO4J_PASSWORD` | `password` | |
| `EMBED_PROVIDER` | unset (semantic recall disabled) | `llamacpp` (default local), `ollama`, or `openai` |
| `LLAMACPP_CHAT_URL` | `http://127.0.0.1:8090/v1` | llama.cpp chat endpoint — the shared `llama-swap` proxy on `:8090` (routes by `model`; `:8080` is retired) |
| `LLAMACPP_N_CTX` | probed → `8192` fallback | Per-request context window. llama-swap doesn't expose `/props`, so **set it explicitly** = the server's **per-slot** size: `-c N --parallel P` → `N/P` (e.g. `262144/4` → **`65536`**). Do NOT use the total `-c` — a request would exceed its slot and 400 |
| `DREAM_TRANSCRIPT_MAX_CHARS` | derived from `n_ctx` | Explicit transcript cap (overrides the derivation). A quality-validated value is recommended for big contexts, e.g. `90000` for a 64K slot |
| `DREAM_COMPACT_TRANSCRIPT` | unset | `1` = denser event encoding: merges each PreToolUse+PostToolUse into one `tool(input) -> output` line, drops ISO timestamps for an index. ~41% fewer chars / ~50% more events per dream call; A/B shows quality holds |
| `LLAMACPP_EMBED_URL` | `http://127.0.0.1:8081/v1` | llama.cpp embeddings server (e.g. docker `infra-embeddings`, nomic-embed) |
| `DREAM_LLAMACPP_MODEL` | `gemma-4-12B-it-Q4_K_M.gguf` | dream model id sent to the llama.cpp chat server |
| `DREAM_MIN_EVENTS` | `2` | skip sessions with fewer than N events (no LLM call; watermark still advances). `1` disables |
| `EMBED_MODEL_LLAMACPP` | `nomic-embed-text-v1.5.f16.gguf` | embedding model id (768-dim, same space as the Ollama nomic model) |
| `EMBED_MODEL_OPENAI` | `text-embedding-3-small` | |
| `EMBED_MODEL_OLLAMA` | `nomic-embed-text:latest` | |
| `EMBED_MODEL` | — | Override the active provider's default |
| `OLLAMA_HOST` | `http://localhost:11434` | |
| `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` | — | Required by their respective dream providers |
| `INJECT_PROFILE_LIMIT` | 5 | SessionStart `profile/*` cap |
| `INJECT_TOOLS_LIMIT` | 5 | SessionStart `tools/*` cap |
| `INJECT_PROJECT_LIMIT` | 5 | SessionStart `project/*` cap |
| `INJECT_CHAR_BUDGET` | 4000 | SessionStart total-chars soft cap |
| `INJECT_PROJECT_BOOST` | 0.5 | RRF tie-break for in-project hits |
| `DREAM_MEMORY_MIN_CHARS` | 30 | Quality-gate min body length |
| `DREAM_MEMORY_MAX_CHARS` | 20000 | Quality-gate max body length |
| `HOOKS_OPT_OUT_PATHS` | — | Semicolon-separated cwds to skip |
| `HOOKS_DISABLE_SCRUB=1` | — | Disable secret scrubbing (tests only) |
| `HOOKS_CAPTURE_TRANSCRIPT=1` | off | Enable transcript capture |
| `HOOKS_TRANSCRIPT_MAX_CHARS` | 20000 | Cap for captured transcripts |
| `DASHBOARD_WRITE=1` | off | Enable dashboard edit/delete/archive |
| `DASHBOARD_HOST` | `127.0.0.1` | |
| `DASHBOARD_PORT` | 5000 | |
| `HOOKS_CAPTURE_MODE` | `direct` | `spool` = durable append + later `ingest` (Phase B) |
| `HOOKS_SPOOL_DIR` | `~/.njhook/spool` | Spool + DLQ location |
| `HOOKS_DLQ_FAIL_RATE` | 5 | DLQ/hour above which `health` FAILs (Phase B) |
| `HOOKS_SENSITIVE_PATHS` | — | Semicolon cwds whose events are `high`-sensitivity (Phase H) |
| `DREAM_ALLOW_SENSITIVE_EGRESS=1` | off | Allow sensitive sessions to remote dream providers (Phase H) |
| `DREAM_GROUNDING_MIN` | 0.10 | A-MAC grounding floor; below → `pending_review` (Phase D2) |
| `DREAM_POISON_MIN_EVENTS` | 5 | Anti-poisoning: "thin session" threshold (Phase H3) |
| `DREAM_POISON_NOVELTY_MIN` | 0.6 | Anti-poisoning: novelty threshold (Phase H3) |
| `DREAM_CONTRADICTION_CHECK=1` | off | Nightly LLM contradiction auto-flag (Phase E) |
| `NJHOOK_REHEARSAL_DAYS` | 30 | Restore-rehearsal staleness WARN (Phase H4) |
| `NJHOOK_FRESHNESS_DAYS` | 7 | Dream-freshness staleness WARN |
| `DREAM_EVAL_GROUNDING_MIN` / `DREAM_EVAL_COVERAGE_MIN` | 0.30 / 0.66 | Distillation-eval thresholds (Phase D3) |

---

## Setup / health

### `migrate`

Run the full schema migration. Idempotent — run once after install or
after pulling schema-touching upgrades.

- Drops the legacy `Session.session_id UNIQUE` constraint if present.
- Creates `Session.session_key UNIQUE`, `Event.event_id UNIQUE`,
  `Memory.path UNIQUE`.
- Creates `memory_fulltext`, `memory_project`, `session_id_lookup` indexes.
- Backfills `session_key = "<client>:<session_id>"` on pre-PR-B sessions.

```bash
./njhook.cmd migrate
```

### `health`

Stack-readiness check. Walks the pipeline: Neo4j reachability, constraints,
indexes, hook wrappers, user-level configs, env vars, the local model backend
(**llama.cpp chat `:8090` (llama-swap) + embed `:8081`** when `*_PROVIDER=llamacpp`, or the
Ollama daemon + embed model when `=ollama`), scheduled task, last dream log,
dream freshness, **event spool / DLQ rate (Phase B)**, **egress policy (Phase H)**,
and **restore-rehearsal age (Phase H4)**.
Prints `[OK] / [WARN] / [FAIL]` per row plus a summary. **Exit 1** on any FAIL.
The spool row FAILs on a rising DLQ *rate*, not a static nonzero count.

```bash
./njhook.cmd health
```

### `migrate-kinds`

**Phase D1.** Re-tag memories whose body frontmatter still uses a legacy bucket
label (`profile/tool/project/general`) to the semantic `kind` vocabulary, and
backfill the queryable `m.kind` property. A legacy rewrite is an audited `edit`
(prior body snapshotted as a `:MemoryRevision`); an already-semantic body just
gets its property backfilled. Idempotent.

| Flag | Effect |
|---|---|
| `--dry-run` | Report what would change without writing |

---

## Memory inspection / curation

### `list`

Tabular list of memories. Excludes archived by default.

| Flag | Default | Effect |
|---|---|---|
| `--kind X` | — | Filter by top-level path component (`profile`, `tools`, `project`, `general`) |
| `--project X` | — | Filter by project tag |
| `--since 7d/24h/30m` | — | Only memories updated since the window |
| `--limit N` | 0 (unbounded) | |
| `--include-archived` | off | Show archived memories too |

### `show <path>`

Print the full content of one memory (frontmatter + body).

### `search <query>`

Fulltext search (Lucene over `m.content` + `m.path`). Special chars
escaped automatically.

| Flag | Default |
|---|---|
| `--min-score F` | 0.5 |
| `--limit N` | 10 |

### `edit <path>`

Open the memory in `$EDITOR` (or notepad on Windows). Saves changes
back to Neo4j on editor exit.

| Flag | Effect |
|---|---|
| `--create` | Allow creating a new memory at a path that doesn't exist |

### `delete <path>`

Detach-delete one memory.

| Flag | Effect |
|---|---|
| `-y / --yes` | Skip the interactive confirm |

### `unarchive <path>`

Sets `m.archived = false` on a previously-archived memory.

### `stats`

Memory counts **by bucket** (path prefix) AND **by semantic `kind`** (the
`m.kind` property; `untyped` until `migrate-kinds`), plus sessions by client and
total events.

---

## Review & conflicts (Phase E)

### `review <action> [paths…]`

Adjudicate the `pending_review` queue and `:CONTRADICTS` pairs. Recall only
injects `status='active'` memories, so flagged/pending ones are advisory-only
until resolved.

| Action | Effect |
|---|---|
| `list` | Show pending memories + contradiction pairs |
| `approve <path>` | → `active` (re-injected); audited |
| `reject <path>` | → `rejected` (hidden, kept); audited |
| `supersede <winner> <loser>` | winner active, loser `superseded` + `:SUPERSEDED_BY`; audited |
| `flag <a> <b>` | Mark two as `:CONTRADICTS` → both `pending_review` |
| `auto-resolve` | Resolve every open contradiction by authority×recency |

The nightly can auto-flag contradictions at write time with
`DREAM_CONTRADICTION_CHECK=1` (or `dream.py --check-contradictions`): a new memory
contradicting an active one is flagged + `pending_review` while the established
one stays active.

### `audit <path> | --recent [N]` (Phase H2)

A memory's full mutation log — every dream write, manual edit, and review
transition, time-ordered with actor + status, reconstructed from the
`:MemoryRevision` chain. `--recent [N]` gives a graph-wide view (default 20).

---

## Durable capture (Phase B)

### `ingest`

Drain the durable event spool into Neo4j. Idempotent replay — the
`Event.event_id` UNIQUE constraint is the inbox, so a crash mid-ingest can't
duplicate events; malformed records dead-letter (DLQ) and the worker continues;
older-schema records are upcast on read. Only does work when
`HOOKS_CAPTURE_MODE=spool` has been capturing.

```bash
./njhook.cmd ingest
```

---

## Sessions / events

### `sessions`

List captured sessions, keyed by `session_key` (composite
`<client>:<session_id>`).

| Flag | Default |
|---|---|
| `--client {claude_code,codex,cursor,gemini}` | — |
| `--since 7d/24h` | — |
| `--limit N` | 20 |

### `session <id> [-v]`

Walk events of one session. Accepts either the full `session_key` or
just the raw `session_id` for ergonomics. If a raw id matches multiple
sessions across clients, candidates are listed and you must rerun with
the explicit composite key.

| Flag | Effect |
|---|---|
| `-v / --verbose` | Include prompt / tool-input / tool-output snippets |

---

## Recall / embeddings

### `embed-backfill`

Compute embeddings for memories that don't have them yet. Requires
`EMBED_PROVIDER`. Lazily creates the `memory_embeddings` vector index
sized to the model's dimension.

| Flag | Default |
|---|---|
| `--force` | off — re-embed every memory |
| `--batch-size N` | 16 |

### `reindex`

Compares the active `EMBED_PROVIDER`'s model+dim to what's stored on
existing memories. On mismatch (or `--force`), drops the vector index,
clears stale `m.embedding`/`embedding_model`/`embedding_dim`, and
re-runs `embed-backfill` so every memory uses the current model.

| Flag | Effect |
|---|---|
| `--force` | Rebuild even when active matches stored |
| `--dry-run` | Preview only |

---

## Maintenance

### `consolidate`

LLM-merge near-duplicate memories. Walks the vector index for pairs
above a cosine-similarity threshold, asks the dream provider to merge
each pair, replaces both with the merged memory. Provenance is rewired
(every Session that DREAMED an original now DREAMs the merged one).
Requires `EMBED_PROVIDER` and a dream provider.

| Flag | Default |
|---|---|
| `--threshold F` | 0.92 |
| `--rounds N` | 10 |
| `--provider {anthropic,openai,ollama}` | from `$DREAM_PROVIDER` |
| `--dry-run` | preview |

### `archive`

Set `m.archived = true` on memories whose `last_accessed_at` AND
`updated_at` are both older than `--stale-days` days. `profile/*`
memories are exempt. Recall queries filter `archived=false`, so
archived memories vanish from sessions but stay queryable via
`list --include-archived`.

| Flag | Default |
|---|---|
| `--stale-days N` | 60 |
| `--dry-run` | preview |

---

## Backup / restore

### `backup`

JSON dump. Default = memories only (~10 KB for ~12 memories).
`--with-sessions` triggers the per-event streaming export and **requires
an explicit scope flag** (the OOM-safety guard from PR-I).

| Flag | Effect |
|---|---|
| `--out FILE` | Default: `njhook-backup-<timestamp>.json` in cwd |
| `--with-embeddings` | Include `m.embedding` vectors (large) |
| `--with-sessions` | Include sessions+events. Requires one of the next four. |
| `--since 7d/24h` | Only sessions created within window |
| `--session-key K` | One specific session |
| `--limit N` | N most-recent sessions |
| `--all-sessions` | Explicit unbounded — also requires `--no-tool-response` OR `--max-field-chars` (Python-memory guard) |
| `--no-tool-response` | Drop `tool_response` and `transcript` server-side (never fetched) |
| `--max-field-chars N` | Substring kept string fields server-side at N chars (`0` = unlimited) |

**Exit 2** if `--with-sessions` lacks a scope flag, or if
`--all-sessions` lacks a trim flag.

### `restore --in FILE`

Idempotent upsert from a backup. Memories merge by `path`; sessions
merge by `session_key`. Validates backup shape up front (missing
`event_id` / `path` / `session_key` / non-list `events` → abort with
rc=2) before any DB write. Always wipes a session's existing event
chain before rebuilding, so shorter or empty-events backups produce
the correct end state.

| Flag | Effect |
|---|---|
| `--in FILE` | **Required** |
| `--with-embeddings` | Restore `m.embedding` when present |
| `--dry-run` | Show first 5 of each, no writes |
| `--allow-malformed` | Skip malformed records (logs counts to stderr). Never fabricates `unknown:unknown` sentinel session keys. |

---

## Discovery

### `patterns`

Surfaces three classes of repeated signal across captured sessions:

- **commands** — exact-normalized Bash commands
- **files** — file paths repeatedly Read/Edit/Write'd
- **prompts** — `UserPromptSubmit` / `BeforeAgent` prompts greedily
  clustered by embedding cosine similarity (requires `EMBED_PROVIDER`)

Each surfaced pattern carries a stable 6-char `id` (sha1 of defining
content) so you can reference it across runs.

| Flag | Default |
|---|---|
| `--show {commands,files,prompts,all}` | `all` |
| `--min-count N` | 3 |
| `--since 7d/24h` | — |
| `--similarity F` | 0.8 (prompt-cluster threshold) |
| `--promote ID` | Convert the named pattern to a draft `:Memory`. Preview-only by default. |
| `--dry-run` | (with `--promote`) print draft, don't write |
| `-y / --yes` | (with `--promote`) actually write |

---

## Programmatic interfaces (Phase G)

These expose the same recall + capture core the hooks use, for runtimes that
aren't hook-capable. (REST API in `api/server.py`, MCP server in
`api/mcp_server.py` — both thin shells over `hooks/service.py`.)

### `recall <prompt>`

Ranked memory hits for a prompt over the shared engine (same ranking the hook
injects). `--cwd DIR` scopes to a project; `--limit N` (default 5); `--json` for
machine output.

### `write-event --client X [--json FILE]`

Capture an event from JSON (stdin or `--json FILE`) through the same scrub +
opt-out + spool/direct path the hooks use.

### `render --target {agents,claude,gemini,cursor,all}` (Phase G PR-3)

Render the project's memory into a runtime's startup context file
(`AGENTS.md` / `CLAUDE.md` / `GEMINI.md` / `.cursor/rules/njhook-memory.mdc`) as a
delimited **managed block** — human content outside the markers is never touched;
idempotent. `--root DIR` (default cwd) is also the project scope; `--stdout`
previews without writing.

---

## Governance & evaluation (Phases H, D3)

### `rehearse-restore` (Phase H4)

Prove the backup→restore pipeline works end-to-end on a disposable marker
subgraph (real backup → confirm captured → restore from the backup's own format →
verify → clean up), recording a `:RehearsalRun`. `health` reports its age and
FAILs if the last one failed. "Untested backups aren't backups."

### `eval-retrieval` (Phase D3)

Seed a golden query→path set and score recall (`hit@k` + `MRR`) — the ranking
regression guard. Deterministic (fulltext-only); seeds + cleans up its fixture.

### `eval-distillation [--provider X] [--model M]` (Phase D3)

Score dream **output quality** over golden sessions via a *real* provider
(structural validity + path bucket + `kind` enum + grounding + fact coverage),
printing a per-provider matrix. Opt-in (needs the provider SDK / Ollama); the same
deterministic scorer is CI-gated in the test suite.

---

## Common workflows

```bash
# After install
./njhook.cmd migrate
./njhook.cmd health

# After enabling EMBED_PROVIDER for the first time
./njhook.cmd embed-backfill

# After switching embedding models
./njhook.cmd reindex

# Daily inspection
./njhook.cmd stats
./njhook.cmd list --kind project --since 7d
./njhook.cmd patterns --since 7d

# Promote a recurring command pattern to a memory
./njhook.cmd patterns --promote 135e52        # preview
./njhook.cmd patterns --promote 135e52 -y     # write

# Clean up
./njhook.cmd consolidate --dry-run            # preview merges
./njhook.cmd consolidate                      # actually merge
./njhook.cmd archive --stale-days 60          # flag cold memories

# Disaster prep
./njhook.cmd backup --with-embeddings --out backup.json
./njhook.cmd backup --with-sessions --since 7d --no-tool-response \
                    --out sessions-week.json

# Disaster recovery
./njhook.cmd restore --in backup.json --with-embeddings --dry-run
./njhook.cmd restore --in backup.json --with-embeddings
./njhook.cmd rehearse-restore                 # prove restore works, on a schedule

# Review the pending/conflict queue
./njhook.cmd review list
./njhook.cmd review approve profile/role.md
./njhook.cmd audit profile/role.md            # full mutation log
./njhook.cmd audit --recent 20                # graph-wide

# Durable capture (set HOOKS_CAPTURE_MODE=spool first)
./njhook.cmd ingest                           # drain the spool into Neo4j

# Typed-kind migration (one-time, after upgrading)
./njhook.cmd migrate-kinds --dry-run
./njhook.cmd migrate-kinds

# Render memory into another runtime's context file
./njhook.cmd render --target all --root /path/to/repo

# Evals
./njhook.cmd eval-retrieval
./njhook.cmd eval-distillation --provider anthropic
```
