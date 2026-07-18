#!/bin/bash
# Wrapper invoked by Claude Code for the memory injector.
# Deployed recall config (fix/project-scoped-recall): hard project scoping +
# tightened injection budget — mirrors inject_memory.cmd so both platforms run
# the same policy. Code default stays "soft"; dashboard/CLI are untouched.
export INJECT_PROJECT_SCOPE=hard
export INJECT_CHAR_BUDGET=1800
export INJECT_PROJECT_LIMIT=3
export INJECT_PROFILE_LIMIT=2
export INJECT_TOOLS_LIMIT=2
export INJECT_PROJECT_BOOST=2.0
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$REPO_ROOT/hooks/inject_memory.py" --client claude_code
