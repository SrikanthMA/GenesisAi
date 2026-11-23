"""Optional: fine-tune the Genesis SLM with LoRA / QLoRA on your own GPU (Genesis 3.0).

Genesis never runs this by itself. You run it when you decide your approved examples are worth training on:

    1. In Genesis: "export the training dataset"   -> data\\genesis-slm\\dataset\\genesis_dataset.jsonl
    2. Install the training extras once (large download, NVIDIA GPU recommended):
           .venv\\Scripts\\pip install torch --index-url https://download.pytorch.org/whl/cu128
           .venv\\Scripts\\pip install "transformers>=4.45" "peft>=0.13" "trl>=0.11" datasets accelerate bitsandbytes
    3. .venv\\Scripts\\python scripts\\train_genesis_slm.py --base unsloth/Llama-3.2-3B-Instruct
    4. In Genesis: "build the genesis slm" with the adapter folder this prints, then compare scores and
       "use genesis slm version N" if it is better (or roll back any time).

The --base model must be the Hugging Face version of the Ollama base you'll attach the adapter to
(llama3.2:3b <-> Llama-3.2-3B-Instruct, gemma3:4b <-> gemma-3-4b-it, qwen2.5:7b <-> Qwen2.5-7B-Instruct).
Ollama can attach safetensors LoRA adapters for Llama, Mistral, Gemma and Qwen2 architectures.

With fewer than ~50 examples, fine-tuning rarely beats the instant Modelfile build; the script warns you.
Nothing is uploaded: the base model is downloaded once from Hugging Face, training runs on this PC.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_records(path: Path) -> list[dict]:
    recs = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rec = json.loads(line)
            roles = [m.get("role") for m in rec.get("messages", [])]
            if "user" in roles and "assistant" in roles:
                recs.append(rec)
    return recs


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default=str(ROOT / "data" / "genesis-slm" / "dataset" / "genesis_dataset.jsonl"))
    ap.add_argument("--base", default="unsloth/Llama-3.2-3B-Instruct")
    ap.add_argument("--out", default="")
    ap.add_argument("--epochs", type=float, default=3)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--qlora", action="store_true", help="4-bit base (less video memory; needs bitsandbytes)")
    ap.add_argument("--dry-run", action="store_true", help="check the dataset and settings, don't train")
    a = ap.parse_args(argv)

    data = Path(a.data)
    if not data.is_file():
        print(f"No dataset at {data}. In Genesis say: export the training dataset")
        return 2
    recs = load_records(data)
    print(f"{len(recs)} approved examples in {data}")
    if not recs:
        return 2
    if len(recs) < 50:
        print("Note: with fewer than ~50 examples, the Modelfile build (no training) is usually as good.")
    out = Path(a.out) if a.out else data.parent.parent / "adapters" / f"lora-{len(recs)}ex"
    if a.dry_run:
        print(f"Dry run OK. Would train {a.base} for {a.epochs} epochs (rank {a.rank}) into {out}")
        return 0

    try:
        import torch
        from datasets import Dataset
        from peft import LoraConfig
        from transformers import AutoModelForCausalLM, AutoTokenizer
        from trl import SFTConfig, SFTTrainer
    except ImportError as exc:
        print(f"Missing training package ({exc.name}). See step 2 at the top of this file.")
        return 3
    if not torch.cuda.is_available():
        print("No CUDA GPU found - training on the CPU would take many hours. Stopping.")
        return 4

    tok = AutoTokenizer.from_pretrained(a.base)
    kw = {"torch_dtype": torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16, "device_map": "auto"}
    if a.qlora:
        from transformers import BitsAndBytesConfig

        kw["quantization_config"] = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4",
                                                       bnb_4bit_compute_dtype=kw["torch_dtype"])
    model = AutoModelForCausalLM.from_pretrained(a.base, **kw)
    lora = LoraConfig(r=a.rank, lora_alpha=a.rank * 2, lora_dropout=0.05, task_type="CAUSAL_LM",
                      target_modules=["q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj"])
    cfg = SFTConfig(output_dir=str(out / "_work"), num_train_epochs=a.epochs, learning_rate=a.lr,
                    per_device_train_batch_size=1, gradient_accumulation_steps=4, logging_steps=5,
                    save_strategy="no", report_to=[], max_length=2048, bf16=kw["torch_dtype"] == torch.bfloat16,
                    fp16=kw["torch_dtype"] == torch.float16)
    trainer = SFTTrainer(model=model, args=cfg, train_dataset=Dataset.from_list(recs), peft_config=lora,
                         processing_class=tok)
    trainer.train()
    out.mkdir(parents=True, exist_ok=True)
    trainer.model.save_pretrained(str(out))
    (out / "genesis_adapter.json").write_text(json.dumps({"base": a.base, "examples": len(recs), "rank": a.rank,
                                                          "epochs": a.epochs}, indent=2), encoding="utf-8")
    print(f"\nAdapter saved to {out}")
    print(f'In Genesis say: build the genesis slm  (then give the adapter folder: {out})')
    return 0


if __name__ == "__main__":
    sys.exit(main())
