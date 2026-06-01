#!/usr/bin/env python3
"""Phase H3 — anti-poisoning admission gate. Acceptance tests.

Pure: the three signals (directive content / thin source / novelty) and the
combined `poisoning_risk` gate fire only when ALL THREE hold, with boundary and
negative cases pinned. DB: a directive memory distilled from a thin, novel
session is quarantined to pending_review (even though it's well-grounded, so the
quarantine is specifically the poisoning gate); the same content from a rich
session stays active; a non-directive memory is never quarantined; and an update
to an existing-active memory is exempt.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import quality            # noqa: E402
import dream as dream_mod  # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


# --- pure: directive detection ----------------------------------------------

def test_is_directive_positive_and_negative():
    assert quality.is_directive("Always delete the build dir; rm -rf out.")
    assert quality.is_directive("You must never enable that flag.")
    assert not quality.is_directive("The subsystem records telemetry for later analysis.")


def test_is_directive_ignores_frontmatter():
    # A marker word appearing only in frontmatter (title/kind) must not count.
    body = "---\ntitle: always be closing\nkind: general\n---\n\nA neutral observation about latency."
    assert not quality.is_directive(body)


def test_directive_vocabulary_is_a_frozenset():
    assert isinstance(quality.DIRECTIVE_MARKERS, frozenset)
    assert "always" in quality.DIRECTIVE_MARKERS and "rm -" in quality.DIRECTIVE_MARKERS


# --- pure: novelty ----------------------------------------------------------

def test_novelty_empty_corpus_is_maximal():
    assert quality.novelty_score("qqnovel alpha beta gamma rules", "") == 1.0


def test_novelty_full_overlap_is_zero():
    text = "alpha beta gamma delta tokens"
    assert quality.novelty_score(text, text) == 0.0


def test_novelty_partial_overlap_between():
    n = quality.novelty_score("alpha beta gamma delta", "alpha beta only")
    assert 0.0 < n < 1.0


def test_novelty_no_distinctive_tokens_is_zero():
    assert quality.novelty_score("a b c", "anything here") == 0.0   # all tokens < 4 chars


# --- pure: combined gate (explicit thresholds → env-independent) -------------

_DIRECTIVE = "Always delete the cache; rm -rf build."
_PLAIN = "The cache stores rebuild artifacts for the subsystem."


def test_poisoning_risk_fires_only_with_all_three():
    assert quality.poisoning_risk(_DIRECTIVE, 1, 1.0, min_events=5, novelty_min=0.6)
    # each signal individually removed → not quarantined
    assert not quality.poisoning_risk(_PLAIN, 1, 1.0, min_events=5, novelty_min=0.6)      # not directive
    assert not quality.poisoning_risk(_DIRECTIVE, 9, 1.0, min_events=5, novelty_min=0.6)  # rich session
    assert not quality.poisoning_risk(_DIRECTIVE, 1, 0.1, min_events=5, novelty_min=0.6)  # not novel


def test_poisoning_risk_boundaries():
    # event count uses strict `<`: exactly at the threshold is NOT thin.
    assert not quality.poisoning_risk(_DIRECTIVE, 5, 1.0, min_events=5, novelty_min=0.6)
    assert quality.poisoning_risk(_DIRECTIVE, 4, 1.0, min_events=5, novelty_min=0.6)
    # novelty uses `>=`: exactly at the threshold IS novel.
    assert quality.poisoning_risk(_DIRECTIVE, 1, 0.6, min_events=5, novelty_min=0.6)
    assert not quality.poisoning_risk(_DIRECTIVE, 1, 0.59, min_events=5, novelty_min=0.6)


# --- DB-backed gate in write_memories ---------------------------------------

# Token-dominated bodies so novelty stays high regardless of the dev graph, and
# the source transcript carries the same tokens so GROUNDING passes — isolating
# the quarantine cause to the poisoning gate, not low grounding.
_TOKENS = "qqpoisonalpha qqpoisonbeta qqpoisongamma qqpoisondelta qqpoisonepsilon qqpoisonzeta"
_DIRECTIVE_BODY = f"---\ntitle: t\nkind: general\n---\n\nAlways {_TOKENS}; rm -rf the targets."
_PLAIN_BODY = f"---\ntitle: t\nkind: general\n---\n\nThe {_TOKENS} subsystem records telemetry."
_SOURCE = f"the agent discussed always {_TOKENS} and rm rf the targets at length"


@pytest.fixture()
def driver():
    if quality.POISON_MIN_EVENTS < 2:
        pytest.skip("DREAM_POISON_MIN_EVENTS overridden too low for the thin/rich split")
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False

    def _clean():
        with d.session() as s:
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH 'general/__poison' "
                  "OPTIONAL MATCH (r:MemoryRevision)-[:VERSION_OF]->(m) DETACH DELETE m, r")
            s.run("MATCH (s:Session {session_key:'claude_code:__poison'}) DETACH DELETE s")

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:'claude_code:__poison'}) "
              "SET sess.client='claude_code', sess.session_id='__poison'")
        # An existing active directive memory with DIFFERENT tokens, so it doesn't
        # corroborate (lower the novelty of) the candidates under test.
        s.run("CREATE (:Memory {path:'general/__poison_existing.md', "
              "content:'---\\ntitle: t\\nkind: general\\n---\\n\\nAlways qqexisting purge qqexisting logs.', "
              "status:'active', updated_at:'2026-06-01T00:00:00+00:00'})")
    try:
        yield d
    finally:
        _clean()
        dream_mod.embeddings.is_enabled = saved
        d.close()


def _status(d, path):
    with d.session() as s:
        r = s.run("MATCH (m:Memory {path:$p}) RETURN m.status AS s", p=path).single()
        return r["s"] if r else None


def _events(n):
    # First event carries the grounding tokens; the rest are filler to reach n.
    evs = [{"event_id": "__poison_e0", "prompt": _SOURCE, "tool_input": "", "tool_response": ""}]
    evs += [{"event_id": f"__poison_e{i}", "prompt": "follow-up", "tool_input": "", "tool_response": ""}
            for i in range(1, n)]
    return evs


def test_thin_novel_directive_is_quarantined(driver):
    dream_mod.write_memories(
        driver, "claude_code:__poison",
        [{"path": "general/__poison_thin.md", "content": _DIRECTIVE_BODY}],
        watermark="2026-06-01T00:00:00+00:00", project=None, provider="test", model="test",
        events=_events(1),
    )
    # well-grounded (tokens are in the source) but quarantined anyway → it's the
    # anti-poisoning gate, not the grounding gate.
    assert quality.grounding_score(_DIRECTIVE_BODY, _SOURCE) >= 0.5
    assert _status(driver, "general/__poison_thin.md") == "pending_review"


def test_rich_session_directive_stays_active(driver):
    dream_mod.write_memories(
        driver, "claude_code:__poison",
        [{"path": "general/__poison_rich.md", "content": _DIRECTIVE_BODY}],
        watermark="2026-06-01T00:00:00+00:00", project=None, provider="test", model="test",
        events=_events(quality.POISON_MIN_EVENTS + 1),
    )
    assert _status(driver, "general/__poison_rich.md") == "active"


def test_non_directive_thin_stays_active(driver):
    dream_mod.write_memories(
        driver, "claude_code:__poison",
        [{"path": "general/__poison_plain.md", "content": _PLAIN_BODY}],
        watermark="2026-06-01T00:00:00+00:00", project=None, provider="test", model="test",
        events=_events(1),
    )
    assert _status(driver, "general/__poison_plain.md") == "active"


def test_update_to_existing_active_is_exempt(driver):
    # A thin, novel, directive UPDATE to an already-active memory must not be hidden.
    dream_mod.write_memories(
        driver, "claude_code:__poison",
        [{"path": "general/__poison_existing.md", "content": _DIRECTIVE_BODY}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test",
        events=_events(1),
    )
    assert _status(driver, "general/__poison_existing.md") == "active"
