#!/usr/bin/env python3
"""PR (NOW-3): the nightly schedules the maintenance jobs that already exist.

consolidate() and archive() were complete, tested, and CLI-exposed, but nothing
invoked them — graph hygiene depended on a human remembering `njhook consolidate`.
This pins that dream/run_dream.cmd now runs them as separate stages (they are
mutually exclusive with distillation inside dream.py, so they need their own
invocations), each logged, behind a skip switch for ad-hoc distill-only runs.

Static (reads the .cmd); no Neo4j. The dry-run smoke that the chained commands
are *valid* invocations lives in test_consolidate / live verification.
"""
import os

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CMD = os.path.join(ROOT, "dream", "run_dream.cmd")


def _cmd() -> str:
    return open(CMD, encoding="utf-8").read()


def test_nightly_runs_distill_consolidate_archive_stages():
    src = _cmd()
    # stage 1 still distills...
    assert "dream.py --since" in src
    # ...and stages 2 + 3 now schedule the dedup + archival jobs.
    assert "--consolidate" in src
    assert "--archive" in src


def test_stages_are_individually_logged():
    src = _cmd()
    for marker in ("distill start", "distill end",
                   "consolidate start", "consolidate end",
                   "archive start", "archive end"):
        assert marker in src, f"missing nightly log marker: {marker!r}"
    # every stage records its exit code so a silent failure is visible in the log
    assert src.count("exit=%errorlevel%") >= 3


def test_maintenance_is_skippable_and_ordered_after_distill():
    src = _cmd()
    assert "DREAM_SKIP_MAINTENANCE" in src                 # ad-hoc distill-only escape hatch
    # ordering: distill precedes consolidate precedes archive
    assert src.index("--since") < src.index("--consolidate") < src.index("--archive")


def test_maintenance_knobs_have_conservative_defaults():
    src = _cmd()
    # exposed + overridable, with conservative defaults baked in
    assert "DREAM_CONSOLIDATE_THRESHOLD" in src and "0.92" in src
    assert "DREAM_CONSOLIDATE_ROUNDS" in src
    assert "DREAM_STALE_DAYS" in src
