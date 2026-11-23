"""Plain-language commands for local AI and the Genesis SLM (3.0).

``match(text)`` returns ``(tool_name, args)`` for a recognised request, or ``None``. It is deterministic (no model),
so these commands work even when no local model is running.
"""
from __future__ import annotations

import re
from typing import Optional

_MODEL = r"([a-z0-9][\w.\-]*(?::[\w.\-]+)?)"
_RULES: list[tuple[re.Pattern, str, callable]] = []


def _rule(pattern: str, tool: str, args=lambda m: {}):
    _RULES.append((re.compile(pattern, re.I), tool, args))


_rule(r"^(check|show|what'?s) (my |the )?(local ai|ai setup|local models?|ollama)( setup| status)?\??$",
      "local_ai_status")
_rule(r"^(which|what) (model|agent) (answered|replied|was that|did that|are you using)\b.*$", "local_ai_status")
_rule(r"^(are you|is genesis) (running )?(local|offline|on my (pc|computer))\??$", "local_ai_status")
_rule(r"^(recommend|suggest|which) (local )?models?( for (my|this) (pc|computer|laptop))?( should i (use|download))?"
      r"\??$", "local_ai_recommend")
_rule(r"^(show|how fast are) (the )?(local )?(model|ai) (usage|performance|speed|stats)\??$|^how fast are the (local )?"
      r"models\??$", "local_ai_usage")
_rule(rf"^(download|install|pull) (the )?(local )?model {_MODEL}$", "local_ai_download",
      lambda m: {"model": m.group(4)})
_rule(rf"^(use|switch to) {_MODEL} for (suzanne|andrew|complex tasks?)$", "local_ai_set_model",
      lambda m: {"model": m.group(2), "agent": m.group(3).split()[0].lower()})
_rule(rf"^(set|change) (suzanne|andrew)(?:'s)? model to {_MODEL}$", "local_ai_set_model",
      lambda m: {"agent": m.group(2).lower(), "model": m.group(3)})
_rule(r"^(unload|free) (the )?(local )?models?( memory)?$|^free (up )?(gpu|video|ai) memory$", "local_ai_unload")
_rule(r"^(save|keep|remember) (that|this|it) as a good (answer|example)\.?$|^that was a (good|great|perfect) "
      r"answer,? save it\.?$", "slm_approve_answer")
_rule(r"^(no,? )?the (right|correct|better) answer (is|would be)[: ]+(.{3,})$", "slm_approve_answer",
      lambda m: {"answer": m.group(4).strip()})
_rule(r"^(show )?(the )?genesis slm( status| versions)?\??$|^genesis slm status$", "slm_status")
_rule(rf"^(build|create|make) (a |the |my )?(new )?genesis slm( version)?( (from|on) {_MODEL})?$", "slm_build",
      lambda m: {"base": m.group(7)} if m.group(7) else {})
_rule(r"^(build|create|make) (a |the |my )?(new )?genesis slm (with|from) (the )?adapter (in |at )?[\"']?(.+?)[\"']?$",
      "slm_build", lambda m: {"adapter": m.group(7)})
_rule(r"^(use|activate|switch to|roll ?back to|go back to) (the )?genesis slm( version)? v?(\d+)$", "slm_activate",
      lambda m: {"version": int(m.group(4))})
_rule(r"^export (the |my )?(genesis slm |training )?(dataset|examples)$", "slm_export_dataset")
_rule(r"^export (the |my )?local ai data$", "local_ai_export")

# ---- 3.0.1: Wikipedia, documents, Excel, code, digital twin -------------------------------------------------------
_W = r"(?:the |a |an )?"
_rule(r"^(?:open |search |look up |check |get |find |use )?(?:on )?(?:wikipedia|wiki)(?: (?:on|for|about|page (?:on|for|about)|"
      r"article (?:on|for|about)))? (.+?),? (?:and )?(?:copy|paste|put|save|add|write|bring|move)(?: (?:it|that|this|the "
      r"(?:info(?:rmation)?|text|content|article)))?(?: and paste(?: it)?)? (?:in|into|to|on) (?:a |the |my )?(?:new )?"
      r"(?:ms |microsoft )?word(?: (?:doc|document|file))?$", "wikipedia_to_word", lambda m: {"topic": m.group(1)})
