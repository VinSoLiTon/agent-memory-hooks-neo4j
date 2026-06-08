"""Item #18 — LLM critique pass for the nightly (faithfulness gate).

Mirrors dream/judge.py structurally but answers a different question: is this NEW
candidate memory FAITHFUL to the bounded source transcript it was distilled from?
Every concrete claim — commands, ports, URLs, file paths, version numbers, names,
stated preferences, decisions — should trace to something in the transcript. A
fabricated, unsupported, or contradicted claim makes the note NOT faithful.

The grounding gate (token overlap) catches a memory that shares *no* vocabulary
with the transcript; it cannot catch a fluent hallucination that reuses the
session's words but inverts a value ("port 8080" → "port 8081"). This LLM pass is
that second, semantic check, run only when explicitly enabled (DREAM_CRITIQUE=1).

Lenient by construction (True-on-error): any exception, empty, or ambiguous
answer returns True (faithful). Only an explicit leading "no" marks a note
unfaithful. A flaky model can therefore only MISS a hallucination, never
quarantine a good memory by mistake — the safe failure mode for an automated gate
that routes only NEW candidates to pending_review (the established active memory
is never touched). This is the deliberate INVERSE of judge.py's False-on-error:
the judge must never wrongly flag a contradiction; the critic must never wrongly
hold a good memory.
"""
from __future__ import annotations

import json
import os
from typing import Callable

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
LLAMACPP_CHAT_URL = os.environ.get("LLAMACPP_CHAT_URL", "http://127.0.0.1:8080/v1")

CRITIC_SYSTEM = (
    "You audit whether a MEMORY NOTE is faithful to the SESSION TRANSCRIPT it was "
    "distilled from. Every concrete claim in the note — commands, ports, URLs, file "
    "paths, version numbers, names, stated preferences, and decisions — must be "
    "supported by the transcript. If any material claim is fabricated, unsupported, "
    "or contradicted by the transcript, the note is NOT faithful. Reasonable "
    "summarization or generalization of what the transcript shows is fine. Reply "
    "with exactly one word: yes (faithful) or no (not faithful)."
)


def _pair(candidate: str, transcript: str) -> str:
    return (f"SESSION TRANSCRIPT (the only source of truth):\n{transcript}\n\n"
            f"MEMORY NOTE distilled from it:\n{candidate}\n\n"
            "Is every material claim in the note supported by the transcript? "
            "Answer yes or no.")


def is_faithful(text: str) -> bool:
    """Faithful unless the model leads with an explicit 'no'. Empty / unknown /
    error → True (lenient: never quarantine a good memory on a flaky answer)."""
    return not (text or "").strip().lower().startswith("n")


def _anthropic_critic(model: str) -> Callable[[str, str], bool]:
    cache: dict = {}  # client built lazily on first call → get_critic stays import-safe

    def critic(candidate: str, transcript: str) -> bool:
        try:
            if "c" not in cache:
                from anthropic import Anthropic  # lazy
                cache["c"] = Anthropic()
            msg = cache["c"].messages.create(
                model=model, max_tokens=5,
                system=CRITIC_SYSTEM,
                messages=[{"role": "user", "content": _pair(candidate, transcript)}],
            )
            return is_faithful("".join(b.text for b in msg.content if b.type == "text"))
        except Exception:
            return True

    return critic


def _openai_critic(model: str) -> Callable[[str, str], bool]:
    cache: dict = {}

    def critic(candidate: str, transcript: str) -> bool:
        try:
            if "c" not in cache:
                from openai import OpenAI  # lazy
                cache["c"] = OpenAI()
            resp = cache["c"].chat.completions.create(
                model=model, max_tokens=5,
                messages=[{"role": "system", "content": CRITIC_SYSTEM},
                          {"role": "user", "content": _pair(candidate, transcript)}],
            )
            return is_faithful(resp.choices[0].message.content or "")
        except Exception:
            return True

    return critic


def _ollama_critic(model: str) -> Callable[[str, str], bool]:
    import urllib.request
    import urllib.error

    def critic(candidate: str, transcript: str) -> bool:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": CRITIC_SYSTEM},
                         {"role": "user", "content": _pair(candidate, transcript)}],
            "stream": False,
            "think": False,
            "options": {"num_predict": 4, "temperature": 0.0},
        }
        try:
            req = urllib.request.Request(
                f"{OLLAMA_HOST.rstrip('/')}/api/chat",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return is_faithful((body.get("message") or {}).get("content", ""))
        except Exception:
            return True

    return critic


def _llamacpp_critic(model: str) -> Callable[[str, str], bool]:
    import urllib.request
    import urllib.error

    def critic(candidate: str, transcript: str) -> bool:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": CRITIC_SYSTEM},
                         {"role": "user", "content": _pair(candidate, transcript)}],
            "max_tokens": 4,
            "temperature": 0.0,
        }
        try:
            req = urllib.request.Request(
                f"{LLAMACPP_CHAT_URL.rstrip('/')}/chat/completions",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json", "Authorization": "Bearer no-key"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            return is_faithful((body.get("choices") or [{}])[0].get("message", {}).get("content", ""))
        except Exception:
            return True

    return critic


CRITICS: dict[str, Callable[[str], Callable[[str, str], bool]]] = {
    "anthropic": _anthropic_critic,
    "openai": _openai_critic,
    "ollama": _ollama_critic,
    "llamacpp": _llamacpp_critic,
}


def get_critic(provider: str, model: str) -> Callable[[str, str], bool]:
    """Build a critic for the given provider (falls back to anthropic for an
    unknown name). The returned callable is the lenient critic above:
    critic(candidate, transcript) -> True if faithful, False if not."""
    return CRITICS.get(provider, _anthropic_critic)(model)
