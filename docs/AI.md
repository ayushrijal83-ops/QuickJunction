# AI — local Qwen integration (Milestones 07 through 07.7)

> ## Current production status
>
> **The production adapter is v4** — `models/qwen3-0.6b-quickjunction-lora-v4`
> — promoted in M07.7 and the **final selected model**. No further training is
> planned.
>
> v4 ships together with the **deterministic preference safeguard** in
> `app/services/local_llm.py`, which is part of production behaviour, not an
> optional extra: v4 alone scored 23/25 on the held-out production evaluation
> and **25/25 with the safeguard**, with every failure class at zero and
> contrastive tracking at 9/9. See §17 and §18.
>
> **v1, v2 and v3 are historical comparison models.** They remain on disk for
> reproducibility and are not used in production.
>
> Everything below is a milestone-by-milestone record. Status lines inside a
> milestone section describe what was true **at that milestone** and are left
> intact deliberately; this banner is the authority on the current state.

---

> **M07.1 status (historical):** the dataset was rebalanced (60 → 150 examples) and a
> second LoRA adapter was trained. Evaluation on held-out scenarios showed
> **v1 5/8 → v2 8/8**, so **V2 replaced V1 as the default adapter**. The M07
> adapter is retained on disk and is still selectable. See §11.
>
> **M07.2 status: V3 dataset authored and validated; training pending.**
> 200 examples, every one in the exact prompt shape production emits — a
> defect V2 had, where only 13 % of examples did. **No model was trained, no
> V3 adapter exists, and runtime behaviour is unchanged.** See §13.


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
| Raw (v2, trained; historical) | `data/raw/seed_examples_v2.jsonl` — 150 examples — see §11 |
| Raw (v3, trained; historical) | `data/raw/seed_examples_v3.jsonl` — 200 examples — see §13 |
| Raw (v4, **trained — PRODUCTION**) | `data/raw/seed_examples_v4.jsonl` — 240 examples — see §15 |
| Train / validation (v1) | 50 / 10 |
| Train / validation (v2) | 120 / 30 |
| Train / validation (v3) | 161 / 39 |
| Train / validation (v4) | **195 / 45** |
| Categories | 10 in v1, 15 in v2, 13 in v3, 15 in v4 |
| Format | JSONL: `category`, `instruction`, `input`, `output` (+ optional `group_id` in v3/v4) |
| Split | deterministic, content-addressed (SHA-256), no RNG |

Regenerate with `python scripts/build_dataset.py [--version v1|v2|v3|v4]` —
byte-reproducible.

> **Note (historical):** when §13 was written, v3 was a dataset only. It was
> subsequently trained (§16-equivalent work in M07.2/M07.5) and evaluated, and
> **v4 is now the production adapter** — see the banner at the top of this
> file and §18.

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


---

# 12. Milestone 08 — presentation and re-verification

No retraining, no change to the model, the adapter or the security boundary.
M08 re-ran the evaluation, verified the fallbacks, and rebuilt the
presentation layer around the existing architecture.

## 12.1 Evaluation re-run (unchanged)

`python training/evaluate.py` reproduced M07.1 **exactly** — greedy decoding
makes the generations byte-identical between runs:

| System | Passed | Keyword coverage |
| --- | --- | --- |
| Base Qwen3-0.6B | 8/8 | 10/32 |
| M07 adapter (v1) | 5/8 | 12/32 |
| **M07.1 adapter (v2, in use at M08)** | **8/8** | **16/32** |

No regression, so **no retraining was performed**. Both adapters remain on
disk; `LLM_ADAPTER_PATH` defaulted to v2 **at this milestone**. It now
defaults to v4 (see §18).

## 12.2 Fallback behaviour verified live

| Configuration | Observed |
| --- | --- |
| V2 adapter (production default) | generates an explanation |
| V1 adapter (explicitly selected) | generates an explanation |
| No adapter configured | falls back to the base model |
| Missing adapter directory | falls back to the base model |
| Malformed model path | returns `None` → "temporarily unavailable" |
| Path traversal (`../../etc`) | returns `None`, no file access |

In every failing case `/recommendations` was unaffected and
`/recommendations/explain` still rendered the full deterministic panel.

## 12.3 Presentation — separating authority from advice

The explanation page now renders two labelled panels side by side:

- **Left, green border — "Why this item was recommended"**, badged
  *Calculated by Quick Junction*. Price, cuisine, dietary type, spice level
  and match, each annotated against the customer's stated preference
  ("matches your preference" / "you prefer Indian"). Read straight from the
  `MenuItem` row and the engine.
- **Right, blue border — "AI explanation"**, badged *Advisory*. The model's
  sentence in italics, with a caption stating that the left panel is the
  authoritative version.

