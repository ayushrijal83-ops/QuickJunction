"""Milestone 07.1 tests: dataset V2 integrity, category coverage, the
contradiction/mismatch balance that M07 lacked, and the evaluation harness.

No model is loaded here. Adapter-dependent behaviour is asserted structurally
(paths, manifests, held-out guarantees); actual generation quality is measured
by `training/evaluate.py`, whose recorded results live in docs/AI.md.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_V1 = BASE_DIR / "data" / "raw" / "seed_examples.jsonl"
RAW_V2 = BASE_DIR / "data" / "raw" / "seed_examples_v2.jsonl"
V2_DIR = BASE_DIR / "data" / "processed" / "v2"
V1_DIR = BASE_DIR / "data" / "processed" / "v1"

REQUIRED_FIELDS = ("instruction", "input", "output")

EXPECTED_V2_CATEGORIES = {
    "strong_match",
    "partial_match",
    "weak_match",
    "cuisine_mismatch",
    "spice_mismatch",
    "dietary_mismatch",
    "ingredient_mismatch",
    "multiple_conflicts",
    "unavailable_items",
    "no_recommendations",
    "correct_explanation",
    "contradictory_candidate",
    "history_influence",
    "preference_over_history",
    "why_not_match",
}

# Categories whose whole purpose is teaching the model to say "this does not
# match". The M07 defect was that these barely existed.
MISMATCH_CATEGORIES = {
    "partial_match",
    "weak_match",
    "cuisine_mismatch",
    "spice_mismatch",
    "dietary_mismatch",
    "ingredient_mismatch",
    "multiple_conflicts",
    "contradictory_candidate",
    "why_not_match",
    "preference_over_history",
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def example_hash(example: dict) -> str:
    payload = {f: example[f] for f in REQUIRED_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# --- 1. Schema ----------------------------------------------------------------


def test_1_dataset_v2_schema_is_valid():
    for path in (RAW_V2, V2_DIR / "train.jsonl", V2_DIR / "validation.jsonl"):
        assert path.is_file(), f"missing {path}"
        rows = load_jsonl(path)
        assert rows, f"{path} is empty"
        for index, row in enumerate(rows):
            for field in REQUIRED_FIELDS:
                assert field in row, f"{path}:{index} missing {field!r}"
                assert isinstance(row[field], str) and row[field].strip(), f"{path}:{index} empty {field!r}"
            assert row["category"] in EXPECTED_V2_CATEGORIES, f"{path}:{index} unknown category {row['category']!r}"


def test_1b_dataset_v2_has_the_expected_size():
    assert len(load_jsonl(RAW_V2)) == 150
    assert len(load_jsonl(V2_DIR / "train.jsonl")) == 120
    assert len(load_jsonl(V2_DIR / "validation.jsonl")) == 30


# --- 2/3. Duplicates and split overlap ---------------------------------------


def test_2_no_duplicates_in_v2():
    rows = load_jsonl(RAW_V2)
    hashes = [example_hash(r) for r in rows]
    duplicates = [h for h, n in Counter(hashes).items() if n > 1]
    assert not duplicates, f"{len(duplicates)} duplicate example(s) in seed_examples_v2.jsonl"


def test_3_no_train_validation_overlap_in_v2():
    train = {example_hash(r) for r in load_jsonl(V2_DIR / "train.jsonl")}
    validation = {example_hash(r) for r in load_jsonl(V2_DIR / "validation.jsonl")}
    assert not (train & validation), "example leaked across the v2 split"
    # The split partitions the seed file exactly.
    assert train | validation == {example_hash(r) for r in load_jsonl(RAW_V2)}


def test_3b_builder_rejects_a_duplicate(tmp_path):
    """The duplicate guard must actually fire, not just be present."""
    rows = load_jsonl(RAW_V2)
    seed = tmp_path / "dupes.jsonl"
    seed.write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in (rows[0], rows[0])) + "\n",
        encoding="utf-8",
    )
    script = BASE_DIR / "scripts" / "build_dataset.py"
    source = script.read_text(encoding="utf-8").replace(
        'RAW_DIR / "seed_examples_v2.jsonl"', f"Path({str(seed)!r})"
    )
    patched = tmp_path / "build_patched.py"
    patched.write_text(source, encoding="utf-8")
    result = subprocess.run([sys.executable, str(patched), "--version", "v2"],
                            capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode != 0
    assert "duplicate" in (result.stdout + result.stderr).lower()


# --- 4. Reproducibility -------------------------------------------------------


def test_4_v2_split_is_reproducible():
    before = ((V2_DIR / "train.jsonl").read_bytes(), (V2_DIR / "validation.jsonl").read_bytes())
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "build_dataset.py"), "--version", "v2"],
        capture_output=True, text=True, cwd=str(BASE_DIR),
    )
    assert result.returncode == 0, result.stderr
    after = ((V2_DIR / "train.jsonl").read_bytes(), (V2_DIR / "validation.jsonl").read_bytes())
    assert before == after


def test_4b_manifest_records_identity_and_matches_the_files():
    manifest = json.loads((V2_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_version"] == "v2"
    assert manifest["totals"] == {"raw": 150, "train": 120, "validation": 30, "categories": 15}
    for key, path in (
        ("raw", RAW_V2),
        ("train", V2_DIR / "train.jsonl"),
        ("validation", V2_DIR / "validation.jsonl"),
    ):
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert manifest["sha256"][key] == expected, f"stale sha256 for {key}"
    for field in ("curation_method", "duplicate_policy", "split_method"):
        assert manifest[field].strip()


def test_4c_v1_dataset_is_untouched_by_m07_1():
    """M07's dataset and its processed splits must be preserved exactly."""
    assert len(load_jsonl(RAW_V1)) == 60
    legacy_train = BASE_DIR / "data" / "processed" / "train.jsonl"
    legacy_validation = BASE_DIR / "data" / "processed" / "validation.jsonl"
    assert len(load_jsonl(legacy_train)) == 50
    assert len(load_jsonl(legacy_validation)) == 10
    # v1 rebuilt into its own directory must equal the legacy files.
    assert (V1_DIR / "train.jsonl").read_bytes() == legacy_train.read_bytes()
    assert (V1_DIR / "validation.jsonl").read_bytes() == legacy_validation.read_bytes()


