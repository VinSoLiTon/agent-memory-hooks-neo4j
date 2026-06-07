<!--
Migration ledger: unify njhook's LOCAL model onto a llama.cpp server (Gemma 12B),
off Ollama. One PR per slice, like the A–H program. Update this alongside each PR.
-->

# njhook — migrate the local model from Ollama → llama.cpp

**Goal:** unify the local LLM onto the llama.cpp servers already running in Docker, and stop depending on Ollama. Anthropic stays as the hybrid fallback.

## Verified infrastructure (2026-06-07)
| Service | Container | URL | Model | Notes |
|---|---|---|---|---|
| Chat | `infra-llama-1` | `http://127.0.0.1:8080/v1` | `gemma-4-12B-it-Q4_K_M.gguf` | OpenAI-compatible; `n_ctx=8192`; `response_format: json_schema` honored (GBNF-backed), `json_object` fallback works |
| Embeddings | `infra-embeddings-1` | `http://127.0.0.1:8081/v1` | `nomic-embed-text-v1.5.f16.gguf` | OpenAI-compatible; **768 dim — matches the stored Ollama vectors** (same family) → no forced reindex |

## Design decision
Add a **dedicated `llamacpp` provider** (chat + embed), NOT a reuse of the `openai` provider pointed at the base URL. Reason: `anthropic`/`openai` are treated as **remote** by the Phase H egress policy (`dream.egress_blocked`) and the hybrid-fallback logic. A `llamacpp` name is treated as **local** (like `ollama`): safe for sensitivity-tagged sessions, eligible as the local primary. Structured output uses `response_format: json_schema` (same structural guarantee Ollama got from `format=<schema>`).

## Plan (one PR per slice)
- **PR-1 — chat provider (dream + judge)** ✅ *this PR*. `dream/providers.py::dream_llamacpp`, `dream/judge.py::_llamacpp_judge`, few-shot prompt for the local model, `--provider llamacpp` everywhere. Local-egress invariant pinned. 8 unit tests (mocked) + live smoke against the real Gemma server.
- **PR-2 — embeddings** ✅. `hooks/embeddings.py::_embed_llamacpp` (`EMBED_PROVIDER=llamacpp`, OpenAI `/v1/embeddings` on `:8081`, dim 768; `LLAMACPP_EMBED_URL`, `EMBED_MODEL_LLAMACPP`). 4 unit tests (mocked). **A/B verified: cosine = 1.0000** between llama.cpp `nomic-embed-text-v1.5.f16` and ollama `nomic-embed-text` on the same texts → identical vector space → **no reindex needed**, stored vectors stay compatible.
- **PR-3 — health, nightly cutover, docs** ✅. `njhook health` probes the llama.cpp chat (`:8080`) + embed (`:8081`) servers when either provider is `llamacpp` (FAIL if the selected server is down). `dream/run_dream.cmd` now defaults the nightly to `DREAM_PROVIDER=llamacpp` + `EMBED_PROVIDER=llamacpp` (Anthropic hybrid fallback kept). Docs (cli/README, dream/README) updated; Ollama adapters kept as legacy. **Eval gate PASSED** before cutover: `eval-distillation --provider llamacpp` → both golden sessions valid/path/kind/grounded=1.0, coverage 0.67 & 1.0, overall **PASS**.

## Validation gate (before flipping the scheduled nightly)
Measure-before-wire — Gemma Q4 12B is weaker than Opus and the dream-stack notes flag gemma hallucination:
1. `njhook eval-distillation --provider llamacpp` — path/kind validity, grounding, fact-coverage on golden sessions.
2. `njhook eval-retrieval` after switching embeddings — hit@k/MRR unchanged.
3. Flip the nightly only if it passes. A-MAC grounding gate + anti-poisoning gate + Anthropic hybrid fallback stay on regardless.

## Env vars
- `LLAMACPP_CHAT_URL` (default `http://127.0.0.1:8080/v1`)
- `DREAM_LLAMACPP_MODEL` (default `gemma-4-12B-it-Q4_K_M.gguf`)
- `LLAMACPP_EMBED_URL` / `EMBED_MODEL_LLAMACPP` — PR-2.

## Pre-flight / risks
- **Durability** (root cause of the earlier outage): ensure `infra-llama-1` + `infra-embeddings-1` have a Docker `restart: unless-stopped` policy and Docker Desktop autostarts — else the nightly fails on reboot. (PR-3 health check covers detection.)
- **8K context:** keep `DREAM_EXISTING_CONTEXT_MODE=paths` (default) and ensure the transcript cap fits `n_ctx`.
- **Quality:** validate via the eval gate; keep Anthropic fallback.

## Open decisions
1. ~~Reindex embeddings on the cutover, or A/B-test first?~~ **RESOLVED (PR-2):** A/B cosine = 1.0000 (identical vector space) → **no reindex**; stored vectors stay valid.
2. Delete the Ollama adapters, or keep as legacy fallback? (lean: keep until Gemma proven over ~a week of nightlies.)

## Notes
- Unrelated pre-existing test: `test_hooks.py::test_inject_memory` fails on a populated dev graph (its seeded `profile/identity.md` is truncated out by the 4000-char SessionStart budget). Confirmed failing on `main` too — independent of this migration.