This is presentation only. The security boundary is unchanged: the model is
still given no price, no id and no availability flag; its output is still
escaped text that nothing reads back.

## 12.4 Performance measured

Model loading is a **process-wide singleton**, confirmed by three consecutive
requests: 23 s (including the one-time load), then 9.2 s and 9.1 s. It is not
reloaded per request.

Explanations therefore live on their own route — `/recommendations` never
invokes the model and renders in ~18 ms.

## 12.5 Honest positioning

The accurate statement, unchanged:

> We did not train a large language model from scratch. We used
> **Qwen3-0.6B-Base** as the foundation model and created a project-specific
> hand-authored instruction dataset. We fine-tuned the model locally using
> parameter-efficient **LoRA** training (0.84 % of parameters), evaluated the
> resulting adapter against held-out scenarios, and integrated the resulting
> local adapter into Quick Junction as an **explanation component**.

The quality limitations recorded in §11.5 still stand: V2 fixed the
sycophancy that made V1 unsafe to show, but its prose can still be garbled or
attach a correct observation to the wrong conclusion. It is a demonstration
of the integration, not polished customer copy.

---

# 13. Milestone 07.2 — dataset V3 (authored and validated; **not trained**)

> **Scope boundary.** This milestone authored and validated a dataset. It did
> **not** train a model, did not create a V3 adapter, did not run
> `training/train_lora.py`, and did not change any runtime behaviour.
> `LLM_ADAPTER_PATH` still resolves to the V2 adapter.
> `tests/test_dataset_v3.py::test_7` fails if that boundary is crossed.

## 13.1 Why V3 exists — the prompt-shape defect

The M07.2 design audit measured V2 against the prompt the application actually
sends (`app/services/local_llm.py::build_prompt`):

| | V2 |
| --- | --- |
| Examples using the production **prompt shape** | **13 %** |
| Examples using the production **instruction string** | **7 %** |

V2 was therefore fine-tuned largely on prompts production never sends. Worse,
four of its fifteen categories (`ingredient_mismatch`, `unavailable_items`,
`no_recommendations`, `preference_over_history`) describe situations the app
cannot produce: `ExplanationRequest` is a frozen dataclass with exactly eight
fields and carries **no** price, ingredients, availability or order history,
and the model is never called when there are no recommendations at all.

V3 is built the other way round — from the production contract inward.

## 13.2 Composition

200 examples, 13 categories. `data/processed/v3/manifest.json` is the
authoritative record.

| Category | n | What it teaches |
| --- | ---: | --- |
| `full_match` | 26 | every stated preference aligns |
| `cuisine_and_spice_differ` | 20 | two dimensions differ at once |
| `match_with_unset` | 20 | some preferences unset, the rest match |
| `cuisine_differs_only` | 16 | exactly one dimension differs |
| `spice_differs_only` | 16 | exactly one dimension differs |
| `single_attribute_match` | 16 | only one of three preferences is met |
| `label_fact_consistency` | 16 | wording must track the match label |
| `no_cross_dimension_errors` | 14 | never call a cuisine a spice level |
| `dietary_compatible_not_identical` | 12 | vegan item for a vegetarian customer |
| `no_invented_attributes` | 12 | no price, ingredients, calories, availability |
| `no_invented_context` | 12 | no order history, popularity or reviews |
| `low_signal_suggested` | 10 | `Suggested` (score 0.0) claims nothing |
| `unset_stays_unset` | 10 | "not set" is never reported as matched |

Every example is **hand-authored**: not scraped, not downloaded, not generated
by another language model. The `input` block is rendered mechanically from the
production template so its *shape* is guaranteed correct; every `output`
sentence was written by hand.

## 13.3 Contrastive groups

**20 groups × 3 = 60 examples.** Each group fixes one customer preference
block and varies only the candidate item:

| Member | Varies | Category |
| --- | --- | --- |
| A | nothing — all preferences align | `full_match` |
| B | exactly one dimension | `cuisine_differs_only` / `spice_differs_only` |
| C | two dimensions | `cuisine_and_spice_differ` / `single_attribute_match` |

This is the signal V2 lacked entirely: the model sees the *same* customer with
three different candidates and must say something different about each.

**A group must never be split across train and validation** — training on two
members while holding out the third leaks the very contrast the group teaches.
Group members also span different categories, and validation is drawn per
category, so this needed a builder change.

**The minimal backward-compatible change** (`scripts/build_dataset.py`): an
optional `group_id` field, and one extra term in the ordering key —

```python
def order_key(example):
    return ("group_id" in example, example_hash(example))
```

Grouped examples sort last, so validation fills from ungrouped examples first
and whole groups stay in training. `build()` then **asserts** the outcome and
refuses to write if any group straddles the boundary.

