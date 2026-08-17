import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

# ============================================================
# Configuration
# ============================================================

MODEL_PATH = r"D:\QuickJunction\models\Qwen3-0.6B-Base"

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