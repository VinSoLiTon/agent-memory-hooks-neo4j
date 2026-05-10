#!/usr/bin/env python3
"""njhook dashboard — local Flask UI for the memory graph.

Routes:
    /                       redirects to /memories
    /memories               list / filter memories
    /memory/<path>          view one memory + provenance
    /memory/<path>/edit     edit (POST saves)
    /memory/<path>/delete   POST removes
    /memory/<path>/archive  POST toggles archived
    /sessions               list captured sessions
    /session/<sid>          walk a session's events
    /search?q=...           hybrid search

Run:
    python dashboard/app.py            # http://localhost:5000

Bind a different port via DASHBOARD_PORT or --port. Bind interface via
DASHBOARD_HOST or --host (default 127.0.0.1; only loopback by default
since memories may contain sensitive content).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request, url_for
from neo4j import GraphDatabase
from markupsafe import Markup, escape

# Pull in the shared modules from hooks/.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import embeddings  # noqa: E402

NEO4J_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
NEO4J_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")

driver_singleton = None


def driver():
    global driver_singleton
    if driver_singleton is None:
        # PR-G #2: silence harmless "property does not exist" notifications.
        driver_singleton = GraphDatabase.driver(
            NEO4J_URI, auth=(NEO4J_USER, NEO4J_PASSWORD),
            notifications_disabled_classifications=["UNRECOGNIZED"],
        )
    return driver_singleton


app = Flask(__name__)

# PR-H #3: write routes (edit, delete, archive, save) are gated behind an
# explicit env var. Dashboard binds to localhost by default but the routes are
# still destructive and any local process can hit them; require opt-in.
WRITE_ENABLED = os.environ.get("DASHBOARD_WRITE") == "1"


def _require_write():
    """abort(403) when write routes are disabled. Use as the first line of
    every POST/destructive handler."""
    if not WRITE_ENABLED:
        abort(403, "dashboard is read-only; set DASHBOARD_WRITE=1 to enable edit/delete/archive")


# --- minimal styling, embedded so there are no static-file deps -----------

BASE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{{title or 'njhook dashboard'}}</title>
<style>
  :root{--bg:#0f1115;--fg:#dde2e6;--muted:#7a8590;--accent:#7aa2f7;--card:#171a21;--border:#262b33;--bad:#e06c75;--ok:#98c379;}
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:14px;line-height:1.5}
  header{padding:12px 24px;border-bottom:1px solid var(--border);background:var(--card);display:flex;align-items:center;gap:24px;position:sticky;top:0;z-index:10}
  header strong{color:var(--accent)}
  header a{color:var(--fg);text-decoration:none;padding:4px 0;border-bottom:2px solid transparent}
  header a:hover, header a.active{border-color:var(--accent);color:#fff}
  header form{margin-left:auto}
  header input[type=search]{background:var(--bg);border:1px solid var(--border);color:var(--fg);padding:6px 10px;border-radius:4px;width:280px;font:inherit}
  main{padding:24px;max-width:1100px;margin:0 auto}
  table{width:100%;border-collapse:collapse;margin:8px 0}
  th,td{padding:6px 12px;border-bottom:1px solid var(--border);text-align:left;vertical-align:top}
  th{color:var(--muted);font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
  tr:hover td{background:var(--card)}
  code,pre,.mono{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace}
  pre{background:var(--card);border:1px solid var(--border);padding:14px;border-radius:6px;overflow-x:auto;white-space:pre-wrap;word-break:break-word}
  .muted{color:var(--muted)}
  .pill{display:inline-block;padding:1px 8px;border-radius:10px;background:var(--card);border:1px solid var(--border);font-size:11px;color:var(--muted)}
  .pill.bad{color:var(--bad);border-color:var(--bad)}
  .pill.ok{color:var(--ok);border-color:var(--ok)}
  .toolbar{display:flex;gap:8px;align-items:center;margin:0 0 16px 0;flex-wrap:wrap}
  .toolbar a, .toolbar button{background:var(--card);border:1px solid var(--border);color:var(--fg);text-decoration:none;padding:6px 12px;border-radius:4px;font:inherit;cursor:pointer}
  .toolbar a:hover, .toolbar button:hover{border-color:var(--accent);color:#fff}
  .toolbar form{display:inline}
  textarea{width:100%;min-height:60vh;background:var(--bg);color:var(--fg);border:1px solid var(--border);padding:12px;border-radius:6px;font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;font-size:13px;line-height:1.5}
  h1{margin:0 0 16px 0;font-size:18px}
  h2{margin:24px 0 8px 0;font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.5px}
  a{color:var(--accent)}
  .small{font-size:12px}
  details{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin:6px 0}
  details summary{cursor:pointer;color:var(--muted)}
  .row{display:flex;gap:24px;align-items:baseline}
  .row > *:first-child{min-width:120px;color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.5px}
</style>
</head>
<body>
<header>
  <strong>njhook</strong>
  <a href="{{url_for('memories')}}" {% if active=='memories' %}class="active"{% endif %}>Memories</a>
  <a href="{{url_for('sessions')}}" {% if active=='sessions' %}class="active"{% endif %}>Sessions</a>
  <a href="{{url_for('stats')}}" {% if active=='stats' %}class="active"{% endif %}>Stats</a>
  {% if read_only %}<span class="pill" title="set DASHBOARD_WRITE=1 to enable">read-only</span>{% endif %}
  <form action="{{url_for('search')}}" method="get">
    <input type="search" name="q" placeholder="search memories…" value="{{q or ''}}" autofocus>
  </form>
</header>
<main>{{ body|safe }}</main>
</body>
</html>"""


