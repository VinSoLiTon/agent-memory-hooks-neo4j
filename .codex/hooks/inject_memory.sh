#!/bin/bash
# Wrapper invoked by Codex for the memory injector.
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$REPO_ROOT/hooks/inject_memory.py" --client codex
