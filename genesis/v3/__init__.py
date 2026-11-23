"""Genesis 3 — Prototype 1: local-first intelligence.

Everything new in 3.0 lives in this package; existing code reaches it through small hooks marked ``v3 hook``.

* ``mode``      — local / hybrid operating mode; external AI only when explicitly enabled, never silently.
* ``hardware``  — CPU, RAM, NVIDIA GPU/VRAM and disk detection (measured, not assumed).
* ``ollama``    — native Ollama client: availability, installed/loaded models, chat (+ streaming), pull, unload.
* ``catalog``   — open-weight model catalogue (size, licence, strengths) and hardware-aware recommendations.
* ``agents``    — SUZANNE (personal intelligence) and ANDREW (technical intelligence).
* ``router``    — task complexity + agent selection + smallest capable model; deterministic shortcuts (arithmetic).
* ``provider``  — ``GenesisLocalProvider``: the LLM provider Genesis uses in local mode (routes every call).
* ``store``     — local SQLite: model usage, conversation summaries, agent config, approved training examples,
                  Genesis SLM versions.
* ``slm``       — the GENESIS SLM pipeline: Modelfile build, approved datasets, evaluation, versions, rollback
                  (LoRA fine-tuning is an optional, user-run script: scripts/train_genesis_slm.py).
* ``tools`` / ``commands`` — "check my AI setup", "which model answered?", "save that as a good answer", …
"""

__version__ = "3.5.0"