# --- 5. Category coverage -----------------------------------------------------


def test_5_all_fifteen_categories_present_on_both_sides():
    assert {r["category"] for r in load_jsonl(RAW_V2)} == EXPECTED_V2_CATEGORIES
    assert {r["category"] for r in load_jsonl(V2_DIR / "train.jsonl")} == EXPECTED_V2_CATEGORIES
    assert {r["category"] for r in load_jsonl(V2_DIR / "validation.jsonl")} == EXPECTED_V2_CATEGORIES


def test_5b_every_category_has_a_meaningful_number_of_examples():
    counts = Counter(r["category"] for r in load_jsonl(RAW_V2))
    for category in EXPECTED_V2_CATEGORIES:
        assert counts[category] >= 8, f"{category} has only {counts[category]} examples"


# --- 6. Contradiction / mismatch balance -- the M07 defect --------------------


def test_6_mismatch_examples_dominate_the_dataset():
    """The M07 failure was a dataset dominated by strong positive matches.
    V2 must be weighted the other way."""
    rows = load_jsonl(RAW_V2)
    counts = Counter(r["category"] for r in rows)
    mismatch = sum(counts[c] for c in MISMATCH_CATEGORIES)
    strong = counts["strong_match"]

    assert mismatch / len(rows) >= 0.5, f"only {mismatch}/{len(rows)} mismatch-oriented examples"
    assert strong <= len(rows) * 0.15, f"strong_match is {strong}/{len(rows)} -- too dominant"
    assert mismatch > strong * 3


