"""Unit tests for hooks/project.py — runs without Neo4j."""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hooks"))
import project  # noqa: E402


def test_derive_project_from_git_root(tmp_path: Path):
    repo = tmp_path / "MyRepo"
    (repo / ".git").mkdir(parents=True)
    sub = repo / "src" / "deep" / "nested"
    sub.mkdir(parents=True)
    assert project.derive_project(str(sub)) == "myrepo"
    assert project.derive_project(str(repo)) == "myrepo"


def test_derive_project_no_git_falls_back_to_leaf(tmp_path: Path):
    leaf = tmp_path / "Standalone-Project"
    leaf.mkdir()
    # No .git anywhere up to tmp_path. Falls back to leaf folder name (lowercased).
    assert project.derive_project(str(leaf)) == "standalone-project"


def test_derive_project_handles_nonexistent_path():
    # Must not raise; returns a slug or None.
    out = project.derive_project("/this/does/not/exist/myproj")
    assert out in ("myproj", None)


def test_derive_project_none_for_empty():
    assert project.derive_project(None) is None
    assert project.derive_project("") is None


def test_dominant_project_picks_majority(tmp_path: Path):
    a = tmp_path / "alpha"; a.mkdir(); (a / ".git").mkdir()
    b = tmp_path / "beta";  b.mkdir(); (b / ".git").mkdir()
    cwds = [str(a), str(a), str(b), str(a / "sub"), None]
    # alpha: 3, beta: 1
    (a / "sub").mkdir()
    assert project.dominant_project(cwds) == "alpha"


def test_dominant_project_empty():
    assert project.dominant_project([]) is None
    assert project.dominant_project([None, None]) is None


if __name__ == "__main__":
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
    print(f"\n{failed} failure(s)" if failed else "\nAll project tests passed.")
    sys.exit(1 if failed else 0)
