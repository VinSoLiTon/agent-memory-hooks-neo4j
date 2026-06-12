#!/usr/bin/env python3
"""Dashboard session views must NOT use the unbounded `[:FIRST_EVENT|NEXT*0..]`
variable-length traversal — it times out / OOMs Neo4j on long sessions (the same
hard rule as dream._walk_session_events). /session and /sessions walk the chain
hop-by-hop instead.

A source-guard test (no varlength in app.py) + a live-Neo4j render test that a
multi-event session shows all its events in order and the count on /sessions.
"""
import os
import re
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for p in ("hooks", "dream", "dashboard"):
    sys.path.insert(0, os.path.join(ROOT, p))

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

SK = "test:__dashsess"
SID = "__dashsess"


def test_no_unbounded_varlength_chain_in_dashboard():
    """Regression guard: the OOM-prone pattern must never reappear in the dashboard."""
    src = open(os.path.join(ROOT, "dashboard", "app.py"), encoding="utf-8").read()
    assert "NEXT*0.." not in src and "NEXT *0.." not in src, \
        "dashboard uses the forbidden unbounded event-chain varlength traversal"
    assert re.search(r"FIRST_EVENT\|NEXT\s*\*", src) is None


@pytest.fixture()
def seeded():
    d = GraphDatabase.driver(URI, auth=(USER, PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])

    def _clean():
        with d.session() as s:
            s.run("MATCH (e:Event) WHERE e.event_id STARTS WITH $p DETACH DELETE e", p=SID)
            s.run("MATCH (s:Session {session_key:$sk}) DETACH DELETE s", sk=SK)

    _clean()
    with d.session() as s:
        # far-future created_at so it sorts into /sessions' (created DESC) top-200
        # ahead of the live data, regardless of how many real sessions exist.
        s.run("MERGE (sess:Session {session_key:$sk}) SET sess.client='test', sess.session_id=$sid, "
              "sess.created_at='2099-12-31T00:00:00+00:00'", sk=SK, sid=SID)
        s.run(
            "MATCH (sess:Session {session_key:$sk}) "
            "CREATE (e1:Event {event_id:$i1, client:'test', timestamp:'2026-06-01T00:00:00+00:00', "
            "        event_name:'UserPromptSubmit', prompt:'FIRSTMARK do the thing'}), "
            "       (e2:Event {event_id:$i2, client:'test', timestamp:'2026-06-01T00:01:00+00:00', "
            "        event_name:'PreToolUse', tool_name:'Bash', tool_input:'SECONDMARK ls'}), "
            "       (e3:Event {event_id:$i3, client:'test', timestamp:'2026-06-01T00:02:00+00:00', "
            "        event_name:'Stop', tool_response:'THIRDMARK done'}) "
            "CREATE (sess)-[:FIRST_EVENT]->(e1)-[:NEXT]->(e2)-[:NEXT]->(e3) "
            "CREATE (sess)-[:LATEST_EVENT]->(e3)",
            sk=SK, i1=SID + "_e1", i2=SID + "_e2", i3=SID + "_e3")
    try:
        yield d
    finally:
        _clean()
        d.close()


def _client():
    import app as dash
    dash.app.config["TESTING"] = True
    return dash.app.test_client()


def test_session_view_renders_all_events_in_order(seeded):
    html = _client().get(f"/session/{SK}").data.decode()
    assert "3 events" in html
    # all three events present…
    for mark in ("FIRSTMARK", "SECONDMARK", "THIRDMARK"):
        assert mark in html
    # …and in chain order (first → second → third)
    assert html.index("FIRSTMARK") < html.index("SECONDMARK") < html.index("THIRDMARK")


def test_sessions_list_shows_event_count(seeded):
    html = _client().get("/sessions").data.decode()
    assert SK[:60] in html
    # the count cell for a 3-event session
    assert ">3<" in html


def test_walk_session_events_helper_caps(seeded):
    import app as dash
    with seeded.session() as s:
        evs, trunc = dash.walk_session_events(s, SK, cap=2)
        assert len(evs) == 2 and trunc is True          # capped
        evs, trunc = dash.walk_session_events(s, SK, cap=600)
        assert len(evs) == 3 and trunc is False          # full chain, in order
        assert [e["event_id"] for e in evs] == [SID + "_e1", SID + "_e2", SID + "_e3"]
