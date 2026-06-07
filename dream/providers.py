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
    "llamacpp":  os.environ.get("DREAM_LLAMACPP_MODEL",  "gemma-4-12B-it-Q4_K_M.gguf"),
}

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
# llama.cpp server's OpenAI-compatible base URL (e.g. the `infra-llama` docker
# container). A LOCAL provider — like ollama, its data never leaves the machine,
# so it is NOT subject to the Phase H remote-egress block.
LLAMACPP_CHAT_URL = os.environ.get("LLAMACPP_CHAT_URL", "http://127.0.0.1:8080/v1")


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

    def _build_payload(use_prefix: bool) -> dict:
        msgs: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_msg},
        ]
        if use_prefix:
            # Pre-fill the assistant turn so the model continues from a valid
            # JSON open-bracket — no room for prose preamble.
            msgs.append({"role": "assistant", "content": '{"memories":['})
        return {
            "model": model,
            "messages": msgs,
            "format": DREAM_JSON_SCHEMA,
            "stream": False,
            "think": False,
            "options": {
                "num_predict": max_tokens,
                "temperature": 0.1,
                "top_p": 0.9,
                "repeat_penalty": 1.05,
            },
        }

    def _post(payload: dict) -> str:
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
        return text

    def _try_parse(text: str, with_prefix: bool) -> list[dict] | None:
        """Try the full pipeline of parse strategies. Returns None on total failure."""
        candidates = []
        if with_prefix:
            candidates.append('{"memories":[' + text)
        candidates.append(text)
        for c in candidates:
            try:
                return json.loads(c).get("memories", [])
            except Exception:
                try:
                    return _extract_json_object(c).get("memories", [])
                except Exception:
                    continue
        return None

    # Attempt 1: with assistant prefix (cheaper, usually wins).
    text = _post(_build_payload(use_prefix=True))
    parsed = _try_parse(text, with_prefix=True)
    if parsed is not None:
        return parsed

    # PR-F #3: retry once without the prefix. If Ollama returned a complete
    # JSON object on its own (rather than a continuation), the prepend-prefix
    # path corrupts it; some daemons / models behave inconsistently between
    # the two modes.
    print("ollama: prefix-mode parse failed; retrying without assistant prefix", file=__import__("sys").stderr)
    text2 = _post(_build_payload(use_prefix=False))
    parsed2 = _try_parse(text2, with_prefix=False)
    if parsed2 is not None:
        return parsed2

    raise ValueError(
        f"ollama returned malformed JSON across two attempts. "
        f"first response: {text[:200]!r}; retry response: {text2[:200]!r}"
    )


# --- llama.cpp (local, OpenAI-compatible) -------------------------------

_LLAMACPP_NCTX_CACHE: dict = {}
_CHARS_PER_TOK = 3.5  # conservative: over-estimates tokens, so we under-fill (never 400)


def _llamacpp_n_ctx(base: str) -> int:
    """The server's context window (tokens). Cached per base URL. Resolution order:
    `LLAMACPP_N_CTX` env override → the server's `/props` (lives at the root, not
    `/v1`) → 8192 fallback. So the dream sizes its request to whatever the server
    is actually configured for, no hard-coding."""
    env = os.environ.get("LLAMACPP_N_CTX")
    if env:
        try:
            return int(env)
        except ValueError:
            pass
    if base in _LLAMACPP_NCTX_CACHE:
        return _LLAMACPP_NCTX_CACHE[base]
    import urllib.request
    root = base[:-3] if base.endswith("/v1") else base   # /props is a root endpoint
    n = 8192
    try:
        with urllib.request.urlopen(f"{root.rstrip('/')}/props", timeout=10) as resp:
            props = json.loads(resp.read().decode("utf-8"))
        n = int((props.get("default_generation_settings") or {}).get("n_ctx") or 8192)
    except Exception:
        n = 8192
    _LLAMACPP_NCTX_CACHE[base] = n
    return n


