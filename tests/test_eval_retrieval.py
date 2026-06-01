#!/usr/bin/env python3
"""Phase D3 — retrieval eval as a CI gate.

Seeds the golden fixture and asserts the recall engine retrieves the right memory
for each query (hit@5) and ranks it at/near the top (MRR). Runs fulltext-only
(embeddings off) for determinism, so it passes in CI without Ollama. A ranking
regression in recall.py drops these numbers and fails the build.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import eval_retrieval as er  # noqa: E402
import embeddings            # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = embeddings.is_enabled
    embeddings.is_enabled = lambda: False   # fulltext-only → deterministic
    er.cleanup(d)
    try:
        yield d
    finally:
        er.cleanup(d)
        embeddings.is_enabled = saved
        d.close()


def test_retrieval_eval_meets_thresholds(driver):
    er.seed(driver)
    rep = er.score(driver, k=5)
    # every golden query's expected memory must be retrieved...
    assert rep["hit_at_k"] == 1.0, rep["queries"]
    # ...and ranked at/near the top (ranking discrimination on the shared-token pair)
    assert rep["mrr"] >= 0.75, rep["queries"]
