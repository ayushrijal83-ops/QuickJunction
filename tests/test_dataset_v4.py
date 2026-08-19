"""Milestone 07.4 tests: dataset V4 and the not-set correction.

V4 exists because of a regression M07.3 measured in V3. V3 was better than V2
on every axis except one: it *narrated* unset preferences instead of leaving
them alone, and produced

    "…it is Mexican rather than the cuisine you set."      (no cuisine was set)
    "You only wanted vegan and mild preferences."          (a cuisine was set)
    "Fattoush Salad is not set and has no heat."           (placeholder leaked)

Root cause, measured against the V3 file: 31 of its 41 unset-involving
examples narrate the unsetness, and **none** models silently omitting an unset
dimension while describing the ones that were set.

A second, related defect: V3 had 16 examples whose item cuisine is the enum
value ``other`` and named it in none of them, so the model had no learned way
to refer to that cuisine and substituted a concrete wrong one ("It is an
Indian dish").

V4 keeps the 159 V3 examples that carry no unset preference -- the ones that
produced V3's wins, including all 20 contrastive groups -- and replaces the 41
defective ones with 81 newly authored examples.

No model is trained or loaded here.
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
RAW_V4 = BASE_DIR / "data" / "raw" / "seed_examples_v4.jsonl"
V4_DIR = BASE_DIR / "data" / "processed" / "v4"

REQUIRED_FIELDS = ("instruction", "input", "output")
INSTRUCTION = (
    "Explain in one or two sentences why this menu item was recommended. "
    "Use only the facts provided."
)
UNSET = "not set"

EXPECTED_COUNTS = {
    "full_match": 26, "cuisine_and_spice_differ": 20, "cuisine_differs_only": 16,
    "spice_differs_only": 16, "single_attribute_match": 16,
    "no_cross_dimension_errors": 14, "dietary_compatible_not_identical": 12,
    "label_fact_consistency": 12, "no_invented_attributes": 12,
    "no_invented_context": 12, "low_signal_suggested": 13,
    "unset_omitted_silently": 30, "unset_partial_match": 16,
    "unset_none_set": 10, "cuisine_other_unnamed": 15,
}

# Categories authored under the strict silent-omission rule. `unset_none_set`
# is the deliberate exception: with nothing set, there is nothing else to say.
STRICT_SILENT = {"unset_omitted_silently", "unset_partial_match",
                 "cuisine_other_unnamed", "low_signal_suggested"}

CUISINES = {"indian", "chinese", "italian", "continental", "mexican", "thai",
            "multi_cuisine", "other"}
DIETARY = {"vegetarian", "vegan", "eggetarian", "non_vegetarian"}
SPICE = {"none", "mild", "medium", "hot", "extra_hot"}
LABELS = {"Strong match", "Good match", "Fair match", "Suggested"}

DIETARY_COMPATIBILITY = {
    "vegan": {"vegan"},
    "vegetarian": {"vegan", "vegetarian"},
    "eggetarian": {"eggetarian", "vegan", "vegetarian"},
    "non_vegetarian": {"vegetarian", "vegan", "eggetarian", "non_vegetarian"},
}

PROMPT_RE = re.compile(
    r"^Customer preference:\ncuisine: (?P<pc>[a-z_ ]+)\ndietary: (?P<pd>[a-z_ ]+)\n"
    r"spice: (?P<ps>[a-z_ ]+)\n\nRecommended item:\nname: (?P<name>[^\n]+)\n"
    r"cuisine: (?P<ic>[a-z_]+)\ndietary: (?P<id>[a-z_]+)\nspice: (?P<is>[a-z_]+)\n"
    r"match: (?P<label>[A-Za-z ]+)$")

DIMENSION_WORDS = {
    "cuisine": [c.replace("_", " ") for c in CUISINES] + ["cuisine"],
    "dietary": [d.replace("_", "-") for d in DIETARY] + ["dietary", "diet"],
    "spice": [s.replace("_", " ") for s in SPICE if s != "none"]
             + ["spice", "spiced", "heat", "unspiced"],
}


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def example_hash(example: dict) -> str:
    payload = {f: example[f] for f in REQUIRED_FIELDS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def parsed_rows(path: Path = RAW_V4):
    out = []
    for index, row in enumerate(load_jsonl(path)):
        match = PROMPT_RE.match(row["input"])
        assert match, f"{path.name}:{index} does not match the production prompt shape"
        out.append((index, row, match))
    return out


# --- 1. Shape and size --------------------------------------------------------


def test_1_v4_schema_and_size():
    for path in (RAW_V4, V4_DIR / "train.jsonl", V4_DIR / "validation.jsonl"):
        assert path.is_file(), f"missing {path}"
        for index, row in enumerate(load_jsonl(path)):
            for field in REQUIRED_FIELDS:
                assert isinstance(row[field], str) and row[field].strip()
            assert row["category"] in EXPECTED_COUNTS
            assert set(row) <= set(REQUIRED_FIELDS) | {"category", "group_id"}

    assert len(load_jsonl(RAW_V4)) == 240
    assert len(load_jsonl(V4_DIR / "train.jsonl")) == 195
    assert len(load_jsonl(V4_DIR / "validation.jsonl")) == 45


def test_1b_category_counts_are_exact():
    assert Counter(r["category"] for r in load_jsonl(RAW_V4)) == Counter(EXPECTED_COUNTS)


def test_1c_production_prompt_contract_holds():
    assert {r["instruction"] for r in load_jsonl(RAW_V4)} == {INSTRUCTION}
    for index, _, m in parsed_rows():
        assert m["pc"] == UNSET or m["pc"] in CUISINES, index
        assert m["pd"] == UNSET or m["pd"] in DIETARY, index
        assert m["ps"] == UNSET or m["ps"] in SPICE, index
        assert m["ic"] in CUISINES and m["id"] in DIETARY and m["is"] in SPICE, index
        assert m["label"] in LABELS, index


def test_1d_dietary_hard_filter_would_allow_every_item():
    for index, _, m in parsed_rows():
        if m["pd"] == UNSET:
            continue
        assert m["id"] in DIETARY_COMPATIBILITY[m["pd"]], index


# --- 2. The not-set fix -- the reason V4 exists -------------------------------


def test_2_literal_not_set_never_appears_in_any_output():
    """V3 leaked the placeholder as an item attribute ("X is not set and…").
    Removing the token from every target string removes it as a target."""
    for index, row, _ in parsed_rows():
        assert not re.search(r"\bnot set\b", row["output"], re.I), \
            f"{index}: output contains the literal placeholder"


def test_2b_unset_dimension_is_never_mentioned_in_strict_silent_categories():
    """The core correction: describe what was set, say nothing about the rest."""
    for index, row, m in parsed_rows():
        if row["category"] not in STRICT_SILENT:
            continue
        # The dish name is a proper noun, not a claim about a dimension.
        text = row["output"].lower().replace(m["name"].lower(), " ")
        for dim, key in (("cuisine", "pc"), ("dietary", "pd"), ("spice", "ps")):
            if m[key] != UNSET:
                continue
            for wordform in DIMENSION_WORDS[dim]:
                assert not re.search(rf"\b{re.escape(wordform)}\b", text), \
                    f"{index}: mentions unset {dim} via {wordform!r}"


def test_2c_strict_silent_categories_never_narrate_unsetness():
    narration = re.compile(
        r"\bunset\b|did not (?:state|set|specify)|you set no|have not (?:set|chosen|recorded)|"
        r"left .{0,25}unset|not recorded|not specified|no preferences", re.I)
    for index, row, _ in parsed_rows():
        if row["category"] in STRICT_SILENT:
            assert not narration.search(row["output"]), f"{index}: narrates unsetness"


def test_2d_never_asserts_a_preference_on_an_unset_dimension():
    """V3 produced "rather than the cuisine you set" with no cuisine set."""
    for index, row, m in parsed_rows():
        text = row["output"].lower()
        for dim, key in (("cuisine", "pc"), ("dietary", "pd"), ("spice", "ps")):
            if m[key] != UNSET:
                continue
            assert not re.search(
                rf"your {dim} preference|matche?s? your {dim}|"
                rf"the {dim} you (?:set|chose|asked for)|your (?:chosen|stated|preferred) {dim}",
                text), f"{index}: asserts an unset {dim} preference"


def test_2e_unset_none_set_acknowledges_without_claiming_a_match():
    """The one place acknowledgment is correct: nothing is set at all."""
    rows = [(i, r, m) for i, r, m in parsed_rows() if r["category"] == "unset_none_set"]
    assert len(rows) == 10
    for index, row, m in rows:
        assert m["pc"] == m["pd"] == m["ps"] == UNSET, f"{index}: not all preferences are unset"
        text = row["output"].lower()
        assert "general suggestion" in text, f"{index}: missing suggestion framing"
        assert not re.search(r"matche?s|as you (?:asked|chose|prefer)|you prefer", text), \
            f"{index}: claims a match when nothing was set"


def test_2f_silent_omission_is_the_dominant_pattern_and_v3_had_none():
    """A handful of examples would not shift behaviour; this must be the norm.

    Stated as a comparison against V3 rather than an invented threshold: V3
    carried 41 unset-involving examples and taught silent omission in none of
    them, which is why the model narrated and got it wrong.
    """
    v4_unset = [(i, r, m) for i, r, m in parsed_rows()
                if UNSET in (m["pc"], m["pd"], m["ps"])]
    v3_unset = [r for r in load_jsonl(RAW_V3)
                if UNSET in PROMPT_RE.match(r["input"]).group("pc", "pd", "ps")]

    # V4 must cover the failing region more heavily than V3 did.
    assert len(v4_unset) > len(v3_unset), (
        f"V4 has {len(v4_unset)} unset-involving examples vs V3's {len(v3_unset)}"
    )

    silent = [r for _, r, _ in v4_unset if r["category"] in STRICT_SILENT]
    assert len(silent) >= 46, f"only {len(silent)} examples teach silent omission"
    assert len(silent) / len(v4_unset) >= 0.75, (
        f"only {len(silent)}/{len(v4_unset)} unset examples teach silent omission; "
        "V3 failed precisely because this pattern was absent"
    )
    # The remainder is exactly the all-unset case, where acknowledgment is right.
    remainder = {r["category"] for _, r, _ in v4_unset} - STRICT_SILENT
    assert remainder == {"unset_none_set"}, remainder


# --- 3. The `other` cuisine defect -------------------------------------------


def test_3_other_cuisine_is_never_named_as_a_concrete_cuisine():
    """V3 named `other` in 0 of 16 examples, so the model invented one."""
    concrete = CUISINES - {"other"}
    for index, row, m in parsed_rows():
        if m["ic"] != "other":
            continue
        text = row["output"].lower()
        for cuisine in concrete:
            word = cuisine.replace("_", " ")
            assert not re.search(
                rf"\b(?:is|being)\s+(?:an?\s+)?{re.escape(word)}\b(?!\s*(?:rather|instead))", text), \
                f"{index}: names {cuisine!r} as the item's cuisine when it is 'other'"


def test_3b_a_dedicated_category_teaches_the_other_cuisine():
    rows = [r for r in load_jsonl(RAW_V4) if r["category"] == "cuisine_other_unnamed"]
    assert len(rows) == 15
    assert all(re.search(r"^cuisine: other$", r["input"], re.M) for r in rows)


# --- 4. V3's proven behaviour is preserved ------------------------------------


def test_4_all_twenty_contrastive_groups_survive_intact():
    groups = defaultdict(list)
    for row in load_jsonl(RAW_V4):
        if "group_id" in row:
            groups[row["group_id"]].append(row)
    assert len(groups) == 20
    assert {len(v) for v in groups.values()} == {3}


def test_4b_no_group_is_split_across_the_v4_split():
    train = {r["group_id"] for r in load_jsonl(V4_DIR / "train.jsonl") if "group_id" in r}
    validation = {r["group_id"] for r in load_jsonl(V4_DIR / "validation.jsonl") if "group_id" in r}
    assert not (train & validation)
    assert len(train) == 20 and not validation


def test_4c_exactly_the_159_clean_v3_examples_were_retained():
    """V4 keeps V3's proven core and drops precisely the defective examples."""
    v3 = load_jsonl(RAW_V3)
    v4_hashes = {example_hash(r) for r in load_jsonl(RAW_V4)}
    retained = [r for r in v3 if example_hash(r) in v4_hashes]
    dropped = [r for r in v3 if example_hash(r) not in v4_hashes]
    assert len(retained) == 159
    assert len(dropped) == 41
    # Everything retained is free of unset preferences; everything dropped has one.
    for row in retained:
        assert UNSET not in PROMPT_RE.match(row["input"]).group("pc", "pd", "ps")
    for row in dropped:
        assert UNSET in PROMPT_RE.match(row["input"]).group("pc", "pd", "ps")


