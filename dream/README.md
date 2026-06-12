# Dream phase

Offline memory consolidation for the agent-memory hooks. Reads recent
session events from Neo4j, asks an LLM (a local **llama.cpp** server — the
default, e.g. Gemma 12B — or Anthropic / OpenAI; Ollama is still supported as a
legacy local backend) to distill them into durable markdown-style memories, and
writes them back as `:Memory` nodes — with embeddings if `EMBED_PROVIDER` is set.

Hooks capture *what happened*. The dream phase decides *what's worth
remembering* — user profile, tool-usage patterns, project context — so
future sessions can read it cold across any of the four supported CLIs
(Claude Code, Codex, Cursor, Gemini).

### Admission gates & quality (Phases D, E, H)

Before a distilled memory becomes `active`, `write_memories` applies several
gates (each tunable; see `cli/README.md` env table):

- **Quality gate** (`quality.validate_memory`) — valid frontmatter, in-vocab path
  bucket, and a semantic `kind` from the 15-type vocabulary (`memory_types.py`;
  legacy bucket labels accepted during a migration window). `kind` is stamped as
  a queryable `m.kind` node property.
- **A-MAC grounding gate** (Phase D2) — a NEW memory whose body doesn't overlap
  the source transcript (`< DREAM_GROUNDING_MIN`) is routed to `pending_review`,
  not `active`.
- **Critique / faithfulness pass** (item #18, opt-in `DREAM_CRITIQUE=1`) — the
  semantic complement to grounding. Grounding (token overlap) passes a fluent
  hallucination that reuses the session's words but inverts a value ("port 5000"
  → "port 9999"); this pass asks the LLM whether each NEW candidate is *faithful*
  to the bounded transcript and quarantines the ones that aren't (`critic.py`).
  Lenient-by-failure — the inverse of the contradiction judge: it returns
  "faithful" on any error/ambiguity, so a flaky model can only miss a
  hallucination, never wrongly quarantine a good memory. NEW-only; updates to an
  existing-active memory are exempt. Transcript capped at `DREAM_CRITIQUE_MAX_CHARS`
  (default 12000).
- **Anti-poisoning gate** (Phase H3) — a NEW directive memory from a thin, novel
  session is quarantined to `pending_review` (`quality.poisoning_risk`).
- **Contradiction check** (Phase E) — asks the LLM whether each new memory
  contradicts an active one; on a hit, links `:CONTRADICTS` and quarantines the
  NEW memory while the established one stays active. **On by default in the
  nightly** (`run_dream.cmd` sets `DREAM_CONTRADICTION_CHECK=1`; set `=0` to
  disable); also available ad-hoc via `--check-contradictions`. The judge is
  conservative-by-failure (returns "no" on any doubt), so it can only miss a
  contradiction, never wrongly quarantine. Candidates come from **two channels**:
  vector neighbours (semantic) ∪ Lucene fulltext (lexical) — the latter catches
  antonym-style contradictions ("deploy via Docker" vs "never containers") that
  score low on cosine.

Pending/quarantined/contradicted memories are advisory-only (recall injects
`active` only); adjudicate with `njhook review`. Evaluate output quality with
`njhook eval-retrieval` (deterministic) and `njhook eval-distillation` (per-provider).

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

`run_dream.cmd` defaults `DREAM_PROVIDER=llamacpp` + `EMBED_PROVIDER=llamacpp`
(the local Gemma 12B / nomic-embed servers; see `docs/LLAMACPP_MIGRATION.md`)
with `DREAM_FALLBACK_PROVIDER=anthropic` as the 0-yield/error safety net.
Override via User-scope env vars. Logs at `dream/logs/dream_YYYY-MM-DD.log`.

It runs **three logged stages** (the dedup + archival jobs already existed but
nothing scheduled them; each is mutually exclusive with distillation inside
`dream.py`, so they need their own invocations):

1. **distill** — `dream.py --since %DREAM_SINCE%` (default `36h`).
2. **consolidate** — `dream.py --consolidate` (vector-similarity merge of
   near-duplicates; `DREAM_CONSOLIDATE_THRESHOLD` 0.92, `DREAM_CONSOLIDATE_ROUNDS` 5).
3. **archive** — `dream.py --archive` (flag memories untouched for
   `DREAM_STALE_DAYS`, default 60; excluded from recall, kept queryable).

Each stage logs `<stage> start … / <stage> end exit=<rc>` so a failure in one is
visible and does not abort the others. Set `DREAM_SKIP_MAINTENANCE=1` for an
ad-hoc distill-only run.

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
has events newer than that watermark (or has never been dreamed). On a
**clean** result — the model ran and produced memories, or genuinely found
nothing — the watermark advances even on a 0-yield, so low-signal sessions
are never re-dreamed forever. Existing memories are passed to the model
alongside new events so it can merge updates by path rather than duplicate.

**Compact transcript** (`DREAM_COMPACT_TRANSCRIPT=1`, opt-in). A denser "dream
language" for the events fed to the model: each PreToolUse+PostToolUse pair
collapses to one `tool(input) -> output` line (a tool call costs one header, not
two), the 32-char ISO timestamp becomes a short `#index`, and event names shrink
to markers — the high-signal prompt body stays full. On real sessions this is
**~41% fewer chars / ~50% more events** per dream call (`scripts/dream_encoding_ab.py`).
A/B held quality: `eval-distillation` scores were identical to the verbose render,
and on a tool-heavy session compact distilled 5 memories where the verbose render
truncated its JSON output. Default-off.

**Short-session skip.** A session with fewer than `DREAM_MIN_EVENTS` events
(default **2**) is a lone SessionStart / single prompt with no cross-event
pattern to distill — in practice ~89% of sessions. It is skipped **without an
LLM call**; the watermark still advances so it's retired (re-dreamed only if it
later grows past the threshold). Set `DREAM_MIN_EVENTS=1` to disable. Skips are
counted in the `short` column of `njhook dream-stats`.

**Transient-failure back-off.** A 0-yield caused by a provider *error* (the
local model busy / unreachable / a timeout) is NOT treated as an empty
session: the session is **deferred** — the watermark is left where it is and
the next scheduled run retries it — and there is no fall-back egress on a
blip. This matters in full-offline mode (`DREAM_FALLBACK_PROVIDER=none`):
without it, every llama.cpp hiccup would silently drop the in-flight
sessions. Deferred counts surface in `njhook dream-stats` (the `defer`
column) and on the dashboard `/nightly` ledger. The distinction is by
failure mode: a thrown provider error defers; a clean empty result advances.

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
