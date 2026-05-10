@echo off
setlocal
cd /d "%~dp0\.."
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOG=%~dp0logs\dream_%date:~10,4%-%date:~4,2%-%date:~7,2%.log"

rem Action D: nightly uses local Ollama with gemma4 by default (free, private,
rem better extraction quality than qwen3.5 in our eval — 4 memories vs 2).
rem Latency doesn't matter at 3 AM. Override by exporting DREAM_PROVIDER /
rem DREAM_OLLAMA_MODEL in your User env if you want a different default.
if "%DREAM_PROVIDER%"=="" set "DREAM_PROVIDER=ollama"
if "%DREAM_OLLAMA_MODEL%"=="" set "DREAM_OLLAMA_MODEL=gemma4:latest"

echo [%date% %time%] dream run start (provider=%DREAM_PROVIDER% model=%DREAM_OLLAMA_MODEL%) >> "%LOG%"
python dream\dream.py --since 36h >> "%LOG%" 2>&1
echo [%date% %time%] dream run end exit=%errorlevel% >> "%LOG%"
