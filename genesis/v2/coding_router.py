"""Send coding requests where the user is working (2.2).

"write a program for bubble sort" means different things depending on where you are:

* in a **Google Colab** notebook (or you said "in colab" / "cell")  -> Colab cell tools (write, run, edit, fix)
* in an **online compiler / coding site**, or you said "here" / "in this editor" -> type it into that window
* anywhere else -> unchanged 1.x behaviour (a program file, VS Code, ...)

Saying where explicitly always wins ("in VS Code", "save it as a file", "in colab", "here").
"""
from __future__ import annotations

import re
from typing import Any, Optional

from ..nlp.intents import Intent, NLUResult
from . import window_context as wc

I = re.I
COLAB_WORDS = re.compile(r"\bcolab\b|\bnotebook\b|\bcells?\b|\bgoogle colab\b", I)
HERE_WORDS = re.compile(
    r"\bhere\b|\bthis (?:window|editor|compiler|page|site|website|tab|ide|code|program|question|problem)\b|"
    r"\b(?:in|on|into) (?:the |this |my )?(?:online compiler|compiler|editor|browser|screen|website|site|page|tab|"
    r"code editor|ide)\b|\bonline compiler\b|\b(?:leetcode|hackerrank|programiz|onlinegdb|replit|jdoodle|codechef|"
    r"geeksforgeeks|onecompiler|hackerearth|codeforces)\b", I)
LOCAL_WORDS = re.compile(r"\bvs ?code\b|\bvisual studio\b|\bpycharm\b|\bas a file\b|\b(?:in|to|into) a (?:new )?file\b|"
                         r"\bsave (?:it|this)\b|\bon my (?:computer|pc|laptop)\b|\blocally\b|\bproject\b|\bfolder\b|\.py\b", I)

WRITE = re.compile(r"^(?:please\s+)?(?:(?:can|could) you\s+)?(?:write|code|create|make|generate|build|implement|program|"
                   r"solve|do)\b", I)
CODE_NOUN = re.compile(r"\b(?:program|code|function|script|class|algorithm|solution|app|game|calculator|sort|"
                       r"binary search|linear search|"
                       r"fibonacci|factorial|prime|palindrome|loop|pattern|recursion|query|snippet|question|problem|"
                       r"method|api|matrix|array|string|linked list|stack|queue|tree|graph)\b", I)
EDIT = re.compile(r"^(?:please\s+)?(?:(?:can|could) you\s+)?(?:edit|change|modify|update|refactor|rewrite|improve|"
                  r"optimi[sz]e|add|remove|rename|convert|make)\b(?P<rest>.*)$", I)
# an edit must be about code ("make it louder" / "add this document to …" are not)
EDIT_TARGET = re.compile(r"\b(?:code|cell|program|function|loop|variable|output|print|input|class|method|comments?|"
                         r"algorithm|array|list|string|recursion|exception|error handling|return|import|syntax|indentation|"
                         r"script)\b|\b(?:it|this) (?:to|so (?:that|it)) (?:print|use|take|read|return|show|handle|accept|"
                         r"sort|plot|ask|work|run|loop|display|calculate|count)\b", I)
FIX = re.compile(r"^(?:please\s+)?(?:(?:can|could) you\s+)?(?:fix|debug|correct|solve|repair)\s+(?:the |this |my |that )?"
                 r"(?:error|errors|bug|bugs|code|it|cell|program|issue|problem|exception|this)\b", I)
RUN = re.compile(r"^(?:please\s+)?(?:(?:can|could) you\s+)?(?:run|execute|compile|test)\s+(?:the |this |my |that )?"
                 r"(?:code|program|cell|it|this|script|solution)(?: again| now)?$|^(?:please\s+)?run$", I)
AND_RUN = re.compile(r"\b(?:and|then)\s+(?:run|execute|test|compile)\b(?:\s+it)?|\brun it\b", I)
DELETE_CELL = re.compile(r"^(?:please\s+)?(?:delete|remove|clear)\s+(?:the |this )?cell$", I)
WHERE_AM_I = re.compile(r"^(?:which|what) (?:window|app|application|tab|page|program) (?:am i (?:in|on|using)|is (?:open|this|"
                        r"active|in front)|is in front)\??$|^where am i\??$|^what am i (?:looking at|using|working in)\??$", I)

# Intent.COLAB is not here: 1.x already handles "write code in colab to ..." / "run the cell" itself
CODING_INTENTS = {Intent.CODE_ACTION, Intent.CONVERSATION, Intent.UNKNOWN, Intent.COMPUTER,
                  Intent.FILE_OPERATION, Intent.INFORMATION_REQUEST, Intent.GOAL}


