@echo off
setlocal
cd /d "%~dp0\.."
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOG=%~dp0logs\dream_%date:~10,4%-%date:~4,2%-%date:~7,2%.log"

rem Nightly uses local Ollama with qwen3.5 by default (free, private,
rem fast, and clean — gemma4 captured slightly more per session but had a
rem merge-vs-replace pollution failure mode that consolidate can't fix
rem within a single memory. qwen3.5's omissions self-heal across nights;
rem gemma4's pollution required manual `njhook edit` cleanup.
rem Override by exporting DREAM_PROVIDER / DREAM_OLLAMA_MODEL in User env.
rem Weekly Anthropic Opus consolidate handles cross-memory dedup separately.
if "%DREAM_PROVIDER%"=="" set "DREAM_PROVIDER=ollama"
if "%DREAM_OLLAMA_MODEL%"=="" set "DREAM_OLLAMA_MODEL=qwen3.5:latest"

rem Hybrid yield safety net: small local models distil nothing for large/real
rem sessions (verified: qwen3.5 returns empty, gemma4 hallucinates). When the local
rem model yields 0 memories for a session, that session is retried on Anthropic
rem (full context, reliable). Only the sessions the local model can't handle egress.
rem Set DREAM_FALLBACK_PROVIDER=none to keep the nightly fully local (no egress).
if "%DREAM_FALLBACK_PROVIDER%"=="" set "DREAM_FALLBACK_PROVIDER=anthropic"

echo [%date% %time%] dream run start (provider=%DREAM_PROVIDER% model=%DREAM_OLLAMA_MODEL%) >> "%LOG%"
python dream\dream.py --since 36h >> "%LOG%" 2>&1
echo [%date% %time%] dream run end exit=%errorlevel% >> "%LOG%"