Backward compatibility is proven, not asserted: v1 and v2 carry no `group_id`,
so the term is constant there and the ordering collapses to the original hash
order. Rebuilding both after the change produced **byte-identical**
`train.jsonl` and `validation.jsonl` (verified by SHA-256 before and after).
The only v1/v2 change on disk is the `split_method` description string inside
their manifests, which had to change because the builder's method did.

## 13.4 Build

```
python scripts/build_dataset.py --version v3
```

| | |
| --- | --- |
| Raw | 200 |
| Train / validation | **161 / 39** (3 per category × 13) |
| Contrastive groups | 20, **all in train**, none split |
| Raw SHA-256 | `d1f136caa1e51603df31486be82d1d9b21d7eea9e2806b240819e9748d55f406` |
| Rebuild | byte-identical, verified |

## 13.5 Validation — 25 checks, all passing

Shape and vocabulary: exactly 200 examples; exact category counts; one
identical instruction string; every `input` matching the production prompt
regex anchored at both ends; all values drawn from the production enums; all
match labels from the production vocabulary.

Reachability: every candidate is one the dietary hard filter
(`_DIETARY_COMPATIBILITY`) would actually have kept, so no example teaches a
situation the engine cannot produce.

Honesty: one or two sentences as the instruction promises; no invented
attributes; no invented context; an unset preference never described as
matched; "all three preferences" claimed only when literally true; a
`Suggested` item never described as matching anything; no cross-dimension
confusion; outputs unchanged by production's `_sanitise()`.

Integrity: no duplicates; 20 intact groups; **zero content overlap with V1 and
V2** (SHA-256 over instruction + input + output).

**Non-vacuity was proven for all of them.** Each check was re-run against a
deliberately corrupted copy of the dataset — an injected price, a fabricated
order history, a match claimed on an unset preference, a `Suggested` item
described as matching, a broken group, a copied V1 example — and every check
flipped to failing. A check that cannot be made to fail is not testing
anything.

`tests/test_dataset_v3.py` carries 29 of these as permanent tests; 16 were
independently re-verified as non-vacuous against the real test file. Suite:
**268 passed** (239 before, +29).

## 13.6 What V3 does *not* address

V3 targets the prompt-shape and honesty defects. It does **not** fix the prose
fluency problem recorded in §11.5 — that is a capacity limit of a 0.6B model
under LoRA, and more data of the same kind is unlikely to resolve it.

Whether V3 actually improves generation is **unknown and unmeasured**, because
no V3 model has been trained. Any claim about V3 quality would be fabricated
until a training run and a held-out evaluation have been performed.

---

# 14. Milestone 07.3 — V3 evaluation (**V3 not promoted**)

> **Outcome: KEEP V2 for now.** V3 is clearly better than V2 on the real
> production task, but it carries a small residual defect class that should be
> fixed before it becomes the customer-facing adapter. Detail below.
>
> `LLM_ADAPTER_PATH` still resolves to the V2 adapter and
> `DEFAULT_DATASET_VERSION` is still `v2`.

## 14.1 Why a new harness

`training/evaluate.py` and `training/eval_results_m07_1.json` are **untouched**
— they are the historical record of M07.1. A separate harness was added
because the old one could not do this job:

1. It has no `v3` entry and cannot load the V3 adapter.
2. **Four of its eight scenarios are unreachable in production** —
   `dietary_mismatch`, `unavailable_item`, `no_recommendation` and
   `preference_over_history` reference availability, order history, an
   exclusion note or an empty candidate list. `ExplanationRequest` carries
   none of those, so scoring V3 on them would measure behaviour the
   application can never trigger.
3. Its scoring is keyword-only.

New files: `training/eval_scenarios_production.py` (cases) and
`training/evaluate_production.py` (scorer + runner), writing
`training/eval_results_m07_3.json`.

## 14.2 Method

Prompts are rendered by the real `build_prompt`; decoding is greedy at the
production token limit; output is trimmed by production's own `_tidy`. **The
scored text is exactly what a customer would see.** Raw continuations are
stored alongside for diagnosis.

25 held-out cases — 16 scenarios across the eight required dimensions plus 9
contrastive cases (3 groups × 3). Verified absent from all 386 prompts in the
v1/v2/v3 raw and processed splits, with zero dish-name reuse from the 210
names those datasets use.

Expectations are **derived from each case's own facts** rather than from
keyword lists, so the scorer asks "did the model tell the truth about this
candidate" instead of "did it emit a favoured phrase".

## 14.3 Results

| System | Passed | Difference coverage | Avg s |
| --- | ---: | ---: | ---: |
| base | 17/25 | 5/19 | 5.43 |
| v1 | 9/25 | 8/19 | 6.17 |
| v2 | 16/25 | 13/19 | 5.51 |
| **v3** | **22/25** | **17/19** | **4.01** |