def test_4d_grounding_guarantees_are_still_enforced():
    attributes = re.compile(
        r"\b(price|cost|rupee|calorie|ingredient|contains|made with|available|"
        r"in stock|sold out|healthy|nutritious|protein)\b", re.I)
    context = re.compile(
        r"\b(you ordered|previously|order history|popular|best.?sell|highly rated|"
        r"reviews?|our chef|our restaurant|only item on the menu)\b", re.I)
    for index, row, _ in parsed_rows():
        assert not attributes.search(row["output"]), f"{index}: invented attribute"
        assert not context.search(row["output"]), f"{index}: invented context"


def test_4e_outputs_are_one_or_two_sentences():
    for index, row, _ in parsed_rows():
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", row["output"].strip()) if s]
        assert 1 <= len(sentences) <= 2, f"{index}: {len(sentences)} sentences"


# --- 5. Split integrity and leakage ------------------------------------------


def test_5_no_duplicates_and_split_partitions_exactly():
    raw = load_jsonl(RAW_V4)
    assert len({example_hash(r) for r in raw}) == 240
    train = {example_hash(r) for r in load_jsonl(V4_DIR / "train.jsonl")}
    validation = {example_hash(r) for r in load_jsonl(V4_DIR / "validation.jsonl")}
    assert not (train & validation)
    assert train | validation == {example_hash(r) for r in raw}


