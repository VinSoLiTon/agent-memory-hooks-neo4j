#!/usr/bin/env python3
"""Phase G (PR-3) — file renderers.

Pure tests pin the managed-block splice contract (idempotent, human content
outside the markers is never touched, a truncated half-marker can't eat the
doc) and the closed target vocabulary round-tripped through the CLI parser.
The DB-backed tests prove a real render writes/updates the right file from the
same recall core the hook uses, with Cursor frontmatter kept outside the block.
"""
import importlib.util
import os
import sys

import pytest
from neo4j import GraphDatabase

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))

import render  # noqa: E402

MARK = "__rendertest"
TOKEN = "qqrendermark"
FAR_FUTURE = "2099-01-01T00:00:00+00:00"   # sorts first in fetch_bucket (updated_at DESC)

_URI = os.environ.get("HOOKS_NEO4J_URI", "bolt://localhost:7687")
_USER = os.environ.get("HOOKS_NEO4J_USER", "neo4j")
_PWD = os.environ.get("HOOKS_NEO4J_PASSWORD", "password")


# --- pure splice / vocabulary tests (no DB) ---------------------------------

def test_build_block_wraps_with_markers():
    blk = render.build_block("hello memory")
    assert blk.startswith(render.BEGIN)
    assert blk.rstrip().endswith(render.END)
    assert "hello memory" in blk


def test_build_block_empty_uses_placeholder():
    assert "no memories distilled yet" in render.build_block("   ")


def test_splice_appends_when_no_markers_preserving_human():
    human = "# My project\n\nHand-written notes.\n"
    out = render.splice(human, render.build_block("MEM"))
    assert out.startswith(human)          # every human byte preserved, up front
    assert render.BEGIN in out and "MEM" in out


def test_splice_replaces_only_between_markers():
    existing = ("TOP human\n" + render.build_block("OLD") + "\nBOTTOM human\n")
    out = render.splice(existing, render.build_block("NEW"))
    assert "TOP human" in out and "BOTTOM human" in out   # both human halves kept
    assert "NEW" in out and "OLD" not in out              # only the block swapped
    assert out.count(render.BEGIN) == 1                   # no duplicate block


def test_splice_idempotent():
    once = render.splice("human\n", render.build_block("MEM"))
    twice = render.splice(once, render.build_block("MEM"))
    assert once == twice


def test_splice_truncated_begin_without_end_appends():
    # A half-written file (BEGIN but no END) must not let the block swallow the
    # rest of the document — treat it as marker-less and append.
    broken = "human\n" + render.BEGIN + "\npartial..."
    out = render.splice(broken, render.build_block("MEM"))
    assert out.endswith(render.END + "\n")
    assert "partial..." in out            # the truncated tail is preserved, not eaten


def test_unknown_target_rejected():
    with pytest.raises(ValueError):
        render.target_path("bogus", "/tmp")


def test_target_vocabulary_is_closed():
    assert set(render.RENDER_TARGETS) == {"agents", "claude", "gemini", "cursor"}


def test_cli_target_choices_round_trip():
    # The CLI --target choices must accept every RENDER_TARGETS key (plus 'all')
    # and reject anything out of vocabulary.
    spec = importlib.util.spec_from_file_location("njhook_cli", os.path.join(ROOT, "cli", "njhook.py"))
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    parser = cli.build_parser()
    for t in (*render.RENDER_TARGETS, "all"):
        assert parser.parse_args(["render", "--target", t]).target == t
    with pytest.raises(SystemExit):
        parser.parse_args(["render", "--target", "bogus"])


# --- DB-backed render tests --------------------------------------------------

@pytest.fixture()
def driver():
    d = GraphDatabase.driver(_URI, auth=(_USER, _PWD),
                             notifications_disabled_classifications=["UNRECOGNIZED"])
    _cleanup(d)
    # A profile memory guaranteed to surface: FAR_FUTURE updated_at sorts it first
    # in the profile bucket, high importance keeps it within the char budget.
    with d.session() as s:
        s.run("MERGE (m:Memory {path: $p}) SET m.content = $c, m.status = 'active', "
              "m.importance = 10, m.updated_at = $t, m.kind = 'profile'",
              p=f"profile/{MARK}_role.md", c=f"{TOKEN} the user prefers terse answers.", t=FAR_FUTURE)
    try:
        yield d
    finally:
        _cleanup(d)
        d.close()


def _cleanup(d):
    with d.session() as s:
        s.run("MATCH (m:Memory) WHERE m.path CONTAINS $mark DETACH DELETE m", mark=MARK)


def test_render_creates_then_is_idempotent(driver, tmp_path):
    with driver.session() as s:
        r1 = render.render_target(s, "agents", str(tmp_path))
        out = tmp_path / "AGENTS.md"
        assert r1["action"] == "created" and out.exists()
        text = out.read_text(encoding="utf-8")
        assert render.BEGIN in text and render.END in text
        assert TOKEN in text                      # real recall content landed in the block
        r2 = render.render_target(s, "agents", str(tmp_path))
        assert r2["action"] == "unchanged"
        assert out.read_text(encoding="utf-8") == text   # byte-identical re-render


def test_render_preserves_human_content(driver, tmp_path):
    out = tmp_path / "AGENTS.md"
    out.write_text("# Hand-written\n\nKeep me.\n", encoding="utf-8")
    with driver.session() as s:
        r = render.render_target(s, "agents", str(tmp_path))
    assert r["action"] == "updated"
    text = out.read_text(encoding="utf-8")
    assert text.startswith("# Hand-written")      # human content untouched, still on top
    assert "Keep me." in text and render.BEGIN in text


def test_cursor_frontmatter_outside_block(driver, tmp_path):
    with driver.session() as s:
        render.render_target(s, "cursor", str(tmp_path))
    out = tmp_path / ".cursor" / "rules" / "njhook-memory.mdc"
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert text.startswith("---\n")               # YAML frontmatter at the very top
    assert text.index("alwaysApply") < text.index(render.BEGIN)   # outside the managed block


def test_proposed_text_does_not_write(driver, tmp_path):
    with driver.session() as s:
        text, existed = render.proposed_text(s, "agents", str(tmp_path))
    assert not existed
    assert not (tmp_path / "AGENTS.md").exists()  # preview only, no write
    assert render.BEGIN in text
