#!/usr/bin/env python3
"""llama.cpp embeddings adapter — unit tests, no live server.

Mocks urllib so we pin the OpenAI /v1/embeddings request shape, the data→vectors
parse (ordered by index), the known 768 dim (matches the stored Ollama vectors),
and that EMBED_PROVIDER=llamacpp routes through it.
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

import embeddings  # noqa: E402


class _FakeResp:
    def __init__(self, payload):
        self._b = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture()
def as_llamacpp(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_PROVIDER", "llamacpp")
    monkeypatch.delenv("EMBED_MODEL", raising=False)


def test_is_enabled_includes_llamacpp(as_llamacpp):
    assert embeddings.is_enabled() is True
    assert embeddings.model() == "nomic-embed-text-v1.5.f16.gguf"
    assert embeddings.dim() == 768   # known — no probe needed, matches stored vectors


def test_embed_llamacpp_request_and_parse(as_llamacpp, monkeypatch):
    captured = {}

    def fake(req, timeout=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        # return out of order to prove we sort by index
        return _FakeResp({"data": [
            {"index": 1, "embedding": [0.2, 0.2]},
            {"index": 0, "embedding": [0.1, 0.1]},
        ]})

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    out = embeddings.embed(["alpha", "beta"])
    assert out == [[0.1, 0.1], [0.2, 0.2]]            # reordered by index
    assert captured["url"].endswith("/embeddings")
    assert captured["body"]["model"] == "nomic-embed-text-v1.5.f16.gguf"
    assert captured["body"]["input"] == ["alpha", "beta"]


def test_embed_llamacpp_unreachable_raises(as_llamacpp, monkeypatch):
    def boom(req, timeout=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(urllib.request, "urlopen", boom)
    with pytest.raises(RuntimeError, match="llama.cpp embeddings unreachable"):
        embeddings.embed(["x"])


def test_embed_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(embeddings, "EMBED_PROVIDER", "")
    assert embeddings.is_enabled() is False
    assert embeddings.embed(["x"]) == []
