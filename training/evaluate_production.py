"""M07.3: production-shaped evaluation of base / v1 / v2 / v3.

    python training/evaluate_production.py
    python training/evaluate_production.py --systems base v2 v3
    python training/evaluate_production.py --json training/eval_results_m07_3.json

**This file does not touch `training/evaluate.py` or its recorded results.**
The M07.1 harness and `eval_results_m07_1.json` are historical evidence and
remain exactly as they were; this is an additional, separate harness.

Why a new harness rather than an edit:

1. `evaluate.py` has no `v3` entry and cannot load the V3 adapter.
2. Four of its eight scenarios test situations the application cannot
   produce -- they mention availability, order history, an exclusion note,
   or an empty candidate list, none of which `ExplanationRequest` carries.
   Scoring V3 partly on unreachable behaviour would be misleading.
3. Its scoring is keyword-only. Here, expectations are derived from each
   case's own facts, so the scorer asks "did the model tell the truth about
   *this* candidate" rather than "did it emit a favoured phrase".

Generation matches **production exactly**: the prompt comes from
`app.services.local_llm.build_prompt`, decoding is greedy, the token limit is
the configured production default, and the output is trimmed by production's
own `_tidy`. The scored text is therefore the text a customer would see. The
untrimmed continuation is recorded alongside it for diagnosis.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("USE_JAX", "0")

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from app.services.local_llm import (  # noqa: E402
    DEFAULT_MAX_NEW_TOKENS,
    ExplanationRequest,
    _tidy,
    build_prompt,
)
from training.eval_scenarios_production import all_cases  # noqa: E402

BASE_MODEL = BASE_DIR / "models" / "Qwen3-0.6B-Base"

SYSTEMS = {
    "base": None,
    "v1": BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora",
    "v2": BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora-v2",
    "v3": BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora-v3",
}

# Every dataset surface a case could accidentally have been lifted from.
CORPUS_FILES = [
    BASE_DIR / "data" / "raw" / "seed_examples.jsonl",
    BASE_DIR / "data" / "raw" / "seed_examples_v2.jsonl",
    BASE_DIR / "data" / "raw" / "seed_examples_v3.jsonl",
]
for version in ("v1", "v2", "v3"):
    CORPUS_FILES += [
        BASE_DIR / "data" / "processed" / version / "train.jsonl",
        BASE_DIR / "data" / "processed" / version / "validation.jsonl",
    ]

# app/services/recommendations.py::_DIETARY_COMPATIBILITY
DIETARY_COMPATIBILITY = {
    "vegan": {"vegan"},
    "vegetarian": {"vegan", "vegetarian"},
    "eggetarian": {"eggetarian", "vegan", "vegetarian"},
    "non_vegetarian": {"vegetarian", "vegan", "eggetarian", "non_vegetarian"},
}

CUISINES = {"indian", "chinese", "italian", "continental", "mexican", "thai",
            "multi_cuisine", "other"}
SPICES = {"none", "mild", "medium", "hot", "extra_hot"}
DIETARY = {"vegetarian", "vegan", "eggetarian", "non_vegetarian"}


def word(value: str) -> str:
    """Enum token -> the way prose spells it."""
    return value.replace("_", " ").replace("non vegetarian", "non-vegetarian")


# --------------------------------------------------------------------------
# Failure taxonomy
# --------------------------------------------------------------------------

PASS = "PASS"
CROSS_DIMENSION_ERROR = "CROSS_DIMENSION_ERROR"
NOT_SET_ERROR = "NOT_SET_ERROR"
DIETARY_COMPATIBILITY_ERROR = "DIETARY_COMPATIBILITY_ERROR"
HALLUCINATION = "HALLUCINATION"
LABEL_FACT_CONTRADICTION = "LABEL_FACT_CONTRADICTION"
GARBLED_PROSE = "GARBLED_PROSE"
OVERCLAIM = "OVERCLAIM"
OTHER = "OTHER"

# Facts the model is never given. Any of these is fabrication.
_HALLUCINATION_PATTERNS = {
    "price": r"\b(price[ds]?|pricing|cost[s]?|costly|cheap|expensive|affordable|value for money|rupees?|dollars?)\b|[₹$]",
    "ingredients": r"\b(ingredient[s]?|made (?:with|from)|contains|topped with|cooked (?:with|in)|recipe)\b",
    "availability": r"\b(available|availability|unavailable|in stock|out of stock|sold out|on the menu today)\b",
    "history": r"\b(you (?:ordered|have ordered|previously|last time)|order history|your (?:previous|past) order|again, as you)\b",
    "health": r"\b(healthy|healthier|nutritious|nutrition|calorie[s]?|low[- ]fat|protein[- ]rich|good for you|diet[- ]friendly)\b",
    "popularity": r"\b(popular|best[- ]?sell\w*|customer favou?rite|highly rated|review[s]?|trending|signature dish|house special|our chef|our restaurant)\b",
    "menu_scope": r"\bonly item on the menu\b|\bthe only (?:dish|item)\b|\bevery other item\b",
}


def analyse(output: str, case: dict) -> dict:
    """Derive truth from the case's facts, then judge the text against it."""
    pc, pd, ps = case["pref"]
    name, ic, idt, isp = case["item"]
    label = case["label"]
    text = output or ""
    low = text.lower()

    cuisine_set, dietary_set, spice_set = pc is not None, pd is not None, ps is not None
    cuisine_matches = cuisine_set and pc == ic
    cuisine_differs = cuisine_set and pc != ic
    spice_matches = spice_set and ps == isp
    spice_differs = spice_set and ps != isp
    dietary_identical = dietary_set and pd == idt
    dietary_compatible = (not dietary_set) or idt in DIETARY_COMPATIBILITY[pd]

    errors: list[str] = []
    detail: list[str] = []

    # --- 1. Hallucination -------------------------------------------------
    for kind, pattern in _HALLUCINATION_PATTERNS.items():
        hit = re.search(pattern, low)
        if hit:
            errors.append(HALLUCINATION)
            detail.append(f"hallucinated {kind}: {hit.group(0)!r}")
            break

    # --- 2. Not-set correctness ------------------------------------------
    # An unset preference must never be reported as a preference that was met.
    unset_claims = []
    for is_set, dim, item_value in (
        (cuisine_set, "cuisine", ic),
        (dietary_set, "dietary", idt),
        (spice_set, "spice", isp),
    ):
        if is_set:
            continue
        vw = re.escape(word(item_value))
        patterns = [
            rf"your {dim} preference",
            rf"matche?s? your {dim}",
            rf"you (?:prefer|asked for|wanted|like) {vw}",
            rf"{vw},? (?:which|as) is what you",
            rf"{vw},? as you (?:asked|prefer|wanted)",
            rf"your preferred {dim}",
            # "rather than the cuisine you set" -- asserts a preference exists.
            rf"the {dim} you (?:set|chose|asked for|recorded|wanted)",
            rf"your (?:chosen|stated|recorded) {dim}",
        ]
        for pattern in patterns:
            if re.search(pattern, low):
                unset_claims.append(f"{dim} is not set but text claims it: {pattern!r}")
                break
    # Enumerating which preferences the customer "only" gave is wrong whenever
    # it silently drops a preference that IS set.
    only_wanted = re.search(r"you (?:only )?(?:wanted|asked for|set)(?: only)? ([^.]{0,60})", low)
    if only_wanted:
        listed = only_wanted.group(1)
        # Only an actual enumeration can wrongly omit a preference. A phrase
        # naming a single attribute ("you asked for only an Indian one") is
        # scoped to that dimension and omits nothing.
        enumerated = sum(1 for v in (CUISINES | DIETARY | SPICES)
                         if re.search(rf"\b{re.escape(word(v))}\b", listed))
        if enumerated < 2:
            listed = None
    if only_wanted and listed:
        for is_set, dim, pref_value in (
            (cuisine_set, "cuisine", pc), (dietary_set, "dietary", pd), (spice_set, "spice", ps),
        ):
            if is_set and pref_value is not None:
                pv = word(pref_value)
                if pv not in listed and dim not in listed:
                    unset_claims.append(
                        f"enumerates the customer's preferences but omits {dim}={pref_value}")
                    break
    # With nothing set at all, any possessive preference claim is wrong --
    # unless it is an explicit denial ("matching neither of your preferences").
    if not (cuisine_set or dietary_set or spice_set):
        claims = re.search(r"your (?:stated |recorded )?preference|you prefer|matches your|"
                           r"as you asked|you asked for", low)
        denies = re.search(r"neither|none|nothing|not set|no preferences|have not set|"
                           r"left .{0,30}unset|does not match|don'?t match", low)
        if claims and not denies:
            unset_claims.append("no preferences are set but the text claims one was matched")
    if unset_claims:
        errors.append(NOT_SET_ERROR)
        detail.extend(unset_claims)

    # --- 3. Dietary compatibility ----------------------------------------
    asserts_dietary_problem = re.search(
        r"(?:does not|doesn'?t|not|fails to)\s+(?:\w+\s+){0,3}"
        r"(?:meet|match|suit|satisfy|fit)\w*\s+(?:\w+\s+){0,3}"
        r"(?:dietary|vegetarian|vegan|eggetarian)"
        r"|dietary (?:mismatch|conflict|requirement is not)"
        r"|not (?:a )?(?:vegetarian|vegan|eggetarian)(?: option| dish)?\b"
        r"|not suitable for (?:a |your )?(?:vegetarian|vegan|eggetarian)",
        low)
    if asserts_dietary_problem and dietary_compatible:
        errors.append(DIETARY_COMPATIBILITY_ERROR)
        detail.append(
            f"claims a dietary problem ({asserts_dietary_problem.group(0)!r}) but "
            f"{idt} is compatible with {pd or 'an unset preference'}")

    # --- 4. Cross-dimension errors ---------------------------------------
    cross = []
    # A cuisine or spice difference reported as a dietary failure.
    if dietary_compatible and asserts_dietary_problem and (cuisine_differs or spice_differs):
        cross.append("a cuisine/spice difference is described as a dietary failure")
    # A cuisine value used as a spice level, or vice versa.
    for cuisine in CUISINES:
        cw = re.escape(word(cuisine))
        if re.search(rf"spice (?:level )?is {cw}\b|{cw} spice\b|spiced {cw}\b", low):
            cross.append(f"cuisine {cuisine!r} used as a spice level")
    for spice in SPICES:
        sw = re.escape(word(spice))
        if re.search(rf"cuisine is {sw}\b|{sw} cuisine\b", low):
            cross.append(f"spice {spice!r} used as a cuisine")
    for diet in DIETARY:
        dw = re.escape(word(diet))
        if re.search(rf"cuisine is {dw}\b|spice (?:level )?is {dw}\b", low):
            cross.append(f"dietary {diet!r} used as another dimension")
    if cross:
        errors.append(CROSS_DIMENSION_ERROR)
        detail.extend(cross)

    # --- 5. Label / fact contradiction ------------------------------------
    contradictions = []
    if label == "Suggested":
        if re.search(r"\bmatch(?:es|ing|ed)?\b", low) and not re.search(
                r"none|neither|nothing|not set|no preferences|rather than a match|"
                r"not a match|no match|does not match|don'?t match", low):
            contradictions.append("label is 'Suggested' but the text asserts a match")
    # "is Indian", "is an Indian dish", "being an Indian dish" -- all assert the
    # item's cuisine is the preferred one when it demonstrably is not.
    if cuisine_differs and re.search(
            rf"\b(?:is|being)\s+(?:an?\s+)?{re.escape(word(pc))}\b(?!\s*(?:rather|instead))", low):
        contradictions.append(f"says the item is {pc} but it is {ic}")
    if spice_differs and re.search(
            rf"(?:spice (?:level )?is|it is) {re.escape(word(ps))}\b(?!\s*(?:rather|instead|but))", low):
        contradictions.append(f"says the spice is {ps} but it is {isp}")
    # Stating a spice level the item does not have (e.g. "hot" for extra_hot).
    for other_spice in SPICES - {isp}:
        if re.search(rf"\bit is {re.escape(word(other_spice))}\b(?!\s*(?:rather|instead))", low) \
                and not (spice_set and other_spice == ps):
            contradictions.append(f"states spice {other_spice!r} but the item is {isp}")
            break
    if cuisine_differs and re.search(r"matche?s? your cuisine", low):
        contradictions.append("claims the cuisine matches when it differs")
    if spice_differs and re.search(r"matche?s? your spice", low):
        contradictions.append("claims the spice matches when it differs")
    if contradictions:
        errors.append(LABEL_FACT_CONTRADICTION)
        detail.extend(contradictions)

    # --- 6. Overclaim -----------------------------------------------------
    all_three_true = (cuisine_matches and spice_matches and dietary_identical)
    if re.search(r"all three|every preference|all of your (?:stated |three )?preferences|"
                 r"matches everything|perfect match|exactly what you|all your preferences|"
                 r"each of your preferences", low) and not all_three_true:
        errors.append(OVERCLAIM)
        detail.append("claims a total match that is not literally true")

    # --- 7. Garbled prose -------------------------------------------------
    garbled = []
    if re.search(r"\b(\w+)\s+\1\b", low):
        garbled.append(f"immediate word repetition: {re.search(chr(92)+'b('+chr(92)+'w+)'+chr(92)+'s+'+chr(92)+'1'+chr(92)+'b', low).group(0)!r}")
    same_operand = re.search(r"\b([\w\- ]{3,20}?)\s+rather than\s+\1\b", low)
    if same_operand:
        garbled.append(f"vacuous contrast: {same_operand.group(0)!r}")
    # A repeated 4-gram is the classic small-model degeneration signature.
    tokens = re.findall(r"[a-z]+", low)
    grams = Counter(tuple(tokens[i:i + 4]) for i in range(len(tokens) - 3))
    repeated = [g for g, n in grams.items() if n > 1]
    if repeated:
        garbled.append(f"repeated 4-gram: {' '.join(repeated[0])!r}")
    # False causal link, e.g. "is mild, so you have not set a spice preference".
    if re.search(r",\s*so you have not set", low):
        garbled.append("false causal link to an unset preference")
    # Self-contradiction inside one output.
    if re.search(r"matches all", low) and re.search(r"does not match|not match", low):
        garbled.append("asserts and denies a match in the same output")
    # Leaking the literal "not set" placeholder as if it were an item attribute.
    if re.search(rf"{re.escape(name.lower())} is not set|\bis not set and\b|"
                 r"\bdish is not set\b", low):
        garbled.append("emits the 'not set' placeholder as an item attribute")
    # An unfinished trailing clause ("..., but it.").
    if re.search(r"\b(?:but|and|though|because|so)\s+it\s*\.$", text.strip().lower()):
        garbled.append("output ends mid-clause")
    if garbled:
        errors.append(GARBLED_PROSE)
        detail.extend(garbled)

    # --- 8. Empty / degenerate -------------------------------------------
    if not text.strip():
        errors.append(OTHER)
        detail.append("empty output")

    # --- Coverage: were the real differences actually named? --------------
    expected_diffs = []
    if cuisine_differs:
        # Hyphen/space insensitive ("multi cuisine" vs "multi-cuisine"). The
        # enum value 'other' has no natural prose form, so denying the
        # preferred cuisine ("it is not Thai") counts as naming the difference.
        flexible = re.escape(word(ic)).replace(r"\ ", r"[\s-]")
        named = bool(re.search(flexible, low))
        if not named and ic == "other":
            named = bool(re.search(rf"not {re.escape(word(pc))}|rather than {re.escape(word(pc))}|"
                                   r"another cuisine|different cuisine|outside your", low))
        expected_diffs.append(("cuisine", named))
    if spice_differs:
        named = bool(re.search(re.escape(word(isp)), low)) or (
            isp == "none" and bool(re.search(r"no (?:heat|spice)|unspiced|without heat", low)))
        expected_diffs.append(("spice", named))
    covered = sum(1 for _, ok in expected_diffs if ok)

    sentences = [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]

    return {
        "errors": sorted(set(errors)) or [PASS],
        "passed": not errors,
        "detail": detail,
        "sentences": len(sentences),
        "concise": len(sentences) <= 2,
        "diff_covered": covered,
        "diff_total": len(expected_diffs),
        "facts": {
            "cuisine_matches": cuisine_matches, "cuisine_differs": cuisine_differs,
            "spice_matches": spice_matches, "spice_differs": spice_differs,
            "dietary_identical": dietary_identical, "dietary_compatible": dietary_compatible,
            "unset": [d for d, s in (("cuisine", cuisine_set), ("dietary", dietary_set),
                                     ("spice", spice_set)) if not s],
        },
    }


