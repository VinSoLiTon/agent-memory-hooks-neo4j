#!/usr/bin/env python3
"""A/B the verbose vs compact dream transcript encoder on real sessions.

Measures density only (deterministic, no LLM): event count, char count, estimated
tokens (chars / 2.3 for code/JSON), % reduction, and how many events fit the live
transcript budget each way. Quality is validated separately via eval-distillation
and a real dream comparison.

    python scripts/dream_encoding_ab.py [session_key ...]
    python scripts/dream_encoding_ab.py            # auto-picks the largest sessions
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

from neo4j import GraphDatabase  # noqa: E402
import dream as d  # noqa: E402

URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
CHARS_PER_TOK = 2.3


def _walk(s, sk):
    first = s.run("MATCH (sess:Session {session_key:$sk})-[:FIRST_EVENT]->(e:Event) RETURN e",
                  sk=sk).single()
    if not first:
        return []
    out, seen, node = [], set(), dict(first["e"])
    while node is not None:
        eid = node.get("event_id")
        if not eid or eid in seen:
            break
        seen.add(eid)
        out.append(node)
        nxt = s.run("MATCH (:Event {event_id:$eid})-[:NEXT]->(n:Event) RETURN n LIMIT 1",
                    eid=eid).single()
        node = dict(nxt["n"]) if nxt else None
    return out


def _budget():
    return d._derived_transcript_cap("llamacpp")


def main(argv):
    d_ = GraphDatabase.driver(URI, auth=(USER, PWD),
                              notifications_disabled_classifications=["UNRECOGNIZED"])
    with d_.session() as s:
        if argv:
            keys = argv
        else:
            keys = [r["sk"] for r in s.run(
                "MATCH (sess:Session)-[:FIRST_EVENT]->(e:Event) WHERE (e)-[:NEXT]->() "
                "MATCH (sess)-[:LATEST_EVENT]->(last:Event) "
                "RETURN sess.session_key AS sk ORDER BY last.timestamp DESC LIMIT 5")]
        cap = _budget()
        print(f"transcript budget = {cap:,} chars (~{cap/CHARS_PER_TOK:,.0f} tok)\n")
        hdr = f"{'session':<26}{'events':>7}{'verbose ch':>12}{'compact ch':>12}{'reduct':>8}{'v fit':>7}{'c fit':>7}"
        print(hdr); print("-" * len(hdr))
        tot_v = tot_c = 0
        for sk in keys:
            evs = _walk(s, sk)
            if not evs:
                print(f"{sk[:24]:<26}  (no events)")
                continue
            os.environ.pop("DREAM_COMPACT_TRANSCRIPT", None)
            v_full = d.render_events(evs, max_chars=None)
            v_fit = d.render_events(evs, max_chars=cap)
            os.environ["DREAM_COMPACT_TRANSCRIPT"] = "1"
            c_full = d.render_events(evs, max_chars=None)
            c_fit = d.render_events(evs, max_chars=cap)
            os.environ.pop("DREAM_COMPACT_TRANSCRIPT", None)
            red = 100 * (1 - len(c_full) / max(len(v_full), 1))
            # events that fit the budget = full render unless a dropped-note appears
            vf = "ALL" if "omitted to fit" not in v_fit else str(v_fit.count("\n  input:") + v_fit.count("  prompt:"))
            cf = "ALL" if "omitted to fit" not in c_fit else str(c_fit.count("#"))
            print(f"{sk[:24]:<26}{len(evs):>7}{len(v_full):>12,}{len(c_full):>12,}{red:>7.0f}%{vf:>7}{cf:>7}")
            tot_v += len(v_full); tot_c += len(c_full)
        if tot_v:
            print("-" * len(hdr))
            print(f"{'TOTAL':<26}{'':>7}{tot_v:>12,}{tot_c:>12,}{100*(1-tot_c/tot_v):>7.0f}%")
    d_.close()


if __name__ == "__main__":
    main(sys.argv[1:])
