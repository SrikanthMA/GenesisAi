"""Form filling from the user's own data (their résumé / known profile), with strict rules:

* only values that are actually in the source (résumé, your name, facts you asked Genesis to remember) are filled —
  no guessing; long free-text answers are drafted from the résumé and flagged for review;
* sensitive fields (passwords, ID numbers, bank/card details, date of birth, salary, demographic questions, consent
  boxes) are never filled — Genesis lists them and asks;
* submitting is a separate HIGH-risk step that always asks first.

Works on the page in Genesis's automated browser (DOM fields) or, without it, on the active window's accessible fields.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..ai.llm.base import LLMError
from ..core.logging_setup import get_logger
from ..security.injection import wrap_untrusted
from ..security.permissions import RiskLevel
from .base import Tool, ToolResult, obj, s

log = get_logger("tools.forms")
SENSITIVE = re.compile(r"password|passcode|\bpin\b|ssn|social security|aadhaar|aadhar|\bpan\b|passport|national id|tax id|bank|"
                       r"account (?:number|no)|iban|routing|credit card|debit card|card number|cvv|cvc|expir|date of birth|\bdob\b|"
                       r"birth ?date|\bage\b|salary|compensation|ctc|gender|sex\b|race|ethnic|disab|veteran|religio|caste|marital|"
                       r"criminal|conviction|signature|security question|mother'?s maiden|otp|verification code|captcha", re.I)
CONSENT = re.compile(r"\b(i agree|agree|consent|terms|privacy policy|acknowledge|certify|confirm that|subscribe|newsletter|marketing)\b",
                     re.I)
FREE_TEXT = re.compile(r"\b(why|describe|tell us|cover letter|motivation|about yourself|summary|additional information|anything else)\b",
                       re.I)
MAP_PROMPT = """Fill in a form for the user from THEIR OWN data below. Only use facts that appear in the data. Never invent anything.
Skip (put in "missing") any field whose answer is not in the data, and ALL sensitive fields (passwords, ID/passport/tax numbers,
bank or card details, date of birth, salary, gender, ethnicity, disability, religion, consent/terms checkboxes).
For open questions (cover letter, "why do you want…") you may draft 2-4 sentences strictly based on the résumé.
FORM FIELDS (id, label, type, options):
{fields}
USER DATA:
{data}
Return JSON: {{"fills": [{{"id": 1, "value": "..."}}], "missing": [{{"id": 2, "reason": "not in your résumé"}}]}}"""


# ------------------------------------------------------------------ surfaces
class _BrowserSurface:
    kind = "browser"

    def __init__(self, browser) -> None:
        self.b = browser

    def fields(self) -> dict:
        return self.b.form_fields()

    def fill(self, f: dict, value: str) -> None:
        self.b.fill_field(f["id"], value, "select" if f["type"] == "select" else f["type"])

    def value(self, f: dict) -> str:
        return self.b.field_value(f["id"])

    def submit(self) -> dict:
        return self.b.submit_form()


class _DesktopSurface:
    kind = "desktop"

    def __init__(self, computer) -> None:
        self.c = computer
        self._els: dict[int, object] = {}

    def fields(self) -> dict:
        info = self.c.observe(limit=150)
        self._els = {e.id: e for e in self.c.last_elements}
        fields = [{"id": e.id, "label": e.name, "type": "password" if e.is_password else "text", "name": "", "required": False,
                   "value": e.value, "options": []} for e in self.c.last_elements if e.role == "field"]
        return {"url": "", "title": info["window"]["title"], "fields": fields}

    def fill(self, f: dict, value: str) -> None:
        el = self._els.get(f["id"])
        if el is None:
            raise RuntimeError("field vanished")
        if not self.c.driver.set_value(el, value):
            self.c.type(value, el, replace=True)

    def value(self, f: dict) -> str:
        self.c.observe(limit=150)
        el = next((e for e in self.c.last_elements if e.id == f["id"]), None)
        return getattr(el, "value", "") if el else ""

    def submit(self) -> dict:
        el = self.c.find("submit", role="button")
        self.c.click(el)
        return {"button": el.name, "url_before": "", "url": "", "title": "", "text": ""}


def _surface(ctx):
    b = ctx.browser
    try:
        if b.available() and b.current_url():
            return _BrowserSurface(b)
    except Exception:
        pass
    c = getattr(ctx, "computer", None)
    if c is not None and c.available():
        return _DesktopSurface(c)
    return None


# ------------------------------------------------------------------ profile data
RESUME_NAMES = ("resume", "résumé", "cv", "curriculum vitae")


def find_resume(ctx, hint: str = "") -> Optional[Path]:
    from .files import search_files

    if hint and re.search(r"[\\/]|\.\w{2,4}$", hint):
        p = Path(hint).expanduser()
        return p if p.is_file() else None
    saved = ctx.settings.get("profile.resume_path", "") or ""
    if saved and Path(saved).is_file():
        return Path(saved)
    best: list[Path] = []
    for word in ("resume", "cv"):
        try:
            best += [p for p in search_files(ctx, word, limit=10, sort="newest", time_budget=3.0)
                     if p.suffix.lower() in (".pdf", ".docx", ".txt", ".md") and re.search(r"(resume|résumé|\bcv\b|curriculum)", p.stem, re.I)]
        except Exception:
            continue
    best.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return best[0] if best else None


def profile_text(ctx, source: str = "resume") -> tuple[str, str]:
    """(label, text) of the user's own data used for filling."""
    from ..memory.knowledge import read_document

    parts: list[str] = []
    label = "your profile"
    ident = ctx.identity.get() if getattr(ctx, "identity", None) else None
    if ident and ident.canonical:
        parts.append(f"Full name: {ident.canonical}")
    acct = None
    try:
        acct = ctx.repos.services.get("google")
    except Exception:
        pass
    if acct and acct.get("account"):
        parts.append(f"Email: {acct['account']}")
    if source != "profile":
        path = find_resume(ctx, source if source not in ("resume", "cv") else "")
        if path is None:
            raise FileNotFoundError("resume")
        parts.append(read_document(path)[:12_000])
        label = f"your résumé ({path.name})"
    facts = ctx.memory.context_for_prompt() if ctx.memory.enabled else ""
    if facts:
        parts.append(facts[:2000])
    return label, "\n".join(parts)


