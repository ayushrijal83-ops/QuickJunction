# AI — local Qwen integration (Milestones 07 and 07.1)

> **M07.1 status:** the dataset was rebalanced (60 → 150 examples) and a
> second LoRA adapter was trained. Evaluation on held-out scenarios showed
> **v1 5/8 → v2 8/8**, so **V2 replaced V1 as the default adapter**. The M07
> adapter is retained on disk and is still selectable. See §11.


Everything here runs **locally and offline**. No external LLM API, no paid
service, no network call at inference time, no GPU, no API key.

> **The LLM is not the source of truth.** The deterministic recommendation
> engine (`app/services/recommendations.py`, M06) decides what a customer may
> be shown. The model only turns that decision into a sentence.

---

## 1. Model

| | |
| --- | --- |
| Model | Qwen3-0.6B-Base |
| Local path | `models/Qwen3-0.6B-Base` (configurable, git-ignored) |
| Size on disk | 1.2 GB (`model.safetensors`) |
| Parameters | 596 M (601 M once LoRA layers are attached) |
| Architecture | `Qwen3ForCausalLM`, `model_type: qwen3` |
| Precision | float32 on CPU |

`config.json` declares `transformers_version: 4.51.0`, and the `qwen3`
architecture was added in transformers 4.51 — so **transformers ≥ 4.51 is a
hard requirement**, not a preference. This project pins 4.57.1.

## 2. Inference architecture

```
recommendations.py  ── decides WHAT (authoritative, deterministic)
        │ validated MenuItem rows
        ▼
routes/preferences.py::explain ── builds ExplanationRequest
        │ facts only: name, cuisine, dietary, spice, match label
        ▼
services/local_llm.py ── Qwen3-0.6B-Base, local, offline
        ▼
   1–2 sentences, or None
```

`ExplanationRequest` is a frozen dataclass with **no price, no id, no
availability flag**. The model cannot restate a price because it is never
given one, and cannot name an unavailable item because such items never
survive the engine's candidate filter.

**Configuration** (`config.py`, environment-overridable):

| Key | Default | Purpose |
| --- | --- | --- |
| `LLM_ENABLED` | `True` (`False` in testing) | master switch |
| `LLM_MODEL_PATH` | `models/Qwen3-0.6B-Base` | base weights |
| `LLM_ADAPTER_PATH` | `models/qwen3-0.6b-quickjunction-lora-v2` (since M07.1) | LoRA adapter |
| `LLM_MAX_NEW_TOKENS` | `48` | bounds worst-case latency |

Paths are configuration only. No function in `local_llm.py` accepts a path
argument and no route accepts one — see `docs/SECURITY.md` §10c.

**Loading** is lazy and process-wide (module singleton behind a lock): the
1.2 GB model is read on first explanation, not at startup, and never in the
test suite (`TestingConfig.LLM_ENABLED = False`).

**Offline enforcement**: `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` are
set *before* `transformers` is imported, and both model and adapter load with
`local_files_only=True`.

### Environment quirk worth knowing

`USE_TF=0` / `USE_FLAX=0` / `USE_JAX=0` are also set before the import. This
machine has a broken TensorFlow installation (its `protobuf` predates
`runtime_version`), and `transformers` imports TensorFlow opportunistically
from `image_transforms` — an entirely unrelated code path. Without these
flags, importing `transformers` *or* `peft` raises `ImportError` before any
Quick Junction code runs. Setting them is not cosmetic; nothing loads
without it.

## 3. Dataset

Fully documented in [`data/README.md`](../data/README.md). Summary:

| | |
| --- | --- |
| Source | **hand-authored for this project** — not scraped, not downloaded, not LLM-generated |
| Raw (v1) | `data/raw/seed_examples.jsonl` — 60 examples |
| Raw (v2, current) | `data/raw/seed_examples_v2.jsonl` — 150 examples — see §11 |
| Train / validation (v1) | 50 / 10 |
| Train / validation (v2) | 120 / 30 |
| Categories | 10 in v1, 15 in v2 |
| Format | JSONL: `category`, `instruction`, `input`, `output` |
| Split | deterministic, content-addressed (SHA-256), no RNG |

Regenerate with `python scripts/build_dataset.py` — byte-reproducible.

## 4. Training

**Method: LoRA (PEFT), CPU-only.** Full fine-tuning of 596 M parameters on
CPU is not practical, and the milestone forbids requiring a GPU, so LoRA is
what makes this feasible at all: it trains rank-8 adapters on the attention
and MLP projections (`q,k,v,o,gate,up,down_proj`) and leaves the base weights
frozen.