_rule(r"^(?:copy |get |put |save |paste )?(?:the )?(?:wikipedia|wiki) (?:article |page |info(?:rmation)? )?(?:on |about |for )?"
      r"(.+?) (?:in|into|to) (?:a )?word(?: (?:doc|document))?$", "wikipedia_to_word", lambda m: {"topic": m.group(1)})
_rule(r"^(?:copy |get |put |save )?(?:information |info )?(?:on |about )?(.+?) from (?:wikipedia|wiki) (?:in|into|to) "
      r"(?:a )?word(?: (?:doc|document))?$", "wikipedia_to_word", lambda m: {"topic": m.group(1)})
_rule(r"^(?:open|search|check|look up) (?:wikipedia|wiki) (?:for|on|about) (.+)$", "wikipedia_to_word",
      lambda m: {"topic": m.group(1), "to_word": False})
_rule(r"^(?:write|draft|create|make|generate|prepare) " + _W + r"(?:my )?ieee(?: format(?:ted)?| style| standard)?"
      r"(?: conference| journal| research)? (?:paper|document|article|report|doc)(?: (?:on|about|for|titled|named))? (.+)$",
      "draft_document", lambda m: {"topic": m.group(1).strip(" \"'"), "kind": "ieee"})
_rule(r"^(?:write|draft|create|make|prepare|generate) " + _W + r"(?:my )?(?:word )?(report|essay|letter|proposal|"
      r"document|article|assignment|notes|doc) (?:on|about|for|to|regarding|titled|named|explaining) (.+?)"
      r"(?:[,;] (?:with|including|requirements?:?|it should|make it) (.+))?$", "draft_document",
      lambda m: {"kind": {"doc": "document"}.get(m.group(1).lower(), m.group(1).lower()), "topic": m.group(2),
                 **({"requirements": m.group(3)} if m.group(3) else {})})
_rule(r"^(?:create|make|build|generate|prepare|draft) " + _W + r"(?:new )?(?:excel|spreadsheet|xlsx)(?: (?:sheet|file|workbook|"
      r"spreadsheet|table))?(?: (?:for|of|with|on|about|to track|tracking|listing|showing))? (.+)$", "excel_create",
      lambda m: {"request": m.group(1)})
_rule(r"^(?:open )?(?:vs ?code|visual studio code)(?: and)? (?:write|code|create|make|build|program) (.+)$", "code_in_vscode",
      lambda m: {"task": m.group(1)})
_rule(r"^(?:write|code|create|make|build|program|develop) (.+?) (?:in|on|using|with|inside) (?:vs ?code|visual studio code)$",
      "code_in_vscode", lambda m: {"task": m.group(1)})
_rule(r"^(?:open )?(?:google )?colab(?: and)? (?:write|code|create|make|build|program) (.+)$", "code_in_colab",
      lambda m: {"task": m.group(1)})
_rule(r"^(?:write|code|create|make|build|program|develop|run) (.+?) (?:in|on|using|with|inside) (?:google )?colab"
      r"(?: notebook)?$", "code_in_colab", lambda m: {"task": m.group(1)})

_FOOD = (r"pizza|burger|biryani|food|dinner|lunch|breakfast|coffee|tea|dosa|idli|noodles|momos|shawarma|sandwich|"
         r"ice ?cream|dessert|cake|chicken|paneer|thali|meal|snacks?|my usual")
_rule(r"^(?:i (?:want|would like|wanna|feel like) (?:to )?(?:order |get |have |eat )?|let'?s order |order (?:me )?|get me )"
      r"(?:an? |some |my |the )?(?:usual )?(" + _FOOD + r")(?: (?:from|on|via|using|through) (swiggy|zomato|domino'?s|dominos))?"
      r"(?: (?:tonight|now|today|please))?$", "twin_order",
      lambda m: {"item": m.group(1).lower(), **({"app": m.group(2).lower().replace("domino's", "dominos")}
                                               if m.group(2) else {})})
