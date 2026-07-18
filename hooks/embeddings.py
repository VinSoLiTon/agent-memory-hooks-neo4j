"""Embedding providers for semantic memory recall.

Two adapters with the same shape:
    embed(texts: list[str]) -> list[list[float]]

Selection: EMBED_PROVIDER env var ('openai' | 'ollama' | 'llamacpp' | unset).
When unset, semantic recall is disabled and inject_memory falls back to
fulltext-only — existing behavior is preserved.

Models (override via EMBED_MODEL):
  openai   → text-embedding-3-small (1536 dim)
  ollama   → nomic-embed-text:latest (768 dim) — must be pulled first:
                ollama pull nomic-embed-text
  llamacpp → nomic-embed-text-v1.5.f16.gguf (768 dim) via a local llama.cpp
                embeddings server (LLAMACPP_EMBED_URL, default :8081/v1)

Anthropic doesn't expose an embeddings API as of writing — picking openai
or ollama is the practical menu.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Callable

EMBED_PROVIDER = os.environ.get("EMBED_PROVIDER", "").lower()

DEFAULT_MODELS = {
    "openai": os.environ.get("EMBED_MODEL_OPENAI", "text-embedding-3-small"),
    "ollama": os.environ.get("EMBED_MODEL_OLLAMA", "nomic-embed-text:latest"),
    "llamacpp": os.environ.get("EMBED_MODEL_LLAMACPP", "nomic-embed-text-v1.5.f16.gguf"),
}
# Common dimensions; auto-detected on first call if not listed.
KNOWN_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "nomic-embed-text-v1.5.f16.gguf": 768,   # llama.cpp; same 768-dim space as the Ollama model
    "mxbai-embed-large": 1024,
    "mxbai-embed-large:latest": 1024,
}

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# llama.cpp embeddings server's OpenAI-compatible base URL (e.g. the docker
# `infra-embeddings` container). Local.
LLAMACPP_EMBED_URL = os.environ.get("LLAMACPP_EMBED_URL", "http://127.0.0.1:8081/v1")


def is_enabled() -> bool:
    return EMBED_PROVIDER in ("openai", "ollama", "llamacpp")


def model() -> str:
    if not is_enabled():
        raise RuntimeError("embeddings disabled — set EMBED_PROVIDER=openai|ollama")
    explicit = os.environ.get("EMBED_MODEL")
    return explicit or DEFAULT_MODELS[EMBED_PROVIDER]


def dim() -> int:
    """Return the embedding dimension for the active model. Auto-detects by
    calling embed once if the model isn't in KNOWN_DIMS."""
    m = model()
    if m in KNOWN_DIMS:
        return KNOWN_DIMS[m]
    probe = embed(["dimension probe"])
    if not probe or not probe[0]:
        raise RuntimeError(f"could not determine embedding dim for model {m!r}")
    KNOWN_DIMS[m] = len(probe[0])
    return KNOWN_DIMS[m]


# --- OpenAI -------------------------------------------------------------

def _embed_openai(texts: list[str]) -> list[list[float]]:
    from openai import OpenAI  # lazy
    client = OpenAI()
    resp = client.embeddings.create(model=model(), input=texts)
    return [d.embedding for d in resp.data]


# --- Ollama (local) -----------------------------------------------------

def _embed_ollama(texts: list[str]) -> list[list[float]]:
    """Hit /api/embed on the local Ollama daemon. Batches in one request."""
    payload = {"model": model(), "input": texts}
    req = urllib.request.Request(
        f"{OLLAMA_HOST.rstrip('/')}/api/embed",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Ollama unreachable at {OLLAMA_HOST}: {e}. "
            "Run `ollama serve` and ensure the embedding model is pulled "
            f"(`ollama pull {model().split(':',1)[0]}`)."
        ) from e
    embs = body.get("embeddings")
    if embs is None:
        raise RuntimeError(f"unexpected Ollama embed response: {body}")
    return embs


# --- llama.cpp (local, OpenAI-compatible) -------------------------------

def _embed_llamacpp(texts: list[str]) -> list[list[float]]:
    """Hit a local llama.cpp embeddings server's OpenAI-compatible /v1/embeddings
    (e.g. the docker `infra-embeddings` container). Batches in one request."""
    base = LLAMACPP_EMBED_URL.rstrip("/")
    payload = {"model": model(), "input": texts}
    req = urllib.request.Request(
        f"{base}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": "Bearer no-key"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"llama.cpp embeddings unreachable at {base}: {e}. "
            "Is the embeddings server running (e.g. the `infra-embeddings` docker container)?"
        ) from e
    data = body.get("data")
    if not data:
        raise RuntimeError(f"unexpected llama.cpp embed response: {body}")
    # OpenAI shape: data is a list of {embedding, index}; order by index to be safe.
    rows = sorted(data, key=lambda d: d.get("index", 0))
    return [r["embedding"] for r in rows]


_PROVIDERS: dict[str, Callable[[list[str]], list[list[float]]]] = {
    "openai": _embed_openai,
    "ollama": _embed_ollama,
    "llamacpp": _embed_llamacpp,
}


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings. Returns a list-of-lists (floats) aligned to input.

    Inputs are truncated to EMBED_MAX_CHARS first. Backends with a small context
    window (e.g. Ollama nomic-embed-text, ~2048 tokens) return HTTP 400 on an
    over-long input — and since we batch, one oversize text would fail the whole
    batch, silently dropping embeddings for every memory in it. The path plus the
    opening lines carry most of the similarity signal, so truncating is safe.
    """
    if not is_enabled():
        return []
    if not texts:
        return []
    cap = int(os.environ.get("EMBED_MAX_CHARS", "6000"))
    capped = [t[:cap] for t in texts]
    return _PROVIDERS[EMBED_PROVIDER](capped)


# Cap the text sent to the embedding server. nomic-embed-text has a 2048-token
# context; llama.cpp returns HTTP 500 on overlong input, which aborted a whole
# embed-backfill batch mid-run (live failure 2026-07-18: a 7.5k-char memory
# left 252/380 memories unembedded). 4000 chars ≈ 1300-1400 tokens — safe
# margin; the truncated tail contributes negligible ranking signal anyway.
EMBED_TEXT_MAX_CHARS = int(os.environ.get("EMBED_TEXT_MAX_CHARS", "4000"))


def memory_text(path: str, content: str) -> str:
    """Canonical text for a memory's embedding, capped to the model's safe
    input size. Path is included so file-naming signal contributes to
    similarity (e.g. 'tools/bash/...' matches 'bash')."""
    return f"{path}\n\n{content}"[:EMBED_TEXT_MAX_CHARS]