def _tool(text: str, tool: str, args: dict, description: str) -> NLUResult:
    return NLUResult(text=text, intent=Intent.FOLLOW_UP, confidence=0.95, source="v2-code",
                     slots={"tool": tool, "args": args, "description": description})


def _clean_task(text: str) -> str:
    t = AND_RUN.sub("", text)
    t = re.sub(r"\s+(?:here|in (?:the |this )?(?:online compiler|compiler|editor|colab|notebook|cell|browser|window))\b", "", t, flags=I)
    return re.sub(r"\s{2,}", " ", t).strip(" ,.")


HEARD = re.compile(r"^(?:what did you (?:just )?hear|what did i (?:just )?say|what did you understand)\??$", I)


def classify_request(text: str) -> Optional[str]:
    t = text.strip().rstrip("?.! ")
    if WHERE_AM_I.match(t):
        return "where"
    if HEARD.match(t):
        return "heard"
    if DELETE_CELL.match(t):
        return "delete_cell"
    if RUN.match(t):
        return "run"
    if FIX.match(t):
        return "fix"
    if WRITE.match(t) and CODE_NOUN.search(t):
        return "write"
    m = EDIT.match(t)
    if m and EDIT_TARGET.search(t):
        return "edit"
    return None


def destination(text: str, target: Optional[wc.Target]) -> Optional[str]:
    if LOCAL_WORDS.search(text):
        return None
    if COLAB_WORDS.search(text):
        return "colab"
    if HERE_WORDS.search(text):
        if target is not None and target.kind == "colab":
            return "colab"
        return "window" if target is not None else None
    if target is None:
        return None
    return {"colab": "colab", "code_web": "window"}.get(target.kind)


def route(nlu: Any, svc: Any) -> Any:
    """Assistant hook after NLU (v2 hook 2.2). Returns ``nlu`` unchanged unless this is a coding request for Colab or
    for the window the user is in."""
    if nlu is None or nlu.intent not in CODING_INTENTS:
        return nlu
    text = (nlu.text or "").strip()
    kind = classify_request(text)
    if kind is None:
        return nlu
    if kind == "where":
        return _tool(text, "active_window", {}, "Check which window you're in")
    if kind == "heard":
        return _tool(text, "last_heard", {}, "Say what I heard")
    target = wc.current_target(svc)
    dest = destination(text, target)
    if dest is None:
        return nlu
    run = bool(AND_RUN.search(text))
    if dest == "colab":
        if kind == "delete_cell":
            return NLUResult(text=text, intent=Intent.CONVERSATION, confidence=0.9, source="v2-code",
                             slots={"v2_reply": "I don't delete Colab cells myself - click the cell and press Ctrl+M then D "
                                                "(or the trash icon on the cell)."})
        if kind == "run":
            return _tool(text, "colab_run", {}, "Run the Colab cell")
        if kind == "fix":
            last = getattr(getattr(svc, "colab", None), "last_run", None) or {}
            if last.get("error"):
                return _tool(text, "colab_fix", {"rerun": True}, "Fix the cell and run it again")
            return _tool(text, "colab_edit", {"instruction": "Find and fix any bugs so the cell runs correctly.",
                                              "rerun": True}, "Fix the cell")
        if kind == "edit":
            return _tool(text, "colab_edit", {"instruction": _clean_task(text), "rerun": run}, "Change the Colab cell")
        new_cell = not re.search(r"\bthis cell\b|\bthe cell\b|\bsame cell\b", text, I) or re.search(r"\bnew cell\b", text, I)
        return _tool(text, "colab_code", {"task": _clean_task(text), "run": run, "new_cell": bool(new_cell)},
                     "Write the code in Colab")
    if kind == "delete_cell":
        return nlu
    window = target.title if target else ""
    lang = wc.guess_language(text) if wc.guess_language(text) not in (None, "python") or re.search(r"\bpython\b", text, I) else None
    if kind == "run":
        return _tool(text, "code_in_window", {"op": "run", "window": window}, f"Run the code in {target.page if target else 'the window'}")
    if kind == "fix":
        return _tool(text, "code_in_window", {"op": "fix", "run": run, "window": window}, "Fix the code in this window")
    if kind == "edit":
        return _tool(text, "code_in_window", {"op": "edit", "instruction": _clean_task(text), "run": run, "window": window},
                     "Change the code in this window")
    args = {"op": "write", "task": _clean_task(text), "run": run, "window": window}
    if lang:
        args["language"] = lang
    return _tool(text, "code_in_window", args, "Write the code in this window")