_rule(r"^(?:which|what) (?:kind of |type of )?(.+?) do i (?:like|want|love|prefer|usually (?:order|get|have|eat|drink)|"
      r"always (?:order|get|have))\??$", "twin_recall", lambda m: {"item": m.group(1)})
_rule(r"^what(?:'s| is) my (?:favou?rite|usual|go-?to|preferred) (.+?)\??$", "twin_recall", lambda m: {"item": m.group(1)})
_rule(r"^((?:remember(?: that)?\s+)?my (?:favou?rite|usual|go-?to|preferred|regular) .{2,40}? (?:is|are) .{2,200}"
      r"|(?:remember(?: that)?\s+)?i (?:always|usually|normally) (?:order|get|have|eat|drink|buy|use|take) .{2,200}"
      r"|remember(?: that)?\s+i (?:love|like|prefer|hate|dislike|can't stand|am allergic to) .{2,200})$", "twin_remember",
      lambda m: {"statement": m.group(1)})
_rule(r"^(delete|wipe|clear) (the |my )?local ai data$", "local_ai_wipe")

# ---- 3.1: projects, Mission Mode, modes, emergency stop ----------------------------------------------------------
_P = r"(?:my |the )?([\w.\- ]{2,40}?)(?: project| repo| repository| folder)?"
_WHEN = {"yesterday": "yesterday", "today": "today", "this week": "week", "last week": "week", "recently": "week"}
_rule(r"^(?:emergency stop|stop everything|stop all(?: tasks)?|abort(?: everything| mission)?|cancel (?:the |my )?mission"
      r"|stop the mission)$", "emergency_stop")
_rule(r"^what (?:did|have) i (?:work(?:ed)? on|do|done|change(?:d)?)(?: (yesterday|today|this week|last week|recently))?"
      r"(?: (?:in|on) " + _P + r")?\??$", "project_activity",
      lambda m: {"when": _WHEN.get((m.group(1) or "yesterday").lower(), "yesterday"),
                 **({"project": m.group(2).strip()} if m.group(2) else {})})
_rule(r"^which files (?:changed|were changed|did i change)(?: (yesterday|today|this week|recently))?(?: (?:in|on) " + _P +
      r")?\??$", "project_activity",
      lambda m: {"when": _WHEN.get((m.group(1) or "recently").lower(), "week"),
                 **({"project": m.group(2).strip()} if m.group(2) else {})})
_rule(r"^(?:open|load|launch) " + _P + r"(?: in vs ?code)? and (?:check|show|look at|inspect)(?: me)? (?:the |its )?(?:latest|"
      r"recent|new)? ?changes$", "project_changes", lambda m: {"project": m.group(1).strip(), "open": True})
_rule(r"^(?:check|show|what are|inspect)(?: me)? (?:the )?(?:latest|recent|new) changes (?:in|on|for|to) " + _P + r"\??$",
      "project_changes", lambda m: {"project": m.group(1).strip()})
_rule(r"^(?:summari[sz]e|give me (?:a )?summary of|what(?:'s| is) the (?:current )?state of) (?:my |the )?"
      r"([\w.\- ]{2,40}?) (?:project|repo|repository)(?:'s current state| current state| state)?\??$", "project_summary",
      lambda m: {"project": m.group(1).strip()})
_rule(r"^(?:are there )?(?:any )?(?:unfinished|pending|open) (?:tasks|work|todos?)(?: (?:in|on|for) " + _P + r")?\??$",
      "project_todos", lambda m: {"project": (m.group(1) or "main").strip()})
_rule(r"^what should i work on(?: today| next| now)?(?: (?:in|on|for) " + _P + r")?\??$", "what_to_work_on",
      lambda m: {"project": m.group(1).strip()} if m.group(1) else {})
_rule(r"^(?:start a )?mission[:,]? (?:for |on )?(?:my |the )?([\w.\- ]{2,40}?) project[:,.]? ?(.*)$", "mission_project_report",
      lambda m: {"project": m.group(1).strip(), "objective": (m.group(2) or "").strip() or None} if m.group(2)
      else {"project": m.group(1).strip()})
