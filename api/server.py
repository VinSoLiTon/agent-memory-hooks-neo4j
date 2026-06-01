#!/usr/bin/env python3
"""Phase G — REST API over the shared recall/capture core.

A thin JSON layer so any HTTP-capable runtime can recall + write into the same
graph the hooks use. It calls the exact same code paths (`service.recall_context`
→ `recall.py`; `log_event.log_event` for capture), so hits are identical to the
hook and CLI — that's the Phase G parity guarantee.

Run:
    python api/server.py            # http://127.0.0.1:5099
Bind via NJHOOK_API_HOST / NJHOOK_API_PORT (loopback only by default — memories
may be sensitive).

Routes:
    POST /recall   {prompt, cwd?, limit?}      -> {hits: [...]}
    POST /events   {client, ...hook_payload}   -> {ok: true}   (shared capture path)
    GET  /health                               -> {ok, active_memories, spool_backlog}
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from flask import Flask, jsonify, request
from neo4j import GraphDatabase

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import service       # noqa: E402
import log_event     # noqa: E402
import spool         # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")
VALID_CLIENTS = ("claude_code", "codex", "cursor", "gemini")

_driver = None


def driver():
    global _driver
    if _driver is None:
        _driver = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
            notifications_disabled_classifications=["UNRECOGNIZED"],
        )
    return _driver


app = Flask(__name__)


@app.route("/recall", methods=["POST"])
def recall_route():
    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"error": "prompt required"}), 400
    with driver().session() as s:
        hits = service.recall_context(s, prompt, cwd=body.get("cwd"), limit=int(body.get("limit", 5)))
    return jsonify({"hits": hits})


@app.route("/events", methods=["POST"])
def events_route():
    body = request.get_json(silent=True) or {}
    client = body.get("client")
    if client not in VALID_CLIENTS:
        return jsonify({"error": f"client must be one of {VALID_CLIENTS}"}), 400
    payload = {k: v for k, v in body.items() if k != "client"}
    try:
        log_event.log_event(payload, client=client)   # same capture path as the hook
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


@app.route("/health", methods=["GET"])
def health_route():
    try:
        with driver().session() as s:
            n = s.run("MATCH (m:Memory) WHERE coalesce(m.status, 'active') = 'active' "
                      "RETURN count(m) AS n").single()["n"]
        return jsonify({"ok": True, "active_memories": n, "spool_backlog": spool.backlog_count()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 503


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("NJHOOK_API_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("NJHOOK_API_PORT", "5099")))
    args = p.parse_args()
    app.run(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
