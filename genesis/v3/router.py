"""The orchestrator / model router (3.0).

For every request it decides, in this order:

1. **Deterministic?**  Plain arithmetic ("what is 17.5% of 240", "12*(3+4)") is computed in Python - exact, instant, no
   model at all. (File operations are already deterministic Genesis tools; they never reach a model.)
2. **Which agent?**    Structured/JSON requests and technical requests -> Andrew; everything else -> Suzanne.
3. **Which level?**    1 = simple (short chat, quick question), 2 = moderate (explanations, normal code, summaries),
                       3 = complex (long multi-part analysis, large code, architecture). Thresholds are configurable.
4. **Which model?**    The smallest capable one: the agent's model for levels 1-2; the complex model for level 3 only if it
                       is installed *and* fits in free memory; otherwise the agent's model. For level 1 a model that is
                       already loaded is preferred, to avoid swapping models in and out of memory.

The router is pure logic (no network): ``provider.py`` executes its decisions.
"""
from __future__ import annotations

import ast
import operator
import os
import re
from dataclasses import dataclass, field
from typing import Optional

from .agents import Agent

TECH_WORDS = re.compile(
    r"\b(code|coding|program|script|function|class|method|bug|debug|error|exception|traceback|stack ?trace|compile|"
    r"python|javascript|typescript|java|c\+\+|c#|rust|golang|sql|html|css|regex|json|yaml|api|endpoint|database|"
    r"algorithm|refactor|unit test|pytest|git|docker|kubernetes|linux|powershell|bash|terminal|install|"
    r"architecture|deploy|server|framework|library|package|pip|npm|explain how|how does .* work|step[- ]by[- ]step|"
    r"summari[sz]e|summary|plan|outline|analy[sz]e|analysis|calculate|formula|spreadsheet|excel)\b", re.I)
COMPLEX_WORDS = re.compile(
    r"\b(architecture|design (a|the|an)|in depth|in-depth|detailed|comprehensive|compare .* (and|with|vs)|trade-?offs?|"
    r"whole (project|app|application|codebase)|full (app|application|program|implementation)|refactor|"
    r"multi-?step|research|prove|derive|optimi[sz]e)\b", re.I)
CODE_FENCE = re.compile(r"```|^\s{4}\S", re.M)


@dataclass
class Thresholds:
    """When a request counts as level 2 or 3 (edit in .env: GENESIS_LEVEL2_CHARS / GENESIS_LEVEL3_CHARS)."""
    level2_chars: int = 280
    level3_chars: int = 1500

    @classmethod
    def from_env(cls) -> "Thresholds":
        def num(name, default):
            try:
                return int(os.getenv(name) or default)
            except ValueError:
                return default
        return cls(num("GENESIS_LEVEL2_CHARS", 280), num("GENESIS_LEVEL3_CHARS", 1500))


@dataclass
class Route:
    agent: str                 # "suzanne" | "andrew" | "python"
    level: int                 # 1, 2, 3 (0 for deterministic)
    model: str                 # model name, or "" for deterministic answers
    reason: str
    kind: str = "chat"         # "chat" | "internal" (Genesis's own NLU/planner JSON) | "math" | "twin"
    answer: Optional[str] = None  # set when the answer was computed deterministically
    fallbacks: list[str] = field(default_factory=list)  # smaller models to try if the first fails (e.g. out of memory)

    def to_dict(self) -> dict:
        return {"agent": self.agent, "level": self.level, "model": self.model, "reason": self.reason, "kind": self.kind}


# ------------------------------------------------------------------ deterministic arithmetic
_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod, ast.Pow: operator.pow,
        ast.USub: operator.neg, ast.UAdd: operator.pos}
_MATH_PREFIX = re.compile(r"^\s*(what(?:'s| is)|calculate|compute|work out|how much is|evaluate)\s+", re.I)
_PERCENT_OF = re.compile(r"^\s*([\d.]+)\s*%\s*of\s*([\d.,]+)\s*$", re.I)
_ALLOWED = re.compile(r"^[\d\s.+\-*/%()^x×÷,]+$")


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        left, right = _eval(node.left), _eval(node.right)
        if isinstance(node.op, ast.Pow) and (abs(right) > 100 or abs(left) > 1e6):
            raise ValueError("too large")
        return _OPS[type(node.op)](left, right)
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise ValueError("not arithmetic")


def _fmt(v: float) -> str:
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        v = int(v)
    if isinstance(v, float):
        return f"{v:,.10g}" if abs(v) >= 1000 else f"{v:.10g}"
    return f"{v:,}"


def arithmetic(text: str) -> Optional[str]:
    """Exact answer for a plain arithmetic question, or None if the text isn't pure arithmetic."""
    s = (text or "").strip().rstrip("?.! ")
    s = _MATH_PREFIX.sub("", s)
    m = _PERCENT_OF.match(s)
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2).replace(",", ""))
        except ValueError:
            return None
        return f"{m.group(1)}% of {m.group(2)} is {_fmt(a * b / 100)}."
    if not s or not _ALLOWED.match(s) or not re.search(r"\d\s*[-+*/x×÷^%]\s*[\d(]", s):
        return None
    expr = s.replace("×", "*").replace("x", "*").replace("÷", "/").replace("^", "**").replace(",", "")
    try:
        value = _eval(ast.parse(expr, mode="eval"))
    except ZeroDivisionError:
        return "That divides by zero, so there's no answer."
    except (SyntaxError, ValueError, TypeError, OverflowError):
        return None
    return f"{s.strip()} = {_fmt(value)}"