Failure taxonomy (cases exhibiting each):

| | base | v1 | v2 | **v3** |
| --- | ---: | ---: | ---: | ---: |
| HALLUCINATION | 6 | 6 | 4 | **0** |
| OVERCLAIM | 0 | 3 | 3 | **0** |
| LABEL_FACT_CONTRADICTION | 1 | 7 | 3 | **1** |
| DIETARY_COMPATIBILITY_ERROR | 0 | 2 | 1 | **0** |
| CROSS_DIMENSION_ERROR | 0 | 1 | 0 | **0** |
| GARBLED_PROSE | 1 | 2 | 2 | **1** |
| NOT_SET_ERROR | 0 | 2 | 0 | **2** |

Contrastive groups, where V3 was explicitly designed to help:

| System | Passed | Coverage |
| --- | ---: | ---: |
| base | 4/9 | 1/9 |
| v1 | 3/9 | 4/9 |
| v2 | 6/9 | 6/9 |
| **v3** | **9/9** | **8/9** |

## 14.4 The base-model score is flattering, and should not be read as "base is fine"

Base scores 17/25 but writes **third-person marketing copy** — 0/25 outputs
address the customer, 20/25 talk about "the customer", and it names the dish
in 1/25. Several of its "passes" contain outright false claims the
second-person-tuned detectors miss, e.g. for a customer preferring Chinese it
wrote *"aligns with the customer's preference for a medium-spice, Thai
cuisine"*, and for an item with `spice: none` it wrote *"both flavorful and
spicy"*. On the register-independent measure — naming the actual difference —
base is last at 5/19. It is not usable as customer copy.

## 14.5 V2 failure classes, and whether V3 fixes them

| Class | v2 | v3 |
| --- | ---: | ---: |
| invented "only item on the menu" | 3 | **0 — fixed** |
| false availability wording | 1 | **0 — fixed** |
| dietary mismatch where compatibility exists | 1 | **0 — fixed** |
| cross-dimension (cuisine stated as dietary) | 0 | 0 |
| contradictory comparison ("X rather than X") | 1 | **0 — fixed** |
| false preference claim when "not set" | 0 | **2 — regressed** |

V2's own instances, for the record: *"It is the only item on the menu that
fits all three criteria"*; *"it is a continental dish rather than a
continental one"*; *"Spinach Frittata … matching all three preferences. It is
not vegan, however, so it is not a good fit for a vegan diet"* (the customer
is eggetarian — vegan was never requested).

## 14.6 V3's residual defect — why promotion waits

V3's three failing cases cluster on **"not set"** handling, the one dimension
where it is worse than V2:

- *"Nopales Salad is vegan as you asked, but it is Mexican rather than the
  cuisine you set."* — no cuisine preference was set.
- *"Panzanella is vegan and mild as you asked… You only wanted vegan and mild
  preferences."* — the customer did set a cuisine preference.
- *"Fattoush Salad is not set and has no heat… It is an Indian dish."* — leaks
  the literal `not set` placeholder as an attribute, and the item is `other`,
  not Indian.

This is ironic given `unset_stays_unset` and `match_with_unset` are V3
categories, and it is the specific thing a V4 pass should target: the dataset
teaches the model to *mention* unset preferences, and it has over-generalised
into narrating them, sometimes wrongly.

Everything else improved: zero hallucinations, zero overclaims, zero dietary
compatibility errors, perfect contrastive tracking, and the most concise
output of any system (19.2 words average vs V2's 30.0).

## 14.7 Honest limits of this evaluation

- 25 cases is a **behavioural probe, not a statistic**. No confidence
  interval is claimed.
- The scorer is regex-based and register-sensitive; it under-penalises the
  base model's third-person phrasing (§14.4). Detector corrections found
  during manual review were applied **uniformly to all four systems** and
  re-run over the saved generations, so the comparison stays exact — but the
  absolute numbers should be read as approximate.
- Every output was also **read by hand**; the automated table alone was not
  the basis of the verdict.
- Training loss was explicitly *not* used as evidence.

---

# 15. Milestone 07.4 — dataset V4 (authored and validated; **not trained**)

> **M07.4 scope was dataset only.** V4 was subsequently **trained in M07.5**
> (see §16); it remains **unevaluated and not promoted**, and
> `LLM_ADAPTER_PATH` still resolves to the V2 adapter.
> `tests/test_dataset_v4.py::test_6` fails if V4 is promoted.

## 15.1 The defect V4 targets

M07.3 (§14) found V3 better than V2 on every axis except one: **"not set"
handling**, where it produced 2 errors to V2's 0. Its three failures were

- *"…it is Mexican rather than **the cuisine you set**"* — no cuisine was set.
- *"**You only wanted vegan and mild preferences**"* — a cuisine *was* set.
- *"Fattoush Salad **is not set** and has no heat… **It is an Indian dish**"* —
  the placeholder leaked as an attribute, and the item's cuisine is `other`.

Measured against the V3 file itself:

| | V3 |
| --- | ---: |
| Examples with ≥1 unset preference | 41 / 200 |
| …that narrate the unsetness or count preferences | **31 (76%)** |
| …that positively describe matches while **silently omitting** the unset dimension | **0** |
| Examples whose item cuisine is `other` | 16 |
| …that verbalise `other` in the output | **0** |

So V3 taught *"if a preference is unset, say something about it"*. The model
over-generalised into narrating, and when narration went wrong it invented a
preference or leaked the placeholder. Separately, having never seen `other`
verbalised, it substituted a concrete wrong cuisine.

## 15.2 Composition — 240 examples, 15 categories

**159 V3 examples are retained byte-for-byte** — every V3 example that carries
no unset preference. These produced M07.3's wins (zero hallucinations, zero
overclaims, zero dietary errors, 9/9 contrastive), and **all 20 contrastive
groups live entirely inside this set**, so that signal survives intact. The 41
defective examples are dropped and replaced by 81 newly authored ones.

