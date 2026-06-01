# Dream phase

Offline memory consolidation for the agent-memory hooks. Reads recent
session events from Neo4j, asks an LLM (Anthropic, OpenAI, or local Ollama)
to distill them into durable markdown-style memories, and writes them back
as `:Memory` nodes — with embeddings if `EMBED_PROVIDER` is set.

Hooks capture *what happened*. The dream phase decides *what's worth
remembering* — user profile, tool-usage patterns, project context — so
future sessions can read it cold across any of the four supported CLIs
(Claude Code, Codex, Cursor, Gemini).

## Setup

```bash
pip install -r requirements.txt

# Pick ONE provider (precedence: --provider flag > $DREAM_PROVIDER > anthropic)
export ANTHROPIC_API_KEY=sk-ant-...                  # for --provider anthropic (default)
export OPENAI_API_KEY=sk-...                         # for --provider openai
ollama pull qwen3.5                                   # for --provider ollama (no key)

# Neo4j env vars (optional; defaults shown)
# export HOOKS_NEO4J_URI=bolt://localhost:7687
# export HOOKS_NEO4J_USER=neo4j
# export HOOKS_NEO4J_PASSWORD=password

# Optional: override the default model per provider
# export DREAM_ANTHROPIC_MODEL=claude-opus-4-7
# export DREAM_OPENAI_MODEL=gpt-4o-mini
# export DREAM_OLLAMA_MODEL=qwen3.5:latest
```

## Usage

```bash
# Default — all dreamable sessions through the chosen provider
python dream.py

# Single session
python dream.py --session <session_id_or_session_key>

# Only events from the last 24h / 7d / 30m
python dream.py --since 24h

# Preview without writing
python dream.py --dry-run

# Pick a provider explicitly
python dream.py --provider ollama --model gemma4:latest
python dream.py --provider openai --model gpt-4o-mini

# Maintenance modes
python dream.py --consolidate                       # LLM-merge near-duplicates
python dream.py --consolidate --threshold 0.92 --consolidate-rounds 10
python dream.py --archive --stale-days 60           # flag cold memories
```

## Providers

| Provider | API key needed? | Cost | Privacy |
|---|---|---|---|
| anthropic | `ANTHROPIC_API_KEY` | per token | data leaves machine |
| openai | `OPENAI_API_KEY` | per token | data leaves machine |
| ollama | none | free | data stays local |

Ollama uses a per-provider system prompt with a few-shot example and a real
JSON Schema in the `format` field, plus a pre-filled `{"memories":[`
assistant turn — these together push smaller models (qwen3.5:14B,
gemma4:8B) into reliable structured output. See `prompts.py` and
`providers.py`.

## Eval harness

```bash
python eval.py --provider ollama --model qwen3.5:latest
python eval.py --provider anthropic
```

Seeds a synthetic Rust-engineer-at-Acme session through the live capture
hook, runs `dream.py --dry-run` with the chosen provider, and asserts:

- ≥ 2 memories produced
- every path matches `^(profile|tools|project|general)/.+\.md$`
- every body has YAML frontmatter (`title:` + `kind:`)
- at least one `project/*` memory (project-discrimination check)
- at least one expected topic keyword present

Use it as a regression gate when tuning prompts or swapping models.

## Scheduled runs (Windows Task Scheduler)

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\Projects\njhook\dream\run_dream.cmd"
$trigger = New-ScheduledTaskTrigger -Daily -At 3pm
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
  -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "njhook-dream-nightly" `
  -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited
```

`run_dream.cmd` defaults `DREAM_PROVIDER=ollama` and
`DREAM_OLLAMA_MODEL=qwen3.5:latest` (chosen for clean output and ~5s latency
— gemma4 had a merge-vs-replace pollution failure mode that consolidate
can't fix within a single memory). Override via User-scope env vars. Logs
at `dream/logs/dream_YYYY-MM-DD.log`.

## Schema

Memories imitate markdown files — each `:Memory` node has a `path` and
`content`, plus optional `project`, `embedding`, `archived`, `access_count`,
and provenance fields.

```
(:Memory {path, content, updated_at, project, archived, archived_at,
          access_count, last_accessed_at,
          embedding, embedding_model, embedding_dim,
          consolidated_from, promoted_from_pattern})
  -[:DERIVED_FROM]-> (:Session)
(:Session)-[:DREAMED]->(:Memory)
(:Session {session_key, session_id, ..., last_dreamed_at})
```

Path conventions:

```
profile/role.md                 # cross-project — who the user is
profile/preferences.md          # cross-project — workflow style
tools/<binary>/usage.md         # cross-project — tool conventions
project/<slug>.md               # scoped — per-project rules / architecture
general/<slug>.md               # scoped — cross-cutting notes
```

`profile/*` and `tools/*` memories are tagged `project = null`; everything
else carries the project slug derived from the dominant cwd of the session.
Recall boosts in-project hits via Reciprocal Rank Fusion (see
`hooks/inject_memory.py`).

## How re-runs work

Each `:Session` carries `last_dreamed_at`. A session is re-dreamed when it
has events newer than that watermark (or has never been dreamed). The
watermark **always advances** — even when the model returns no new memories
— so low-signal sessions are never re-billed. Existing memories are passed
to the model alongside new events so it can merge updates by path rather
than duplicate.

Memory writes are upserts on `path`. To delete or rename, use the CLI
(`njhook delete <path>`) or Neo4j directly.

## Consolidation and archive

```bash
python dream.py --consolidate --threshold 0.92    # LLM-merge near-duplicates
python dream.py --archive --stale-days 60         # flag cold memories
```

`--consolidate` walks the vector index for pairs above a cosine-similarity
threshold and asks the active provider to merge each pair into a single
memory. Provenance is rewired so every Session that DREAMED an original
also DREAMs the merged result.

`--archive` sets `m.archived = true` on memories whose `last_accessed_at`
AND `updated_at` are both older than `--stale-days` days. Profile memories
are exempt. Recall queries filter `coalesce(m.archived, false) = false` so
archived memories vanish from sessions — restore via
`njhook unarchive <path>`.

## Inspecting / curating via the CLI

Prefer the `njhook` CLI over raw Cypher for day-to-day work:

```bash
./njhook.cmd list
./njhook.cmd show profile/role.md
./njhook.cmd search "ripgrep"
./njhook.cmd edit project/foo.md
./njhook.cmd consolidate --dry-run
./njhook.cmd archive --stale-days 60 --dry-run
./njhook.cmd reindex --dry-run                  # detect embedding model drift
./njhook.cmd backup --out backup.json
```

## Resetting

```cypher
// Re-dream one session from scratch
MATCH (s:Session {session_key: '<key>'}) REMOVE s.last_dreamed_at;

// Wipe all memories and watermarks
MATCH (m:Memory) DETACH DELETE m;
MATCH (s:Session) REMOVE s.last_dreamed_at;

// Drop the vector index (reindex will recreate)
DROP INDEX memory_embeddings IF EXISTS;
```
