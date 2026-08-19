"""Milestone 07.2 tests: dataset V3 integrity and the production-shape contract.

V3 exists because of a defect the M07.2 design audit found in V2: only 13% of
V2's examples used the prompt shape `app/services/local_llm.py::build_prompt`
actually emits, and only 7% used its exact instruction string. The model was
therefore fine-tuned largely on prompts production never sends. Every test
below that asserts "production shape" is guarding that finding.

No model is loaded and no training is performed here. These are dataset
properties only; generation quality is a separate, later concern.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_V1 = BASE_DIR / "data" / "raw" / "seed_examples.jsonl"
RAW_V2 = BASE_DIR / "data" / "raw" / "seed_examples_v2.jsonl"
RAW_V3 = BASE_DIR / "data" / "raw" / "seed_examples_v3.jsonl"
V3_DIR = BASE_DIR / "data" / "processed" / "v3"

REQUIRED_FIELDS = ("instruction", "input", "output")

INSTRUCTION = (
    "Explain in one or two sentences why this menu item was recommended. "
    "Use only the facts provided."
)

EXPECTED_COUNTS = {
    "full_match": 26,
    "match_with_unset": 20,
    "cuisine_differs_only": 16,
    "spice_differs_only": 16,
    "dietary_compatible_not_identical": 12,
    "cuisine_and_spice_differ": 20,
    "single_attribute_match": 16,
    "low_signal_suggested": 10,
    "no_invented_attributes": 12,
    "no_invented_context": 12,
    "unset_stays_unset": 10,
    "label_fact_consistency": 16,
    "no_cross_dimension_errors": 14,
}

CUISINES = {"indian", "chinese", "italian", "continental", "mexican", "thai",
            "multi_cuisine", "other"}
DIETARY = {"vegetarian", "vegan", "eggetarian", "non_vegetarian"}
SPICE = {"none", "mild", "medium", "hot", "extra_hot"}
LABELS = {"Strong match", "Good match", "Fair match", "Suggested"}
UNSET = "not set"

# Mirrors app/services/recommendations.py::_DIETARY_COMPATIBILITY. An item the
# hard filter would have removed can never reach the explainer, so a training
# example containing one teaches an unreachable situation.
DIETARY_COMPATIBILITY = {
    "vegan": {"vegan"},
    "vegetarian": {"vegan", "vegetarian"},
    "eggetarian": {"eggetarian", "vegan", "vegetarian"},
    "non_vegetarian": {"vegetarian", "vegan", "eggetarian", "non_vegetarian"},
}

# The exact middle block of build_prompt(): preference block, blank line, item
# block. Anchored at both ends so extra or missing lines fail.
PROMPT_RE = re.compile(
    r"^Customer preference:\n"
    r"cuisine: (?P<pref_cuisine>[a-z_ ]+)\n"
    r"dietary: (?P<pref_dietary>[a-z_ ]+)\n"
    r"spice: (?P<pref_spice>[a-z_ ]+)\n"
    r"\n"
    r"Recommended item:\n"
    r"name: (?P<name>[^\n]+)\n"
    r"cuisine: (?P<cuisine>[a-z_]+)\n"
    r"dietary: (?P<dietary>[a-z_]+)\n"
    r"spice: (?P<spice>[a-z_]+)\n"
    r"match: (?P<label>[A-Za-z ]+)$"
)


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def example_hash(example: dict) -> str:
    payload = {f: example[f] for f in REQUIRED_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parsed_rows(path: Path = RAW_V3):
    rows = load_jsonl(path)
    out = []
    for index, row in enumerate(rows):
        match = PROMPT_RE.match(row["input"])
        assert match, f"{path.name}:{index} does not match the production prompt shape"
        out.append((index, row, match))
    return out


# --- 1. Schema and size -------------------------------------------------------


def test_1_v3_schema_is_valid():
    for path in (RAW_V3, V3_DIR / "train.jsonl", V3_DIR / "validation.jsonl"):
        assert path.is_file(), f"missing {path}"
        rows = load_jsonl(path)
        assert rows, f"{path} is empty"
        for index, row in enumerate(rows):
            for field in REQUIRED_FIELDS:
                assert field in row, f"{path}:{index} missing {field!r}"
                assert isinstance(row[field], str) and row[field].strip()
            assert row["category"] in EXPECTED_COUNTS, f"{path}:{index} unknown category"
            # group_id is the only auxiliary field the builder is allowed to
            # carry through; anything else is an authoring mistake.
            assert set(row) <= set(REQUIRED_FIELDS) | {"category", "group_id"}


def test_1b_v3_has_the_expected_size():
    assert len(load_jsonl(RAW_V3)) == 200
    assert len(load_jsonl(V3_DIR / "train.jsonl")) == 161
    assert len(load_jsonl(V3_DIR / "validation.jsonl")) == 39


def test_1c_category_counts_are_exact():
    assert Counter(r["category"] for r in load_jsonl(RAW_V3)) == Counter(EXPECTED_COUNTS)


# --- 2. The production contract -- the reason V3 exists ------------------------


def test_2_every_example_uses_the_production_prompt_shape():
    """The M07.2 finding: V2 trained mostly on prompts production never sends."""
    assert len(parsed_rows()) == 200


def test_2b_every_example_uses_the_production_instruction_string():
    assert {r["instruction"] for r in load_jsonl(RAW_V3)} == {INSTRUCTION}


def test_2c_prompt_shape_matches_build_prompt_exactly():
    """Render a prompt through the real production function and require the
    dataset's input to be its middle block verbatim. This is what stops the
    dataset drifting away from the app."""
    from app.services.local_llm import ExplanationRequest, build_prompt

    prompt = build_prompt(ExplanationRequest(
        item_name="Paneer Tikka", item_cuisine="indian", item_dietary="vegetarian",
        item_spice="hot", preferred_cuisine="indian", preferred_dietary="vegetarian",
        preferred_spice="hot", match_label="Strong match",
    ))
    body = prompt.split("\n\n", 1)[1].rsplit("\n\nExplanation:", 1)[0]

    _, row, _ = parsed_rows()[0]
    assert PROMPT_RE.match(body), "build_prompt no longer emits the shape V3 was authored against"
    # The dataset's instruction is the production prompt's first block.
    assert prompt.split("\n\n", 1)[0] == row["instruction"]


def test_2d_vocabulary_is_the_production_enum_vocabulary():
    for index, _, m in parsed_rows():
        assert m["pref_cuisine"] == UNSET or m["pref_cuisine"] in CUISINES, index
        assert m["pref_dietary"] == UNSET or m["pref_dietary"] in DIETARY, index
        assert m["pref_spice"] == UNSET or m["pref_spice"] in SPICE, index
        assert m["cuisine"] in CUISINES, index
        assert m["dietary"] in DIETARY, index
        assert m["spice"] in SPICE, index
        assert m["label"] in LABELS, index


def test_2e_no_example_survives_only_by_ignoring_the_dietary_filter():
    """Every candidate must be one the dietary hard filter would have kept."""
    for index, _, m in parsed_rows():
        if m["pref_dietary"] == UNSET:
            continue
        assert m["dietary"] in DIETARY_COMPATIBILITY[m["pref_dietary"]], (
            f"{index}: {m['dietary']} would have been filtered out for a "
            f"{m['pref_dietary']} customer and can never reach the explainer"
        )


def test_2f_outputs_survive_production_sanitisation_unchanged():
    """`_sanitise()` strips control characters and markup from free text and
    caps it. An output that would be altered is not what the model should learn."""
    forbidden = set('\r\n\t#`*<>{}[]|')
    for index, row, _ in parsed_rows():
        assert not (forbidden & set(row["output"])), f"{index}: sanitisable characters in output"


# --- 3. Honesty properties ----------------------------------------------------


def test_3_outputs_are_one_or_two_sentences():
    """The instruction promises one or two sentences; the data must honour it."""
    for index, row, _ in parsed_rows():
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", row["output"].strip()) if s]
        assert 1 <= len(sentences) <= 2, f"{index}: {len(sentences)} sentences"


def test_3b_no_invented_attributes():
    """Only name, cuisine, dietary, spice and the label exist in the prompt.
    ExplanationRequest has no price, ingredient or availability field, so any
    mention of one is fabricated."""
    forbidden = re.compile(
        r"\b(price|priced|cost|costs|rupee|rupees|calorie|calories|ingredient|"
        r"ingredients|contains|made with|gram|grams|portion|serving size|"
        r"available|availability|in stock|sold out|healthy|nutritious|protein)\b",
        re.I)
    for index, row, _ in parsed_rows():
        found = forbidden.search(row["output"])
        assert not found, f"{index}: invented attribute {found.group(0)!r}"


def test_3c_no_invented_context():
    """The model is given no order history, popularity data or reviews."""
    forbidden = re.compile(
        r"\b(you ordered|previously|last time|order history|your history|"
        r"popular|best.?sell|customer favourite|customer favorite|highly rated|"
        r"reviews?|our chef|our restaurant|trending|signature dish|house special)\b",
        re.I)
    for index, row, _ in parsed_rows():
        found = forbidden.search(row["output"])
        assert not found, f"{index}: invented context {found.group(0)!r}"


def test_3d_unset_preferences_are_never_claimed_as_matched():
    """"not set" means the customer stated nothing. Saying it matched is the
    sycophancy failure mode in its purest form."""
    patterns = {
        "pref_cuisine": r"matches your cuisine|your cuisine preference is met",
        "pref_dietary": r"matches your dietary|your dietary preference is met",
        "pref_spice": r"matches your spice|your spice preference is met",
    }
    for index, row, m in parsed_rows():
        lowered = row["output"].lower()
        for field, pattern in patterns.items():
            if m[field] == UNSET:
                assert not re.search(pattern, lowered), (
                    f"{index}: claims a match on an unset {field}"
                )


def test_3e_all_three_is_claimed_only_when_literally_true():
    total_claim = re.compile(
        r"all three|every preference|all of your (?:three )?preferences|"
        r"each (?:of your |of the )?(?:three )?preferences|each preference|"
        r"on all three|in full", re.I)
    for index, row, m in parsed_rows():
        if not total_claim.search(row["output"]):
            continue
        assert (m["pref_cuisine"] != UNSET and m["pref_dietary"] != UNSET
                and m["pref_spice"] != UNSET), f"{index}: claims all three but one is unset"
        assert m["pref_cuisine"] == m["cuisine"], f"{index}: cuisine does not actually match"
        assert m["pref_spice"] == m["spice"], f"{index}: spice does not actually match"
        assert m["pref_dietary"] == m["dietary"], f"{index}: dietary does not actually match"


def test_3f_suggested_outputs_never_assert_a_match():
    """"Suggested" is emitted at similarity 0.0 -- nothing lined up."""
    for index, row, m in parsed_rows():
        if m["label"] != "Suggested":
            continue
        lowered = row["output"].lower()
        if not re.search(r"\bmatch(es|ing|ed)?\b", lowered):
            continue
        # A match word is only acceptable inside an explicit denial -- "matches
        # none of your preferences", "rather than a match", "does not match".
        denial = re.search(
            r"none|neither|nothing|not set|no preferences|"
            r"rather than a match|not a match|no match|does not match",
            lowered)
        assert denial, f"{index}: a Suggested item is described as matching something"


def test_3g_no_cross_dimension_confusion():
    """A cuisine must never be described as a spice level, and vice versa."""
    for index, row, _ in parsed_rows():
        lowered = row["output"].lower()
        for cuisine in CUISINES - {"other", "multi_cuisine"}:
            assert not re.search(rf"spice level is {cuisine}\b|{cuisine} spice level", lowered), \
                f"{index}: cuisine used as a spice level"
        for spice in ("mild", "medium", "hot", "extra hot"):
            assert not re.search(rf"\b{spice} cuisine\b|cuisine is {spice}\b", lowered), \
                f"{index}: spice used as a cuisine"
        for diet in DIETARY:
            word = diet.replace("_", "-")
            assert not re.search(rf"cuisine is {word}\b|spice level is {word}\b", lowered), \
                f"{index}: dietary type used as another dimension"


# --- 4. Contrastive groups ----------------------------------------------------


def test_4_twenty_contrastive_groups_of_three():
    groups = defaultdict(list)
    for row in load_jsonl(RAW_V3):
        if "group_id" in row:
            groups[row["group_id"]].append(row)
    assert len(groups) == 20
    assert {len(v) for v in groups.values()} == {3}


def test_4b_each_group_holds_the_preference_fixed_and_varies_the_item():
    """That is the entire point of a minimal pair: one variable at a time."""
    groups = defaultdict(list)
    for row in load_jsonl(RAW_V3):
        if "group_id" in row:
            groups[row["group_id"]].append(row)
    for group_id, rows in groups.items():
        preferences = {r["input"].split("\n\nRecommended item:")[0] for r in rows}
        items = {r["input"].split("\n\nRecommended item:")[1] for r in rows}
        assert len(preferences) == 1, f"{group_id}: preference block differs across the group"
        assert len(items) == 3, f"{group_id}: items are not distinct"


def test_4c_no_group_is_split_across_train_and_validation():
    """Training on two members while holding out the third leaks the contrast."""
    train_groups = {r["group_id"] for r in load_jsonl(V3_DIR / "train.jsonl") if "group_id" in r}
    validation_groups = {r["group_id"] for r in load_jsonl(V3_DIR / "validation.jsonl") if "group_id" in r}
    assert not (train_groups & validation_groups), "a contrastive group straddles the split"
    assert len(train_groups) == 20 and not validation_groups


def test_4d_builder_refuses_to_write_a_straddling_group(tmp_path):
    """The guard must actually fire. Constructed so that a category holds only
    grouped examples, forcing one into validation."""
    rows = [r for r in load_jsonl(RAW_V3) if r.get("group_id") == "contrastive_001"]
    assert len(rows) == 3
    # Collapse the group into a single category with nothing ungrouped to
    # hold out, so the group must be split.
    forced = [dict(r, category="forced") for r in rows]
    forced += [dict(r, category="filler", input=r["input"] + f"\nfiller: {i}")
               for i, r in enumerate(load_jsonl(RAW_V3)[:5])]
    seed = tmp_path / "straddle.jsonl"
    seed.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in forced) + "\n",
                    encoding="utf-8")

    script = BASE_DIR / "scripts" / "build_dataset.py"
    source = script.read_text(encoding="utf-8").replace(
        'RAW_DIR / "seed_examples_v3.jsonl"', f"Path({str(seed)!r})"
    ).replace('"validation_per_category": 3', '"validation_per_category": 2')
    patched = tmp_path / "build_patched.py"
    patched.write_text(source, encoding="utf-8")

    result = subprocess.run([sys.executable, str(patched), "--version", "v3"],
                            capture_output=True, text=True, cwd=str(tmp_path))
    assert result.returncode != 0
    assert "straddle" in (result.stdout + result.stderr).lower()


# --- 5. Split integrity and reproducibility -----------------------------------


def test_5_no_duplicates_within_v3():
    hashes = [example_hash(r) for r in load_jsonl(RAW_V3)]
    duplicates = [h for h, n in Counter(hashes).items() if n > 1]
    assert not duplicates, f"{len(duplicates)} duplicate example(s) in seed_examples_v3.jsonl"


def test_5b_split_partitions_the_seed_file_exactly():
    train = {example_hash(r) for r in load_jsonl(V3_DIR / "train.jsonl")}
    validation = {example_hash(r) for r in load_jsonl(V3_DIR / "validation.jsonl")}
    assert not (train & validation)
    assert train | validation == {example_hash(r) for r in load_jsonl(RAW_V3)}


def test_5c_every_category_is_represented_in_validation():
    counts = Counter(r["category"] for r in load_jsonl(V3_DIR / "validation.jsonl"))
    assert set(counts) == set(EXPECTED_COUNTS)
    assert set(counts.values()) == {3}


def test_5d_v3_split_is_reproducible():
    paths = (V3_DIR / "train.jsonl", V3_DIR / "validation.jsonl", V3_DIR / "manifest.json")
    before = tuple(p.read_bytes() for p in paths)
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "build_dataset.py"), "--version", "v3"],
        capture_output=True, text=True, cwd=str(BASE_DIR),
    )
    assert result.returncode == 0, result.stderr
    assert tuple(p.read_bytes() for p in paths) == before


def test_5e_manifest_records_identity_and_matches_the_files():
    manifest = json.loads((V3_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_version"] == "v3"
    assert manifest["totals"] == {"raw": 200, "train": 161, "validation": 39, "categories": 13}
    assert manifest["validation_per_category"] == 3
    assert manifest["contrastive_groups"] == {"count": 20, "all_in_train": True}
    for key, path in (("raw", RAW_V3), ("train", V3_DIR / "train.jsonl"),
                      ("validation", V3_DIR / "validation.jsonl")):
        assert manifest["sha256"][key] == hashlib.sha256(path.read_bytes()).hexdigest(), \
            f"stale sha256 for {key}"


# --- 6. Isolation from V1 and V2 ----------------------------------------------


def test_6_zero_content_overlap_with_v1_and_v2():
    v3 = {example_hash(r) for r in load_jsonl(RAW_V3)}
    for name, path in (("v1", RAW_V1), ("v2", RAW_V2)):
        overlap = v3 & {example_hash(r) for r in load_jsonl(path)}
        assert not overlap, f"{len(overlap)} example(s) shared with {name}"


def test_6b_v1_and_v2_seed_files_are_untouched():
    """V3 must not have edited an earlier dataset."""
    assert len(load_jsonl(RAW_V1)) == 60
    assert len(load_jsonl(RAW_V2)) == 150
    # V1 and V2 carry no group_id: the builder change V3 needed must not have
    # leaked into them, which is what keeps their splits byte-identical.
    for path in (RAW_V1, RAW_V2):
        assert not any("group_id" in row for row in load_jsonl(path))


def test_6c_v3_did_not_disturb_the_v1_or_v2_splits():
    v1 = BASE_DIR / "data" / "processed" / "v1"
    v2 = BASE_DIR / "data" / "processed" / "v2"
    assert len(load_jsonl(v1 / "train.jsonl")) == 50
    assert len(load_jsonl(v1 / "validation.jsonl")) == 10
    assert len(load_jsonl(v2 / "train.jsonl")) == 120
    assert len(load_jsonl(v2 / "validation.jsonl")) == 30
    # The legacy M07 paths still mirror v1 exactly.
    assert (v1 / "train.jsonl").read_bytes() == \
        (BASE_DIR / "data" / "processed" / "train.jsonl").read_bytes()


# --- 7. V3 is trained but NOT promoted ----------------------------------------
#
# M07.2 authored the dataset only, and this test originally asserted that *no*
# V3 adapter existed -- a guard against training happening unauthorised. The
# V3 training milestone then authorised exactly that, so the adapter now
# exists and the original assertion expired by design.
#
# The guard is re-pointed rather than removed, because the boundary that still
# matters has simply moved one step downstream: V3 is trained, but it must not
# become the default until it has been evaluated and explicitly approved.


def test_7_v3_is_trained_but_never_became_production():
    """V3 exists as an artefact and was never promoted.

    M07.3 measured V3 as better than V2 on every axis except unset-preference
    handling, and it was deliberately not shipped on that basis. M07.7 then
    promoted **V4**, not V3, so V3 must still not be the adapter the
    application loads.
    """
    import config

    adapter = Path(config.BaseConfig.LLM_ADAPTER_PATH)
    assert adapter.name != "qwen3-0.6b-quickjunction-lora-v3", \
        "V3 became the production adapter; it was never approved for that"


def test_7b_v3_adapter_artefact_is_well_formed():
    """If the V3 adapter is present it must be a real, complete artefact.

    Skipped where models/ is absent, since it is git-ignored and not every
    checkout has it.
    """
    v3 = BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora-v3"
    if not v3.is_dir():
        pytest.skip("V3 adapter not present in this checkout (models/ is git-ignored)")

    assert (v3 / "adapter_model.safetensors").is_file()
    assert (v3 / "adapter_config.json").is_file()

    metrics = json.loads((v3 / "training_metrics.json").read_text(encoding="utf-8"))
    # Trained on V3, at the approved hyperparameters, on the real split.
    assert metrics["dataset_version"] == "v3"
    assert metrics["train_examples"] == 161
    assert metrics["validation_examples"] == 39
    assert metrics["lora_rank"] == 8
    assert metrics["learning_rate"] == 2e-4
    assert metrics["batch_size"] == 1
    assert metrics["grad_accum"] == 4
    assert metrics["smoke_run"] is False

    config_json = json.loads((v3 / "adapter_config.json").read_text(encoding="utf-8"))
    assert config_json["r"] == 8
    assert config_json["lora_alpha"] == 16
    assert config_json["lora_dropout"] == 0.05
    assert config_json["bias"] == "none"
    assert config_json["task_type"] == "CAUSAL_LM"
    assert set(config_json["target_modules"]) == {
        "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}


def test_7c_v1_and_v2_adapters_were_not_overwritten():
    """A retrain must never clobber an earlier experiment's evidence."""
    for name, steps, examples in (
        ("qwen3-0.6b-quickjunction-lora", 39, 50),
        ("qwen3-0.6b-quickjunction-lora-v2", 90, 120),
    ):
        adapter = BASE_DIR / "models" / name
        if not adapter.is_dir():
            pytest.skip(f"{name} not present in this checkout (models/ is git-ignored)")
        metrics = json.loads((adapter / "training_metrics.json").read_text(encoding="utf-8"))
        assert metrics["steps"] == steps, f"{name} metrics changed -- it was overwritten"
        assert metrics["train_examples"] == examples, \
            f"{name} metrics changed -- it was overwritten"