# --------------------------------------------------------------------------
# Phase 7: named V2 failure classes
# --------------------------------------------------------------------------

def v2_failure_classes(output: str, case: dict, verdict: dict) -> list[str]:
    low = (output or "").lower()
    hits = []
    if "a cuisine/spice difference is described as a dietary failure" in verdict["detail"]:
        hits.append("cross_dimension_cuisine_as_dietary")
    if re.search(r"only item on the menu|the only (?:dish|item)", low):
        hits.append("invented_only_item_on_menu")
    if any(d.startswith("vacuous contrast") for d in verdict["detail"]):
        hits.append("contradictory_comparison_same_operand")
    if re.search(_HALLUCINATION_PATTERNS["availability"], low):
        hits.append("false_availability_wording")
    if NOT_SET_ERROR in verdict["errors"]:
        hits.append("false_preference_claim_when_not_set")
    if DIETARY_COMPATIBILITY_ERROR in verdict["errors"]:
        hits.append("dietary_mismatch_where_compatible")
    return hits


# --------------------------------------------------------------------------
# Runner
# --------------------------------------------------------------------------

def render(case: dict) -> str:
    pc, pd, ps = case["pref"]
    name, ic, idt, isp = case["item"]
    return build_prompt(ExplanationRequest(
        item_name=name, item_cuisine=ic, item_dietary=idt, item_spice=isp,
        preferred_cuisine=pc, preferred_dietary=pd, preferred_spice=ps,
        match_label=case["label"],
    ))