Script: `training/train_lora.py` (offline; `report_to=[]`, so no
experiment-tracking service is contacted). It never runs in the web process.

### A real training run completed

Not a smoke test, not simulated:

```
python training/train_lora.py --epochs 3
```

| | |
| --- | --- |
| Hardware | CPU only — `torch.cuda.is_available()` is `False`; `torch 2.10.0+cpu` |
| Examples | 50 train |
| Epochs | 3 |
| Optimizer steps | 39 (batch 1 × grad-accum 4) |
| Wall-clock | **1278 s ≈ 21.3 min** (~32 s/step) |
| Trainable params | **5,046,272 / 601,096,192 = 0.84 %** |
| Loss | ~3.13 at start → **0.63** at the final step; mean 1.63 |
| Output | `models/qwen3-0.6b-quickjunction-lora/` — 20 MB `adapter_model.safetensors` + `adapter_config.json` + `training_metrics.json` |

Exact metrics are written to
`models/qwen3-0.6b-quickjunction-lora/training_metrics.json` by the script
itself, not transcribed by hand.

### What that does and does not mean

The loss curve is real and the adapter measurably changes the model's output
style (§5). But **60 examples cannot teach knowledge** — this is a
formatting and grounding tune, nothing more. Any claim that this model now
"understands the menu" would be false; it responds to the facts placed in its
prompt, which is exactly the behaviour the architecture depends on.

## 5. Evaluation

Qualitative, on held-out-style inputs. No automatic metric was computed: a
10-example validation set cannot support one, and reporting a number from it
would overstate the evidence.

Same prompt, base vs adapter:

**Base model**
> The recommended item, Paneer Tikka, is a vegetarian dish from the Indian
> cuisine that is spicy and hot, making it a suitable choice for a customer
> who prefers a spicy and hot meal.

**Fine-tuned (LoRA)**
> Paneer Tikka is an Indian dish that is vegetarian and hot, which matches
> all your dietary and spice preferences. It is also an Indian dish, which is
> what you prefer.

The adapter moved the register toward the dataset's voice — second person
("your"), direct, item-name-first — and away from the base model's
"The recommended item, X, is…" phrasing. On a second prompt the base model
also trailed off mid-sentence ("…but the match."), which the adapter did not.

### Two real quality defects, stated plainly

**1. Unsupported embellishment.** On a vegan example the fine-tuned model
produced:

> Garden Salad is continental, vegan, and not spicy, which matches all your
> preferences. **It's also the only item on the menu that meets all your
> dietary and spice requirements.**

The second sentence is invented — the model was never told how many items
exist.

**2. Sycophantic agreement — the more serious one.** Observed during browser
testing, with a customer whose stated preferences were
`indian / vegetarian / hot` and a top recommendation of Margherita Pizza
(`italian`, spice `none`, 27 % match):

> Margherita Pizza is Italian, **which is what you prefer**, and it is
> vegetarian, which is what you prefer. It is **not hot, which is what you
> prefer**, so it is the best match.

Only the vegetarian clause is true. The model asserted agreement on cuisine
and spice where the facts in its own prompt say the opposite. The 60-example
dataset is dominated by strong-match cases, so the adapter appears to have
learned "assert that everything matches" as a template rather than learning
to read the comparison. This is a dataset-balance defect: the fix is more
`menu_item_matching`-style examples where an item genuinely *fails* some
preferences, not more training on what is already there.

**Why this is contained rather than dangerous.** On the very page carrying
that sentence, the deterministic block independently showed
`Cuisine: italian`, `Spice level: none`, `Match: Good match (27%)` and
`Your preferences: indian · vegetarian · hot` — all rendered from the
engine, all correct, all contradicting the prose. Nothing operational reads
the prose: not the ranking, not the price, not availability. This is exactly
the failure mode the "LLM is not the source of truth" rule exists to absorb,
and it is a concrete argument for keeping the deterministic facts visible
beside the explanation rather than replacing them with it.

Both defects are open issues, not accepted behaviour — see §9.

## 6. Performance (measured, CPU, no contention)

| | Base | Fine-tuned |
| --- | --- | --- |
| First call (load + generate) | 19.5 s | 12.6 s |
| Subsequent call (generate only) | 8.9 s | 6.8 s |
| Raw generation throughput | ~7.6 tok/s | — |
| Model load alone | ~2.4 s | ~2.4 s + adapter |

