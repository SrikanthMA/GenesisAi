"""Open-weight model catalogue and hardware-aware recommendations (3.0).

Sizes are the default (Q4) downloads in the Ollama library and are approximate; Genesis re-checks the real size after
download (``ollama list``). ``need_gb`` is roughly the memory the model takes while answering with a normal context.
Licences are summarised - read the full licence before redistributing a model.

Roles:
* ``suzanne`` - natural conversation (small, fast, good tone)
* ``andrew``  - technical work: code, explanations, structured output
* ``complex`` - level-3 tasks, only when the hardware allows it
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .hardware import Hardware


@dataclass(frozen=True)
class ModelInfo:
    name: str
    params_b: float
    download_gb: float
    need_gb: float
    context_k: int
    licence: str
    roles: tuple[str, ...]
    note: str


CATALOG: tuple[ModelInfo, ...] = (
    ModelInfo("llama3.2:3b", 3.2, 2.0, 3.0, 128, "Llama 3.2 Community Licence", ("suzanne",),
              "fast, natural conversation; good default for Suzanne"),
    ModelInfo("gemma3:4b", 4.3, 3.3, 4.5, 128, "Gemma Terms of Use", ("suzanne",),
              "warm conversational style, multilingual"),
    ModelInfo("phi4-mini", 3.8, 2.5, 3.5, 128, "MIT", ("suzanne", "andrew"),
              "small and strong at reasoning and maths"),
    ModelInfo("qwen2.5:7b", 7.6, 4.7, 6.0, 32, "Apache 2.0", ("suzanne", "andrew"),
              "all-rounder; follows instructions and JSON well"),
    ModelInfo("qwen2.5-coder:7b", 7.6, 4.7, 6.0, 32, "Apache 2.0", ("andrew",),
              "code generation, fixing and explaining; good default for Andrew"),
    ModelInfo("llama3.1:8b", 8.0, 4.9, 6.3, 128, "Llama 3.1 Community Licence", ("suzanne", "andrew"),
              "general assistant with a long context"),
    ModelInfo("qwen2.5:14b", 14.8, 9.0, 11.0, 32, "Apache 2.0", ("complex",),
              "level-3 reasoning; needs a large GPU or GPU+CPU memory"),
    ModelInfo("qwen2.5-coder:14b", 14.8, 9.0, 11.0, 32, "Apache 2.0", ("complex",),
              "level-3 programming tasks"),
    ModelInfo("phi4", 14.7, 9.1, 11.0, 16, "MIT", ("complex",), "level-3 reasoning and maths"),
)
BY_NAME = {m.name: m for m in CATALOG}

# preference order per role (best first among those that fit)
PREFERENCE = {
    "suzanne": ("llama3.2:3b", "gemma3:4b", "qwen2.5:7b", "phi4-mini", "llama3.1:8b"),
    "andrew": ("qwen2.5-coder:7b", "qwen2.5:7b", "phi4-mini", "llama3.1:8b"),
    "complex": ("qwen2.5:14b", "qwen2.5-coder:14b", "phi4"),
}
SAFETY_GB = 1.0  # leave room for Windows, the browser and Genesis itself


def info(name: str) -> Optional[ModelInfo]:
    return BY_NAME.get(name) or BY_NAME.get(name.removesuffix(":latest"))


def budget_gb(hw: Hardware) -> tuple[float, str]:
    """Memory a single model may use, and where it runs ("gpu" / "gpu+cpu" / "cpu")."""
    vram = hw.vram_free_gb
    ram = hw.ram_available_gb
    if vram >= 3.0:
        return vram - 0.5, "gpu"
    return max(0.0, ram - 4.0), "cpu"  # keep ~4 GB of RAM for Windows and apps


def fits(m: ModelInfo, hw: Hardware, *, allow_split: bool = False) -> bool:
    budget, where = budget_gb(hw)
    if m.need_gb <= budget - SAFETY_GB * (0 if where == "gpu" else 1):
        return True
    if allow_split and where == "gpu":
        # part on the GPU, the rest in system RAM: slower, but workable for occasional level-3 tasks
        return m.need_gb <= hw.vram_free_gb + max(0.0, hw.ram_available_gb - 6.0)
    return False


def recommend(hw: Hardware) -> dict:
    """{"suzanne": name, "andrew": name, "complex": name|None, "where": ..., "download_gb": total, "notes": [...]}"""
    rec: dict = {"notes": []}
    budget, where = budget_gb(hw)
    rec["where"] = where
    for role in ("suzanne", "andrew"):
        pick = next((n for n in PREFERENCE[role] if fits(BY_NAME[n], hw)), None)
        if pick is None:
            pick = min((BY_NAME[n] for n in PREFERENCE[role]), key=lambda m: m.need_gb).name
            rec["notes"].append(f"{role.title()}: even the smallest model is tight on this PC; expect slow answers.")
        rec[role] = pick
    rec["complex"] = next((n for n in PREFERENCE["complex"] if fits(BY_NAME[n], hw, allow_split=True)), None)
    if rec["complex"] is None:
        rec["notes"].append("Level-3 tasks will use Andrew's model: a 14B model doesn't fit in the free memory.")
    elif not fits(BY_NAME[rec["complex"]], hw):
        rec["notes"].append(f"{rec['complex']} doesn't fit entirely in video memory; it will share system RAM and "
                            "answer more slowly. Genesis only loads it for complex tasks.")
    names = {rec["suzanne"], rec["andrew"]} | ({rec["complex"]} if rec["complex"] else set())
    rec["download_gb"] = round(sum(BY_NAME[n].download_gb for n in names), 1)
    if hw.disk_free_gb is not None and hw.disk_free_gb < rec["download_gb"] + 5:
        rec["notes"].append(f"Only {hw.disk_free_gb:.0f} GB disk free; the models need about {rec['download_gb']} GB "
                            "plus room to spare.")
        rec["disk_ok"] = False
    else:
        rec["disk_ok"] = True
    return rec