def test_5b_zero_overlap_with_v1_and_v2():
    v4 = {example_hash(r) for r in load_jsonl(RAW_V4)}
    for name, path in (("v1", RAW_V1), ("v2", RAW_V2)):
        assert not (v4 & {example_hash(r) for r in load_jsonl(path)}), f"overlaps {name}"


def test_5c_no_m07_3_evaluation_case_leaked_into_v4():
    """If an evaluation prompt entered V4, the comparison that will judge V4
    would be scoring it on its own training data."""
    from training.eval_scenarios_production import all_cases

    bodies = set()
    for case in all_cases():
        pc, pd, ps = case["pref"]
        name, ic, idt, isp = case["item"]
        bodies.add(
            "Customer preference:\n"
            f"cuisine: {pc or UNSET}\ndietary: {pd or UNSET}\nspice: {ps or UNSET}\n\n"
            f"Recommended item:\nname: {name}\ncuisine: {ic}\ndietary: {idt}\n"
            f"spice: {isp}\nmatch: {case['label']}")
    leaked = [i for i, r in enumerate(load_jsonl(RAW_V4)) if r["input"].strip() in bodies]
    assert not leaked, f"evaluation prompts present in V4 at {leaked}"


def test_5d_v4_split_is_reproducible():
    paths = (V4_DIR / "train.jsonl", V4_DIR / "validation.jsonl", V4_DIR / "manifest.json")
    before = tuple(p.read_bytes() for p in paths)
    result = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "build_dataset.py"), "--version", "v4"],
        capture_output=True, text=True, cwd=str(BASE_DIR))
    assert result.returncode == 0, result.stderr
    assert tuple(p.read_bytes() for p in paths) == before


