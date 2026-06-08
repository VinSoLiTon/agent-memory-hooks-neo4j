@echo off
setlocal
cd /d "%~dp0\.."
if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOG=%~dp0logs\dream_%date:~10,4%-%date:~4,2%-%date:~7,2%.log"

rem Nightly uses the local llama.cpp server (Gemma 12B) by default — the docker
rem `infra-llama` container on :8080, OpenAI-compatible. Replaces the previous
rem Ollama/qwen3.5 backend (migration: docs/LLAMACPP_MIGRATION.md). Embeddings use
rem the local llama.cpp embeddings server (`infra-embeddings`, :8081). Override by
rem exporting DREAM_PROVIDER / EMBED_PROVIDER in User env.
if "%DREAM_PROVIDER%"=="" set "DREAM_PROVIDER=llamacpp"
if "%EMBED_PROVIDER%"=="" set "EMBED_PROVIDER=llamacpp"

rem Hybrid yield safety net: when the local model yields 0 memories for a session
rem (or errors), that session is retried on Anthropic (full context, reliable).
rem Only the sessions the local model can't handle egress. Set
rem DREAM_FALLBACK_PROVIDER=none to keep the nightly fully local (no egress).
if "%DREAM_FALLBACK_PROVIDER%"=="" set "DREAM_FALLBACK_PROVIDER=anthropic"

rem Contradiction detection ON by default in the nightly (it shipped off, so the
rem scheduled run never detected conflicts). The judge is conservative-by-failure
rem (returns False on any doubt) so this can only MISS contradictions, never wrongly
rem quarantine; candidates come from BOTH the vector and fulltext channels. Set
rem DREAM_CONTRADICTION_CHECK=0 to disable.
if "%DREAM_CONTRADICTION_CHECK%"=="" set "DREAM_CONTRADICTION_CHECK=1"

echo [%date% %time%] dream run start (provider=%DREAM_PROVIDER% embed=%EMBED_PROVIDER% contradiction=%DREAM_CONTRADICTION_CHECK%) >> "%LOG%"
python dream\dream.py --since 36h >> "%LOG%" 2>&1
echo [%date% %time%] dream run end exit=%errorlevel% >> "%LOG%"
