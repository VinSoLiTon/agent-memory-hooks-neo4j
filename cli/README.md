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
| `EMBED_PROVIDER` | unset (semantic recall disabled) | `openai` or `ollama` |
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

Stack-readiness check. Walks 9 categories: Neo4j reachability,
constraints, indexes, hook wrappers, user-level configs, env vars, Ollama
daemon + embedding model, scheduled task, last dream log. Prints
`[OK] / [WARN] / [FAIL]` per row plus a summary. **Exit 1** on any FAIL.

```bash
./njhook.cmd health
```

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

Counts by client / kind / archived / embedded; lists top-accessed memories.

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
```
