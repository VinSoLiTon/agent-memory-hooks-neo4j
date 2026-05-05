# Claude with Neo4j Memory Hooks

Claude Code hooks that store session events as a linked list in Neo4j.

## Setup

```bash
pip install -r requirements.txt
```

Set environment variables (or use defaults: bolt://localhost:7687, neo4j/password):
```bash
export HOOKS_NEO4J_URI=bolt://localhost:7687
export HOOKS_NEO4J_USER=neo4j
export HOOKS_NEO4J_PASSWORD=password
```

## Architecture

Each Claude Code session creates a graph structure:

```
(Session {session_id}) -[:FIRST_EVENT]-> (Event) -[:NEXT]-> (Event) -[:NEXT]-> ...
                       -[:LATEST_EVENT]-> (last Event)
```

Events captured: SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop.

## Testing

```bash
python test_hooks.py
```

Requires a running Neo4j instance.
