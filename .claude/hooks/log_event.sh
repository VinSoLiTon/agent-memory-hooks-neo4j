#!/bin/bash
# Wrapper invoked by Claude Code. Pipes stdin (hook JSON) to the shared logger.
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
exec python3 "$REPO_ROOT/hooks/log_event.py" --client claude_code
