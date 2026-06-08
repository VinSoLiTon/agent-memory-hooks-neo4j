"""Per-provider system prompts for the dream phase.

Smaller local models (qwen3.5:14B class) need stricter formatting cues and
few-shot examples to reliably produce well-discriminated, valid JSON output.
Hosted frontier models (Opus, GPT-4o) handle the abstract version fine, so
we keep their prompt lean to save tokens.

Selection happens at call time via system_prompt_for(provider, model).
"""
from __future__ import annotations

from memory_types import MEMORY_KINDS  # Phase D1 — the kind enum (single source)


# Shared "core" rules — what counts as a memory, path conventions, etc.
_CORE_RULES = """\
You are the "dream phase" for a multi-CLI agent memory system. You receive a \
chronological log of session events (SessionStart, UserPromptSubmit / \
BeforeAgent, PreToolUse / BeforeTool, PostToolUse / AfterTool, Stop / \
SessionEnd) plus the set of markdown memories that already exist. Distill the \
session into durable markdown memories that will help future sessions across \
any CLI.

Each memory imitates a markdown file: it has a path and a markdown body with \
YAML frontmatter. Organize paths semantically by topic, e.g.:

  profile/role.md
  profile/preferences.md
  tools/bash/common-flags.md
  tools/edit/conventions.md
  project/<short-slug>.md
  general/<short-slug>.md

The PATH prefix says WHERE the memory is stored (profile/ tools/ project/ \
general/). The `kind` says WHAT KIND of knowledge it is — these are SEPARATE \
axes. `kind` is one of these 15 semantic types:
  preference   — a stated like/dislike or working-style choice
  projectrule  — a rule that governs how this project is built
  decision     — a choice that was made, with its rationale
  procedure    — a repeatable how-to / sequence of steps
  fact         — a stable piece of knowledge about the user/world
  constraint   — a hard limit or must-not (security, policy, perf)
  toolpattern  — a reusable way of using a tool/command
  incident     — something that went wrong + the lesson
  openquestion — an unresolved question to revisit
  commitment   — a promise/TODO the user or agent made
  goal         — an objective being worked toward
  context      — background/situational detail
  learning     — a newly-acquired insight
  observation  — a noted state of the world
  artifact     — a produced file/output worth remembering

Output STRICT JSON only, no prose, matching this schema:

{
  "memories": [
    {
      "path": "profile/role.md",
      "kind": "preference",
      "content": "---\\ntitle: User role\\nkind: preference\\n---\\n\\n<markdown body>",
      "importance": 8
    }
  ]
}

Set the top-level `kind` to one of the 15 types above, and mirror it in the \
body's frontmatter (`title` + `kind`). Pick the type by what the memory IS, not \
where it's stored: a preference in profile/ is `preference`, a build rule in \
project/ is `projectrule` or `constraint`.
Include `importance`: an integer 1-10 for how broadly and durably useful this \
memory is. Anchor your rating to these bands — and SPREAD ratings across them, \
do not default everything to 7-8:
  9-10  core identity or a standing hard rule that applies across most sessions \
(e.g. "user is a Rust engineer"; "never deploy without approval")
  7-8   a durable preference, project rule, or decision that will recur
  5-6   useful context or a procedure scoped to one area
  3-4   a narrow tool detail or a fact that ages quickly
  1-2   trivial / near-ephemeral — rarely worth recalling
The body should be tight markdown a future agent can read cold."""


_CORE_DISCRIMINATION = """\
Project-discrimination rule (HARD):
- profile/* and tools/* memories are CROSS-PROJECT. Anything you put there \
applies to every project the user works on.
- project/* and general/* memories are scoped. NEVER merge events from one \
project into a memory tagged with another project. If the new session's \
working directory or tooling is unrelated to an existing memory, create a NEW \
memory at a NEW path. Do not stuff unrelated rules into a memory whose title \
already pins it to a different project."""


_CORE_MERGE_RULES = """\
Merge / dedup rules:
- If a memory at the same path already exists, return an UPDATED full body \
that merges new evidence with the prior content. Do not duplicate facts. \
Remove anything the new events contradict.
- Skip ephemeral details (one-off filenames, debug output) and anything \
obvious from a fresh repo read (paths, git history).
- Prefer fewer, sharper memories over many vague ones.
- If nothing is worth remembering, return {"memories": []}.
- Each memory must stand alone — a future agent reads it without this transcript."""


# --- Anthropic / OpenAI: lean prompt, no examples (frontier models follow rules well) ---
_FRONTIER_PROMPT = "\n\n".join([_CORE_RULES, _CORE_DISCRIMINATION, _CORE_MERGE_RULES])


# --- Ollama: same rules + concrete few-shot example to anchor format/scope ---
_OLLAMA_FEWSHOT = """\
Example input (events):
[2026-05-09T10:00:00] UserPromptSubmit
  prompt: I'm a Rust systems engineer. We had a UAF last sprint — no unsafe blocks unless I OK it.
[2026-05-09T10:01:00] PreToolUse tool=Bash
  input:  cargo test
[2026-05-09T10:01:30] PostToolUse tool=Bash
  output: 42 passed in 1.2s

Example output (JSON):
{"memories":[
  {"path":"profile/role.md","kind":"fact","content":"---\\ntitle: User role\\nkind: fact\\n---\\n\\nRust systems engineer.","importance":9},
  {"path":"project/rust-safety.md","kind":"constraint","content":"---\\ntitle: Rust safety rules\\nkind: constraint\\n---\\n\\n- No `unsafe` blocks without explicit user approval.\\n- Rationale: UAF incident last sprint.","importance":7}
]}

Note: the role memory went to profile/ (cross-project) and is a `fact`. The safety rule \
went to project/ (scoped to this codebase) and is a `constraint` — the PATH and the \
`kind` are chosen independently. Two distinct memories rather than one stuffed one."""


_OLLAMA_PROMPT = "\n\n".join([
    _CORE_RULES,
    _CORE_DISCRIMINATION,
    _CORE_MERGE_RULES,
    _OLLAMA_FEWSHOT,
])


# --- JSON Schema for Ollama format= constrained output ----------------------

DREAM_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "memories": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "pattern": r"^(profile|tools|project|general)/[A-Za-z0-9._/-]+\.md$",
                    },
                    # Phase D1: first-class semantic type, constrained to the
                    # closed vocabulary so the local model can't invent a kind.
                    "kind": {"type": "string", "enum": sorted(MEMORY_KINDS)},
                    "content": {"type": "string", "minLength": 10},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 10},
                },
                # importance is REQUIRED here so the schema-constrained local path
                # (ollama format=, llama.cpp/openai json_schema) must emit it —
                # killing the degenerate "omitted → flat default 5" distribution
                # on the 12B model. The anthropic json_object fallback path does
                # not enforce this, so it stays optional there (handled by the
                # kind-prior floor in dream._coerce_importance).
                "required": ["path", "content", "importance"],
            },
        }
    },
    "required": ["memories"],
}


# --- Public entry point -----------------------------------------------------

def system_prompt_for(provider: str, model: str | None = None) -> str:
    """Return the right system prompt for the given provider. `model` is
    accepted for future per-model variants but not used today. Local
    small/mid models (ollama, llama.cpp) get the few-shot prompt; hosted
    frontier models get the lean one."""
    if provider in ("ollama", "llamacpp"):
        return _OLLAMA_PROMPT
    return _FRONTIER_PROMPT
