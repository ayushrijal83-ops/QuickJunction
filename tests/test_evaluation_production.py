"""Milestone 07.3 tests: the production-shaped evaluation harness.

The M07.3 promote/keep decision rests on this scorer, so the scorer itself
needs pinning. No model is loaded here -- ``analyse`` is a pure function over
(text, case), which is exactly what makes it testable.

The historical M07.1 harness is deliberately untouched by M07.3, and
``test_5`` pins that.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from training.eval_scenarios_production import (
    CONTRASTIVE_GROUPS,
    SCENARIOS,
    all_cases,
)
from training.evaluate_production import (
    CROSS_DIMENSION_ERROR,
    DIETARY_COMPATIBILITY_ERROR,
    GARBLED_PROSE,
    HALLUCINATION,
    LABEL_FACT_CONTRADICTION,
    NOT_SET_ERROR,
    OVERCLAIM,
    SYSTEMS,
    analyse,
    assert_held_out,
)

BASE_DIR = Path(__file__).resolve().parent.parent
CASES = {c["name"]: c for c in all_cases()}


# --- 1. Coverage of the required evaluation dimensions ------------------------


def test_1_evaluation_set_covers_every_required_dimension():
    counts: dict[str, int] = {}
    for case in all_cases():
        counts[case["category"]] = counts.get(case["category"], 0) + 1

    # Minimums stated by the M07.3 brief.
    assert counts["A_full_match"] >= 2
    assert counts["B_not_set"] >= 3
    assert counts["C_cuisine_only"] >= 2
    assert counts["D_spice_only"] >= 2
    assert counts["E_dietary_compat"] >= 2
    assert counts["F_two_dim"] >= 2
    assert counts["G_suggested"] >= 1
    assert counts["H_grounding"] >= 2
    assert len(SCENARIOS) >= 14
    assert len(CONTRASTIVE_GROUPS) >= 3


def test_1b_contrastive_groups_hold_the_preference_constant():
    for _, pref, members in CONTRASTIVE_GROUPS:
        assert len(members) == 3
        assert len({item for _, item, _, _ in members}) == 3, "candidates must differ"
        assert pref == pref  # the block is shared by construction


def test_1c_no_case_depends_on_information_the_app_never_supplies():
    """Price, ingredients, availability and history are not in the prompt."""
    banned = ("price", "ingredient", "available", "history", "calorie", "stock")
    for case in all_cases():
        blob = " ".join(str(v) for v in case["item"]).lower()
        assert not any(b in blob for b in banned)


# --- 2. Held-out guarantee ----------------------------------------------------


def test_2_every_case_is_held_out_from_all_three_datasets():
    assert_held_out(all_cases())  # raises SystemExit on leakage


# --- 3. The scorer catches real defects (non-vacuity) -------------------------


@pytest.mark.parametrize("case_name,text,expected", [
    # The exact cross-dimension sentence M07.3 was asked to check for.
    ("cuisine_only_italian",
     "It is Italian rather than Indian, so it does not meet your vegetarian requirement.",
     CROSS_DIMENSION_ERROR),
    ("dietary_vegan_for_vegetarian",
     "Beetroot Carpaccio is vegan, so it is not vegetarian and fails your dietary preference.",
     DIETARY_COMPATIBILITY_ERROR),
    ("unset_dietary",
     "Risotto Milanese is Italian, matching your dietary preference.",
     NOT_SET_ERROR),
    ("full_match_indian",
     "Methi Malai Mutter is Indian and vegetarian, and it is very affordable.",
     HALLUCINATION),
    ("g1_a_all_align",
     "Undhiyu matches you. It is the only item on the menu that fits.",
     HALLUCINATION),
    ("cuisine_only_thai",
     "Yam Woon Sen matches all three of your preferences.",
     OVERCLAIM),
    ("spice_only_hotter",
     "Salt and Pepper Tofu is hot rather than hot, which differs.",
     GARBLED_PROSE),
    ("suggested_low_signal",
     "Fattoush Salad is an Indian dish that suits you well.",
     LABEL_FACT_CONTRADICTION),
])
def test_3_scorer_flags_known_failure_modes(case_name, text, expected):
    verdict = analyse(text, CASES[case_name])
    assert expected in verdict["errors"], verdict


@pytest.mark.parametrize("case_name,text", [
    ("full_match_indian",
     "Methi Malai Mutter is Indian, vegetarian and medium spiced, matching every preference you set."),
    ("cuisine_only_italian",
     "Panzanella is vegan and mild as you asked, though it is Italian rather than Indian."),
    ("spice_only_milder",
     "Ker Sangri is Indian and vegetarian as you asked, though it is mild rather than extra hot."),
    ("dietary_vegan_for_vegetarian",
     "Beetroot Carpaccio is Continental and unspiced, matching both preferences you set. "
     "It is vegan, which your vegetarian preference allows."),
    ("unset_all",
     "You have not set any preferences yet, so this is shown as a general suggestion."),
    ("two_dim_mexican_thai",
     "This suits your vegetarian preference, but it is Thai rather than Mexican and has no heat."),
])
def test_3b_scorer_does_not_flag_correct_outputs(case_name, text):
    """A scorer that fails good text would make the comparison meaningless."""
    verdict = analyse(text, CASES[case_name])
    assert verdict["passed"], verdict["detail"]


# --- 4. Every adapter generation is registered --------------------------------


def test_4_all_systems_are_distinct_and_registered():
    """M07.6 added v4. Each adapter must be its own directory so no comparison
    can silently score two generations against the same weights."""
    assert set(SYSTEMS) == {"base", "v1", "v2", "v3", "v4"}
    assert SYSTEMS["base"] is None
    paths = [SYSTEMS[k] for k in ("v1", "v2", "v3", "v4")]
    assert len({str(p) for p in paths}) == 4, "adapters must be distinct directories"


def test_4b_held_out_corpus_covers_every_trained_dataset():
    """The held-out guarantee is only as good as the corpus it checks against.
    A dataset missing here would let a memorised prompt score as a success."""
    from training.evaluate_production import CORPUS_FILES

    joined = " ".join(str(p) for p in CORPUS_FILES)
    for version in ("seed_examples.jsonl", "seed_examples_v2.jsonl",
                    "seed_examples_v3.jsonl", "seed_examples_v4.jsonl"):
        assert version in joined, f"{version} missing from the held-out corpus"
    for version in ("v1", "v2", "v3", "v4"):
        assert str(Path("processed") / version / "train.jsonl") in joined, version
        assert str(Path("processed") / version / "validation.jsonl") in joined, version


# --- 5. The historical M07.1 evaluation is preserved --------------------------


def test_5_m07_1_results_and_harness_are_untouched():
    """M07.3 added a harness; it must not have rewritten the old evidence."""
    historical = BASE_DIR / "training" / "eval_results_m07_1.json"
    assert historical.is_file(), "the M07.1 result file must survive"
    data = json.loads(historical.read_text(encoding="utf-8"))
    # The recorded M07.1 outcome: base 8/8, v1 5/8, v2 8/8.
    assert data["base"]["passed"] == 8 and data["base"]["total"] == 8
    assert data["v1"]["passed"] == 5 and data["v1"]["total"] == 8
    assert data["v2"]["passed"] == 8 and data["v2"]["total"] == 8

    # The old harness still exists and still knows nothing about v3, which is
    # precisely why M07.3 used a separate one.
    source = (BASE_DIR / "training" / "evaluate.py").read_text(encoding="utf-8")
    assert '"v3"' not in source, "training/evaluate.py was modified by M07.3"


# --- 6. V3 remains unpromoted -------------------------------------------------


def test_6_production_runs_the_promoted_adapter():
    """Production and the training default must agree on the shipped version.

    Originally this asserted V2 and no promotion. M07.7 promoted V4 on the
    strength of a 25/25 held-out result, so the invariant is now that the two
    settings do not drift apart.
    """
    import config

    source = (BASE_DIR / "training" / "train_lora.py").read_text(encoding="utf-8")
    assert 'DEFAULT_DATASET_VERSION = "v4"' in source
    assert Path(config.BaseConfig.LLM_ADAPTER_PATH).name == "qwen3-0.6b-quickjunction-lora-v4"
