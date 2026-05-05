# Claude with Neo4j Memory Hooks

A two-stage memory system for [Claude Code](https://claude.com/claude-code),
backed by Neo4j.

1. **Hooks (online)** — capture every Claude Code session event into a graph
   as it happens.
2. **Dream phase (offline)** — periodically read those events and distill
   them into durable, markdown-style memories that future sessions can use.

The hooks record *what happened*. The dream phase decides *what's worth
remembering*.

## Repo layout

```
.claude/
  settings.json            # registers the hooks with Claude Code
  hooks/
    log_event.sh           # entrypoint invoked by Claude Code
    log_event.py           # writes the event into Neo4j
dream/
  dream.py                 # offline consolidation script
  README.md                # dream-phase docs
  requirements.txt
requirements.txt           # hook deps (just neo4j driver)
test_hooks.py              # smoke test for the hook writer
```

## Stage 1 — Hooks

Each Claude Code session becomes a linked list of events:

```
(Session {session_id}) -[:FIRST_EVENT]->  (Event) -[:NEXT]-> (Event) -[:NEXT]-> ...
                       -[:LATEST_EVENT]-> (last Event)
```

Events captured: `SessionStart`, `UserPromptSubmit`, `PreToolUse`,
`PostToolUse`, `Stop`. Each `:Event` stores the raw hook payload — prompt,
tool name, tool input, tool response, transcript snapshot, etc.

### Setup

```bash
pip install -r requirements.txt
# defaults assume bolt://localhost:7687 with neo4j/password
export HOOKS_NEO4J_URI=bolt://localhost:7687
export HOOKS_NEO4J_USER=neo4j
export HOOKS_NEO4J_PASSWORD=password
```

The hooks are already wired up in `.claude/settings.json` for this repo —
just run Claude Code from this directory and events stream into Neo4j.

### Test

```bash
python test_hooks.py    # requires a running Neo4j
```

## Stage 2 — Dream phase

Reads sessions that have events newer than their `last_dreamed_at`
watermark, asks Claude to extract durable memories, and upserts them as
`:Memory` nodes whose `path` + `content` imitate a markdown file.

```bash
pip install -r dream/requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...

python dream/dream.py              # all sessions with new events
python dream/dream.py --since 24h  # only events from last 24h
python dream/dream.py --dry-run    # preview without writing
```

Memory paths are organized semantically:

```
profile/role.md
profile/preferences.md
tools/bash/common-flags.md
project/<slug>.md
general/<slug>.md
```

See [dream/README.md](dream/README.md) for full docs (schema, re-run
behavior, inspect/reset queries).

## Full graph schema

```
(:Session {session_id, created_at, last_dreamed_at})
  -[:FIRST_EVENT]->  (:Event)
  -[:LATEST_EVENT]-> (:Event)
  -[:DREAMED]->      (:Memory)

(:Event {event_id, event_name, timestamp, tool_name, tool_input,
         tool_response, prompt, model, source, transcript_path, transcript, cwd})
  -[:NEXT]-> (:Event)

(:Memory {path, content, updated_at})              // path unique
  -[:DERIVED_FROM]-> (:Session)
```

## Suggested workflow

1. Use Claude Code as normal — hooks capture everything.
2. Run `python dream/dream.py` on a cadence that suits you (manually,
   nightly cron, or after each session).
3. Future sessions / agents can read `:Memory` nodes by path to get a fast
   profile of who the user is, what tools work well, and what's going on
   in the project.
