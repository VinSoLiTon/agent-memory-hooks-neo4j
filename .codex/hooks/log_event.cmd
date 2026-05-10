@echo off
rem Windows wrapper invoked by Codex. Pipes stdin (hook JSON) to the shared logger.
python "%~dp0..\..\hooks\log_event.py" --client codex
