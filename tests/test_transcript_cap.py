#!/usr/bin/env python3
"""Item #19 — n_ctx-derived transcript cap for local providers (no Neo4j).

Long sessions were capped at a hard-coded 16000 chars regardless of the server's
context. Now the cap is derived from llama.cpp's n_ctx (a fraction, leaving
headroom so the provider-layer trim never fires) — a bigger server uses a bigger
slice — while DREAM_TRANSCRIPT_MAX_CHARS still overrides verbatim, and non-llamacpp
local / errors fall back to 16000. (The map-reduce summarization was dropped.)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as dream_mod   # noqa: E402
import providers           # noqa: E402


def test_env_override_is_honored_verbatim(monkeypatch):
    monkeypatch.setenv("DREAM_TRANSCRIPT_MAX_CHARS", "5000")
    assert dream_mod._derived_transcript_cap("llamacpp") == 5000
    assert dream_mod._derived_transcript_cap("ollama") == 5000   # override wins for any provider


def test_llamacpp_derives_from_n_ctx(monkeypatch):
    monkeypatch.delenv("DREAM_TRANSCRIPT_MAX_CHARS", raising=False)
    monkeypatch.setenv("LLAMACPP_N_CTX", "32768")   # _llamacpp_n_ctx honors this (no /props probe)
    providers._LLAMACPP_NCTX_CACHE.clear()
    cap = dream_mod._derived_transcript_cap("llamacpp")
    expected = int((32768 - 3072 - 256) * providers._CHARS_PER_TOK * 0.7)
    assert cap == expected and cap > 16000           # a 32k server uses a bigger slice than the old 16k


def test_bigger_ctx_means_bigger_cap(monkeypatch):
    monkeypatch.delenv("DREAM_TRANSCRIPT_MAX_CHARS", raising=False)
    monkeypatch.setenv("LLAMACPP_N_CTX", "8192")
    providers._LLAMACPP_NCTX_CACHE.clear()
    small = dream_mod._derived_transcript_cap("llamacpp")
    monkeypatch.setenv("LLAMACPP_N_CTX", "65536")
    providers._LLAMACPP_NCTX_CACHE.clear()
    big = dream_mod._derived_transcript_cap("llamacpp")
    assert big > small and small >= 8000             # monotonic + floor preserved on a tiny server


def test_ctx_fraction_knob(monkeypatch):
    monkeypatch.delenv("DREAM_TRANSCRIPT_MAX_CHARS", raising=False)
    monkeypatch.setenv("LLAMACPP_N_CTX", "32768")
    monkeypatch.setenv("DREAM_TRANSCRIPT_CTX_FRACTION", "0.5")
    providers._LLAMACPP_NCTX_CACHE.clear()
    cap = dream_mod._derived_transcript_cap("llamacpp")
    assert cap == int((32768 - 3072 - 256) * providers._CHARS_PER_TOK * 0.5)


def test_non_llamacpp_local_falls_back_to_16000(monkeypatch):
    monkeypatch.delenv("DREAM_TRANSCRIPT_MAX_CHARS", raising=False)
    assert dream_mod._derived_transcript_cap("ollama") == 16000


def test_error_falls_back_to_16000(monkeypatch):
    monkeypatch.delenv("DREAM_TRANSCRIPT_MAX_CHARS", raising=False)

    def boom(*a, **k):
        raise RuntimeError("server down")
    monkeypatch.setattr(providers, "_llamacpp_n_ctx", boom)
    assert dream_mod._derived_transcript_cap("llamacpp") == 16000   # never crashes the run