def page(active: str, title: str, body_html: str, q: str | None = None) -> str:
    return render_template_string(
        BASE, active=active, title=title, body=Markup(body_html), q=q,
        read_only=not WRITE_ENABLED,
    )


def fmt_ts(ts) -> str:
    if not ts:
        return ""
    return str(ts)[:19].replace("T", " ")


# --- routes ---------------------------------------------------------------

@app.route("/")
def index():
    return redirect(url_for("memories"))


@app.route("/memories")
def memories():
    kind = request.args.get("kind") or ""
    project_arg = request.args.get("project") or ""
    include_archived = request.args.get("archived") == "1"

    where, params = [], {}
    if not include_archived:
        where.append("coalesce(m.archived, false) = false")
    if kind:
        where.append("m.path STARTS WITH $kp")
        params["kp"] = kind.rstrip("/") + "/"
    if project_arg:
        where.append("m.project = $project")
        params["project"] = project_arg

    cypher = (
        "MATCH (m:Memory) "
        + (("WHERE " + " AND ".join(where) + " ") if where else "")
        + "RETURN m.path AS path, m.updated_at AS u, m.project AS project, "
        + "       coalesce(m.archived,false) AS archived, "
        + "       coalesce(m.access_count,0) AS access "
        + "ORDER BY m.updated_at DESC, m.path"
    )
    with driver().session() as s:
        rows = list(s.run(cypher, parameters=params))

    body = "<h1>Memories</h1>"
    body += '<div class="toolbar">'
    for k in ("", "profile", "tools", "project", "general"):
        href = url_for("memories", kind=k or None, project=project_arg or None,
                       archived="1" if include_archived else None)
        label = k or "all"
        cls = ' style="border-color:var(--accent);color:#fff"' if (kind or "") == k else ""
        body += f'<a href="{href}"{cls}>{escape(label)}</a>'
    body += f' <a href="{url_for("memories", archived=None if include_archived else "1")}">{"hide" if include_archived else "show"} archived</a>'
    body += "</div>"

    if not rows:
        body += "<p class='muted'>(no memories matched)</p>"
    else:
        body += "<table><tr><th>path</th><th>project</th><th>updated</th><th class='small'>reads</th><th></th></tr>"
        for r in rows:
            archived_pill = ' <span class="pill bad">archived</span>' if r["archived"] else ""
            project_html = f'<span class="pill">{escape(r["project"])}</span>' if r["project"] else ""
            body += (
                f'<tr><td><a class="mono" href="{url_for("memory_view", path=r["path"])}">{escape(r["path"])}</a>{archived_pill}</td>'
                f'<td>{project_html}</td>'
                f'<td class="muted small">{fmt_ts(r["u"])}</td>'
                f'<td class="small muted">{r["access"]}</td><td></td></tr>'
            )
        body += "</table>"
    return page("memories", "Memories", body)


