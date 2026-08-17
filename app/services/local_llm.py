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

    return _tidy(text)


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
