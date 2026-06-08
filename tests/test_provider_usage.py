#!/usr/bin/env python3
"""Item #13 — provider token-usage capture + cost estimate (no Neo4j, no SDK).

The local-vs-fallback cost tradeoff was unmeasurable: every provider discarded
the SDK/HTTP usage object. This adds an additive out-param (`usage_out`) — the
provider return type is UNCHANGED so the eval harness + consolidate's merge call
keep working — plus a static price table + estimate_cost. Pinned here:
  - each provider fills usage_out from a canned response, still returns memories;
  - missing usage is safe (no exception, dict stays empty);
  - estimate_cost prices remote providers and zeroes local/unknown;
  - call_provider / safe_distil thread usage_out through.
"""
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import providers           # noqa: E402
import dream as dream_mod  # noqa: E402

_MEMS = {"memories": [{"path": "profile/x.md", "kind": "fact",
                       "content": "---\ntitle: t\nkind: fact\n---\n\nbody"}]}


class _Resp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# --- estimate_cost (pure) ---------------------------------------------------

def test_estimate_cost_prices_remote_and_zeroes_local():
    one_m = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    in_p, out_p = providers.PRICE_PER_MTOK["anthropic"]["claude-opus-4-8"]
    assert abs(providers.estimate_cost("anthropic", "claude-opus-4-8", one_m) - (in_p + out_p)) < 1e-9
    assert providers.estimate_cost("llamacpp", "gemma", one_m) == 0.0   # local = free
    assert providers.estimate_cost("ollama", "qwen", one_m) == 0.0
    assert providers.estimate_cost("anthropic", "unknown-model", one_m) > 0.0  # _default
    assert providers.estimate_cost("anthropic", "m", None) == 0.0       # no usage → 0


def test_estimate_cost_env_table_override(monkeypatch):
    monkeypatch.setenv("DREAM_PRICE_TABLE_JSON", json.dumps({"anthropic": {"_default": [2.0, 8.0]}}))
    cost = providers.estimate_cost("anthropic", "whatever",
                                   {"input_tokens": 1_000_000, "output_tokens": 0})
    assert abs(cost - 2.0) < 1e-9


# --- llama.cpp usage extraction (urllib-mocked) -----------------------------

def test_llamacpp_populates_usage_out(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "32768")
    providers._LLAMACPP_NCTX_CACHE.clear()

    def fake(req, timeout=None):
        return _Resp({"choices": [{"message": {"content": json.dumps(_MEMS)}}],
                      "usage": {"prompt_tokens": 1234, "completion_tokens": 56}})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    usage = {}
    mems = providers.dream_llamacpp("t", "e", "s", "gemma", usage_out=usage)
    assert mems and mems[0]["path"] == "profile/x.md"          # return type unchanged
    assert usage == {"input_tokens": 1234, "output_tokens": 56}


def test_llamacpp_missing_usage_is_safe(monkeypatch):
    monkeypatch.setenv("LLAMACPP_N_CTX", "32768")
    providers._LLAMACPP_NCTX_CACHE.clear()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp({"choices": [{"message": {"content": json.dumps(_MEMS)}}]}))
    usage = {}
    mems = providers.dream_llamacpp("t", "e", "s", "gemma", usage_out=usage)
    assert mems and usage == {}        # no usage in body → dict stays empty, no error


# --- ollama usage extraction (urllib-mocked) --------------------------------

def test_ollama_populates_usage_out(monkeypatch):
    def fake(req, timeout=None):
        return _Resp({"message": {"content": json.dumps(_MEMS)},
                      "prompt_eval_count": 99, "eval_count": 7})
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    usage = {}
    mems = providers.dream_ollama("t", "e", "s", "qwen", usage_out=usage)
    assert mems and usage == {"input_tokens": 99, "output_tokens": 7}


# --- threading through call_provider / safe_distil --------------------------

def _fake_provider(**kw):
    if kw.get("usage_out") is not None:
        kw["usage_out"].update(input_tokens=10, output_tokens=20)
    return _MEMS["memories"]


def test_call_provider_threads_usage_out():
    usage = {}
    mems = dream_mod.call_provider(_fake_provider, "t", "e", "m", "s", usage_out=usage)
    assert mems and usage == {"input_tokens": 10, "output_tokens": 20}


def test_safe_distil_threads_usage_out():
    usage = {}
    assert dream_mod.safe_distil(_fake_provider, "t", "e", "m", "s", "local", usage_out=usage)
    assert usage == {"input_tokens": 10, "output_tokens": 20}


def test_safe_distil_error_leaves_usage_empty():
    def boom(**kw):
        raise RuntimeError("down")
    usage = {}
    assert dream_mod.safe_distil(boom, "t", "e", "m", "s", "local", usage_out=usage) == []
    assert usage == {}      # error → no usage recorded
