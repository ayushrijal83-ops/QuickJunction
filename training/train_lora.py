"""LoRA fine-tuning for Qwen3-0.6B-Base on the hand-authored dataset.

    python training/train_lora.py --epochs 1
    python training/train_lora.py --max-steps 10 --smoke   # tiny real run

CPU-only by design: this project has no GPU (``torch.cuda.is_available()``
is False here), and the milestone forbids requiring one. LoRA is what makes
that even arguable -- it trains ~1% of the parameters instead of all 596M.
It is still slow; see docs/AI.md for measured timings and for an honest
statement of what was and was not trained.

Never runs inside the web process. Training is an offline script that writes
an adapter to ``models/qwen3-0.6b-quickjunction-lora/``; the application
loads that adapter read-only if it exists (app/services/local_llm.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Must precede the transformers import: keeps the run offline and avoids the
# broken local TensorFlow install (see docs/AI.md).
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("USE_FLAX", "0")
os.environ.setdefault("USE_JAX", "0")

BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_MODEL = BASE_DIR / "models" / "Qwen3-0.6B-Base"

# One output directory per dataset version, so a retrain never overwrites an
# earlier adapter. M07's v1 adapter must stay exactly where M07 left it.
DATASET_VERSIONS = {
    "v1": {
        "dir": BASE_DIR / "data" / "processed" / "v1",
        "output": BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora",
    },
    "v2": {
        "dir": BASE_DIR / "data" / "processed" / "v2",
        "output": BASE_DIR / "models" / "qwen3-0.6b-quickjunction-lora-v2",
    },
}
DEFAULT_DATASET_VERSION = "v2"

MAX_LENGTH = 320


def format_example(example: dict) -> str:
    """The exact shape app/services/local_llm.py::build_prompt emits, so the
    adapter is trained on what it will actually be asked at inference."""
    return (
        f"{example['instruction']}\n\n"
        f"{example['input']}\n\n"
        f"Explanation: {example['output']}"
    )


def load_jsonl(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=str(DEFAULT_MODEL))
    parser.add_argument("--dataset-version", default=DEFAULT_DATASET_VERSION,
                        choices=sorted(DATASET_VERSIONS))
    parser.add_argument("--output", default=None,
                        help="defaults to the dataset version's own directory")
    parser.add_argument("--epochs", type=float, default=1.0)
    parser.add_argument("--max-steps", type=int, default=-1, help="overrides --epochs when > 0")
    parser.add_argument("--batch-size", type=int, default=1)
    parser.add_argument("--grad-accum", type=int, default=4)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--rank", type=int, default=8)
    parser.add_argument("--smoke", action="store_true", help="tiny run to prove the pipeline executes")
    args = parser.parse_args()

    dataset_spec = DATASET_VERSIONS[args.dataset_version]
    train_path = dataset_spec["dir"] / "train.jsonl"
    validation_path = dataset_spec["dir"] / "validation.jsonl"
    output_path = Path(args.output) if args.output else dataset_spec["output"]

    # Refusing to clobber an existing adapter is deliberate: M07's artefact is
    # evidence of a completed experiment and must survive any retrain.
    if output_path.exists() and any(output_path.glob("adapter_model.safetensors")):
        print(f"Refusing to overwrite existing adapter at {output_path}.", file=sys.stderr)
        print("Pass --output <new-dir> or remove it deliberately.", file=sys.stderr)
        return 1

    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import (
        AutoModelForCausalLM,
        AutoTokenizer,
        DataCollatorForLanguageModeling,
        Trainer,
        TrainingArguments,
    )

    if torch.cuda.is_available():
        print("CUDA detected -- training on GPU.")
    else:
        print("No CUDA device: training on CPU. Expect this to be slow.")

    model_path = Path(args.model).resolve()
    if not model_path.is_dir():
        print(f"Model directory not found: {model_path}", file=sys.stderr)
        return 1

    print(f"Loading base model from {model_path} ...")
    tokenizer = AutoTokenizer.from_pretrained(str(model_path), local_files_only=True)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    model = AutoModelForCausalLM.from_pretrained(
        str(model_path), dtype=torch.float32, local_files_only=True
    )
    model.config.use_cache = False

    # Attention + MLP projections only: the standard LoRA target set for this
    # family, and the smallest one that still moves generation style.
    lora_config = LoraConfig(
        r=args.rank,
        lora_alpha=args.rank * 2,
        lora_dropout=0.05,
        bias="none",
        task_type="CAUSAL_LM",
        target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"],
    )
    model = get_peft_model(model, lora_config)
    trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
    total = sum(p.numel() for p in model.parameters())
    print(f"Trainable parameters: {trainable:,} / {total:,} ({100 * trainable / total:.3f}%)")

    train_rows = load_jsonl(train_path)
    validation_rows = load_jsonl(validation_path)
    if args.smoke:
        train_rows = train_rows[:8]
        validation_rows = validation_rows[:2]
    print(f"Training examples: {len(train_rows)} | validation: {len(validation_rows)}")

    class JsonlDataset(torch.utils.data.Dataset):
        def __init__(self, rows: list[dict]):
            self.rows = rows

        def __len__(self) -> int:
            return len(self.rows)

        def __getitem__(self, index: int):
            encoded = tokenizer(
                format_example(self.rows[index]),
                truncation=True,
                max_length=MAX_LENGTH,
                padding="max_length",
            )
            return {k: torch.tensor(v) for k, v in encoded.items()}

    training_args = TrainingArguments(
        output_dir=str(output_path / "checkpoints"),
        num_train_epochs=args.epochs,
        max_steps=args.max_steps,
        per_device_train_batch_size=args.batch_size,
        gradient_accumulation_steps=args.grad_accum,
        learning_rate=args.lr,
        logging_steps=1,
        save_strategy="no",
        report_to=[],          # no experiment-tracking service; stays offline
        use_cpu=not torch.cuda.is_available(),
        seed=42,
    )

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=JsonlDataset(train_rows),
        data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
    )

    print("Starting training ...")
    started = time.time()
    result = trainer.train()
    elapsed = time.time() - started
    print(f"Training finished in {elapsed:.1f}s ({elapsed / 60:.1f} min)")

    output_dir = output_path
    output_dir.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))

    metrics = {
        "dataset_version": args.dataset_version,
        "base_model": str(model_path),
        "learning_rate": args.lr,
        "batch_size": args.batch_size,
        "grad_accum": args.grad_accum,
        "train_runtime_seconds": round(elapsed, 2),
        "train_loss": round(float(result.training_loss), 4),
        "steps": int(result.global_step),
        "train_examples": len(train_rows),
        "validation_examples": len(validation_rows),
        "lora_rank": args.rank,
        "trainable_parameters": trainable,
        "total_parameters": total,
        "trainable_percent": round(100 * trainable / total, 4),
        "device": "cuda" if torch.cuda.is_available() else "cpu",
        "smoke_run": bool(args.smoke),
        "max_steps": args.max_steps,
        "epochs": args.epochs,
    }
    (output_dir / "training_metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
    print(f"Adapter written to {output_dir}")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
