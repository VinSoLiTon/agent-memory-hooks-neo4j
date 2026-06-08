#!/usr/bin/env python3
"""PR (NOW-2): anchored salience rubric + deterministic kind-prior floor.

Importance is a two-place ranking signal (the importance_factor multiplier AND
value-density budget order), but on the 12B local model it collapsed to a
degenerate 7-8 / omitted distribution. Two defences, both pinned here:
  - the schema-constrained local path REQUIRES importance (kills "omitted →
    flat default 5"); the anthropic json_object path keeps it optional;
  - when a value is still absent/garbled, _coerce_importance falls back to a
    per-kind prior (durable kinds high, ephemera low) instead of None.

Pure — no Neo4j.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "hooks"))
sys.path.insert(0, os.path.join(ROOT, "dream"))

import dream as dream_mod   # noqa: E402
import prompts              # noqa: E402
import memory_types         # noqa: E402


# --- model value always wins (when valid) -----------------------------------

def test_valid_model_importance_wins_over_prior():
    # a recognized value is honored (clamped) regardless of kind
    assert dream_mod._coerce_importance(9, "artifact") == 9     # not the artifact prior (3)
    assert dream_mod._coerce_importance(1, "constraint") == 1   # not the constraint prior (8)
    assert dream_mod._coerce_importance(99, "fact") == 10       # clamped to 10
    assert dream_mod._coerce_importance(0, "fact") == 1         # clamped to 1


# --- kind-prior fallback when the model omits / garbles importance ----------

def test_kind_prior_fills_in_when_importance_missing():
    assert dream_mod._coerce_importance(None, "constraint") == 8
    assert dream_mod._coerce_importance(None, "preference") == 7
    assert dream_mod._coerce_importance(None, "artifact") == 3
    assert dream_mod._coerce_importance("garbage", "decision") == 7   # unparseable → prior
    # durable kinds must out-rank ephemeral kinds
    assert dream_mod._coerce_importance(None, "constraint") > dream_mod._coerce_importance(None, "observation")


def test_legacy_kind_label_normalized_before_prior_lookup():
    # legacy bucket label 'project' → 'projectrule' (prior 7) via normalize_kind
    assert dream_mod._coerce_importance(None, "project") == dream_mod._coerce_importance(None, "projectrule")


def test_no_kind_and_no_value_stays_none_backcompat():
    # the old contract: with neither a value nor a kind, importance is unset
    assert dream_mod._coerce_importance(None) is None
    assert dream_mod._coerce_importance("nope") is None


def test_every_semantic_kind_has_a_prior():
    # no normalized kind may fall through to the unrecognized-kind floor by accident
    for kind in memory_types.MEMORY_KINDS:
        assert kind in dream_mod._KIND_IMPORTANCE_PRIOR, f"missing importance prior for kind {kind}"
        assert 1 <= dream_mod._KIND_IMPORTANCE_PRIOR[kind] <= 10


# --- schema: importance REQUIRED on the constrained-decode path -------------

def test_schema_requires_importance():
    item = prompts.DREAM_JSON_SCHEMA["properties"]["memories"]["items"]
    assert "importance" in item["required"]          # local constrained path must emit it
    assert item["properties"]["importance"] == {"type": "integer", "minimum": 1, "maximum": 10}


# --- prompt: anchored rubric present, not the old "omit if unsure" line ------

def test_local_prompt_has_anchored_rubric():
    p = prompts.system_prompt_for("llamacpp")
    assert "9-10" in p and "1-2" in p                # rating bands present
    assert "SPREAD" in p or "Spread" in p            # the anti-degenerate instruction
    assert "Omit if unsure" not in p                 # the old vague line is gone
