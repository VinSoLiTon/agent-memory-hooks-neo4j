"""Phase E (PR-3) — LLM contradiction judge for the nightly.

`hooks/review.detect_contradiction` takes an injected `judge(existing, new) -> bool`
so its logic is unit-tested without an LLM. This builds the *real* judge the
nightly uses when `DREAM_CONTRADICTION_CHECK=1`: it asks the configured provider a
strict yes/no "do these two memories contradict?" question.

Conservative by construction: any error, ambiguity, or empty/again answer returns
False (never flag on doubt). A flaky model can therefore only *miss* a
contradiction, never quarantine a good memory by mistake — the safe failure mode
for an automated gate that routes the new memory to pending_review.
"""
from __future__ import annotations

import json
import os
from typing import Callable

OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")

JUDGE_SYSTEM = (
    "You compare two memory notes about a user or project. Decide whether they "
    "DIRECTLY CONTRADICT — assert facts that cannot both be true at the same time "
    "(e.g. 'prefers tabs' vs 'prefers spaces'). If they are merely different, "
    "complementary, about different topics, or one refines the other, they do NOT "
    "contradict. Reply with exactly one word: yes or no."
)


def _pair(existing: str, new: str) -> str:
    return (f"Memory A (existing):\n{existing}\n\n"
            f"Memory B (new):\n{new}\n\n"
            "Do A and B directly contradict? Answer yes or no.")


def is_yes(text: str) -> bool:
    """True only on an affirmative leading token. Empty/unknown → False."""
    return (text or "").strip().lower().startswith("y")


def _anthropic_judge(model: str) -> Callable[[str, str], bool]:
    cache: dict = {}  # client built lazily on first call → get_judge stays import-safe

    def judge(existing: str, new: str) -> bool:
        try:
            if "c" not in cache:
                from anthropic import Anthropic  # lazy
                cache["c"] = Anthropic()
            msg = cache["c"].messages.create(
                model=model, max_tokens=5,
                system=JUDGE_SYSTEM,
                messages=[{"role": "user", "content": _pair(existing, new)}],
            )
            return is_yes("".join(b.text for b in msg.content if b.type == "text"))
        except Exception:
            return False

    return judge


def _openai_judge(model: str) -> Callable[[str, str], bool]:
    cache: dict = {}

    def judge(existing: str, new: str) -> bool:
        try:
            if "c" not in cache:
                from openai import OpenAI  # lazy
                cache["c"] = OpenAI()
            resp = cache["c"].chat.completions.create(
                model=model, max_tokens=5,
                messages=[{"role": "system", "content": JUDGE_SYSTEM},
                          {"role": "user", "content": _pair(existing, new)}],
            )
            return is_yes(resp.choices[0].message.content or "")
        except Exception:
            return False

    return judge


def _ollama_judge(model: str) -> Callable[[str, str], bool]:
    import urllib.request
    import urllib.error

    def judge(existing: str, new: str) -> bool:
        payload = {
            "model": model,
            "messages": [{"role": "system", "content": JUDGE_SYSTEM},
                         {"role": "user", "content": _pair(existing, new)}],
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
            return is_yes((body.get("message") or {}).get("content", ""))
        except Exception:
            return False

    return judge


JUDGES: dict[str, Callable[[str], Callable[[str, str], bool]]] = {
    "anthropic": _anthropic_judge,
    "openai": _openai_judge,
    "ollama": _ollama_judge,
}


def get_judge(provider: str, model: str) -> Callable[[str, str], bool]:
    """Build a judge for the given provider (falls back to anthropic for an
    unknown name). The returned callable is the conservative judge above."""
    return JUDGES.get(provider, _anthropic_judge)(model)
