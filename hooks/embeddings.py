"""Embedding providers for semantic memory recall.

Two adapters with the same shape:
    embed(texts: list[str]) -> list[list[float]]

Selection: EMBED_PROVIDER env var ('openai' | 'ollama' | unset).
When unset, semantic recall is disabled and inject_memory falls back to
fulltext-only — existing behavior is preserved.

Models (override via EMBED_MODEL):
  openai → text-embedding-3-small (1536 dim)
  ollama → nomic-embed-text:latest (768 dim) — must be pulled first:
              ollama pull nomic-embed-text

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
}
# Common dimensions; auto-detected on first call if not listed.
KNOWN_DIMS = {
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
    "nomic-embed-text": 768,
    "nomic-embed-text:latest": 768,
    "mxbai-embed-large": 1024,
    "mxbai-embed-large:latest": 1024,
}

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def is_enabled() -> bool:
    return EMBED_PROVIDER in ("openai", "ollama")


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


_PROVIDERS: dict[str, Callable[[list[str]], list[list[float]]]] = {
    "openai": _embed_openai,
    "ollama": _embed_ollama,
}


def embed(texts: list[str]) -> list[list[float]]:
    """Embed a batch of strings. Returns a list-of-lists (floats) aligned to input."""
    if not is_enabled():
        return []
    if not texts:
        return []
    return _PROVIDERS[EMBED_PROVIDER](texts)


def memory_text(path: str, content: str) -> str:
    """Canonical text for a memory's embedding. Path is included so file-naming
    signal contributes to similarity (e.g. 'tools/bash/...' matches 'bash')."""
    return f"{path}\n\n{content}"
