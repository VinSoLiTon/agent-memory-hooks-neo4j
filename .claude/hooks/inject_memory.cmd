@echo off
rem Windows wrapper invoked by Claude Code for the memory injector.
python "%~dp0..\..\hooks\inject_memory.py" --client claude_code