@app.route("/memory/<path:path>")
def memory_view(path: str):
    with driver().session() as s:
        r = s.run(
            """
            MATCH (m:Memory {path: $path})
            OPTIONAL MATCH (m)-[:DERIVED_FROM]->(sess:Session)
            WITH m, collect(DISTINCT sess.session_id) AS sessions
            RETURN m.path AS path, m.content AS content, m.updated_at AS u,
                   m.project AS project, coalesce(m.archived,false) AS archived,
                   coalesce(m.access_count,0) AS access,
                   m.last_accessed_at AS last,
                   m.consolidated_from AS consolidated_from,
                   sessions
            """,
            parameters={"path": path},
        ).single()
    if not r:
        abort(404, f"no memory at {path}")

    body = f'<h1 class="mono">{escape(r["path"])}</h1>'
    if WRITE_ENABLED:
        body += '<div class="toolbar">'
        body += f'<a href="{url_for("memory_edit", path=path)}">Edit</a>'
        arch_label = "Unarchive" if r["archived"] else "Archive"
        body += (
            f'<form method="post" action="{url_for("memory_archive", path=path)}">'
            f'<button>{arch_label}</button></form>'
        )
        body += (
            f'<form method="post" action="{url_for("memory_delete", path=path)}" '
            f'onsubmit="return confirm(\'Delete {escape(path)}?\')"><button style="color:var(--bad)">Delete</button></form>'
        )
        body += "</div>"
    else:
        body += '<p class="muted small">Read-only mode. Set <code>DASHBOARD_WRITE=1</code> to enable edit / archive / delete.</p>'

    body += '<div class="row"><div>updated</div><div>' + escape(fmt_ts(r["u"])) + "</div></div>"
    if r["project"]:
        body += '<div class="row"><div>project</div><div><span class="pill">' + escape(r["project"]) + "</span></div></div>"
    body += f'<div class="row"><div>archived</div><div>{"yes" if r["archived"] else "no"}</div></div>'
    body += f'<div class="row"><div>reads</div><div>{r["access"]} (last {escape(fmt_ts(r["last"]))})</div></div>'
    if r["consolidated_from"]:
        body += '<div class="row"><div>merged from</div><div>' + ", ".join(
            f'<code>{escape(p)}</code>' for p in r["consolidated_from"]
        ) + "</div></div>"
    body += "<h2>content</h2><pre>" + escape(r["content"] or "") + "</pre>"
    if r["sessions"]:
        body += "<h2>derived from</h2><ul>"
        for sid in r["sessions"]:
            body += f'<li><a class="mono" href="{url_for("session_view", sid=sid)}">{escape(sid)}</a></li>'
        body += "</ul>"
    return page("memories", path, body)


@app.route("/memory/<path:path>/edit", methods=["GET", "POST"])
def memory_edit(path: str):
    _require_write()
    if request.method == "POST":
        new_content = request.form.get("content", "")
        with driver().session() as s:
            s.run(
                "MERGE (m:Memory {path: $path}) "
                "SET m.content = $content, m.updated_at = $now",
                parameters={"path": path, "content": new_content,
                            "now": datetime.now(timezone.utc).isoformat()},
            )
        return redirect(url_for("memory_view", path=path))

    with driver().session() as s:
        r = s.run("MATCH (m:Memory {path: $path}) RETURN m.content AS content",
                  parameters={"path": path}).single()
    content = (r["content"] if r else "") or ""
    body = f'<h1 class="mono">edit: {escape(path)}</h1>'
    body += f'<form method="post"><textarea name="content">{escape(content)}</textarea>'
    body += '<div class="toolbar"><button>Save</button>'
    body += f'<a href="{url_for("memory_view", path=path)}">Cancel</a></div></form>'
    return page("memories", f"edit {path}", body)


@app.route("/memory/<path:path>/delete", methods=["POST"])
def memory_delete(path: str):
    _require_write()
    with driver().session() as s:
        s.run("MATCH (m:Memory {path: $path}) DETACH DELETE m", parameters={"path": path})
    return redirect(url_for("memories"))


@app.route("/memory/<path:path>/archive", methods=["POST"])
def memory_archive(path: str):
    _require_write()
    with driver().session() as s:
        s.run(
            "MATCH (m:Memory {path: $path}) "
            "SET m.archived = NOT coalesce(m.archived, false), "
            "    m.archived_at = $now",
            parameters={"path": path, "now": datetime.now(timezone.utc).isoformat()},
        )
    return redirect(url_for("memory_view", path=path))


