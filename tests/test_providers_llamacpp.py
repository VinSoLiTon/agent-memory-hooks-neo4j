#!/usr/bin/env python3
"""llama.cpp local provider (dream + judge) — unit tests, no live server.

Mocks urllib so we pin the request shape (OpenAI /chat/completions, json_schema
response_format, model) and the parse/fallback behaviour, plus the invariant that
`llamacpp` is treated as a LOCAL provider (never blocked by the Phase H egress
policy) — the whole reason for a dedicated provider name rather than reusing openai.
"""
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import providers          # noqa: E402
import judge              # noqa: E402
import dream as dream_mod  # noqa: E402

_MEMS = {"memories": [{"path": "profile/x.md", "kind": "fact",
                       "content": "---\ntitle: t\nkind: fact\n---\n\nbody"}]}


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(captured, *, schema_400=False, content=None):
    """Build a fake urlopen that records requests and returns a canned OpenAI
    chat response. If schema_400, a json_schema request raises HTTP 400 (older
    build) so the json_object fallback path is exercised."""
    payload_content = content if content is not None else json.dumps(_MEMS)

    def fake(req, timeout=None):
        body = json.loads(req.data.decode("utf-8"))
        captured.append({"url": req.full_url, "body": body})
        if schema_400 and body.get("response_format", {}).get("type") == "json_schema":
            raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, None)
        return _FakeResp({"choices": [{"message": {"content": payload_content}}]})

    return fake


# --- provider registry ------------------------------------------------------

def test_llamacpp_registered():
    assert "llamacpp" in providers.PROVIDERS
    assert providers.get_provider("llamacpp")[0] == "llamacpp"
    assert "gemma" in providers.default_model("llamacpp").lower()


# --- dream_llamacpp ---------------------------------------------------------

def test_dream_llamacpp_uses_json_schema_and_parses(monkeypatch):
    captured = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(captured))
    mems = providers.dream_llamacpp("transcript", "existing", "system", "gemma", max_tokens=100)
    assert mems and mems[0]["path"] == "profile/x.md"
    assert captured[0]["url"].endswith("/chat/completions")
    assert captured[0]["body"]["response_format"]["type"] == "json_schema"
    assert captured[0]["body"]["model"] == "gemma"


def test_dream_llamacpp_falls_back_to_json_object(monkeypatch):
    captured = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(captured, schema_400=True))
    mems = providers.dream_llamacpp("t", "e", "s", "gemma")
    assert mems and mems[0]["path"] == "profile/x.md"
    assert captured[0]["body"]["response_format"]["type"] == "json_schema"   # tried first
    assert captured[1]["body"]["response_format"]["type"] == "json_object"   # fell back


def test_dream_llamacpp_unreachable_raises(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="llama.cpp unreachable"):
        providers.dream_llamacpp("t", "e", "s", "gemma")


def test_dream_llamacpp_malformed_raises(monkeypatch):
    captured = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(captured, content="not json at all"))
    with pytest.raises(ValueError, match="malformed"):
        providers.dream_llamacpp("t", "e", "s", "gemma")


# --- judge ------------------------------------------------------------------

def test_llamacpp_judge_yes(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp({"choices": [{"message": {"content": "yes"}}]}))
    assert judge.get_judge("llamacpp", "gemma")("a", "b") is True


def test_llamacpp_judge_conservative_on_error(monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("x")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    assert judge.get_judge("llamacpp", "gemma")("a", "b") is False   # never flag on error


# --- the local-provider invariant (Phase H) ---------------------------------

def test_llamacpp_is_local_not_egress_blocked():
    # a sensitive session must NOT be blocked from the local llama.cpp model...
    assert dream_mod.egress_blocked("llamacpp", True, False) is False
    # ...while a remote provider still is (control).
    assert dream_mod.egress_blocked("anthropic", True, False) is True


# --- PR-4: context-aware sizing + error handling ---------------------------

def test_dream_llamacpp_trims_transcript_and_clamps_max_tokens(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "8192")   # avoid /props; deterministic budget
    providers._LLAMACPP_NCTX_CACHE.clear()
    captured = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(captured))
    huge = "event line number forty-two. " * 5000   # ~145k chars, way over 8k ctx
    providers.dream_llamacpp(huge, "existing", "system", "gemma", max_tokens=16384)
    body = captured[0]["body"]
    user = body["messages"][1]["content"]
    assert "[transcript trimmed to fit context]" in user        # transcript was trimmed
    assert len(user) < len(huge)                                # ...and is much smaller
    assert body["max_tokens"] < 8192                            # clamped to fit n_ctx


def test_dream_llamacpp_no_trim_when_it_fits(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "32768")
    providers._LLAMACPP_NCTX_CACHE.clear()
    captured = []
    monkeypatch.setattr(urllib.request, "urlopen", _urlopen(captured))
    providers.dream_llamacpp("a short transcript", "ex", "sys", "gemma", max_tokens=2048)
    user = captured[0]["body"]["messages"][1]["content"]
    assert "[transcript trimmed" not in user                    # small prompt left intact


def test_dream_llamacpp_n_ctx_env_override(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "4096")
    providers._LLAMACPP_NCTX_CACHE.clear()
    assert providers._llamacpp_n_ctx("http://x/v1") == 4096


def test_dream_llamacpp_http_error_is_request_rejected(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "8192")
    providers._LLAMACPP_NCTX_CACHE.clear()

    def always_400(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "bad request", {}, None)
    monkeypatch.setattr(urllib.request, "urlopen", always_400)
    with pytest.raises(RuntimeError, match="request rejected"):   # NOT "unreachable"
        providers.dream_llamacpp("t", "e", "s", "gemma")


def test_dream_llamacpp_urlerror_is_unreachable(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "8192")
    providers._LLAMACPP_NCTX_CACHE.clear()

    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="unreachable"):
        providers.dream_llamacpp("t", "e", "s", "gemma")


# --- PR-4: safe_distil — a hard provider error becomes 0-yield (→ fallback) --

def test_safe_distil_swallows_error_returns_empty():
    def raising(**kw):
        raise RuntimeError("llama.cpp unreachable")
    assert dream_mod.safe_distil(raising, "t", "e", "m", "s", "llamacpp") == []


def test_safe_distil_passes_through_memories():
    def good(**kw):
        return [{"path": "profile/x.md", "content": "body"}]
    assert dream_mod.safe_distil(good, "t", "e", "m", "s")[0]["path"] == "profile/x.md"
