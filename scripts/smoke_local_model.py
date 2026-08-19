"""Standalone smoke test for the local base model.

    python scripts/smoke_local_model.py

Loads models/Qwen3-0.6B-Base and generates once, to prove the weights and the
AI dependency stack are present on this machine. It exercises the *base*
model only -- it does not attach the V4 adapter and is not part of the
application's inference path (see app/services/local_llm.py for that).

The model path resolves the same way the application resolves it: the
LLM_MODEL_PATH environment variable if set, otherwise models/Qwen3-0.6B-Base
relative to the repository root. Nothing here is machine-specific.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

# ============================================================
# Configuration
# ============================================================

# Repository root, derived from this file's own location so the script works
# from any working directory and on any machine.
BASE_DIR = Path(__file__).resolve().parent.parent

# Same variable and same default the application uses (config.py::BaseConfig).
MODEL_PATH = os.environ.get("LLM_MODEL_PATH", str(BASE_DIR / "models" / "Qwen3-0.6B-Base"))

if not Path(MODEL_PATH).is_dir():
    print(f"Base model not found at: {MODEL_PATH}")
    print("The models/ directory is git-ignored and must be copied separately;")
    print("see the 'New machine / handoff setup' section of README.md.")
    print("Override the location with the LLM_MODEL_PATH environment variable.")
    sys.exit(1)

# ============================================================
# Device
# ============================================================

if torch.cuda.is_available():
    device = "cuda"
else:
    device = "cpu"

print("=" * 60)
print("Quick Junction - Qwen3-0.6B Local Model Test")
print("=" * 60)

print(f"PyTorch version : {torch.__version__}")
print(f"Device          : {device}")

if device == "cuda":
    print(f"GPU             : {torch.cuda.get_device_name(0)}")
else:
    print("GPU             : Not detected - using CPU")

print("=" * 60)

# ============================================================
# Load tokenizer
# ============================================================

print("\n[1/3] Loading tokenizer...")

tokenizer = AutoTokenizer.from_pretrained(
    MODEL_PATH
)

print("Tokenizer loaded successfully.")

# ============================================================
# Load model
# ============================================================

print("\n[2/3] Loading Qwen3-0.6B-Base...")

model = AutoModelForCausalLM.from_pretrained(
    MODEL_PATH,
    torch_dtype=torch.float32
)

model.to(device)
model.eval()

print("Model loaded successfully.")

# ============================================================
# Test prompt
# ============================================================

print("\n[3/3] Generating response...")

prompt = """
You are an AI assistant for a restaurant called Quick Junction.

Customer:
I want something spicy with chicken under Rs. 500.

Explain what kind of information you would need to recommend
a suitable menu item.
"""

inputs = tokenizer(
    prompt,
    return_tensors="pt"
)

inputs = {
    key: value.to(device)
    for key, value in inputs.items()
}

# ============================================================
# Generate
# ============================================================

with torch.no_grad():

    outputs = model.generate(
        **inputs,
        max_new_tokens=150,
        do_sample=True,
        temperature=0.7,
        top_p=0.9,
        pad_token_id=tokenizer.eos_token_id
    )

# ============================================================
# Decode
# ============================================================

generated_tokens = outputs[0][inputs["input_ids"].shape[1]:]

response = tokenizer.decode(
    generated_tokens,
    skip_special_tokens=True
)

print("\n" + "=" * 60)
print("MODEL RESPONSE")
print("=" * 60)

print(response)

print("=" * 60)
print("TEST COMPLETED")
print("=" * 60)