def assert_held_out(cases: list[dict]) -> None:
    """No evaluation prompt may appear in any dataset surface."""
    corpus = set()
    for path in CORPUS_FILES:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                corpus.add(json.loads(line)["input"].strip())
    leaked = []
    for case in cases:
        body = render(case).split("\n\n", 1)[1].rsplit("\n\nExplanation:", 1)[0].strip()
        if body in corpus:
            leaked.append(case["name"])
    if leaked:
        raise SystemExit(f"Evaluation cases found in training data: {leaked}")
    print(f"held-out verified: {len(cases)} cases absent from {len(corpus)} dataset prompts")


def load_system(adapter: Path | None):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(str(BASE_MODEL), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(
        str(BASE_MODEL), dtype=torch.float32, local_files_only=True)
    if adapter is not None:
        from peft import PeftModel
        model = PeftModel.from_pretrained(model, str(adapter), local_files_only=True)
    model.eval()
    return tokenizer, model


def generate(tokenizer, model, prompt: str, max_new_tokens: int) -> tuple[str, str]:
    """Return (production-visible text, raw continuation)."""
    import torch

    inputs = tokenizer(prompt, return_tensors="pt")
    with torch.no_grad():
        outputs = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,                      # greedy -> reproducible
            pad_token_id=tokenizer.eos_token_id,
        )
    raw = tokenizer.decode(outputs[0][inputs["input_ids"].shape[1]:], skip_special_tokens=True)
    return (_tidy(raw) or ""), raw


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systems", nargs="+", default=["base", "v1", "v2", "v3"],
                        choices=sorted(SYSTEMS))
    parser.add_argument("--max-new-tokens", type=int, default=DEFAULT_MAX_NEW_TOKENS,
                        help="defaults to the production token limit")
    parser.add_argument("--json", default=None)
    args = parser.parse_args()

    cases = all_cases()
    assert_held_out(cases)
    print(f"{len(cases)} production-shaped cases | greedy | "
          f"max_new_tokens={args.max_new_tokens} | production _tidy applied\n")

    results: dict[str, dict] = {}
    for system in args.systems:
        adapter = SYSTEMS[system]
        if adapter is not None and not (adapter / "adapter_config.json").is_file():
            print(f"[{system}] adapter not found at {adapter} -- skipping\n")
            continue

        print(f"=== {system} ===")
        tokenizer, model = load_system(adapter)
        per_case, timings = [], []
        for case in cases:
            began = time.time()
            visible, raw = generate(tokenizer, model, render(case), args.max_new_tokens)
            took = time.time() - began
            timings.append(took)
            verdict = analyse(visible, case)
            record = {
                "name": case["name"], "category": case["category"], "group": case["group"],
                "output": visible, "raw": raw.strip()[:600],
                "seconds": round(took, 2),
                "v2_failure_classes": v2_failure_classes(visible, case, verdict),
                **verdict,
            }
            per_case.append(record)
            flag = "PASS" if verdict["passed"] else ",".join(verdict["errors"])
            print(f"  [{flag}] {case['name']}")
            print(f"         {visible}")
            for line in verdict["detail"]:
                print(f"         ! {line}")

        passed = sum(1 for r in per_case if r["passed"])
        error_counts = Counter(e for r in per_case for e in r["errors"] if e != PASS)
        cov_num = sum(r["diff_covered"] for r in per_case)
        cov_den = sum(r["diff_total"] for r in per_case)
        results[system] = {
            "passed": passed,
            "total": len(cases),
            "error_counts": dict(error_counts),
            "difference_coverage": {"named": cov_num, "expected": cov_den},
            "concise": sum(1 for r in per_case if r["concise"]),
            "avg_seconds": round(sum(timings) / len(timings), 2),
            "total_seconds": round(sum(timings), 1),
            "cases": per_case,
        }
        print(f"  -> {passed}/{len(cases)} passed | difference coverage {cov_num}/{cov_den} "
              f"| avg {results[system]['avg_seconds']}s\n")
        del model, tokenizer

    print("=== summary ===")
    header = f"{'system':<7} {'passed':>8} {'coverage':>10} {'concise':>8} {'avg s':>7}"
    print(header)
    for system, data in results.items():
        print(f"{system:<7} {data['passed']:>3}/{data['total']:<4} "
              f"{data['difference_coverage']['named']:>4}/{data['difference_coverage']['expected']:<5} "
              f"{data['concise']:>4}/{data['total']:<3} {data['avg_seconds']:>7}")

    if args.json:
        payload = {
            "harness": "training/evaluate_production.py",
            "milestone": "M07.3",
            "generation": {
                "decoding": "greedy (do_sample=False)",
                "max_new_tokens": args.max_new_tokens,
                "post_processing": "app.services.local_llm._tidy (production)",
                "prompt": "app.services.local_llm.build_prompt (production)",
            },
            "case_count": len(cases),
            "systems": results,
        }
        Path(args.json).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