def test_6b_negative_examples_actually_say_no():
    """A mismatch example whose output never expresses disagreement would be
    training the wrong behaviour."""
    negative_markers = (
        "not ", "rather than", "does not", "no spice", "fails", "outside",
        "milder", "stronger", "shortfall", "falls short", "no longer",
        "unavailable", "nothing", "incorrect", "wrong", "overstates", "excluded",
        # Contrastive and comparative phrasings are equally valid ways of
        # saying "this does not match" -- e.g. "…matches two, but it is hotter".
        " but ", "however", "neither", "nor ", "above", "below", "only ",
        "though", "short of", "no heat", "barely", "loosely", "poor fit",
    )
    rows = [r for r in load_jsonl(RAW_V2) if r["category"] in MISMATCH_CATEGORIES]
    negative = [r for r in rows if any(m in r["output"].lower() for m in negative_markers)]
    ratio = len(negative) / len(rows)
    assert ratio >= 0.9, (
        f"only {len(negative)}/{len(rows)} mismatch-category examples express disagreement"
    )

    # The handful that do not are deliberate: `contradictory_candidate` also
    # contains claims that are genuinely *correct*, so the model is not taught
    # to reject every proposal reflexively -- which would simply be the
    # opposite failure mode to M07's.
    affirming = [r for r in rows if r not in negative]
    assert all(r["category"] == "contradictory_candidate" for r in affirming), (
        f"unexpected non-negative examples: {[r['category'] for r in affirming]}"
    )


def test_6b2_contradiction_category_teaches_both_verdicts():
    rows = [r for r in load_jsonl(RAW_V2) if r["category"] == "contradictory_candidate"]
    confirms = [r for r in rows if "claim is correct" in r["output"].lower()]
    rejects = [r for r in rows if "claim is correct" not in r["output"].lower()]
    assert confirms, "no example confirms a correct claim -- model would learn to always reject"
    assert rejects, "no example rejects a false claim -- the whole point of the category"
    # Rejections should still dominate, since that is the defect being fixed.
    assert len(rejects) > len(confirms)


def test_6c_no_example_claims_a_dietary_violation_is_suitable():
    """Hard safety property of the dataset itself: no output may present a
    non-vegetarian item as suiting a vegetarian/vegan customer."""
    # Affirmative suitability claims. Each is checked *without* a preceding
    # negation, because "it is not suitable for you" is the correct output and
    # contains the same substring as the incorrect one.
    affirmative = re.compile(
        r"(?<!not )(?<!never )(?<!no )\b(is suitable for you|meets your dietary requirement"
        r"|is a good vegetarian|suits your vegetarian|works for your vegetarian)\b"
    )
    for row in load_jsonl(RAW_V2):
        combined = (row["input"] + " " + row["output"]).lower()
        if "non_vegetarian" not in combined:
            continue
        if not any(k in combined for k in ("dietary: vegetarian", "dietary preference: vegetarian",
                                           "dietary: vegan", "dietary preference: vegan")):
            continue
        match = affirmative.search(row["output"].lower())
        assert match is None, (
            f"example presents a meat dish as suitable: {row['output'][:90]!r} "
            f"(matched {match.group(0)!r})" if match else ""
        )


def test_6d_contradictory_candidates_are_corrected_not_echoed():
    rows = [r for r in load_jsonl(RAW_V2) if r["category"] == "contradictory_candidate"]
    assert rows
    # Any phrasing that adjudicates the proposed claim rather than repeating it.
    corrective = ("not accurate", "is wrong", "incorrect", "not right", "overstates",
                  "that is not", "claim is correct", "but the last is not",
                  "is not", "wrong", "correct")
    for row in rows:
        assert any(c in row["output"].lower() for c in corrective), \
            f"contradictory_candidate output does not adjudicate the claim: {row['output'][:80]}"


# --- 7/8. Evaluation harness --------------------------------------------------


