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
rem DREAM_CONTRADICTION_CHECK=0 to disable. (Applies to the distill stage below.)
if "%DREAM_CONTRADICTION_CHECK%"=="" set "DREAM_CONTRADICTION_CHECK=1"

rem Nightly window + maintenance knobs (conservative defaults). The dedup and
rem archival jobs already existed (consolidate.py / `njhook consolidate|archive`)
rem but nothing scheduled them, so graph hygiene depended on a human remembering
rem to run them. They run as separate stages below because each is mutually
rem exclusive with distillation inside dream.py (early return). Each stage logs
rem its own start/end+exit so a failure in one is visible and does not abort the
rem others. Set DREAM_SKIP_MAINTENANCE=1 to run only the distill stage.
if "%DREAM_SINCE%"=="" set "DREAM_SINCE=36h"
if "%DREAM_CONSOLIDATE_THRESHOLD%"=="" set "DREAM_CONSOLIDATE_THRESHOLD=0.92"
if "%DREAM_CONSOLIDATE_ROUNDS%"=="" set "DREAM_CONSOLIDATE_ROUNDS=5"
if "%DREAM_STALE_DAYS%"=="" set "DREAM_STALE_DAYS=60"
if "%EVENT_RETENTION_DAYS%"=="" set "EVENT_RETENTION_DAYS=30"

rem --- stage 1: distill recent sessions into memories -------------------------
echo [%date% %time%] dream distill start (provider=%DREAM_PROVIDER% embed=%EMBED_PROVIDER% since=%DREAM_SINCE% contradiction=%DREAM_CONTRADICTION_CHECK%) >> "%LOG%"
python dream\dream.py --since %DREAM_SINCE% >> "%LOG%" 2>&1
echo [%date% %time%] dream distill end exit=%errorlevel% >> "%LOG%"

if "%DREAM_SKIP_MAINTENANCE%"=="1" goto :done

rem --- stage 2: consolidate near-duplicate memories (vector-similarity merge) --
echo [%date% %time%] dream consolidate start (threshold=%DREAM_CONSOLIDATE_THRESHOLD% rounds=%DREAM_CONSOLIDATE_ROUNDS%) >> "%LOG%"
python dream\dream.py --consolidate --consolidate-threshold %DREAM_CONSOLIDATE_THRESHOLD% --consolidate-rounds %DREAM_CONSOLIDATE_ROUNDS% >> "%LOG%" 2>&1
echo [%date% %time%] dream consolidate end exit=%errorlevel% >> "%LOG%"

rem --- stage 3: archive stale memories (excluded from recall, kept queryable) --
echo [%date% %time%] dream archive start (stale_days=%DREAM_STALE_DAYS%) >> "%LOG%"
python dream\dream.py --archive --stale-days %DREAM_STALE_DAYS% >> "%LOG%" 2>&1
echo [%date% %time%] dream archive end exit=%errorlevel% >> "%LOG%"

rem --- stage 4: Tier-1 down-tier old dreamed events (blank heavy text, keep chain).
rem Reversible + lineage-preserving. OFF by default (DREAM_PRUNE_EVENTS=1 to enable)
rem until validated on this graph; prompt is kept unless DREAM_TIER_BLANK_PROMPT=1.
if not "%DREAM_PRUNE_EVENTS%"=="1" goto :done
echo [%date% %time%] dream prune-events start (retention_days=%EVENT_RETENTION_DAYS%) >> "%LOG%"
if "%DREAM_TIER_BLANK_PROMPT%"=="1" (
  python cli\njhook.py prune-events --retention-days %EVENT_RETENTION_DAYS% --blank-prompt >> "%LOG%" 2>&1
) else (
  python cli\njhook.py prune-events --retention-days %EVENT_RETENTION_DAYS% >> "%LOG%" 2>&1
)
echo [%date% %time%] dream prune-events end exit=%errorlevel% >> "%LOG%"

:done
echo [%date% %time%] dream run end >> "%LOG%"
