#!/usr/bin/env python3
"""Phase E (PR-3) — LLM judge wired into the nightly (acceptance #1).

Pure: the judge's yes/no parsing and that get_judge builds a callable without
needing the SDK. DB: write_memories with an injected judge + candidate-finder
flags a contradicting NEW memory to pending_review and links :CONTRADICTS, while
the established active memory STAYS ACTIVE (acceptance #1); a non-contradicting
judge leaves it active; and flag_new_contradiction is audited.
"""
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import judge as judge_mod   # noqa: E402
import review               # noqa: E402
import audit                # noqa: E402
import dream as dream_mod   # noqa: E402

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
MARK = "general/__contra"

_EXISTING = f"{MARK}_existing.md"
_NEW = f"{MARK}_new.md"
_NEW_BODY = "---\ntitle: t\nkind: general\n---\n\nThe user prefers spaces qqcontra for indentation in files."
_SRC = "the user said they prefer spaces qqcontra for indentation in files"


# --- pure -------------------------------------------------------------------

def test_is_yes_parsing():
    assert judge_mod.is_yes("yes") and judge_mod.is_yes("Yes.") and judge_mod.is_yes("y")
    assert not judge_mod.is_yes("no") and not judge_mod.is_yes("") and not judge_mod.is_yes("nope")


def test_get_judge_builds_callable_without_sdk():
    # get_judge must not import/instantiate the provider SDK (lazy in the closure)
    for prov in ("anthropic", "openai", "ollama", "somethingelse"):
        assert callable(judge_mod.get_judge(prov, "m"))


# --- DB ---------------------------------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    saved = dream_mod.embeddings.is_enabled
    dream_mod.embeddings.is_enabled = lambda: False

    def _clean():
        with d.session() as s:
            s.run("MATCH (r:MemoryRevision)-[:VERSION_OF]->(m:Memory) WHERE m.path STARTS WITH $mk "
                  "DETACH DELETE r", mk=MARK)
            s.run("MATCH (m:Memory) WHERE m.path STARTS WITH $mk DETACH DELETE m", mk=MARK)
            s.run("MATCH (s:Session {session_key:'claude_code:__contra'}) DETACH DELETE s")

    _clean()
    with d.session() as s:
        s.run("MERGE (sess:Session {session_key:'claude_code:__contra'}) "
              "SET sess.client='claude_code', sess.session_id='__contra'")
        s.run("CREATE (:Memory {path:$p, content:'The user prefers tabs for indentation.', "
              "status:'active', created_by:'user', updated_at:'2026-06-01T00:00:00+00:00'})", p=_EXISTING)
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


def _events():
    return [{"event_id": "__contra_e1", "prompt": _SRC, "tool_input": "", "tool_response": ""}]


def _finder_returns_existing(session, path, content):
    # stub candidate-finder: surfaces the existing active memory as a neighbour
    return [(_EXISTING, "The user prefers tabs for indentation.")]


def test_contradicting_new_memory_quarantined_existing_stays_active(driver):
    dream_mod.write_memories(
        driver, "claude_code:__contra",
        [{"path": _NEW, "content": _NEW_BODY}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test",
        events=_events(),
        contradiction_judge=lambda existing, new: True,      # the LLM, stubbed: "contradicts"
        find_candidates=_finder_returns_existing,
    )
    # acceptance #1: new is flagged + pending_review; the established one stays active
    assert _status(driver, _NEW) == "pending_review"
    assert _status(driver, _EXISTING) == "active"
    with driver.session() as s:
        n = s.run("MATCH (:Memory {path:$n})-[:CONTRADICTS]->(:Memory {path:$e}) RETURN count(*) AS n",
                  n=_NEW, e=_EXISTING).single()["n"]
    assert n == 1


def test_non_contradicting_new_memory_stays_active(driver):
    dream_mod.write_memories(
        driver, "claude_code:__contra",
        [{"path": _NEW, "content": _NEW_BODY}],
        watermark="2026-06-02T00:00:00+00:00", project=None, provider="test", model="test",
        events=_events(),
        contradiction_judge=lambda existing, new: False,     # "no contradiction"
        find_candidates=_finder_returns_existing,
    )
    assert _status(driver, _NEW) == "active"
    assert _status(driver, _EXISTING) == "active"
    with driver.session() as s:
        n = s.run("MATCH (:Memory {path:$n})-[:CONTRADICTS]-(:Memory) RETURN count(*) AS n", n=_NEW).single()["n"]
    assert n == 0


def test_flag_new_contradiction_is_audited_and_one_sided(driver):
    with driver.session() as s:
        s.run("CREATE (:Memory {path:$p, content:'new claim qqcontra', status:'active', "
              "updated_at:'2026-06-02T00:00:00+00:00'})", p=_NEW)
        review.flag_new_contradiction(s, _EXISTING, _NEW)   # (existing, new)
        t = audit.trail(s, _NEW)
    assert _status(driver, _NEW) == "pending_review"
    assert _status(driver, _EXISTING) == "active"           # one-sided: existing untouched
    ops = [e["operation"] for e in t["entries"]]
    assert "flag_contradiction" in ops
    assert any(e["actor"] == "dream" for e in t["entries"] if e["operation"] == "flag_contradiction")