@app.route("/sessions")
def sessions():
    """PR-F #1: list by session_key (canonical) so cross-client raw-id
    collisions can't silently merge views."""
    with driver().session() as s:
        rows = list(s.run(
            """
            MATCH (s:Session)
            OPTIONAL MATCH (s)-[:FIRST_EVENT|NEXT*0..]->(e:Event)
            WITH s, count(DISTINCT e) AS events
            RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS session_key,
                   s.session_id AS sid, s.client AS client, s.created_at AS created,
                   s.last_dreamed_at AS dreamed, events
            ORDER BY s.created_at DESC LIMIT 200
            """
        ))
    body = "<h1>Sessions</h1>"
    body += "<table><tr><th>session_key</th><th>client</th><th>created</th><th>events</th><th>dreamed</th></tr>"
    for r in rows:
        sk = r["session_key"]
        body += (
            f'<tr><td><a class="mono" href="{url_for("session_view", sid=sk)}">{escape(sk[:60])}</a></td>'
            f'<td><span class="pill">{escape(r["client"] or "?")}</span></td>'
            f'<td class="muted small">{fmt_ts(r["created"])}</td>'
            f'<td class="small">{r["events"]}</td>'
            f'<td>{"<span class=\"pill ok\">yes</span>" if r["dreamed"] else "<span class=\"pill\">—</span>"}</td></tr>'
        )
    body += "</table>"
    return page("sessions", "Sessions", body)


@app.route("/session/<path:sid>")
def session_view(sid: str):
    """PR-F #1: query by session_key, accept session_id as fallback. If the
    raw id collides across clients, render a chooser instead of merging."""
    with driver().session() as s:
        candidates = list(s.run(
            "MATCH (s:Session) WHERE s.session_key = $sid OR s.session_id = $sid "
            "RETURN coalesce(s.session_key, s.client + ':' + s.session_id) AS sk, s.client AS client",
            parameters={"sid": sid},
        ))
        if not candidates:
            abort(404, f"no session matching {sid}")
        if len(candidates) > 1:
            body = f'<h1 class="mono">{escape(sid)} matches {len(candidates)} sessions</h1>'
            body += "<p class='muted'>Pick one:</p><ul>"
            for c in candidates:
                body += f'<li><a class="mono" href="{url_for("session_view", sid=c["sk"])}">{escape(c["sk"])}</a> (client={escape(c["client"] or "?")})</li>'
            body += "</ul>"
            return page("sessions", sid, body)
        session_key = candidates[0]["sk"]

        rows = list(s.run(
            """
            MATCH (s:Session {session_key: $sk})-[:FIRST_EVENT|NEXT*0..]->(e:Event)
            WITH DISTINCT e
            RETURN e.timestamp AS ts, e.event_name AS name, e.tool_name AS tool,
                   e.prompt AS prompt, e.tool_input AS ti, e.tool_response AS tr
            ORDER BY e.timestamp
            """,
            parameters={"sk": session_key},
        ))
    if not rows:
        abort(404, f"no events for session {session_key}")
    body = f'<h1 class="mono">session {escape(session_key)}</h1>'
    body += '<p class="muted">' + str(len(rows)) + " events</p>"
    for r in rows:
        head = f"<strong>{escape(r['name'] or '?')}</strong>"
        if r["tool"]:
            head += f' <span class="pill">tool={escape(r["tool"])}</span>'
        body += f'<details><summary>{escape(fmt_ts(r["ts"]))} — {head}</summary>'
        for label, val in (("prompt", r["prompt"]), ("input", r["ti"]), ("output", r["tr"])):
            if val:
                body += f'<h2>{escape(label)}</h2><pre>{escape(str(val))}</pre>'
        body += "</details>"
    return page("sessions", sid, body)


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return redirect(url_for("memories"))

    # Escape Lucene reserved chars so user queries with `:`, `-`, `(`, etc. work.
    import re as _re
    safe_q = _re.sub(r'([+\-!(){}\[\]^"~*?:\\/]|&&|\|\|)', r'\\\1', q)

    rows = []
    with driver().session() as s:
        try:
            rows.extend(s.run(
                """
                CALL db.index.fulltext.queryNodes('memory_fulltext', $q)
                YIELD node, score
                WHERE coalesce(node.archived,false) = false
                RETURN node.path AS path, node.content AS content, score, 'fulltext' AS source
                ORDER BY score DESC LIMIT 20
                """,
                parameters={"q": safe_q},
            ).data())
        except Exception:
            pass
        if embeddings.is_enabled():
            try:
                qvec = embeddings.embed([q])
                if qvec:
                    rows.extend(s.run(
                        """
                        CALL db.index.vector.queryNodes('memory_embeddings', 20, $qvec)
                        YIELD node, score
                        WHERE coalesce(node.archived,false) = false
                        RETURN node.path AS path, node.content AS content, score, 'vector' AS source
                        """,
                        parameters={"qvec": qvec[0]},
                    ).data())
            except Exception:
                pass

    # Reciprocal Rank Fusion across the two streams.
    by_path: dict[str, dict] = {}
    rrf: dict[str, float] = {}
    k = 60
    for source in ("fulltext", "vector"):
        ranked = [r for r in rows if r["source"] == source]
        for rank, r in enumerate(ranked):
            rrf[r["path"]] = rrf.get(r["path"], 0.0) + 1.0 / (k + rank + 1)
            by_path.setdefault(r["path"], r)
    ordered = sorted(by_path.values(), key=lambda r: rrf[r["path"]], reverse=True)[:30]

    body = f"<h1>Search results for <code>{escape(q)}</code></h1>"
    if not ordered:
        body += "<p class='muted'>no matches</p>"
    else:
        body += "<table><tr><th>path</th><th>preview</th><th class='small'>rrf</th></tr>"
        for r in ordered:
            preview = (r["content"] or "").splitlines()
            # skip frontmatter
            i = 0
            if preview and preview[0].strip() == "---":
                i = 1
                while i < len(preview) and preview[i].strip() != "---":
                    i += 1
                i += 1
            while i < len(preview) and not preview[i].strip():
                i += 1
            line = preview[i] if i < len(preview) else ""
            body += (
                f'<tr><td><a class="mono" href="{url_for("memory_view", path=r["path"])}">{escape(r["path"])}</a></td>'
                f'<td class="small muted">{escape(line[:160])}</td>'
                f'<td class="small muted">{rrf[r["path"]]:.4f}</td></tr>'
            )
        body += "</table>"
    return page("memories", f"search: {q}", body, q=q)