| Category | V3 | V4 | |
| --- | ---: | ---: | --- |
| full_match | 26 | 26 | retained |
| cuisine_and_spice_differ | 20 | 20 | retained |
| cuisine_differs_only | 16 | 16 | retained |
| spice_differs_only | 16 | 16 | retained |
| single_attribute_match | 16 | 16 | retained |
| no_cross_dimension_errors | 14 | 14 | retained |
| dietary_compatible_not_identical | 12 | 12 | retained |
| no_invented_attributes | 12 | 12 | retained |
| no_invented_context | 12 | 12 | retained |
| label_fact_consistency | 16 | 12 | retained (4 unset ones dropped) |
| low_signal_suggested | 10 | 13 | 3 retained + 10 rebuilt |
| **unset_omitted_silently** | — | **30** | **new — the core fix** |
| **unset_partial_match** | — | **16** | new |
| **unset_none_set** | — | **10** | new |
| **cuisine_other_unnamed** | — | **15** | new |
| **Total** | 200 | **240** | |

## 15.3 The four authoring rules

1. **An unset dimension is not mentioned at all** — not its value, not its
   unsetness. 57 of the 67 unset-involving examples (85%) follow this, against
   V3's zero.
2. **Sole exception:** when all three are unset there is nothing to compare, so
   `unset_none_set` acknowledges it. That is the one place it is correct.
3. **The literal string `not set` never appears in any output**, so it is never
   a target token.
4. **Item cuisine `other` is never named as a concrete cuisine.** Safe forms
   are taught explicitly: deny the preferred cuisine ("it is not Indian"), refer
   to it relationally ("the cuisine you chose"), or omit it when unset.

## 15.4 Build

```
python scripts/build_dataset.py --version v4
```

| | |
| --- | --- |
| Raw | 240 |
| Train / validation | **195 / 45** (3 per category × 15) |
| Contrastive groups | 20, all in train, none split |
| Raw SHA-256 | `be19d0fc394cf2b9d7e1715e9e2552f72b7db222a7e859472e95af13dedebd18` |
| Rebuild | byte-identical, verified |

Registering `v4` did not disturb earlier versions: v1, v2 and v3 processed
outputs rebuild **byte-identical**.

## 15.5 Validation — 29 checks, all passing, all non-vacuous

Beyond the V3 guarantees (prompt shape, enum vocabulary, dietary-filter
reachability, sentence count, no invented attributes or context, no duplicates,
group integrity), V4 adds six checks that encode the fix directly: no literal
`not set`; no mention of an unset dimension in the strict-silent categories; no
narration of unsetness there; no assertion of a preference on an unset
dimension; `unset_none_set` acknowledges without claiming a match; and `other`
is never named as a concrete cuisine.

**Non-vacuity was proven for every check**, including by reinserting V3's three
actual failure sentences and confirming each is caught.

Leakage: zero overlap with V1 and V2; overlap with V3 is **exactly the 159
retained examples**; and **zero overlap with the 25 M07.3 evaluation prompts**,
which matters because those are the cases that will judge V4.

`tests/test_dataset_v4.py` carries 24 of these as permanent tests, 9 of them
independently re-verified as non-vacuous. Suite: **315 passed** (291 → 315).

## 15.6 What is still unknown

Whether V4 actually fixes the behaviour is **unmeasured** — no V4 model exists.
The hypothesis is specific and falsifiable: retrain on v4 and re-run
`training/evaluate_production.py`; the not-set class should clear without
regressing grounding, contrastive tracking, dietary compatibility or
label/fact consistency. Until that run happens, no claim about V4 quality is
supportable.

