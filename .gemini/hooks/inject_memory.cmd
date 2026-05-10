@echo off
rem Windows wrapper invoked by Gemini CLI for the memory injector.
python "%~dp0..\..\hooks\inject_memory.py" --client gemini
