"""Fast routing decisions that save language-model calls.

* ``is_answer_only`` — questions, explanations and pure writing ("explain recursion", "write a Python function that…")
  need no tools: they go straight to the conversational model (one call instead of NLU + chat).
* ``looks_like_request`` — an instruction Genesis has no rule for: it goes straight to the LLM planner, which picks the
  tools (one call instead of NLU + planner).
"""
from __future__ import annotations

import re

from . import rules

COMMAND_START = re.compile(rf"^(?:please\s+)?{rules.ACTION_VERBS}\b", re.I)
REQUEST_START = re.compile(r"^(?:please\s+|can you\s+|could you\s+|would you\s+|will you\s+|i (?:want|need|would like|'d like)\s+"
                           r"(?:you\s+)?to\s+|help me\s+|let'?s\s+|go ahead and\s+|get me\s+|i'?m trying to\s+)", re.I)
GOAL_VERBS = re.compile(r"^(?:prepare|set up|setup|get|organi[sz]e|clean up|tidy|plan|research|compare|collect|gather|figure out|"
                        r"find out|work out|arrange|book|check|look into|build|make|start|finish|prep)\b", re.I)


# Questions, explanations and pure writing ("explain recursion", "write a Python function that…", "tell me a joke") are
# answered by the conversational LLM directly — they need no tools, so they skip the tool planner.
ANSWER_ONLY = re.compile(
    r"^(?:please\s+|can you\s+|could you\s+|would you\s+)?(?:"
    r"explain|describe|define|teach me|tell me (?:a |an |about |what |why |how |who |the difference)|"
    r"what(?:'s| is| are| was| were| does| do)?\b|why\b|how (?:do|does|did|is|are|can|to|would)\b|who\b|"
    r"give me (?:an? )?(?:example|idea|ideas|summary|overview|tip|tips|joke|fact)|"
    r"write (?:me )?(?:a|an|some)\s+(?:python |javascript |java |c\+\+ |c# |go |rust |sql |bash |short |simple |quick )?"
    r"(?:function|class|method|script snippet|snippet|regex|query|poem|story|haiku|joke|limerick|paragraph|essay|"
    r"sentence|bio|summary|explanation|example|algorithm|program)\b(?!.*\b(?:to|into|in|as) (?:a |the )?file\b))",
    re.I)


LIVE_INFO = re.compile(r"\b(latest|news|today|tonight|tomorrow|yesterday|this week|current(ly)?|right now|live|price|"
                       r"weather|forecast|score|stock|exchange rate|trending|my\b|on (?:my|the) screen|this (?:page|file|email|error))",
                       re.I)


def is_answer_only(text: str) -> bool:
    t = text.strip()
    return bool(ANSWER_ONLY.match(t)) and not LIVE_INFO.search(t)


def looks_like_request(text: str) -> bool:
    """An instruction (not small talk) — worth asking the LLM planner to work out the steps."""
    t = text.strip()
    if is_answer_only(t):
        return False
    return bool(COMMAND_START.match(t) or REQUEST_START.match(t) or GOAL_VERBS.match(t))
