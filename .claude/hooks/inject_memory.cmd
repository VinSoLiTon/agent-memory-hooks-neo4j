@echo off
rem Windows wrapper invoked by Claude Code for the memory injector.
rem Deployed recall config (fix/project-scoped-recall): hard project scoping +
rem tightened injection budget. Code default stays "soft" — these knobs are the
rem only place the hard behaviour ships, so dashboard/CLI callers are untouched.
set INJECT_PROJECT_SCOPE=hard
set INJECT_CHAR_BUDGET=1800
set INJECT_PROJECT_LIMIT=3
set INJECT_PROFILE_LIMIT=2
set INJECT_TOOLS_LIMIT=2
set INJECT_PROJECT_BOOST=2.0
python "%~dp0..\..\hooks\inject_memory.py" --client claude_code