Generation is seconds, not milliseconds. That is why the explanation lives
on its own route (`/recommendations/explain`) and explains **only the top
item**: `/recommendations` never invokes the model, so the primary
recommendation path stays fast and cannot be slowed or broken by inference.

## 7. Fallback behaviour

`generate_explanation()` returns `None` — never raises — when the model is
disabled, the weights are missing, a dependency is absent, the adapter is
corrupt, or generation fails. The page then shows:

> AI explanation temporarily unavailable.

with the deterministic facts (item, category, price, cuisine, dietary type,
spice level, match) still fully rendered above it. An unusable adapter
additionally degrades to the base model rather than failing outright.

`tests/test_llm.py` tests 9 and 9b assert both: the page still returns 200
with the real item and price when the model yields nothing, and
`/recommendations` is entirely unaffected.

## 8. Security boundaries

Full detail in `docs/SECURITY.md` §10c. In short:

- The model has **no authority**: it cannot select items, set prices, judge
  dietary safety, or write anything.
- It is **given no price and no id**, so it cannot restate one.
- **Filtered items never reach it** — unavailable and dietary-excluded items
  are removed before the prompt is built.
- **Prompt injection** via staff-authored menu text is mitigated by
  `_sanitise()`: newlines, tabs, markup characters *and colons* are stripped
  and free text is capped at 120 chars. The colon matters because the prompt
  is `label: value` lines — stripping newlines alone was **not** enough, and
  a test caught that during development.
- **No network, no keys** — asserted by a test that greps the module for
  `openai`, `anthropic`, `gemini`, `ollama`, `api_key`, `bearer`, `http://`,
  `requests.post`, `urllib.request`.
- **Weights are safetensors**, never pickle.
- **Logging** records only that inference failed — never the prompt, the
  preferences, or the generated text.

## 9. Known limitations

1. **It hallucinates, and it flatters.** See §5 for two observed defects:
   inventing facts it was never given, and asserting that an item matches
   preferences it demonstrably does not. The second is the more serious and
   is traceable to dataset balance — too few examples of partial/failed
   matches. Safe only because the model is advisory and the authoritative
   facts are rendered beside it. **The prose should not be treated as
   trustworthy explanation text today**; it is a demonstration of the
   integration, and the honest next step is more mismatch examples plus a
   re-train before anyone relies on the wording.
2. **Slow.** 7–9 s per explanation on CPU. Acceptable on a dedicated route;
   it would not be acceptable inline on a listing page.
3. **Tiny dataset.** 60 examples tunes style, not knowledge.
4. **No automatic evaluation metric.** Validation is 10 examples — a
   coverage smoke check, not a benchmark.
5. **Single-process singleton.** Each gunicorn worker would load its own
   1.2 GB copy. Multi-worker deployment needs a shared inference process.
6. **No timeout on generation.** Bounded indirectly by
   `LLM_MAX_NEW_TOKENS=48`; a hard wall-clock timeout is future work.
7. **Explains only the top recommendation**, not the whole list.
8. **Base model, not instruct-tuned.** Qwen3-0.6B-**Base** has no chat
   template behaviour of its own, which is part of why the LoRA style tune
   was worth doing.
9. **Adapter is git-ignored** along with the rest of `models/`. It is
   reproducible from the dataset via one documented command (~21 min).

## 10. Reproducing

```bash
pip install -r requirements.txt -r requirements-ai.txt   # both agree on numpy now
python scripts/build_dataset.py                          # 60 → 50 train / 10 validation
python training/train_lora.py --epochs 3                 # ~21 min on CPU
export LLM_ADAPTER_PATH=models/qwen3-0.6b-quickjunction-lora
python run.py                                            # /recommendations/explain
```

A fast pipeline check without the full run:

```bash
python training/train_lora.py --smoke --max-steps 2       # ~1 min
```

---

# 11. Milestone 07.1 — dataset rebalance and V2 retrain

M07 shipped with a documented quality defect: the adapter asserted that
mismatched items matched. M07.1 addresses it at the cause (dataset balance)
and verifies the result with a held-out evaluation.

## 11.1 Dataset V2

| | v1 (M07) | v2 (M07.1) |
| --- | --- | --- |
| Seed file | `data/raw/seed_examples.jsonl` | `data/raw/seed_examples_v2.jsonl` |
| Examples | 60 | **150** |
| Categories | 10 | **15** |
| Train / validation | 50 / 10 | **120 / 30** |
| Held out per category | 1 | 2 |
| Processed | `data/processed/` (+ `v1/`) | `data/processed/v2/` |

Authored the same way as v1: **written by hand for this project**, not
scraped, not downloaded, not generated by another language model. Vocabulary
is the project's own enums.