def test_7_evaluation_scenarios_are_held_out():
    """Evaluation prompts must not appear in either training set."""
    sys.path.insert(0, str(BASE_DIR / "training"))
    try:
        import evaluate  # noqa: PLC0415
    finally:
        sys.path.pop(0)

    training_inputs = set()
    for path in (RAW_V1, RAW_V2):
        for row in load_jsonl(path):
            training_inputs.add(row["input"].strip())

    assert len(evaluate.SCENARIOS) >= 8
    for item in evaluate.SCENARIOS:
        assert item["input"].strip() not in training_inputs, f"{item['name']} leaked into training data"
    # The module's own guard must agree.
    evaluate.assert_held_out()


def test_7b_evaluation_covers_every_required_dimension():
    sys.path.insert(0, str(BASE_DIR / "training"))
    try:
        import evaluate
    finally:
        sys.path.pop(0)

    names = {s["name"] for s in evaluate.SCENARIOS}
    assert {
        "strong_match", "partial_match", "cuisine_mismatch", "spice_mismatch",
        "dietary_mismatch", "unavailable_item", "no_recommendation",
        "preference_over_history",
    } <= names


def test_7c_scorer_is_deterministic_and_catches_the_m07_failure():
    sys.path.insert(0, str(BASE_DIR / "training"))
    try:
        import evaluate
    finally:
        sys.path.pop(0)

    cuisine = next(s for s in evaluate.SCENARIOS if s["name"] == "cuisine_mismatch")

    # The literal sentence M07's adapter produced in browser testing.
    m07_output = ("Margherita Pizza is Italian, which is what you prefer, and it is "
                  "vegetarian, which is what you prefer.")
    bad = evaluate.score(m07_output, cuisine)
    assert not bad["passed"], "scorer fails to catch the known M07 sycophancy"
    assert bad["must_not_violations"]

    good = evaluate.score(
        "Margherita Pizza is Italian, not Indian, and it has no spice heat, so it does "
        "not match your cuisine or spice preference.", cuisine)
    assert good["passed"]
    assert good["should_hits"]

    # Same input twice -> same verdict.
    assert evaluate.score(m07_output, cuisine) == bad


def test_8_system_registry_points_at_separate_adapters():
    sys.path.insert(0, str(BASE_DIR / "training"))
    try:
        import evaluate
    finally:
        sys.path.pop(0)

    assert evaluate.SYSTEMS["base"] is None
    assert evaluate.SYSTEMS["v1"].name == "qwen3-0.6b-quickjunction-lora"
    assert evaluate.SYSTEMS["v2"].name == "qwen3-0.6b-quickjunction-lora-v2"
    # V2 must never be written over V1.
    assert evaluate.SYSTEMS["v1"] != evaluate.SYSTEMS["v2"]


# --- 9/10. Adapter artefacts and regression safety ----------------------------


def test_9_m07_adapter_is_preserved():
    v1_adapter = BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora"
    if not v1_adapter.is_dir():
        pytest.skip("M07 adapter not present in this checkout (models/ is git-ignored)")
    assert (v1_adapter / "adapter_model.safetensors").is_file()
    metrics = json.loads((v1_adapter / "training_metrics.json").read_text(encoding="utf-8"))
    # M07's recorded run: 50 train examples, 39 steps.
    assert metrics["train_examples"] == 50
    assert metrics["steps"] == 39


def test_10_training_script_refuses_to_overwrite_an_existing_adapter():
    source = (BASE_DIR / "training" / "train_lora.py").read_text(encoding="utf-8")
    assert "Refusing to overwrite existing adapter" in source
    assert "qwen3-0.6b-quickjunction-lora-v2" in source
    # Still offline, still no tracking service.
    assert "HF_HUB_OFFLINE" in source and "local_files_only=True" in source
    assert "report_to=[]" in source


def test_10b_evaluation_is_offline_and_keyless():
    source = (BASE_DIR / "training" / "evaluate.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in ("openai", "anthropic", "gemini", "ollama", "api_key",
                      "bearer", "http://", "https://", "requests.post", "urllib.request"):
        assert forbidden not in lowered, f"evaluate.py references {forbidden!r}"
    assert "local_files_only=True" in source
    assert "do_sample=False" in source  # greedy -> reproducible
