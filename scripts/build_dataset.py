"""Build train/validation JSONL from a hand-authored seed file.

    python scripts/build_dataset.py            # v2 (current)
    python scripts/build_dataset.py --version v1

Reads  data/raw/seed_examples[_v2].jsonl   (manually written, one JSON object per line)
Writes data/processed/<version>/train.jsonl
       data/processed/<version>/validation.jsonl
       data/processed/<version>/manifest.json

v1 additionally keeps writing to data/processed/{train,validation}.jsonl so
the M07 artefacts stay exactly where M07 left them; nothing about the v1
dataset changes in M07.1.

The split is **deterministic and content-addressed**: each example is hashed
(SHA-256 over its canonical JSON), examples are ordered by that hash within
their category, and a fixed fraction of each category goes to validation.
No RNG and no seed to drift -- re-running on unchanged input reproduces
byte-identical output, and adding an example never reshuffles the ones
already placed.

Splitting *within* category keeps every category represented on both sides,
which matters here because the categories are small and unevenly useful.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
RAW_DIR = BASE_DIR / "data" / "raw"
PROCESSED_DIR = BASE_DIR / "data" / "processed"

REQUIRED_FIELDS = ("instruction", "input", "output")

# One entry per dataset version. `validation_per_category` is chosen so the
# validation slice stays near 20% without ever emptying a category.
VERSIONS = {
    "v1": {
        "raw": RAW_DIR / "seed_examples.jsonl",
        "validation_per_category": 1,
        # M07 wrote these paths; they are kept for backwards compatibility.
        "legacy_output": PROCESSED_DIR,
        "description": "M07 seed dataset (60 examples, 10 categories).",
    },
    "v2": {
        "raw": RAW_DIR / "seed_examples_v2.jsonl",
        "validation_per_category": 2,
        "legacy_output": None,
        "description": (
            "M07.1 rebalanced dataset (150 examples, 15 categories). Adds "
            "partial/weak/mismatch and contradiction-correction categories to "
            "fix the M07 sycophancy defect."
        ),
    },
    "v3": {
        "raw": RAW_DIR / "seed_examples_v3.jsonl",
        "validation_per_category": 3,
        "legacy_output": None,
        "description": (
            "M07.2 dataset (200 examples, 13 categories). Every example uses "
            "the exact production prompt shape emitted by "
            "app/services/local_llm.py::build_prompt, which only 13% of v2 did. "
            "Includes 20 contrastive groups of 3 that hold the customer "
            "preference fixed and vary the candidate item."
        ),
    },
    "v4": {
        "raw": RAW_DIR / "seed_examples_v4.jsonl",
        "validation_per_category": 3,
        "legacy_output": None,
        "description": (
            "M07.4 dataset (240 examples, 15 categories). Retains the 159 v3 "
            "examples that carry no unset preference -- the ones that produced "
            "v3's M07.3 wins, including all 20 contrastive groups -- and "
            "replaces the 41 unset-involving examples with 81 newly authored "
            "ones. Fixes v3's not-set regression by teaching silent omission "
            "of unset dimensions, and teaches the 'other' cuisine that v3 "
            "never verbalised."
        ),
    },
}

DEFAULT_VERSION = "v2"


def example_hash(example: dict) -> str:
    """Stable identity for one example, independent of key order."""
    payload = {field: example[field] for field in REQUIRED_FIELDS}
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_raw(raw_path: Path) -> list[dict]:
    if not raw_path.is_file():
        raise SystemExit(f"seed file not found: {raw_path}")
    examples = []
    with raw_path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                example = json.loads(line)
            except json.JSONDecodeError as exc:
                raise SystemExit(f"{raw_path}:{line_number}: invalid JSON -- {exc}")
            missing = [f for f in REQUIRED_FIELDS if f not in example]
            if missing:
                raise SystemExit(f"{raw_path}:{line_number}: missing field(s) {missing}")
            for field in REQUIRED_FIELDS:
                if not isinstance(example[field], str) or not example[field].strip():
                    raise SystemExit(f"{raw_path}:{line_number}: field {field!r} must be a non-empty string")
            if "category" not in example:
                raise SystemExit(f"{raw_path}:{line_number}: missing 'category'")
            examples.append(example)
    return examples


def split(examples: list[dict], validation_per_category: int) -> tuple[list[dict], list[dict]]:
    """Hold out the first N examples of each category, ordered by content hash.

    **Contrastive groups (v3).** An example may carry an optional ``group_id``.
    Grouped examples are minimal-pair variants that share one customer
    preference block and differ only in the candidate item, so holding one
    member out while training on its siblings would leak the contrast the
    group exists to teach. Group members also span *different* categories,
    and validation is chosen per category, so without special handling they
    would be scattered across both splits.

    The fix is one term in the sort key: grouped examples sort *after*
    ungrouped ones, so validation is filled from ungrouped examples first and
    whole groups stay in training. Every category must therefore keep more
    than ``validation_per_category`` ungrouped examples -- ``build`` asserts
    the resulting integrity rather than trusting it.

    Backwards compatible by construction: v1 and v2 carry no ``group_id``, so
    the extra term is constant there and the ordering collapses to the
    original hash order, reproducing byte-identical output.
    """
    by_category: dict[str, list[dict]] = defaultdict(list)
    for example in examples:
        by_category[example["category"]].append(example)

    def order_key(example: dict) -> tuple[bool, str]:
        return ("group_id" in example, example_hash(example))

    train: list[dict] = []
    validation: list[dict] = []
    for category in sorted(by_category):
        ordered = sorted(by_category[category], key=order_key)
        if len(ordered) <= validation_per_category:
            raise SystemExit(
                f"category {category!r} has only {len(ordered)} example(s); "
                f"cannot hold out {validation_per_category}"
            )
        validation.extend(ordered[:validation_per_category])
        train.extend(ordered[validation_per_category:])
    return train, validation


def write(path: Path, examples: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for example in examples:
            handle.write(json.dumps(example, ensure_ascii=False, sort_keys=True) + "\n")


def build(version: str) -> dict:
    spec = VERSIONS[version]
    raw_path: Path = spec["raw"]
    examples = load_raw(raw_path)

    hashes = [example_hash(e) for e in examples]
    seen: dict[str, int] = {}
    duplicates = []
    for index, digest in enumerate(hashes, start=1):
        if digest in seen:
            duplicates.append((seen[digest], index))
        else:
            seen[digest] = index
    if duplicates:
        pairs = ", ".join(f"lines {a}&{b}" for a, b in duplicates[:5])
        raise SystemExit(f"{len(duplicates)} duplicate example(s) in {raw_path.name} ({pairs}); remove them first.")

    train, validation = split(examples, spec["validation_per_category"])

    train_hashes = {example_hash(e) for e in train}
    validation_hashes = {example_hash(e) for e in validation}
    overlap = train_hashes & validation_hashes
    if overlap:
        raise SystemExit(f"{len(overlap)} example(s) appear in both splits -- refusing to write.")

    # A contrastive group must land entirely in one split (see `split`). This
    # verifies the outcome instead of assuming the ordering achieved it, so a
    # future category whose ungrouped examples run short fails the build rather
    # than silently leaking a minimal pair into validation.
    group_splits: dict[str, set[str]] = defaultdict(set)
    for name, bucket in (("train", train), ("validation", validation)):
        for example in bucket:
            if "group_id" in example:
                group_splits[example["group_id"]].add(name)
    straddling = sorted(g for g, s in group_splits.items() if len(s) > 1)
    if straddling:
        raise SystemExit(
            f"{len(straddling)} contrastive group(s) straddle the train/validation "
            f"boundary ({', '.join(straddling[:5])}) -- refusing to write. Add more "
            f"ungrouped examples to the affected categories."
        )

    out_dir = PROCESSED_DIR / version
    write(out_dir / "train.jsonl", train)
    write(out_dir / "validation.jsonl", validation)

    if spec["legacy_output"] is not None:
        write(spec["legacy_output"] / "train.jsonl", train)
        write(spec["legacy_output"] / "validation.jsonl", validation)

    counts: dict[str, dict[str, int]] = {}
    for example in train:
        counts.setdefault(example["category"], {"train": 0, "validation": 0})["train"] += 1
    for example in validation:
        counts.setdefault(example["category"], {"train": 0, "validation": 0})["validation"] += 1

    manifest = {
        "dataset_version": version,
        "description": spec["description"],
        "curation_method": (
            "Manually authored by the developer for this project. Not scraped, "
            "not downloaded from a third party, and not generated by another "
            "language model."
        ),
        "duplicate_policy": (
            "Exact duplicates (by SHA-256 over canonical instruction/input/output) "
            "are rejected at build time; train and validation may never share an "
            "example."
        ),
        "split_method": (
            "Deterministic and content-addressed: SHA-256 per example, ordered by "
            "hash within category, first N of each category held out. No RNG. "
            "Examples carrying a 'group_id' sort last, so contrastive groups stay "
            "whole in training; the build fails if any group straddles the split."
        ),
        "validation_per_category": spec["validation_per_category"],
        "totals": {
            "raw": len(examples),
            "train": len(train),
            "validation": len(validation),
            "categories": len(counts),
        },
        "category_counts": {k: counts[k] for k in sorted(counts)},
        "sha256": {
            "raw": file_sha256(raw_path),
            "train": file_sha256(out_dir / "train.jsonl"),
            "validation": file_sha256(out_dir / "validation.jsonl"),
        },
    }
    # Only versions that actually use contrastive groups carry this key, so
    # the v1 and v2 manifests keep the shape M07/M07.1 published.
    if group_splits:
        manifest["contrastive_groups"] = {
            "count": len(group_splits),
            "all_in_train": all(s == {"train"} for s in group_splits.values()),
        }

    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", default=DEFAULT_VERSION, choices=sorted(VERSIONS))
    parser.add_argument("--all", action="store_true", help="rebuild every version")
    args = parser.parse_args()

    versions = sorted(VERSIONS) if args.all else [args.version]
    for version in versions:
        manifest = build(version)
        totals = manifest["totals"]
        print(
            f"[{version}] total {totals['raw']} | train {totals['train']} | "
            f"validation {totals['validation']} | categories {totals['categories']}"
        )
        print(f"{'category':<28} {'train':>5} {'val':>4}")
        for category, counts in manifest["category_counts"].items():
            print(f"{category:<28} {counts['train']:>5} {counts['validation']:>4}")
        print(f"raw sha256: {manifest['sha256']['raw']}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