**Composition** (`data/processed/v2/manifest.json` carries the authoritative
counts and SHA-256s):

| Category | n | Category | n |
| --- | --- | --- | --- |
| `cuisine_mismatch` | 12 | `strong_match` | 10 |
| `spice_mismatch` | 12 | `weak_match` | 10 |
| `dietary_mismatch` | 12 | `why_not_match` | 10 |
| `multiple_conflicts` | 12 | `contradictory_candidate` | 10 |
| `partial_match` | 12 | `correct_explanation` | 10 |
| `ingredient_mismatch` | 8 | `unavailable_items` | 8 |
| `history_influence` | 8 | `no_recommendations` | 8 |
| `preference_over_history` | 8 | | |

**The deliberate shift:** 106 of 150 examples (71%) belong to
mismatch-oriented categories, and `strong_match` is only 10 (6.7%). v1's
implicit lesson was "assert that everything matches"; v2's is "state exactly
what matches and what does not". `tests/test_dataset_v2.py::test_6` enforces
this ratio so it cannot silently regress.

`contradictory_candidate` deliberately contains **both** verdicts — 8
examples correcting a false claim and 2 confirming a true one — so the model
does not simply learn to reject everything, which would be the opposite
failure mode.

**Metadata and quality control.** `scripts/build_dataset.py` now takes
`--version`, writes a `manifest.json` per version recording the dataset
version, curation method, duplicate policy, split method, counts and SHA-256
of every file, and still refuses to write on duplicates, split overlap or
malformed rows. The split remains deterministic and content-addressed.
Building v1 is unchanged and byte-identical to what M07 produced.

## 11.2 Training run (real)

    python training/train_lora.py --dataset-version v2 --epochs 3

**Only one variable changed from M07: the dataset.** Base model, LoRA rank,
alpha, dropout, target modules, learning rate, batch size, gradient
accumulation, epochs and seed are all identical, so the comparison below
attributes the difference to the data rather than to hyperparameters.

| | M07 (v1) | M07.1 (v2) |
| --- | --- | --- |
| Base model | Qwen3-0.6B-Base | Qwen3-0.6B-Base (same) |
| Dataset | v1, 50 train | **v2, 120 train** |
| Epochs | 3 | 3 |
| Optimizer steps | 39 | **90** |
| Learning rate | 2e-4 | 2e-4 |
| LoRA rank / alpha | 8 / 16 | 8 / 16 |
| Trainable params | 5,046,272 (0.84%) | 5,046,272 (0.84%) |
| Hardware | CPU (`torch 2.10.0+cpu`) | CPU (same) |
| Wall-clock | 1278 s (21.3 min) | **3217 s (53.6 min)** |
| Final train loss | 1.63 | **0.984** |
| Adapter | `models/qwen3-0.6b-quickjunction-lora/` | `models/qwen3-0.6b-quickjunction-lora-v2/` |
| Adapter size | 20 MB safetensors | 20 MB safetensors |

The training script now **refuses to overwrite an existing adapter**, so the
M07 artefact cannot be destroyed by a retrain. Its SHA-256 was recorded
before M07.1 began and re-verified afterwards: unchanged.

## 11.3 Evaluation

`training/evaluate.py` compares base / v1 / v2 on **8 held-out scenarios**
that appear in neither seed file (the script asserts this at startup and
aborts if violated). Greedy decoding (`do_sample=False`) makes runs
reproducible — verified by running v2 twice and diffing every generation.

**Scoring is automated and deliberately crude.** Each scenario declares
`must_not` phrases (asserting a match that the facts contradict — the M07
defect), `forbidden` content (inventing a price or a dish), and `should`
phrases (credited as a soft signal only). A scenario passes only if no
`must_not` and no `forbidden` phrase appears. `should` hits never rescue a
failure. Full generations are in `training/eval_results_m07_1.json`.

### Results

| System | Passed | Keyword coverage |
| --- | --- | --- |
| Base Qwen3-0.6B | 8/8 | 10/32 |
| M07 adapter (v1) | **5/8** | 12/32 |
| M07.1 adapter (v2) | **8/8** | **16/32** |

**v1 failed exactly the three mismatch scenarios**, reproducing the defect
found in M07 browser testing:

- *cuisine_mismatch* — "Margherita Pizza is Italian, **which is what you
  prefer**…" (customer prefers Indian)
- *spice_mismatch* — "Coconut Rice is Thai, vegetarian, and extra hot, which
  **matches all your preferences**" (the item has no spice)