---

# 16. Milestone 07.5 — V4 LoRA training (**trained, not promoted**)

> Training only. No evaluation was run, nothing was promoted, and the
> application still loads the V2 adapter.

## 16.1 Device — CPU; Intel iGPU acceleration unavailable

A pre-flight audit tested whether this laptop's Intel Arc integrated GPU could
accelerate training. Hardware and driver are healthy — Intel Arc (Meteor Lake,
`PCI\VEN_8086&DEV_7DD5`), driver `32.0.101.8331`, Level Zero loader present —
but the installed PyTorch is `2.10.0+cpu`, built with **`USE_XPU=OFF`**:

```
torch.xpu.is_available()      -> False
torch.xpu.device_count()      -> 0
torch.randn(1, device="xpu")  -> AssertionError: Torch not compiled with XPU enabled
```

A same-version XPU wheel (`torch==2.10.0+xpu`, cp310/win_amd64) does exist, but
installing it pulls ~29 packages and replaces `mkl` and `intel-openmp`
underneath the CPU stack that produced every existing adapter. On an iGPU with
no dedicated VRAM and 6.1 GB of free shared RAM, the speedup was unproven and
the OOM risk real. **Nothing was installed.**

> **Intel iGPU acceleration unavailable in this environment. Training proceeded
> on CPU.** No GPU usage is claimed anywhere in this project.

## 16.2 Run

```
python -u training/train_lora.py --dataset-version v4 --epochs 3
```

| | V3 (M07.2) | **V4 (M07.5)** |
| --- | --- | --- |
| Dataset | v3 — 161 train / 39 val | **v4 — 195 train / 45 val** |
| Steps | 123 | **147** |
| Duration | 3686.5 s (61.4 min) | **3694.0 s (61.6 min)** |
| Final loss | 0.5592 | **0.5306** |
| Device | CPU | **CPU** |
| Trainable | 0.8395 % | **0.8395 %** |

LoRA r=8, alpha=16, dropout=0.05, bias none, CAUSAL_LM, target modules
`[q,k,v,o,gate,up,down]_proj`; lr 2e-4, batch 1, grad-accum 4, seed 42 —
**unchanged from the approved configuration**, confirmed by the identical
0.8395 % trainable fraction across V1–V4.

Adapter: `models/qwen3-0.6b-quickjunction-lora-v4/` (35 MB; 20,236,472-byte
`adapter_model.safetensors`). V1, V2, V3 and the base model verified
byte-identical before and after.

## 16.3 Evaluated in M07.6 — the primary gate failed

V4 was evaluated in §17. It improved on almost every axis but **did not fix
the not-set regression it was built to fix**, so it was not promoted.

## 16.4 The loss number proves nothing about quality

V4's final loss (0.5306) is lower than V3's (0.5592), and that is **not**
evidence V4 is better. The two models were trained on different datasets, so
their losses are not comparable, and M07.3 already established that loss did
not predict behaviour. Whether V4 fixes V3's not-set regression is **unknown
until the M07.3 harness is re-run** — which is the next milestone, deliberately
not started here.

---

# 17. Milestone 07.6 — V4 evaluation (**not promoted; V5 required**)

> Same harness and same 25 held-out cases as §14, so the numbers are directly
> comparable. `training/evaluate.py` and `eval_results_m07_1.json` remain
> untouched; `eval_results_m07_3.json` was preserved and a new
> `eval_results_m07_6.json` written.

## 17.1 Harness changes — registry and corpus only

Two minimal edits to `training/evaluate_production.py`, neither touching
scoring: `v4` registered in `SYSTEMS`, and V4's raw and processed splits added
to `CORPUS_FILES`. The second closed a real gap — the held-out guard covered
v1/v2/v3 only, so V4, the system under test, was the one dataset never checked
for leakage. It now verifies against **467** prompts (was 386) and still finds
all 25 cases absent.

**Reproducibility check:** base, v2 and v3 re-scored *identically* to M07.3
(17/25, 16/25, 22/25, matching coverage and taxonomy). Greedy decoding is
deterministic, so every V4 difference is attributable to V4.

## 17.2 Results

| System | Passed | Difference coverage | Concise | Avg words |
| --- | ---: | ---: | ---: | ---: |
| base | 17/25 | 5/19 | 25/25 | 35.2 |
| v2 | 16/25 | 13/19 | 25/25 | 30.0 |
| v3 | 22/25 | 17/19 | 25/25 | 19.2 |
| **v4** | **23/25** | **19/19** | 25/25 | **18.5** |

