#!/usr/bin/env python3
"""Render docs/IMPROVEMENT_PROPOSAL.md -> docs/IMPROVEMENT_PROPOSAL.html.

Standalone, self-contained HTML (embedded CSS, no external assets) so the
proposal can be opened/shared without a server. Re-run after editing the .md.
"""
import os

import markdown

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "IMPROVEMENT_PROPOSAL.md")
OUT = os.path.join(HERE, "IMPROVEMENT_PROPOSAL.html")

CSS = """
:root{--bg:#0f1115;--fg:#dde2e6;--muted:#7a8590;--accent:#7aa2f7;--card:#171a21;--border:#262b33;--ok:#98c379;--bad:#e06c75;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;font-size:15px;line-height:1.65}
main{max-width:960px;margin:0 auto;padding:48px 28px 96px}
h1{font-size:28px;margin:0 0 8px;line-height:1.25}
h2{font-size:21px;margin:40px 0 12px;padding-bottom:6px;border-bottom:1px solid var(--border)}
h3{font-size:16px;margin:24px 0 6px;color:#fff}
a{color:var(--accent);text-decoration:none}
a:hover{text-decoration:underline}
p{margin:10px 0}
strong{color:#fff}
code{font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace;background:var(--card);border:1px solid var(--border);border-radius:4px;padding:1px 5px;font-size:.88em}
pre{background:var(--card);border:1px solid var(--border);border-radius:6px;padding:14px;overflow-x:auto}
pre code{border:0;padding:0;background:none}
blockquote{margin:16px 0;padding:10px 16px;border-left:3px solid var(--accent);background:var(--card);border-radius:0 6px 6px 0;color:var(--muted)}
ul,ol{padding-left:22px}
li{margin:4px 0}
hr{border:0;border-top:1px solid var(--border);margin:32px 0}
table{width:100%;border-collapse:collapse;margin:16px 0;font-size:14px}
th,td{border:1px solid var(--border);padding:7px 10px;text-align:left;vertical-align:top}
th{background:var(--card);color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.5px}
tr:nth-child(even) td{background:rgba(255,255,255,.015)}
.meta{color:var(--muted);font-size:13px;margin-bottom:24px}
"""

HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>njhook — Improvement Proposal</title>
<style>{css}</style></head>
<body><main>
<p class="meta">njhook deep-review · generated from docs/IMPROVEMENT_PROPOSAL.md</p>
{body}
</main></body></html>"""


def main() -> int:
    md = open(SRC, encoding="utf-8").read()
    body = markdown.markdown(
        md, extensions=["extra", "tables", "sane_lists", "toc", "nl2br"],
        output_format="html5",
    )
    open(OUT, "w", encoding="utf-8").write(HTML.format(css=CSS, body=body))
    print(f"wrote {OUT} ({len(body)} chars of body)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
