#!/usr/bin/env python3
"""Extended golden corpus for the distillation DOE.

The 4 inline fixtures in eval_distillation.py are the CI regression sanity-check;
they're too few and too tiny to tell a real prompt/format change from the 12B's
run-to-run variance. This is the bigger, realistic set the DOE measures against:
multi-event sessions across the kind vocabulary, each with `facts` (distinctive
token-sets a good memory must capture) and `negatives` (ephemera — tool outputs,
transient values — that a disciplined distillation must NOT memorialize).

Same fixture shape as eval_distillation.GOLDEN_SESSIONS, so the same deterministic
`score()` applies. Keep facts to 2-3 distinctive tokens that co-occur in the body
of one good memory; keep negatives to tokens only present in ephemeral tool I/O.
"""

CORPUS = [
    {
        "name": "db-choice-decision",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Let's go with Postgres over DynamoDB for the orders service — we need "
                       "multi-row transactions and the team already knows SQL. Document that."},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "createdb orders_dev"},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "CREATE DATABASE\ndone in 0.4s"},
        ],
        "facts": [["postgres", "orders"], ["transactions", "sql"]],
        "negatives": [["createdb", "0.4s"], ["create", "database"]],
    },
    {
        "name": "async-preference",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "I strongly prefer async/await over callback chains in our Node services. "
                       "Also use 2-space indent, not tabs."},
        ],
        "facts": [["async", "await"], ["2-space", "indent"]],
        "negatives": [],
    },
    {
        "name": "secrets-constraint",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Hard rule: never commit secrets to git. All credentials go through "
                       "AWS Secrets Manager, loaded at runtime — no .env files in the repo."},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "git status"},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "nothing to commit, working tree clean"},
        ],
        "facts": [["secrets", "git"], ["aws", "manager"]],
        "negatives": [["working", "tree", "clean"]],
    },
    {
        "name": "deploy-procedure",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Our deploy procedure: merge to main, CI builds the image, then I "
                       "manually promote staging to prod via the GitHub Actions 'promote' "
                       "workflow after smoke tests pass."},
        ],
        "facts": [["deploy", "staging", "prod"], ["promote", "workflow"]],
        "negatives": [],
    },
    {
        "name": "migration-incident",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "We just had an outage: an Alembic migration locked the users table for "
                       "8 minutes under load because it lacked a lock_timeout. Always set "
                       "lock_timeout on ALTER TABLE against busy tables."},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "ERROR: canceling statement due to lock timeout (pid 4821)"},
        ],
        "facts": [["lock_timeout", "alter"], ["migration", "outage"]],
        "negatives": [["pid", "4821"], ["canceling", "statement"]],
    },
    {
        "name": "grep-toolpattern",
        "events": [
            {"event_name": "UserPromptSubmit", "prompt": "find every TODO across the python files"},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "rg -n --type py 'TODO' ."},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "app/main.py:42: # TODO refactor\napp/db.py:7: # TODO index"},
            {"event_name": "UserPromptSubmit",
             "prompt": "Good — remember we use ripgrep (rg) with --type py for scoped searches, "
                       "it's much faster than find+grep here."},
        ],
        "facts": [["ripgrep", "type"], ["faster", "find"]],
        "negatives": [["main.py", "refactor"], ["db.py", "index"]],
    },
    {
        "name": "perf-learning",
        "events": [
            {"event_name": "UserPromptSubmit", "prompt": "why is the dashboard query slow?"},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "EXPLAIN ANALYZE SELECT ..."},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "Seq Scan on events ... rows=2100000 ... 3400ms"},
            {"event_name": "UserPromptSubmit",
             "prompt": "Right — the events table needs an index on session_id; the seq scan "
                       "over 2M rows is the bottleneck. Learned: always index the join key."},
        ],
        "facts": [["index", "session_id"], ["seq", "scan", "bottleneck"]],
        "negatives": [["3400ms"], ["rows", "2100000"]],
    },
    {
        "name": "project-stack-fact",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "For context on this repo: it's a FastAPI backend with Celery workers, "
                       "Redis as the broker, and Postgres for storage. Python 3.12."},
        ],
        "facts": [["fastapi", "celery"], ["redis", "broker"]],
        "negatives": [],
    },
    {
        "name": "review-projectrule",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Project rule for this codebase: every PR needs one approving review and "
                       "a green CI run before merge. No direct pushes to main, ever."},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "git push origin main"},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "remote: error: protected branch hook declined"},
        ],
        "facts": [["approving", "review"], ["no", "direct", "main"]],
        "negatives": [["protected", "branch", "declined"]],
    },
    {
        "name": "multi-fact-session",
        "events": [
            {"event_name": "UserPromptSubmit",
             "prompt": "Two things to remember: I deploy to AWS eu-west-2 (London), and our "
                       "error budget is 99.9% monthly uptime — page me if we breach it."},
            {"event_name": "PreToolUse", "tool_name": "Bash", "tool_input": "aws configure list"},
            {"event_name": "PostToolUse", "tool_name": "Bash", "tool_response": "region eu-west-2\naccess_key ****1234"},
        ],
        # two distinct facts that ideally land in two memories
        "facts": [["eu-west-2", "london"], ["error", "budget", "uptime"]],
        "negatives": [["1234"], ["configure", "list"]],
    },
]
