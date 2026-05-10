@echo off
rem Windows wrapper invoked by Codex for the memory injector.
python "%~dp0..\..\hooks\inject_memory.py" --client codex
