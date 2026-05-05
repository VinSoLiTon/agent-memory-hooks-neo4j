# Dream phase

Offline memory consolidation for the Claude Code hook log. Reads recent
session events from Neo4j, asks Claude to distill them into durable
markdown-style memories, and writes them back as `:Memory` nodes.

The hooks capture *what happened*. The dream phase decides *what's worth
remembering* — user profile, tool-usage patterns, project context — so
future sessions can read it cold.

## Setup

```bash
pip install -r requirements.txt
export ANTHROPIC_API_KEY=sk-ant-...
# Neo4j env vars are optional; defaults: bolt://localhost:7687, neo4j/password
# export HOOKS_NEO4J_URI=...
# export HOOKS_NEO4J_USER=...
# export HOOKS_NEO4J_PASSWORD=...
# export DREAM_MODEL=claude-opus-4-7   # optional override
```

## Usage

```bash
# Dream over every session with events newer than its last-dreamed watermark
python dream.py

# Dream over a single session
python dream.py --session <session_id>

# Only consider events from the last 24h / 7d / 30m
python dream.py --since 24h

# Print what would be written without touching Neo4j
python dream.py --dry-run
```

## Schema

Memories imitate markdown files — each `:Memory` node has a `path` and a
`content` field holding the full markdown body (YAML frontmatter + prose).

```
(:Memory {path, content, updated_at})           // path is unique
(:Memory)-[:DERIVED_FROM]->(:Session)
(:Session)-[:DREAMED]->(:Memory)
(:Session {..., last_dreamed_at})                // high-water mark
```

Paths are organized semantically by topic, e.g.:

```
profile/role.md
profile/preferences.md
tools/bash/common-flags.md
tools/edit/conventions.md
project/<short-slug>.md
general/<short-slug>.md
```

## How re-runs work

Each `:Session` carries a `last_dreamed_at` watermark. A session is
re-dreamed when it has events with `timestamp > last_dreamed_at` (or has
never been dreamed). Only the *new* events are sent to the LLM, but the
*full* set of existing memories is passed alongside so the model can merge
new evidence into prior memory bodies.

Memory writes are upserts on `path`: emitting the same path overwrites the
content. The model has no way to delete or rename — for those, edit Neo4j
directly.

## Inspecting

```bash
# List memories
cypher-shell -u neo4j -p password \
  "MATCH (m:Memory) RETURN m.path, m.updated_at ORDER BY m.path"

# Read one
cypher-shell -u neo4j -p password \
  "MATCH (m:Memory {path: 'profile/role.md'}) RETURN m.content"

# Which sessions contributed to a memory
cypher-shell -u neo4j -p password \
  "MATCH (m:Memory {path: 'profile/role.md'})-[:DERIVED_FROM]->(s:Session)
   RETURN s.session_id, s.last_dreamed_at"
```

## Resetting

```cypher
// Re-dream one session from scratch
MATCH (s:Session {session_id: '<id>'}) REMOVE s.last_dreamed_at;

// Wipe all memories and watermarks
MATCH (m:Memory) DETACH DELETE m;
MATCH (s:Session) REMOVE s.last_dreamed_at;
```