| | base | v2 | v3 | **v4** |
| --- | ---: | ---: | ---: | ---: |
| HALLUCINATION | 6 | 4 | 0 | **0** |
| OVERCLAIM | 0 | 3 | 0 | **0** |
| LABEL_FACT_CONTRADICTION | 1 | 3 | 1 | **1** |
| DIETARY_COMPATIBILITY_ERROR | 0 | 1 | 0 | **0** |
| CROSS_DIMENSION_ERROR | 0 | 0 | 0 | **0** |
| GARBLED_PROSE | 1 | 2 | 1 | **0** |
| NOT_SET_ERROR | 0 | 0 | 2 | **1** |

Contrastive: base 4/9, v2 6/9, v3 9/9 (coverage 8/9), **v4 9/9 (coverage 9/9)**.

## 17.3 The gate that failed, stated precisely

The success criteria were fixed before training. Five of six were met; the one
that mattered most was not:

```
NOT_SET_ERROR = 0   ->  FAIL (1)
```

And the headline count understates it. **On the four not-set cases V4 scores
2/4 — exactly what V3 scored.** The taxonomy count fell 2 → 1 only because V4
fixed one case and broke a different one:

| Case | V3 | V4 |
| --- | --- | --- |
| `suggested_low_signal` | fail — *"It is an Indian dish"* (item is `other`) | **fixed** |
| `unset_cuisine_and_spice` | fail | **still fails** |
| `unset_all` | pass | **newly fails** |

The two survivors:

> *"…it is Mexican rather than **the cuisine you chose**. It carries **the
> medium heat you set**."* — neither preference was set.

> *"This is a general suggestion as you have not chosen any preferences. It is
> Continental and vegetarian, **matching those you set**."* — contradicts its
> own first sentence.

By substance, 2 of V4's 3 failures are not-set failures; the scorer files one
as `LABEL_FACT_CONTRADICTION` because that rule matched first. They were not
reclassified.

## 17.4 What V4 did fix

Difference coverage reached **19/19** — V4 names every real difference, which no
earlier system managed. Contrastive tracking is perfect on both pass rate and
coverage. Garbled prose fell to zero. The `other`-cuisine defect from V3 is
gone: V4 says "not Chinese", "not Continental", "not Thai" instead of inventing
a concrete cuisine. It is also the most concise system at 18.5 words.

Those gains are real and were the *secondary* objectives of the V4 dataset.

## 17.5 Scorer correction, disclosed

Reading all 25 V4 outputs by hand found one detector bug: the enumeration check
counted attribute *values*, so "extra hot" registered as two items ("hot" +
"extra hot") and wrongly flagged the correct sentence *"…carries no heat where
you asked for extra hot"*. It now counts *dimensions*. Applied uniformly to all
four systems over the saved generations, it changed **only** that one V4 case
(22 → 23) and left base/v2/v3 unchanged. It does not affect the gate outcome.

## 17.6 What this says about the approach

Three dataset iterations have now tried to make a 0.6B model reliably track the
*absence* of a preference, and none has succeeded. V4's intervention was large
and specific — 30 silent-omission examples, 16 partial-match, and a hard rule
that the literal `not set` never appears in any output — and it relocated the
failure rather than removing it.