def dream_llamacpp(transcript: str, existing: str, system: str, model: str, max_tokens: int = 4096) -> list[dict]:
    """Hit a local llama.cpp server's OpenAI-compatible API (e.g. the docker
    `infra-llama` container serving Gemma). LOCAL — data never leaves the machine.

    Context-aware (PR-4): reads the server's `n_ctx`, trims the transcript so the
    prompt fits with an output reserve, and clamps `max_tokens` — so it never 400s
    on "exceeds context size" nor truncates the JSON mid-output, on ANY server.

    Structured output uses `response_format={"type":"json_schema",...}` (GBNF —
    same structural guarantee the Ollama path got from `format=<schema>`), falling
    back to `{"type":"json_object"}` on an older build, then tolerant extraction.
    """
    import urllib.request
    import urllib.error

    from prompts import DREAM_JSON_SCHEMA  # type: ignore

    base = LLAMACPP_CHAT_URL.rstrip("/")
    n_ctx = _llamacpp_n_ctx(base)
    out_reserve = int(os.environ.get("LLAMACPP_OUTPUT_RESERVE", "3072"))  # tokens for the JSON
    margin = 256

    def _wrap(t: str) -> str:
        return f"<existing_memories>\n{existing}\n</existing_memories>\n\n<events>\n{t}\n</events>"

    # Fit the transcript so system + wrapper + existing + transcript stays under
    # (n_ctx - out_reserve - margin) tokens (estimated via chars/_CHARS_PER_TOK).
    prompt_tok_budget = max(512, n_ctx - out_reserve - margin)
    prompt_char_budget = int(prompt_tok_budget * _CHARS_PER_TOK)
    transcript_budget = max(800, prompt_char_budget - len(system) - len(_wrap("")))
    t = transcript
    if len(t) > transcript_budget:
        head = int(transcript_budget * 0.6)
        tail = max(0, transcript_budget - head - 40)
        t = t[:head] + "\n...[transcript trimmed to fit context]...\n" + (t[-tail:] if tail else "")
    user_msg = _wrap(t)

    est_prompt_tok = int((len(system) + len(user_msg)) / _CHARS_PER_TOK)
    eff_max_tokens = max(512, min(max_tokens, n_ctx - est_prompt_tok - margin))

    def _post(response_format: dict) -> str:
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "temperature": 0.1,
            "max_tokens": eff_max_tokens,
            "response_format": response_format,
        }
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer no-key"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        choices = body.get("choices") or []
        if not choices:
            raise RuntimeError(f"empty response from llama.cpp: {body}")
        return choices[0].get("message", {}).get("content", "") or ""

    # Prefer schema-constrained output; fall back to json_object if THIS request's
    # json_schema is rejected (older build). Distinguish a reachable-but-rejecting
    # server (HTTPError) from an unreachable one (URLError) — both raise RuntimeError
    # so the dream's hybrid fallback (PR-4) takes over instead of crashing.
    try:
        try:
            text = _post({"type": "json_schema",
                          "json_schema": {"name": "memories", "schema": DREAM_JSON_SCHEMA}})
        except urllib.error.HTTPError:
            text = _post({"type": "json_object"})
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")[:200]
        except Exception:
            pass
        raise RuntimeError(f"llama.cpp request rejected (HTTP {e.code}) at {base}: {detail or e.reason}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(
            f"llama.cpp unreachable at {base}: {e}. Is the server running "
            "(e.g. the `infra-llama` docker container)?"
        ) from e

    try:
        return json.loads(text).get("memories", [])
    except Exception:
        try:
            return _extract_json_object(text).get("memories", [])
        except Exception:
            raise ValueError(f"llama.cpp returned malformed JSON: {text[:200]!r}")


PROVIDERS: dict[str, Callable[..., list[dict]]] = {
    "anthropic": dream_anthropic,
    "openai":    dream_openai,
    "ollama":    dream_ollama,
    "llamacpp":  dream_llamacpp,
}


def get_provider(name: str | None) -> tuple[str, Callable[..., list[dict]]]:
    """Resolve a provider name to (canonical_name, callable). Honors env fallback."""
    name = (name or os.environ.get("DREAM_PROVIDER") or "anthropic").lower()
    if name not in PROVIDERS:
        raise ValueError(f"unknown provider {name!r}. Choices: {sorted(PROVIDERS)}")
    return name, PROVIDERS[name]


def default_model(provider: str) -> str:
    return DEFAULT_MODELS[provider]
