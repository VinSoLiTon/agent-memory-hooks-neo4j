"""Unit tests for hooks/privacy.py — runs without Neo4j or hooks scaffolding."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import privacy  # noqa: E402


def test_scrub_anthropic_key():
    # Synthetic Anthropic-shaped key — 95 random chars, structurally valid
    # for the regex but never issued. Real keys must never appear in tests
    # or any committed file (GitHub secret scanning will block the push).
    s = "key=sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA next"
    out = privacy.scrub(s)
    assert "sk-ant-api03" not in out, out
    assert "<REDACTED:anthropic_key>" in out


def test_scrub_openai_key():
    s = "OPENAI=sk-proj-abcdefghij1234567890klmnop"
    out = privacy.scrub(s)
    assert "sk-proj-" not in out
    # OpenAI key falls under the generic api_key pattern OR the env-style assignment;
    # either redaction is acceptable.
    assert "REDACTED" in out


def test_scrub_github_token():
    s = "token=gho_aBcDeFgHiJkLmNoPqRsTuVwXyZ0123456789"
    out = privacy.scrub(s)
    assert "gho_" not in out
    assert "<REDACTED" in out


def test_scrub_aws_access_key_id():
    s = "AWS=AKIAIOSFODNN7EXAMPLE other"
    out = privacy.scrub(s)
    assert "AKIA" not in out


def test_scrub_jwt():
    s = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    out = privacy.scrub(s)
    assert "eyJ" not in out
    assert "Bearer" in out  # marker preserved


def test_scrub_env_assignment_keeps_var_name():
    s = "DATABASE_PASSWORD=hunter2hunter2 next line"
    out = privacy.scrub(s)
    assert "DATABASE_PASSWORD=<REDACTED>" in out, out


def test_scrub_pem_block():
    s = "before\n-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB...\n-----END RSA PRIVATE KEY-----\nafter"
    out = privacy.scrub(s)
    assert "PRIVATE KEY" not in out
    assert "before" in out and "after" in out


def test_scrub_disabled_via_env():
    raw = "sk-ant-api03-AAAAAAAAAAAAAAAAAAAA"
    os.environ["HOOKS_DISABLE_SCRUB"] = "1"
    try:
        assert privacy.scrub(raw) == raw
    finally:
        del os.environ["HOOKS_DISABLE_SCRUB"]


def test_scrub_passthrough_for_non_strings():
    assert privacy.scrub(None) is None
    assert privacy.scrub(123) == 123
    assert privacy.scrub({"x": 1}) == {"x": 1}


def test_optout_via_env(tmp_path: Path):
    # Use a real path so .resolve() succeeds (Path(...).resolve() requires
    # the parent to exist on Windows; tmp_path is guaranteed to exist).
    target = tmp_path / "secret-project"
    target.mkdir()
    os.environ["HOOKS_OPT_OUT_PATHS"] = str(target)
    try:
        assert privacy.is_optout(str(target)) is True
        assert privacy.is_optout(str(target / "nested")) is True
        assert privacy.is_optout(str(tmp_path / "other")) is False
        assert privacy.is_optout(None) is False
    finally:
        del os.environ["HOOKS_OPT_OUT_PATHS"]


def test_optout_no_blocklist_returns_false(tmp_path: Path):
    # No env var, no file (or unrelated entries) → never opts out.
    os.environ.pop("HOOKS_OPT_OUT_PATHS", None)
    assert privacy.is_optout(str(tmp_path)) is False


if __name__ == "__main__":
    # Lightweight runner so we don't require pytest in the dream venv.
    failed = 0
    for name, fn in list(globals().items()):
        if not name.startswith("test_") or not callable(fn):
            continue
        try:
            sig = fn.__code__.co_varnames[: fn.__code__.co_argcount]
            if sig == ("tmp_path",):
                with tempfile.TemporaryDirectory() as d:
                    fn(Path(d))
            else:
                fn()
            print(f"  PASS {name}")
        except AssertionError as e:
            print(f"  FAIL {name}: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR {name}: {type(e).__name__}: {e}")
            failed += 1
    print(f"\n{failed} failure(s)" if failed else "\nAll privacy tests passed.")
    sys.exit(1 if failed else 0)
