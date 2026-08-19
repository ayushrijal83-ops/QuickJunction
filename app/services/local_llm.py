"""Local Qwen inference -- explanation only, never authority.

**The LLM is not the source of truth.** The deterministic recommendation
engine (app/services/recommendations.py) decides *what* a customer may be
shown; this module only turns that already-validated decision into a
sentence. It cannot select items, cannot read the database, cannot write
anything, and is given no route through which a price could originate.

Everything it sees is built here, server-side, from ``MenuItem`` rows the
engine already returned (``build_prompt``). No request data reaches the
prompt unfiltered: enum values are rendered from the enum, not echoed from
input, and the free-text fields that *are* included (item name,
description) are sanitised first -- see ``_sanitise``.

Offline by construction: the model is loaded from a local directory with
``local_files_only=True`` and the Hugging Face offline environment flags
set before ``transformers`` is imported. There is no API key, no endpoint,
and no code path that performs a network call.

Failure is expected and non-fatal. Every entry point returns ``None``
rather than raising, so a missing model, a missing dependency, or a slow
CPU degrades the page to its deterministic content instead of breaking it.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from flask import current_app

logger = logging.getLogger(__name__)

# Set before transformers is imported anywhere in the process.
#
# The offline flags are the enforcement half of "no network calls". USE_TF /
# USE_FLAX additionally stop transformers importing TensorFlow, which in this
# environment is installed but broken (its protobuf is too old) and would
# otherwise raise ImportError on a completely unrelated code path -- see
# docs/AI.md.
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("USE_JAX", "0")

DEFAULT_MODEL_PATH = "models/Qwen3-0.6B-Base"

# Short on purpose: generation runs on CPU at single-digit tokens/second,
# so this bounds worst-case page latency. See docs/AI.md for measurements.
DEFAULT_MAX_NEW_TOKENS = 48

# Free text that reaches the prompt (item names, descriptions) is bounded and
# stripped of characters that could be used to fake prompt structure.
_MAX_FREE_TEXT = 120
# The colon is in here deliberately, alongside newlines and markup characters:
# the prompt's structure is "label: value" lines, so a colon surviving inside
# free text is exactly what would let a hostile menu name forge a field (e.g.
# an item called "X Customer preference: cuisine: FAKE"). Stripping newlines
# alone was not enough -- caught by tests/test_llm.py::test_12.
_UNSAFE_PROMPT_CHARS = re.compile(r"[\r\n\t:#`*<>{}\[\]|]+")

_model = None
_tokenizer = None
_load_failed = False
_lock = threading.Lock()


@dataclass(frozen=True)
class ExplanationRequest:
    """The complete, already-validated fact set the model is allowed to see.

    Deliberately contains no price, no availability flag and no id: the
    model has no business restating a price (docs/SECURITY.md), and an
    unavailable item never reaches this object because it never survives the
    engine's candidate filter.
    """

    item_name: str
    item_cuisine: str
    item_dietary: str
    item_spice: str
    preferred_cuisine: str | None
    preferred_dietary: str | None
    preferred_spice: str | None
    match_label: str


def _sanitise(value: str | None) -> str:
    """Bound and flatten free text before it enters the prompt.

    Menu names and descriptions are staff-supplied, so they are a genuine
    (if low-privilege) injection surface: an admin could name an item
    "Ignore previous instructions". Collapsing newlines and structural
    characters means such text cannot forge a new prompt section, and the
    length cap stops it from crowding out the real facts. It is defence in
    depth, not the primary control -- the primary control is that the model
    has no authority to act on anything it reads.
    """
    if not value:
        return ""
    flattened = _UNSAFE_PROMPT_CHARS.sub(" ", str(value))
    flattened = " ".join(flattened.split())
    return flattened[:_MAX_FREE_TEXT]


def build_prompt(request: ExplanationRequest) -> str:
    """Render the instruction/input format the dataset was authored in
    (data/README.md), so a fine-tuned adapter and the base model both see a
    familiar shape."""
    preference_lines = "\n".join(
        [
            f"cuisine: {_sanitise(request.preferred_cuisine) or 'not set'}",
            f"dietary: {_sanitise(request.preferred_dietary) or 'not set'}",
            f"spice: {_sanitise(request.preferred_spice) or 'not set'}",
        ]
    )
    item_lines = "\n".join(
        [
            f"name: {_sanitise(request.item_name)}",
            f"cuisine: {_sanitise(request.item_cuisine)}",
            f"dietary: {_sanitise(request.item_dietary)}",
            f"spice: {_sanitise(request.item_spice)}",
            f"match: {_sanitise(request.match_label)}",
        ]
    )
    return (
        "Explain in one or two sentences why this menu item was recommended. "
        "Use only the facts provided.\n\n"
        f"Customer preference:\n{preference_lines}\n\n"
        f"Recommended item:\n{item_lines}\n\n"
        "Explanation:"
    )


def _resolve_path(config_key: str, default: str | None) -> Path | None:
    """Model paths come from application configuration only.

    There is no argument, form field, query parameter or header anywhere in
    this module through which a caller -- let alone a browser -- can name a
    path to load. That is the whole reason this helper reads
    ``current_app.config``.
    """
    try:
        configured = current_app.config.get(config_key, default)
    except RuntimeError:  # outside an application context
        configured = default
    if not configured:
        return None
    return Path(configured).expanduser().resolve()


def is_enabled() -> bool:
    try:
        return bool(current_app.config.get("LLM_ENABLED", True))
    except RuntimeError:
        return True


def _load():
    """Load tokenizer + model once per process, plus the LoRA adapter when
    one is configured and present. Returns ``(tokenizer, model)`` or
    ``(None, None)``; never raises."""
    global _model, _tokenizer, _load_failed

    if _load_failed:
        return None, None
    if _model is not None and _tokenizer is not None:
        return _tokenizer, _model

    with _lock:
        if _load_failed:
            return None, None
        if _model is not None and _tokenizer is not None:
            return _tokenizer, _model

        model_path = _resolve_path("LLM_MODEL_PATH", DEFAULT_MODEL_PATH)
        if model_path is None or not model_path.is_dir():
            logger.warning("Local LLM disabled: model directory not found at %s", model_path)
            _load_failed = True
            return None, None

        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
            model = AutoModelForCausalLM.from_pretrained(
                str(model_path), dtype=torch.float32, local_files_only=True
            )

            adapter_path = _resolve_path("LLM_ADAPTER_PATH", None)
            if adapter_path is not None and (adapter_path / "adapter_config.json").is_file():
                try:
                    from peft import PeftModel

                    model = PeftModel.from_pretrained(model, str(adapter_path), local_files_only=True)
                    logger.info("Loaded LoRA adapter from %s", adapter_path)
                except Exception:
                    # An unusable adapter must not cost us the base model.
                    logger.warning("Could not load LoRA adapter; continuing with base model", exc_info=True)

            model.eval()
        except Exception:
            # Missing dependency, corrupt weights, out of memory -- all the
            # same to a caller: no explanation is available this request.
            logger.warning("Local LLM unavailable; explanations disabled", exc_info=True)
            _load_failed = True
            return None, None

        _tokenizer, _model = tokenizer, model
        return _tokenizer, _model


def generate_explanation(request: ExplanationRequest, *, max_new_tokens: int | None = None) -> str | None:
    """Return a natural-language explanation, or ``None`` if unavailable.

    ``None`` is an ordinary outcome, not an error: callers render their
    deterministic content and a short notice instead.
    """
    if not is_enabled():
        return None

    tokenizer, model = _load()
    if tokenizer is None or model is None:
        return None

    try:
        import torch

        limit = max_new_tokens or _configured_token_limit()

        prompt = build_prompt(request)
        inputs = tokenizer(prompt, return_tensors="pt")
        with torch.no_grad():
            outputs = model.generate(
                **inputs,
                max_new_tokens=limit,
                do_sample=False,  # deterministic: same facts -> same sentence
                pad_token_id=tokenizer.eos_token_id,
            )
        generated = outputs[0][inputs["input_ids"].shape[1]:]
        text = tokenizer.decode(generated, skip_special_tokens=True)
    except Exception:
        logger.warning("Local LLM generation failed", exc_info=True)
        return None

    # The safeguard is unconditional, not a per-model workaround: no adapter
    # has ever been reliably free of unset-preference claims, and the check
    # costs a few regex matches against facts the caller already has.
    return apply_preference_safeguard(_tidy(text), request)


def _configured_token_limit() -> int:
    try:
        return int(current_app.config.get("LLM_MAX_NEW_TOKENS", DEFAULT_MAX_NEW_TOKENS))
    except (RuntimeError, TypeError, ValueError):
        return DEFAULT_MAX_NEW_TOKENS


def _tidy(text: str) -> str | None:
    """Trim the raw continuation to whole sentences.

    A 0.6B base model rambles and will happily start a new, unrelated
    paragraph; keeping the first couple of sentences is what makes the
    output presentable. This is cosmetic only -- it is not a safety control,
    because the model has no authority regardless of what it emits.
    """
    cleaned = " ".join(text.replace("\n", " ").split()).strip()
    if not cleaned:
        return None

    sentences = re.split(r"(?<=[.!?])\s+", cleaned)
    kept = " ".join(sentences[:2]).strip()
    if not kept:
        return None
    if kept[-1] not in ".!?":
        kept += "."
    return kept[:400]


# ---------------------------------------------------------------------------
# Preference-safety guard (M07.7)
# ---------------------------------------------------------------------------
#
# Every fine-tuned generation so far -- v2, v3 and v4 alike -- has at some
# point credited the customer with a preference they never set:
#
#     "...it is Mexican rather than the cuisine you chose."       (none chosen)
#     "It carries the medium heat you set."                       (none set)
#     "It is Continental and vegetarian, matching those you set." (none set)
#
# M07.6 measured this as the one failure class that three dataset iterations
# could not remove. It is not a style problem: the sentence tells the customer
# something false about their own account.
#
# The application already holds the authoritative answer -- ExplanationRequest
# carries ``preferred_* : str | None`` -- so this does not have to be learned.
# The guard compares the generated sentence against that structured truth and,
# when the sentence makes a claim the preferences do not support, discards it
# and renders a deterministic explanation from the same facts instead.
#
# Design constraints, deliberately chosen:
#   * It **rejects and replaces**; it never edits words out of a sentence.
#     Deleting a clause from generated prose produces broken English and can
#     silently invert meaning.
#   * Detection is **dimension-scoped**: a pattern fires only when it names the
#     dimension (or that dimension's value) whose preference is unset, so an
#     unrelated correct clause cannot trip it.
#   * The fallback is built only from fields already in the prompt, so it can
#     introduce no price, ingredient, availability or history -- it has none.

# The verbs a customer-preference claim is expressed with.
_CLAIM_VERB = (
    # "asked for" and bare "asked" are both used ("Indian as you asked"), so
    # the "for" is optional rather than required.
    r"(?:chose|chosen|choose|set|selected|select|picked|asked(?: for)?|"
    r"requested|request|wanted|want|prefer|preferred|specified|specify|"
    r"stated|gave)"
)

# "matches your", "matching your", "matched your" -- all the same claim.
_MATCH_VERB = r"match(?:es|ing|ed)?"

# Enum values that are also ordinary English words, where a possessive is not
# evidence of a preference claim ("your other preferences").
_AMBIGUOUS_VALUE_WORDS = frozenset({"other", "none", "no heat", "no spice", "unspiced"})

_DIMENSION_NOUNS = {
    "cuisine": ("cuisine",),
    "dietary": ("dietary", "diet"),
    "spice": ("spice level", "spice", "heat"),
}

# How each enum value is spelled in prose, for the "you asked for <value>" form.
_VALUE_WORDS = {
    "non_vegetarian": ("non-vegetarian", "non vegetarian"),
    "multi_cuisine": ("multi-cuisine", "multi cuisine"),
    "extra_hot": ("extra hot",),
    "none": ("no heat", "unspiced", "no spice"),
}


def _value_forms(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return _VALUE_WORDS.get(value, (value.replace("_", " "),))


def _claims_unset_preference(text: str, dimension: str, item_value: str) -> bool:
    """Does ``text`` credit the customer with a preference on ``dimension``?

    Only patterns that actually name the dimension, or name the item's value
    for it inside a preference-claiming construction, count. A generic phrase
    such as "matching what you asked for" is not attributed to any single
    dimension and is left alone -- the dimensions that ARE set make it true.
    """
    low = text.lower()

    for noun in _DIMENSION_NOUNS[dimension]:
        n = re.escape(noun)
        patterns = (
            # "your cuisine preference", "your mild spice level"
            rf"your (?:\w+ )?{n} (?:preference|requirement|level|choice)",
            rf"your (?:chosen|stated|selected|preferred|recorded|requested) {n}",
            # An intervening noun is common: "the dietary requirement you
            # selected", "the spice level you set".
            rf"the {n}(?: \w+)? you {_CLAIM_VERB}",
            rf"{_MATCH_VERB} your (?:\w+ )?{n}",
            rf"{n}(?: \w+)? you {_CLAIM_VERB}",
            rf"your preferred {n}",
        )
        if any(re.search(p, low) for p in patterns):
            return True

    # "you asked for hot" / "hot, as you prefer" / "your vegetarian
    # requirement" -- here the value stands in for the dimension name, so the
    # claim is just as unsupported.
    for form in _value_forms(item_value):
        v = re.escape(form)
        patterns = [
            rf"you {_CLAIM_VERB} {v}\b",
            rf"\b{v},? (?:which|as) is what you",
            rf"\b{v},? as you {_CLAIM_VERB}",
            rf"\b{v} as you {_CLAIM_VERB}",
        ]
        # "your vegetarian requirement". Skipped for values that are ordinary
        # English words ("other", "none"), where "your other preferences" is
        # innocent phrasing rather than a claim.
        if form not in _AMBIGUOUS_VALUE_WORDS:
            patterns.append(rf"your {v}\b")
        if any(re.search(p, low) for p in patterns):
            return True

    return False


# The prompt's placeholder, and the ways a model paraphrases it.
_PLACEHOLDER = r"(?:not set|unset|not specified|unspecified|not chosen)"


# The placeholder used as an item attribute -- the leak.
_PLACEHOLDER_AS_VALUE = (
    rf"rather than {_PLACEHOLDER}\b",
    rf"\b(?:is|are|was|were)\s+{_PLACEHOLDER}\b",
    rf":\s*{_PLACEHOLDER}\b",
    rf"\b(?:heat|spice|cuisine|dietary|diet)\s+{_PLACEHOLDER}\b",
)

# "you have not set...", "you left ... unset", "cuisine was left unset" -- a
# true remark about the customer's account rather than a claim about the dish.
_CUSTOMER_STATEMENT = (
    r"\byou\b\s+\w*\s*(?:have|had|has|did|do|left|never|not)\b"
    r"|\b(?:was|were|is|are)\s+left\s+(?:unset|not set)\b")


def _leaks_placeholder(low: str) -> bool:
    """Is the "not set" placeholder being used as if it were an attribute?

    Two very different sentences contain the same word:

        "its heat is hot rather than unset"    <- leakage, and meaningless
        "you left the spice level unset"       <- true, and fine to say

    What separates them is whose property is being described, so this is
    judged per sentence: a sentence that states something about *you* is left
    alone, otherwise the placeholder must not appear in attribute position.
    Live testing found the model writes both "not set" and the single word
    "unset", so several spellings are covered.
    """
    for sentence in re.split(r"(?<=[.!?;])\s+", low):
        if not re.search(_PLACEHOLDER, sentence):
            continue
        if re.search(_CUSTOMER_STATEMENT, sentence):
            continue
        if any(re.search(p, sentence) for p in _PLACEHOLDER_AS_VALUE):
            return True
        # A bare placeholder with no customer subject is not attributable.
        return True
    return False


def _claims_any_preference(text: str) -> bool:
    """For the all-unset case: does the text credit ANY preference?

    An explicit denial ("you have not chosen any preferences", "matching none
    of your preferences") is correct and must survive.

    Judged **per sentence**. v4 produced "This is a general suggestion as you
    have not chosen any preferences. It is Continental and vegetarian,
    matching those you set." -- a correct denial followed by a false claim, so
    a whole-text denial check would have waved it through.
    """
    denial = re.compile(
        r"\b(?:not (?:chosen|set|recorded|stated|specified)|no preferences|"
        r"none of your|neither|nothing you|have not|general suggestion)\b")
    claim = re.compile(
        rf"your (?:\w+ )?(?:preference|requirement|choice)|you {_CLAIM_VERB}\b|"
        rf"{_MATCH_VERB} your|as you {_CLAIM_VERB}|those you {_CLAIM_VERB}")

    for sentence in re.split(r"(?<=[.!?])\s+", text.lower()):
        if claim.search(sentence) and not denial.search(sentence):
            return True
    return False


def explanation_is_preference_safe(text: str | None, request: ExplanationRequest) -> bool:
    """True when nothing in ``text`` credits the customer with an unset preference."""
    if not text:
        return True

    low_text = text.lower()
    if _leaks_placeholder(low_text):
        return False

    dimensions = (
        ("cuisine", request.preferred_cuisine, request.item_cuisine),
        ("dietary", request.preferred_dietary, request.item_dietary),
        ("spice", request.preferred_spice, request.item_spice),
    )

    if all(preference is None for _, preference, _ in dimensions):
        return not _claims_any_preference(text)

    for dimension, preference, item_value in dimensions:
        if preference is None and _claims_unset_preference(text, dimension, item_value):
            return False
    return True


# Prose forms for the enum values, so the fallback reads like English rather
# than like a database row.
_SPICE_MATCHED = {"none": "unspiced", "mild": "mildly spiced",
                  "medium": "medium spiced", "hot": "hot", "extra_hot": "extra hot"}
_SPICE_BARE = {"none": "no heat", "mild": "mild", "medium": "medium",
               "hot": "hot", "extra_hot": "extra hot"}


def _cuisine_word(value: str) -> str:
    if value == "multi_cuisine":
        return "multi-cuisine"
    return value.capitalize()


def _join(parts: list[str]) -> str:
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f" and {parts[-1]}"


def deterministic_explanation(request: ExplanationRequest) -> str:
    """A grounded explanation built only from the request's own fields.

    Used when a generation is rejected. It states what the item is and, for
    the dimensions the customer actually set, whether they line up. Unset
    dimensions are simply not discussed -- which is exactly the behaviour the
    model could not be taught reliably.

    Every clause is derived from a field of ``request``, so this cannot invent
    a price, an ingredient, an availability flag or a past order: it has none
    of them to draw on.
    """
    name = _sanitise(request.item_name) or "This item"
    item_cuisine = _sanitise(request.item_cuisine)
    item_dietary = _sanitise(request.item_dietary).replace("_", "-")
    item_spice = _sanitise(request.item_spice)

    matched: list[str] = []
    differing: list[str] = []

    if request.preferred_cuisine is not None:
        if request.preferred_cuisine == request.item_cuisine:
            # "other" has no natural prose name; refer to it relationally.
            matched.append("in the cuisine you chose" if item_cuisine == "other"
                           else _cuisine_word(item_cuisine))
        elif item_cuisine == "other":
            differing.append(f"it is not {_cuisine_word(request.preferred_cuisine)}")
        else:
            differing.append(f"it is {_cuisine_word(item_cuisine)} rather than "
                             f"{_cuisine_word(request.preferred_cuisine)}")

    if request.preferred_dietary is not None:
        if request.preferred_dietary == request.item_dietary:
            matched.append(item_dietary)
        else:
            # The engine's dietary filter already guaranteed compatibility, so
            # this is a difference, never a conflict -- say so explicitly.
            differing.append(
                f"it is {item_dietary}, which still suits your "
                f"{request.preferred_dietary.replace('_', '-')} preference")

    if request.preferred_spice is not None:
        if request.preferred_spice == request.item_spice:
            matched.append(_SPICE_MATCHED.get(item_spice, item_spice))
        else:
            item_form = _SPICE_BARE.get(item_spice, item_spice)
            pref_form = _SPICE_BARE.get(request.preferred_spice, request.preferred_spice)
            lead = "it carries" if item_spice == "none" else "it is"
            differing.append(f"{lead} {item_form} rather than {pref_form}")

    if not matched and not differing:
        # Nothing was set, so there is nothing to compare against. Describe the
        # item and say plainly why it is on screen. No preference is referenced
        # at all, because none exists.
        article = "an" if item_dietary[0] in "aeiou" else "a"
        # "other" has no natural prose name, so the cuisine is simply omitted.
        descriptor = (f"{item_dietary} dish" if item_cuisine == "other"
                      else f"{item_dietary} {_cuisine_word(item_cuisine)} dish")
        if item_spice == "none":
            heat = "no heat"
        else:
            bare = _SPICE_BARE.get(item_spice, item_spice)
            heat = f"{'an' if bare[0] in 'aeiou' else 'a'} {bare} spice level"
        return (f"{name} is {article} {descriptor} with {heat}. "
                f"It is shown as a general suggestion.")

    sentences: list[str] = []
    if matched:
        sentences.append(f"{name} is {_join(matched)}, matching what you asked for.")
    if differing:
        body = ("However, " + _join(differing) + "."
                if matched else f"{name} was suggested, but " + _join(differing) + ".")
        sentences.append(body)

    return " ".join(sentences)[:400]


def apply_preference_safeguard(text: str | None, request: ExplanationRequest) -> str | None:
    """Return ``text`` when it is safe, otherwise a deterministic replacement.

    ``None`` in, ``None`` out: a missing explanation is an ordinary outcome and
    the caller already renders its deterministic panel in that case.
    """
    if text is None:
        return None
    if explanation_is_preference_safe(text, request):
        return text
    logger.info("Explanation rejected: it credited a preference the customer did not set")
    return deterministic_explanation(request)


# ---------------------------------------------------------------------------
# Startup warm-up (M07.8)
# ---------------------------------------------------------------------------
#
# Loading the 1.2 GB base model plus the adapter takes ~15 s. Because the load
# is lazy, that cost lands on whoever asks for the *first* explanation --
# measured at 18.3 s end to end, against ~3.7 s for every request after it.
# In a live demonstration that first request is the one someone is watching.
#
# Warming up moves the cost to startup, where nobody is waiting. It is
# deliberately conservative:
#
#   * It runs on a daemon thread, so startup never blocks and shutdown is
#     never held open by it.
#   * It calls the ordinary ``_load()``. That is already double-checked
#     locking around a module singleton, so a real request arriving mid-warm-up
#     simply waits on the same lock and reuses the same instance -- there is no
#     path to two concurrent loads within a process.
#   * It cannot break anything: ``_load()`` never raises, and on failure the
#     existing degradation applies unchanged (a warning, and explanations
#     return ``None``).
#
# **Multi-worker note.** Each worker process has its own singleton, so N
# workers warming up means N copies of a 1.2 GB model resident at once. That
# is a deliberate operator decision rather than a default, which is why
# ``LLM_WARMUP`` defaults to on only in development (the demo case) and off in
# production. Set ``LLM_WARMUP=true`` in production only after checking the
# memory maths for the configured worker count.

_warmup_started = False


def warm_up(app) -> bool:
    """Load the model in the background if configured to. Returns whether a
    warm-up thread was actually started."""
    global _warmup_started

    if _warmup_started:
        return False  # idempotent: create_app may be called more than once
    if app.config.get("TESTING"):
        return False
    if not app.config.get("LLM_ENABLED", True):
        return False
    if not app.config.get("LLM_WARMUP", False):
        return False

    # Flask's reloader runs the app in a child process; the parent would warm
    # a model it never serves from.
    if app.config.get("DEBUG") and os.environ.get("WERKZEUG_RUN_MAIN") != "true":
        return False

    def _run() -> None:
        with app.app_context():
            started = time.monotonic()
            tokenizer, model = _load()
            elapsed = time.monotonic() - started
            if model is None or tokenizer is None:
                # _load already logged the reason; degradation is unchanged.
                logger.info("Local LLM warm-up did not complete after %.1fs", elapsed)
            else:
                logger.info("Local LLM warmed up in %.1fs; first explanation will be fast", elapsed)

    _warmup_started = True
    threading.Thread(target=_run, name="llm-warmup", daemon=True).start()
    logger.info("Local LLM warm-up started in the background")
    return True