@app.route("/stats")
def stats():
    with driver().session() as s:
        m_total = s.run("MATCH (m:Memory) RETURN count(m) AS n").single()["n"]
        m_archived = s.run("MATCH (m:Memory) WHERE coalesce(m.archived,false)=true RETURN count(m) AS n").single()["n"]
        m_emb = s.run("MATCH (m:Memory) WHERE m.embedding IS NOT NULL RETURN count(m) AS n").single()["n"]
        m_by_kind = list(s.run(
            "MATCH (m:Memory) WITH split(m.path,'/')[0] AS kind, count(*) AS n RETURN kind,n ORDER BY n DESC"
        ))
        s_total = s.run("MATCH (s:Session) RETURN count(s) AS n").single()["n"]
        s_by_client = list(s.run("MATCH (s:Session) RETURN s.client AS client, count(*) AS n ORDER BY n DESC"))
        e_total = s.run("MATCH (e:Event) RETURN count(e) AS n").single()["n"]
        top_accessed = list(s.run(
            "MATCH (m:Memory) WHERE m.access_count IS NOT NULL "
            "RETURN m.path AS path, m.access_count AS n ORDER BY n DESC LIMIT 10"
        ))
    body = "<h1>Stats</h1>"
    body += f"<p>Memories: <strong>{m_total}</strong> ({m_archived} archived, {m_emb} embedded)</p>"
    body += "<table><tr><th>kind</th><th>count</th></tr>"
    for r in m_by_kind:
        body += f'<tr><td><a href="{url_for("memories", kind=r["kind"])}">{escape(r["kind"])}</a></td><td>{r["n"]}</td></tr>'
    body += "</table>"
    body += f"<h2>Sessions: {s_total}</h2><table><tr><th>client</th><th>count</th></tr>"
    for r in s_by_client:
        body += f'<tr><td>{escape(r["client"] or "?")}</td><td>{r["n"]}</td></tr>'
    body += "</table>"
    body += f"<p>Events: <strong>{e_total}</strong></p>"
    if top_accessed:
        body += "<h2>Most-accessed memories</h2><table><tr><th>path</th><th>reads</th></tr>"
        for r in top_accessed:
            body += f'<tr><td><a class="mono" href="{url_for("memory_view", path=r["path"])}">{escape(r["path"])}</a></td><td>{r["n"]}</td></tr>'
        body += "</table>"
    return page("stats", "Stats", body)


# --- launcher -------------------------------------------------------------

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", "5000")))
    p.add_argument("--debug", action="store_true")
    args = p.parse_args()
    print(f"njhook dashboard on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
