#!/usr/bin/env python3
"""Memory-hygiene guard (fix/memory-hygiene-guards).

Root-caused corruption class: rendered-injection output (the `## <path>` block
headers + framing markers recall emits into hook context) was written back
into a memory body (`project/njhook.md` accumulated 4 stacked self-headers +
3x bloat + drift). These tests pin the guard at every sanctioned write door:
dream's validate_memory, service.propose_memory, and the embed-text cap that
keeps oversized bodies from 500-ing the embedding server mid-backfill.

Pure / static — no Neo4j, no embedding server.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import content_guard  # noqa: E402
import embeddings  # noqa: E402
import quality  # noqa: E402
import service  # noqa: E402

CLEAN = "---\ntitle: t\nkind: fact\n---\n\nPostgres was chosen for multi-row transactions."
ECHOED = ("---\ntitle: t\nkind: context\n---\n\n"
          "## project/njhook.md\n## project/njhook.md\n"
          "Repo overview text that embeds its own rendered header.")


def test_clean_body_passes():
    assert content_guard.injection_artifacts(CLEAN) == []


def test_rendered_header_detected():
    found = content_guard.injection_artifacts(ECHOED)
    assert found and "rendered memory-block header" in found[0]


def test_every_framing_marker_detected():
    for marker in content_guard.INJECTION_MARKERS:
        body = f"---\ntitle: t\nkind: fact\n---\n\nsome text\n{marker} trailing"
        found = content_guard.injection_artifacts(body)
        assert found, f"marker not detected: {marker!r}"


def test_inline_path_mention_is_not_flagged():
    # Prose that MENTIONS a memory path (not a rendered block header on its
    # own line) must stay legal — memories legitimately reference each other.
    body = "---\ntitle: t\nkind: fact\n---\n\nSee project/njhook.md and `## headings` generally."
    assert content_guard.injection_artifacts(body) == []


def test_validate_memory_rejects_artifacts():
    errors = quality.validate_memory({"path": "project/x.md", "content": ECHOED})
    assert any("rendered memory-block header" in e for e in errors)


def test_validate_memory_still_accepts_clean():
    errors = quality.validate_memory({"path": "project/x.md", "content": CLEAN})
    assert not any("artifact" in e or "rendered" in e for e in errors)


def test_propose_memory_rejects_artifacts():
    out = service.propose_memory(None, "general/echo.md", ECHOED)
    assert out["ok"] is False and "injection-render artifacts" in out["error"]


def test_embed_text_capped():
    huge = "z" * 20000
    t = embeddings.memory_text("project/big.md", huge)
    assert len(t) <= embeddings.EMBED_TEXT_MAX_CHARS