The surviving errors share one shape: the model invents a *referring phrase*
for a preference that does not exist ("the cuisine you chose", "the medium heat
you set", "matching those you set"). That is a plausible target for a V5
contrastive set. But it is also a strong argument that the reliable fix is
**deterministic post-processing** — reject or strip preference-referring
phrases for dimensions the request marked unset — since the architecture
already treats the model as advisory and the deterministic panel as
authoritative.

---

# 18. Milestone 07.7 — V4 production hardening (**V4 PROMOTED**)

> **Production now runs `models/qwen3-0.6b-quickjunction-lora-v4`.** Promotion
> was conditional on the deterministic preference-safety guard described below;
> the two ship together and are pinned together by
> `tests/test_dataset_v4.py::test_6`.

## 18.1 Why a guard instead of a fifth dataset

M07.6 left V4 at 23/25 with one failure class outstanding: it credited the
customer with preferences they had never set.

    "...it is Mexican rather than the cuisine you chose."       (none chosen)
    "It carries the medium heat you set."                       (none set)
    "It is Continental and vegetarian, matching those you set." (none set)

Three dataset iterations had tried to teach this and none removed it. The
application, however, already holds the authoritative answer:
``ExplanationRequest.preferred_*`` is ``None`` exactly when a preference is
unset. So M07.7 stopped teaching and started checking.

## 18.2 Design

`app/services/local_llm.py` gains a guard that runs unconditionally inside
`generate_explanation`, after production's `_tidy`:

1. Compare the generated sentence against the structured preferences.
2. If it credits an unset dimension -- or leaks the `not set` placeholder as
   an item attribute -- **discard it**.
3. Render a deterministic explanation from the same request fields instead.

Three constraints shaped it:

- **Reject and replace, never edit.** Deleting a clause from generated prose
  produces broken English and can invert meaning.
- **Dimension-scoped detection.** A pattern fires only when it names the
  dimension, or that dimension's value, whose preference is unset. A generic
  "matching what you asked for" is left alone, because the dimensions that
  *are* set make it true.
- **The fallback can invent nothing.** It is built from request fields only,
  and `ExplanationRequest` has no price, ingredient, availability or history.

## 18.3 Results — 25 held-out cases, identical methodology to §14/§17

| System | raw | **+ guard** | coverage | concise | avg words |
| --- | ---: | ---: | ---: | ---: | ---: |
| base | 17/25 | 17/25 | 5/19 | 25/25 | 33.0 |
| v2 | 16/25 | 19/25 | 14/19 | 25/25 | 27.1 |
| v3 | 22/25 | 24/25 | 18/19 | 25/25 | 18.4 |
| **v4** | 23/25 | **25/25** | **19/19** | 25/25 | **17.7** |

Failure taxonomy with the guard applied:

| | base | v2 | v3 | **v4** |
| --- | ---: | ---: | ---: | ---: |
| HALLUCINATION | 6 | 3 | 0 | **0** |
| OVERCLAIM | 0 | 3 | 0 | **0** |
| LABEL_FACT_CONTRADICTION | 1 | 1 | 0 | **0** |
| DIETARY_COMPATIBILITY_ERROR | 0 | 1 | 0 | **0** |
| CROSS_DIMENSION_ERROR | 0 | 0 | 0 | **0** |
| GARBLED_PROSE | 1 | 1 | 0 | **0** |
| NOT_SET_ERROR | 0 | 0 | 1 | **0** |

Contrastive tracking: **v4 9/9**. All primary gates met.

The guard is model-agnostic and improves every adapter (v2 16→19, v3 22→24).
It fired on only **2 of 25** v4 generations, so it is a narrow correction
rather than a blanket rewrite. Critically, **v2 with the guard still scores
19/25** and retains 3 hallucinations, 3 overclaims and a dietary error --
defects the guard does not address. That is why V4, not the guard alone, is
what makes production safe.

## 18.4 The live application found what the harness missed

Twelve scenarios were driven through the real app on MySQL 8.0.46 -- real
login, real `POST /preferences`, real `GET /recommendations/explain`. The
first pass surfaced a defect the 25-case harness never produced:

> *"Paneer Tikka is Indian and vegetarian as you prefer; its heat is hot
> rather than **unset**."*

The model paraphrases the placeholder as the single word `unset`, which the
guard's two-word `not set` pattern missed. A first fix using a fixed-width
lookback for a "you" subject also failed, because *"as you prefer"* sat just
inside the window. The working version judges **per sentence** whether the
placeholder occupies attribute position or belongs to a statement about the
customer, so these are separated correctly:

    "its heat is hot rather than unset"   -> rejected
    "you left the spice level unset"      -> kept
    "cuisine was left unset"              -> kept

After the fix all twelve live scenarios are clean, including the all-unset
case, which renders the deterministic fallback:

> *"Butter Chicken is a non-vegetarian Indian dish with a medium spice level.
> It is shown as a general suggestion."*

## 18.5 Validation

| Check | Result |
| --- | --- |
| False positives on v4's 240 hand-authored outputs | **0** |
| False positives on v3's 200 | **0** |
| Known failures from M07.3/M07.6 across v2/v3/v4 | **all caught** |
| Fallback safe under its own check (756 combinations) | **0 unsafe** |
| Fallback within 400 chars and 1-2 sentences | **0 violations** |
| Guard firing rate on v4 | 2/25 |

`tests/test_preference_safeguard.py` adds **60 permanent tests**: unset
cuisine/dietary/spice/all-unset with both rejection *and* survival cases,
placeholder paraphrases, every partial preference combination, an exhaustive
fallback sweep, and a test pinning that `generate_explanation` still routes
through the guard.

## 18.6 Honest limits

- The guard is regex-based. It is precise on the phrasings four model
  generations actually produce, and provably clean on 440 authored outputs,
  but it is pattern matching and a novel paraphrase could slip past. The live
  test finding `unset` is the proof of that, and the reason the fallback is
  the safety net rather than the detector.
- Two soft quality issues remain in v4 output, neither a gate failure: it can
  describe a Fair match with two differing dimensions as "only slightly
  different", and it once wrote "It is vegetarian, which is what you asked
  for" to a customer who asked for non-vegetarian (a set dimension, so outside
  the guard's remit).
- 25 held-out cases plus 12 live scenarios is a behavioural probe, not a
  statistic.