# ------------------------------------------------------------------ routing
def specialist_for(text: str, specialists: list[dict]) -> Optional[dict]:
    """A registered specialist whose declared capability phrase appears in the request (whole words), else None.
    Specialists are never used for general conversation - only for what the user said they are good at."""
    low = " " + " ".join(re.findall(r"[a-z0-9+#]+", (text or "").lower())) + " "
    best = None
    for sp in specialists or []:
        for cap in sp.get("capabilities") or []:
            phrase = " " + " ".join(re.findall(r"[a-z0-9+#]+", cap.lower())) + " "
            if phrase.strip() and phrase in low and (best is None or len(phrase) > best[0]):
                best = (len(phrase), sp)
    return best[1] if best else None


def last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages or []):
        if m.get("role") == "user":
            return str(m.get("content") or "")
    return ""


def is_internal(messages: list[dict], json_mode: bool) -> bool:
    """Genesis's own understanding/planning/translation steps (structured, not a chat with the user)."""
    if json_mode:
        return True
    sys = " ".join(str(m.get("content") or "") for m in messages if m.get("role") == "system").lower()
    return "return only json" in sys or "respond with json" in sys or "json object" in sys


def level_of(text: str, th: Thresholds) -> int:
    n = len(text)
    if n >= th.level3_chars or (COMPLEX_WORDS.search(text) and n >= th.level2_chars // 2):
        return 3
    if n >= th.level2_chars or TECH_WORDS.search(text) or CODE_FENCE.search(text) or COMPLEX_WORDS.search(text):
        return 2
    return 1


class Router:
    def __init__(self, agents: dict[str, Agent], *, complex_model: str = "", thresholds: Optional[Thresholds] = None):
        self.agents = agents
        self.complex_model = complex_model
        self.thresholds = thresholds or Thresholds.from_env()
        self.mode = "casual"          # conversation mode (3.1): coding mode sends conversation to Andrew
        self.specialists: list[dict] = []  # registered specialist SLMs (e.g. TwinSim): only for their declared skills

    def route(self, messages: list[dict], *, json_mode: bool = False, installed: Optional[set[str]] = None,
              loaded: Optional[set[str]] = None, can_fit=None) -> Route:
        """Decide; ``installed``/``loaded`` are model-name sets from Ollama, ``can_fit(model)`` checks free memory."""
        text = last_user_text(messages)
        installed = installed if installed is not None else set()
        loaded = loaded or set()
        suz, andy = self.agents["suzanne"], self.agents["andrew"]

        def have(model: str) -> bool:
            return not installed or model in installed or model.split(":")[0] in installed

        if is_internal(messages, json_mode):
            # Genesis's internal steps: short, structured, low temperature -> Andrew, but use whichever capable model is
            # already in memory so a quick NLU call doesn't evict the model the user is chatting with.
            model = andy.model
            if suz.model in loaded and andy.model not in loaded and len(text) < 600:
                model = suz.model
            return Route("andrew", 1, model if have(model) else suz.model, "structured request (Genesis's own step)",
                         kind="internal", fallbacks=[m for m in (suz.model,) if m != model])

        from .twin import is_twin_request

        if is_twin_request(text):
            return Route("suzanne", 2, suz.model if have(suz.model) else andy.model,
                         "digital twin: answering as you", kind="twin",
                         fallbacks=[m for m in (andy.model,) if m != suz.model])

        ans = arithmetic(text)
        if ans is not None:
            return Route("python", 0, "", "plain arithmetic: computed exactly in Python", kind="math", answer=ans)

        spec = specialist_for(text, self.specialists)
        if spec is not None and have(spec["model"]):
            return Route("specialist", 2, spec["model"], f"matches {spec['name']}'s declared skill", kind="specialist",
                         fallbacks=[suz.model, andy.model])

        level = level_of(text, self.thresholds)
        technical = bool(TECH_WORDS.search(text) or CODE_FENCE.search(text)) or self.mode == "coding"
        agent = andy if technical else suz
        reason = "technical request" if technical else "conversation"

        model = agent.model
        if level == 3 and self.complex_model and have(self.complex_model):
            if can_fit is None or can_fit(self.complex_model):
                model = self.complex_model
                reason += ", complex (level 3): using the larger model"
            else:
                reason += f", complex (level 3) but {self.complex_model} doesn't fit in free memory now"
        elif level == 1 and model not in loaded:
            other = andy if agent is suz else suz
            if other.model in loaded and not technical:
                model = other.model  # already in memory: answer now instead of swapping models
                reason += f", simple: reused {other.model} (already loaded)"
        if not have(model):
            alt = next((a.model for a in (suz, andy) if have(a.model)), None)
            if alt:
                reason += f"; {model} isn't installed, using {alt}"
                model = alt
        fallbacks = [m for m in dict.fromkeys([agent.model, suz.model, andy.model]) if m != model]
        return Route(agent.key, level, model, reason, fallbacks=fallbacks)