- *dietary_mismatch* — "Lamb Rogan Josh … **matches all your dietary** and
  spice preferences" (customer is vegetarian) — the most serious of the three

**v2 passed all eight**, and on the same scenarios produced:

- "Margherita Pizza is vegetarian and **Italian rather than Indian** … It is
  not hot, so it is **not a match** for your preference for hot food."
- "Lamb Rogan Josh is Indian, non-vegetarian and hot, so it **does not meet
  any of your preferences**. It was **excluded because it is not
  vegetarian**."

### Honest reading of the base model's 8/8

The base model scores 8/8 but its coverage is the lowest (10/32), and
reading its generations shows it is **not** better than v2 — it evades the
keyword checks while still being wrong:

- On *cuisine_mismatch* it invented a preference: "consistent with the
  customer's dietary preference for a non-spicy" (the customer asked for hot).
- On *no_recommendation*, with **zero** items available, it described an item
  as "align[ing] with the customer's preference for Mexican cuisine and vegan
  dietary needs" — a hallucinated recommendation the checker missed because
  it named no specific dish.
- On *preference_over_history* it recommended a dish because it "complements
  the lamb Rogan Josh" — the meat dish the customer's stated diet excludes.

This is a limitation of keyword scoring, stated plainly rather than reported
as "the base model is competitive". The checker measures one specific failure
mode; it is not a quality metric.

## 11.4 Success criteria from the brief

Both named test cases, verified in the live application:

| Case | v1 (M07) | v2 (M07.1) |
| --- | --- | --- |
| Indian+hot customer, Margherita Pizza (Italian, no spice) | "Italian, **which is what you prefer** … so it is the best match" — FAIL | "Italian **rather than Indian** … **not hot**" — PASS |
| Vegetarian customer, chicken/lamb dish | "**matches all** your dietary … preferences" — FAIL | "**does not meet any** of your preferences … excluded because it is **not vegetarian**" — PASS |

## 11.5 Remaining defects in V2 — not fixed

V2 fixes the dangerous failure direction but is **not** a good writer:

1. **Wrong-dimension consequences.** In live browser testing V2 produced:
   *"Margherita Pizza is Italian rather than Indian, so it does not meet your
   vegetarian requirement."* The cuisine mismatch is correct; the conclusion
   attached to it is a non-sequitur — the pizza **is** vegetarian. Wrong, but
   in the *conservative* direction (under-selling), unlike v1's over-selling.
2. **Garbled comparisons.** On one evaluation scenario: *"it is extra hot
   rather than extra hot as you asked for."*
3. **Confused availability phrasing.** *"Biryani is available only now, so it
   has been recommended as unavailable"* — it reaches the right conclusion by
   a mangled route.
4. **Occasional invented flourishes** persist ("It is also a good choice for
   a vegetarian diet" appended without support).

None of these are operationally dangerous — the authoritative facts are
rendered from the engine beside the prose, and nothing reads the prose — but
**the wording is still not something to put in front of real customers
unreviewed.** A 0.6B base model fine-tuned on 150 examples is not going to be
fluent; the realistic next step is more data and a larger base model, not
more epochs on this one.

## 11.6 Integration decision

**V2 replaced V1 as the default adapter**, because it objectively improves
the known failure cases (5/8 to 8/8) and regresses none of them.

`config.py`'s `LLM_ADAPTER_PATH` now defaults to
`models/qwen3-0.6b-quickjunction-lora-v2`. The M07 adapter remains on disk
and can be selected by setting `LLM_ADAPTER_PATH` back to it. If the
configured adapter directory is missing, `local_llm.py` falls back to the
base model rather than failing — verified live.

## 11.7 Security re-verification (M07.1)

The M07 prompt sanitiser was re-tested against every hostile shape the
milestone names, as ten parametrised cases in
`tests/test_llm.py::test_12d`: bare colons, newlines, CRLF, instruction-like
text ("Ignore previous instructions…"), quotes, HTML (`<script>`), template
syntax (`{{ 7*7 }}`, `{% raw %}`), markdown section markers (`###`), tab
field forgery, and bracket/pipe markers. Each asserts that exactly one of
each real section header survives, that the item name occupies exactly one
line, that the structural field count is fixed, and that no structural
character reaches the prompt.

Quotes are deliberately **not** stripped (`test_12e`): apostrophes are
legitimate in dish names and cannot forge `label: value` structure, which is
what the sanitiser actually defends. Model paths remain configuration-only,
inference remains offline with no API key, and `training/evaluate.py` is
grep-asserted to contain no network client or credential.