def test_5e_manifest_matches_the_files():
    manifest = json.loads((V4_DIR / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["dataset_version"] == "v4"
    assert manifest["totals"] == {"raw": 240, "train": 195, "validation": 45, "categories": 15}
    assert manifest["contrastive_groups"] == {"count": 20, "all_in_train": True}
    for key, path in (("raw", RAW_V4), ("train", V4_DIR / "train.jsonl"),
                      ("validation", V4_DIR / "validation.jsonl")):
        assert manifest["sha256"][key] == hashlib.sha256(path.read_bytes()).hexdigest()


# --- 6. V4 is trained but NOT promoted ----------------------------------------
#
# In M07.4 this asserted that *no* V4 adapter existed -- a guard against
# training happening unauthorised while that milestone was dataset-only. M07.5
# authorised exactly that training, so the adapter now exists and the original
# assertion expired by design.
#
# The guard is re-pointed rather than removed: the boundary that still matters
# has moved one step downstream. V4 is trained, but it must not become the
# adapter the application loads until it has been evaluated and approved.


def test_6_v4_is_the_production_adapter_and_is_guarded():
    """V4 was promoted in M07.7 after scoring 25/25 on the held-out evaluation.

    Promotion was conditional: V4 alone scored 23/25 and still credited the
    customer with preferences they had not set. It reached 25/25 only with the
    deterministic guard in ``app/services/local_llm.py``. Shipping V4 without
    that guard would reinstate the defect, so the two are pinned together.
    """
    import config
    from app.services import local_llm

    adapter = Path(config.BaseConfig.LLM_ADAPTER_PATH)
    assert adapter.name == "qwen3-0.6b-quickjunction-lora-v4", \
        f"LLM_ADAPTER_PATH points at {adapter.name}, not the promoted V4 adapter"

    import inspect
    assert "apply_preference_safeguard" in inspect.getsource(local_llm.generate_explanation), \
        "V4 is in production but the preference safeguard is no longer applied"


def test_6a_v4_adapter_artefact_is_well_formed():
    """If the V4 adapter is present it must be a real, complete artefact
    trained on v4 at the approved hyperparameters.

    Skipped where models/ is absent, since it is git-ignored.
    """
    v4 = BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora-v4"
    if not v4.is_dir():
        pytest.skip("V4 adapter not present in this checkout (models/ is git-ignored)")

    assert (v4 / "adapter_model.safetensors").is_file()
    assert (v4 / "adapter_config.json").is_file()

    metrics = json.loads((v4 / "training_metrics.json").read_text(encoding="utf-8"))
    assert metrics["dataset_version"] == "v4"
    assert metrics["train_examples"] == 195
    assert metrics["validation_examples"] == 45
    assert metrics["lora_rank"] == 8
    assert metrics["learning_rate"] == 2e-4
    assert metrics["batch_size"] == 1
    assert metrics["grad_accum"] == 4
    assert metrics["smoke_run"] is False

    config_json = json.loads((v4 / "adapter_config.json").read_text(encoding="utf-8"))
    assert config_json["r"] == 8
    assert config_json["lora_alpha"] == 16
    assert config_json["lora_dropout"] == 0.05
    assert config_json["bias"] == "none"
    assert config_json["task_type"] == "CAUSAL_LM"
    assert set(config_json["target_modules"]) == {
        "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"}


def test_6b_earlier_adapters_were_not_overwritten():
    """A retrain must never clobber an earlier experiment's evidence."""
    for name, steps, examples in (
        ("qwen3-0.6b-quickjunction-lora", 39, 50),
        ("qwen3-0.6b-quickjunction-lora-v2", 90, 120),
        ("qwen3-0.6b-quickjunction-lora-v3", 123, 161),
    ):
        adapter = BASE_DIR / "models" / name
        if not adapter.is_dir():
            pytest.skip(f"{name} not present in this checkout (models/ is git-ignored)")
        metrics = json.loads((adapter / "training_metrics.json").read_text(encoding="utf-8"))
        assert metrics["steps"] == steps, f"{name} was overwritten"
        assert metrics["train_examples"] == examples, f"{name} was overwritten"


def test_6b_earlier_datasets_are_untouched():
    assert len(load_jsonl(RAW_V1)) == 60
    assert len(load_jsonl(RAW_V2)) == 150
    assert len(load_jsonl(RAW_V3)) == 200