# ------------------------------------------------------------------ mapping
def _grounded(value: str, text: str) -> bool:
    v = value.strip().lower()
    if not v:
        return False
    low = text.lower()
    digits = re.sub(r"\D", "", v)
    if len(digits) >= 7 and digits in re.sub(r"\D", "", low):
        return True
    toks = [t for t in re.findall(r"[\w@.+-]{2,}", v) if t not in ("the", "and", "of", "in", "at")]
    return bool(toks) and sum(1 for t in toks if t in low) >= max(1, (len(toks) + 1) // 2)


def heuristic_fills(fields: list[dict], text: str) -> dict[int, str]:
    """No-LLM mapping for the common contact fields."""
    found: dict[str, str] = {}
    m = re.search(r"Full name:\s*(.+)", text)
    if m:
        found["name"] = m.group(1).strip()
    m = re.search(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+", text)
    if m:
        found["email"] = m.group(0)
    m = re.search(r"(?:\+?\d[\d\s().-]{8,}\d)", text)
    if m:
        found["phone"] = re.sub(r"\s+", " ", m.group(0)).strip()
    for key, rx in (("linkedin", r"(?:https?://)?(?:www\.)?linkedin\.com/in/[\w-]+/?"), ("github", r"(?:https?://)?github\.com/[\w-]+/?")):
        m = re.search(rx, text, re.I)
        if m:
            found[key] = m.group(0)
    out: dict[int, str] = {}
    for f in fields:
        lab = f"{f['label']} {f.get('name', '')}".lower()
        if "email" in lab and "email" in found:
            out[f["id"]] = found["email"]
        elif re.search(r"phone|mobile|contact number|tel", lab) and "phone" in found:
            out[f["id"]] = found["phone"]
        elif "linkedin" in lab and "linkedin" in found:
            out[f["id"]] = found["linkedin"]
        elif "github" in lab and "github" in found:
            out[f["id"]] = found["github"]
        elif "name" in found and re.search(r"\bfirst\b|given", lab):
            out[f["id"]] = found["name"].split()[0]
        elif "name" in found and re.search(r"\blast\b|surname|family", lab):
            out[f["id"]] = found["name"].split()[-1]
        elif "name" in found and re.search(r"\b(full )?name\b", lab) and not re.search(r"company|school|university|user", lab):
            out[f["id"]] = found["name"]
    return out


def plan_fills(ctx, fields: list[dict], text: str) -> tuple[dict[int, str], list[dict], bool]:
    """→ ({field_id: value}, missing [{id,label,reason}], used_llm)"""
    fills: dict[int, str] = {}
    missing: list[dict] = []
    used_llm = False
    candidates = [f for f in fields if f["type"] not in ("password",)]
    if getattr(ctx.llm, "is_generative", False) and candidates:
        try:
            spec = "\n".join(json.dumps({"id": f["id"], "label": f["label"], "type": f["type"],
                                         **({"options": f["options"][:15]} if f.get("options") else {})}) for f in candidates)
            data = ctx.llm.chat_json([{"role": "user", "content": MAP_PROMPT.format(fields=wrap_untrusted(spec, "web_form"),
                                                                                      data=wrap_untrusted(text[:9000], "profile"))}],
                                     max_tokens=1500, temperature=0.1)
            for item in (data.get("fills") or []) if isinstance(data, dict) else []:
                try:
                    fills[int(item["id"])] = str(item["value"]).strip()
                except (KeyError, TypeError, ValueError):
                    continue
            used_llm = True
        except LLMError as exc:
            log.info("form mapping via LLM failed: %s", exc)
    if not used_llm:
        fills = heuristic_fills(candidates, text)
    by_id = {f["id"]: f for f in fields}
    final: dict[int, str] = {}
    for fid, value in fills.items():
        f = by_id.get(fid)
        if f is None or not value:
            continue
        label = f"{f['label']} {f.get('name', '')}"
        if SENSITIVE.search(label) or f["type"] == "password":
            continue
        if f["type"] in ("checkbox", "radio") and CONSENT.search(label):
            continue
        if f.get("options") and value not in f["options"]:
            continue
        free = f["type"] == "textarea" and FREE_TEXT.search(label)
        if not free and not _grounded(value, text):
            continue  # not supported by the user's data → don't guess
        final[fid] = value
    for f in fields:
        if f["id"] in final or (f.get("value") and f["type"] not in ("checkbox", "radio")):
            continue
        label = f"{f['label']} {f.get('name', '')}"
        if SENSITIVE.search(label) or f["type"] == "password":
            reason = "sensitive — please fill this in yourself"
        elif f["type"] in ("checkbox", "radio") and CONSENT.search(label):
            reason = "needs your own decision"
        else:
            reason = "not in your data"
        if f.get("required") or reason != "not in your data" or f["type"] not in ("checkbox", "radio"):
            missing.append({"id": f["id"], "label": f["label"] or f.get("name") or f"field {f['id']}", "reason": reason,
                            "required": bool(f.get("required"))})
    return final, missing, used_llm


# ------------------------------------------------------------------ tools
class FillForm(Tool):
    name = "fill_form"
    category = "forms"
    description = ("Fill the form on the current page/window with the user's own information (their résumé by default). "
                   "Never guesses, never fills sensitive fields, never submits (that's form_submit).")
    parameters = obj({"source": s("'resume' (default), 'profile', or a path to the document to use", maxLength=500)})
    output = {"filled": "list[{label, value}]", "missing": "list[{label, reason, required}]", "surface": "browser|desktop"}
    risk = RiskLevel.LOW  # it types into a page but never submits (form_submit is HIGH)
    remote = True

    def confirmation_prompt(self, args):
        return "Fill in this form using your résumé? I won't submit it."

    def run(self, args, ctx):
        surf = _surface(ctx)
        if surf is None:
            return ToolResult.fail("I can't see a form to fill. Open it in my browser (say “open <website> in your browser”) "
                                   "or bring its window to the front.")
        try:
            info = surf.fields()
        except Exception as exc:
            return ToolResult.fail(f"I couldn't read the form: {exc}")
        fields = info.get("fields") or []
        if not fields:
            return ToolResult.fail("I don't see any form fields on this page.")
        try:
            label, text = profile_text(ctx, args.get("source") or "resume")
        except FileNotFoundError:
            return ToolResult.fail("I couldn't find your résumé.", hint="Tell me where it is, e.g. “my resume is "
                                   "Documents\\resume.pdf”, or save it in Documents with “resume” in the name.")
        except Exception as exc:
            return ToolResult.fail(f"I couldn't read your résumé: {exc}")
        values, missing, used_llm = plan_fills(ctx, fields, text)
        by_id = {f["id"]: f for f in fields}
        done, failed = [], []
        for fid, value in values.items():
            try:
                surf.fill(by_id[fid], value)
                done.append({"id": fid, "label": by_id[fid]["label"], "value": value})
            except Exception as exc:
                log.info("fill failed for field %s: %s", fid, exc)
                failed.append(by_id[fid]["label"])
        form = {"url": info.get("url", ""), "title": info.get("title", ""), "surface": surf.kind,
                "fields": [dict(f, value=values.get(f["id"], f.get("value", ""))) for f in fields], "filled": done,
                "missing": missing}
        ctx.extras["last_form"] = form
        plain = [m for m in missing if m["reason"] == "not in your data"]
        ctx.extras["last_form_field"] = (plain or missing)[0]["label"] if missing else (done[-1]["label"] if done else None)
        if not done:
            return ToolResult.fail("I couldn't fill anything from " + label + " — none of the fields matched what's in it.",
                                   missing=missing, form=form)
        lines = "\n".join(f"• {d['label'] or 'field'}: {d['value'][:80]}" for d in done[:12])
        msg = f"I filled {len(done)} field{'s' if len(done) != 1 else ''} from {label}:\n{lines}"
        if missing:
            msg += "\nI left these for you:\n" + "\n".join(f"• {m['label']} ({m['reason']})" for m in missing[:10])
        if failed:
            msg += f"\nI couldn't type into: {', '.join(failed[:5])}."
        if not used_llm:
            msg += "\n(My AI model is offline, so I only filled the basic contact fields.)"
        msg += "\nI haven't submitted it — say “submit it” when you're happy, or “change <field> to …”."
        return ToolResult.ok(msg, filled=done, missing=missing, surface=surf.kind, form=form)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        surf = _surface(ctx)
        if surf is None:
            return None
        try:
            fields = {f["id"]: f for f in result.data["form"]["fields"]}
            checks = [surf.value(fields[d["id"]]).strip() == d["value"].strip() for d in result.data["filled"][:10]
                      if fields[d["id"]]["type"] not in ("select", "checkbox", "radio")]
            return all(checks) if checks else None
        except Exception:
            return None


class FormSetField(Tool):
    name = "form_set_field"
    category = "forms"
    description = "Change one answer in the form Genesis is filling ('change my phone number to …', 'set the city to Pune')."
    parameters = obj({"field": s("Field label/description, or 'that' for the one just discussed", maxLength=200),
                      "value": s("New value", maxLength=5000)}, ["value"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        from .web import rank_elements

        form = ctx.extras.get("last_form")
        surf = _surface(ctx)
        if not form or surf is None:
            return ToolResult.fail("There's no form in progress. Say “fill this form using my resume” first.")
        want = (args.get("field") or "").strip()
        if not want or re.fullmatch(r"(that|this|it|that one|that question|this question|the answer)", want, re.I):
            want = ctx.extras.get("last_form_field") or ""
        if not want:
            return ToolResult.fail("Which field should I change?")
        fields = form["fields"]
        if SENSITIVE.search(want):
            return ToolResult.fail("That's a sensitive field — please fill it in yourself. I never enter it for you.")
        ranked = rank_elements([{"name": f["label"] or f.get("name", ""), "role": "field", "_f": f} for f in fields], want, "field")
        if not ranked:
            return ToolResult.fail(f"I can't find a field like “{want}” in the form.")
        f = ranked[0][1]["_f"]
        if SENSITIVE.search(f["label"]) or f["type"] == "password":
            return ToolResult.fail("That's a sensitive field — please fill it in yourself.")
        try:
            surf.fill(f, args["value"])
        except Exception as exc:
            return ToolResult.fail(f"I couldn't change “{f['label']}”: {exc}")
        f["value"] = args["value"]
        form["missing"] = [m for m in form["missing"] if m["id"] != f["id"]]
        ctx.extras["last_form_field"] = f["label"]
        return ToolResult.ok(f"Changed “{f['label']}” to “{args['value'][:120]}”. Still not submitted.", field=f["label"],
                             value=args["value"], field_id=f["id"])

    def verify(self, args, result, ctx):
        surf = _surface(ctx)
        if not result.success or surf is None:
            return None if result.success else False
        try:
            f = next(x for x in ctx.extras["last_form"]["fields"] if x["id"] == result.data["field_id"])
            return surf.value(f).strip() == args["value"].strip() if f["type"] not in ("select", "checkbox", "radio") else None
        except Exception:
            return None


class FormSubmit(Tool):
    name = "form_submit"
    category = "forms"
    description = "Submit the form currently being filled (clicks its Submit/Apply button). Always asks first."
    parameters = obj({})
    risk = RiskLevel.HIGH
    always_confirm = True
    remote = True

    def confirmation_prompt(self, args):
        return "Submit the form now? Once it's sent it can't be taken back."

    def run(self, args, ctx):
        surf = _surface(ctx)
        form = ctx.extras.get("last_form") or {}
        if surf is None:
            return ToolResult.fail("There's no form open to submit.")
        if not form:
            return ToolResult.fail("I haven't filled a form in this session, so I won't submit one blindly. "
                                   "Say “fill this form using my resume” first, or press Submit yourself.")
        if form.get("surface") and form["surface"] != surf.kind:
            return ToolResult.fail("The form I filled isn't the one in front of me now, so I didn't submit anything.")
        if surf.kind == "desktop":
            try:
                title = ctx.computer.window().title
            except Exception:
                title = None
            if title != form.get("title"):
                return ToolResult.fail("The window changed since I filled the form, so I didn't submit anything.")
        if surf.kind == "browser" and form.get("url") and (ctx.browser.current_url() or "").split("#")[0] != form["url"].split("#")[0]:
            return ToolResult.fail("The page changed since I filled the form, so I didn't submit anything. Please check it.")
        req = [m["label"] for m in form.get("missing", []) if m.get("required")]
        if req:
            return ToolResult.fail("I didn't submit it: these required fields are still empty — " + ", ".join(req[:6]) + ".")
        try:
            info = surf.submit()
        except Exception as exc:
            return ToolResult.fail(f"I couldn't submit the form: {exc}")
        ok_text = re.search(r"\b(thank you|thanks for|submitted|received|success|application (?:has been )?sent|we'?ll be in touch)\b",
                            info.get("text", ""), re.I)
        err_text = re.search(r"\b(required|invalid|please (?:fill|enter|correct)|error)\b", info.get("text", ""), re.I)
        changed = info.get("url") and info.get("url") != info.get("url_before")
        if err_text and not ok_text and not changed:
            return ToolResult.fail("I clicked Submit, but the page is showing a problem with the form (a required or invalid "
                                   "field). It was not sent.", **info)
        ctx.extras.pop("last_form", None)
        return ToolResult.ok(f"Submitted (I clicked “{info.get('button', 'Submit')}”)." +
                             (" The page confirms it was received." if ok_text else ""), confirmed_by_page=bool(ok_text or changed), **info)

    def verify(self, args, result, ctx):
        return (True if result.data.get("confirmed_by_page") else None) if result.success else False


TOOLS = [FillForm(), FormSetField(), FormSubmit()]