_rule(r"^(?:.*\b)?my ([\w.\-]{2,40}) project\b(?=.*\b(?:check|inspect|review|audit|identify|go through)\b)"
      r"(.*\b(?:report|due|deadline|submission)\b.*)$", "mission_project_report",
      lambda m: {"project": m.group(1).strip(), "objective": ("my " + m.group(1) + " project" + m.group(2)).strip()[:480]})
_rule(r"^(?:debug|fix|repair) (?:my |the )?([\w.\- ]{2,40}?) (?:project|repo|repository|code)$|^(?:fix|repair) (?:the )?"
      r"(?:failing|broken) tests? in (?:my |the )?([\w.\- ]{2,40}?)(?: project)?$|^(?:find and fix|fix) (?:the )?bugs? in "
      r"(?:my |the )?([\w.\- ]{2,40}?)(?: project)?$|^make (?:the )?tests pass in (?:my |the )?([\w.\- ]{2,40}?)(?: project)?$",
      "debug_project", lambda m: {"project": "" if next(g for g in m.groups() if g).strip().lower() in
                                  ("my", "the", "this", "it", "our") else next(g for g in m.groups() if g).strip()})
_rule(r"^undo (?:the |your |my )?(?:last )?(?:code )?fix(?:es)?$|^revert (?:the |your )?(?:last )?fix(?:es)?$", "undo_last_fixes")
_rule(r"^(?:what mode are you in|which mode(?: are you in)?|current mode)\??$", "conversation_mode",
      lambda m: {"mode": "status"})
_rule(r"^register (?:the )?(?:model )?([\w.:\-]+) as (?:a |the )?([\w\- ]{2,40}?) (?:model )?for (.{3,300})$",
      "register_specialist_model", lambda m: {"model": m.group(1), "name": m.group(2), "skills": m.group(3)})


# Genesis already understands some of these requests (its own Word/Excel creation, "my preferred … is …"). For those the
# existing behaviour wins: these 3.0.1 commands are only used when Genesis's own rules found nothing ("soft").
SOFT_TOOLS = {"twin_remember", "draft_document", "excel_create"}


def is_soft(tool: str, args: dict) -> bool:
    if tool.startswith(("business_", "gace_", "privacy_")):
        return not args.get("_hard")
    return tool in SOFT_TOOLS and not (tool == "draft_document" and args.get("kind") == "ieee")


def match(text: str) -> Optional[tuple[str, dict]]:
    from .twin import TWIN_RE

    t = " ".join((text or "").strip().split()).rstrip(".!")
    m = TWIN_RE.match(t)
    if m and t[m.end():].strip():
        return "twin_answer", {"question": re.sub(r"^to\b:?\s*", "", t[m.end():].strip(), flags=re.I)}
    t = re.sub(r"^(genesis|hey genesis|suzanne|andrew)[, ]+", "", t, flags=re.I)
    t = re.sub(r"^(please|can you|could you)\s+", "", t, flags=re.I)
    from .modes import parse_mode

    mode = parse_mode(t)
    if mode:
        return "conversation_mode", {"mode": mode}
    for pat, tool, args in _RULES:
        m = pat.match(t)
        if m:
            return tool, args(m)
    from .coding import match as coding_match  # 3.5 GACE + mute commands
    from .business import match as business_match  # 3.4 Business Automation (fallback phase)

    return coding_match(text) or business_match(text)


def nlu(text: str, phase: str = "any"):
    """The matched command as an NLU result Genesis's planner runs directly (a FOLLOW_UP tool call), or None.

    ``phase``: "hard" = only commands that run before Genesis's own rules, "soft" = only the fallback ones, "any" = all.
    """
    hit = match(text)
    if hit is None:
        return None
    if phase != "any" and is_soft(*hit) != (phase == "soft"):
        return None
    from ..nlp.intents import Intent, NLUResult

    tool, args = hit
    args = {k: v for k, v in args.items() if k != "_hard"}
    return NLUResult(text=text, intent=Intent.FOLLOW_UP, confidence=0.97, source="rules",
                     slots={"tool": tool, "args": args, "description": tool.replace("_", " ")})
