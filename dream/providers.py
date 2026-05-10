"""Provider adapters for the dream phase.

Each provider exposes the same shape:
    dream(transcript: str, existing: str, system: str, model: str, max_tokens: int) -> list[dict]

Returns the list of memory dicts (each with `path` and `content`). All
provider-specific JSON-shape recovery happens here so dream.py stays clean.

Selection precedence:
  --provider CLI flag  >  DREAM_PROVIDER env var  >  default 'anthropic'
"""
from __future__ import annotations

import json
import os
from typing import Callable

DEFAULT_MODELS = {
    "anthropic": os.environ.get("DREAM_ANTHROPIC_MODEL", "claude-opus-4-7"),
    "openai":    os.environ.get("DREAM_OPENAI_MODEL",    "gpt-4o-mini"),
    "ollama":    os.environ.get("DREAM_OLLAMA_MODEL",    "qwen3.5:latest"),
}

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")


def _extract_json_object(text: str) -> dict:
    """Find the outermost {...} and parse it. Tolerant of leading/trailing prose."""
    text = text.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(text[start : end + 1])


# --- Anthropic ----------------------------------------------------------

def dream_anthropic(transcript: str, existing: str, system: str, model: str, max_tokens: int = 4096) -> list[dict]:
    from anthropic import Anthropic  # lazy import — provider may not be selected
    client = Anthropic()
    user_msg = f"<existing_memories>\n{existing}\n</existing_memories>\n\n<events>\n{transcript}\n</events>"
    msg = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_msg}],
    )
    text = "".join(b.text for b in msg.content if b.type == "text")
    return _extract_json_object(text).get("memories", [])


# --- OpenAI -------------------------------------------------------------

def dream_openai(transcript: str, existing: str, system: str, model: str, max_tokens: int = 4096) -> list[dict]:
    from openai import OpenAI  # lazy
    client = OpenAI()
    user_msg = f"<existing_memories>\n{existing}\n</existing_memories>\n\n<events>\n{transcript}\n</events>"
    resp = client.chat.completions.create(
        model=model,
        max_tokens=max_tokens,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ],
    )
    text = resp.choices[0].message.content or ""
    return _extract_json_object(text).get("memories", [])


# --- Ollama (local) -----------------------------------------------------

def dream_ollama(transcript: str, existing: str, system: str, model: str, max_tokens: int = 4096) -> list[dict]:
    """Hit a local Ollama server. No API key needed; data never leaves the machine.

    PR-C bundle for smaller-model quality:
    - format=<JSON Schema> instead of format="json": Ollama 0.5+ supports a
      real JSON Schema in the format field, structurally guaranteeing valid
      output. The path is regex-constrained so the model can't hallucinate
      a path outside profile/ tools/ project/ general/.
    - Assistant turn pre-filled with `{"memories":[`: leaves the model
      nowhere to put prose preamble.
    - think=False for thinking-capable models like qwen3.5.
    - Lower temperature (0.1) + repeat_penalty for structural tasks.
    """
    import urllib.request
    import urllib.error

    # Lazy import to avoid a circular dep with dream.py during init.
    from prompts import DREAM_JSON_SCHEMA  # type: ignore

    user_msg = f"<existing_memories>\n{existing}\n</existing_memories>\n\n<events>\n{transcript}\n</events>"
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
            # Pre-fill the assistant turn so the model continues from a valid
            # JSON open-bracket — no room for prose preamble.
            {"role": "assistant", "content": '{"memories":['},
        ],
        "format": DREAM_JSON_SCHEMA,
        "stream": False,
        "think": False,  # qwen3.5 / similar; ignored by models without thinking
        "options": {
            "num_predict": max_tokens,
            "temperature": 0.1,
            "top_p": 0.9,
            "repeat_penalty": 1.05,
        },
    }
    req = urllib.request.Request(
        f"{OLLAMA_HOST.rstrip('/')}/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"Ollama unreachable at {OLLAMA_HOST}: {e}. "
            "Is `ollama serve` running, or is the daemon installed?"
        ) from e
    text = (body.get("message") or {}).get("content", "")
    if not text:
        raise RuntimeError(f"empty response from Ollama: {body}")
    # The pre-filled assistant turn `{"memories":[` shapes generation but is NOT
    # echoed back; Ollama returns only the model's continuation. Prepend it so
    # we can parse the full object. Fall back to bracket-extraction if the
    # daemon already gave us the full object (older Ollama / different mode).
    full = '{"memories":[' + text
    try:
        return json.loads(full).get("memories", [])
    except Exception:
        try:
            return _extract_json_object(full).get("memories", [])
        except Exception:
            return _extract_json_object(text).get("memories", [])


PROVIDERS: dict[str, Callable[..., list[dict]]] = {
    "anthropic": dream_anthropic,
    "openai":    dream_openai,
    "ollama":    dream_ollama,
}


def get_provider(name: str | None) -> tuple[str, Callable[..., list[dict]]]:
    """Resolve a provider name to (canonical_name, callable). Honors env fallback."""
    name = (name or os.environ.get("DREAM_PROVIDER") or "anthropic").lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}. Choices: {sorted(PROVIDERS)}")
    return name, PROVIDERS[name]


def default_model(provider: str) -> str:
    return DEFAULT_MODELS[provider]
