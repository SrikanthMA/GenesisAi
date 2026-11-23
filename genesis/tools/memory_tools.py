"""Memory tools (explicit, user-requested only)."""
from __future__ import annotations

from ..security.validators import ValidationError, validate_email
from .base import Tool, ToolResult, obj, s


class Remember(Tool):
    name = "remember"
    category = "memory"
    description = "Store a fact the user explicitly asked Genesis to remember."
    parameters = obj({"text": s("The fact", maxLength=1000),
                      "kind": {"type": "string", "enum": ["fact", "instruction"], "default": "fact"}}, ["text"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I didn't save that. You can enable it in Settings.")
        mid = ctx.memory.remember(args["text"], kind=args.get("kind", "fact"))
        if args.get("kind") == "instruction":
            return ToolResult.ok(f"Understood — from now on I'll {args['text'][:1].lower() + args['text'][1:].rstrip('.')}. "
                                 "(Say “forget that” to undo.)", memory_id=mid)
        return ToolResult.ok(f"Got it — I'll remember that {args['text'].rstrip('.')}. (Say “forget that” to undo.)", memory_id=mid)


class Forget(Tool):
    name = "forget"
    category = "memory"
    description = "Delete remembered facts that match the text."
    parameters = obj({"text": s("Text to match", maxLength=500)}, ["text"])

    def run(self, args, ctx):
        n = ctx.memory.forget(args["text"])
        if not n:
            return ToolResult.ok("I didn't have anything like that stored.", deleted=0)
        return ToolResult.ok(f"Forgotten ({n} item{'s' if n != 1 else ''} deleted).", deleted=n)


class AddContact(Tool):
    name = "add_contact"
    category = "memory"
    description = "Save a contact (name, optional alias like 'professor', optional e-mail)."
    parameters = obj({"name": s("Name", maxLength=200), "alias": s("Relation/alias", maxLength=100),
                      "email": s("E-mail", maxLength=320)}, ["name"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I can't save contacts.")
        email = None
        if args.get("email"):
            try:
                email = validate_email(args["email"])
            except ValidationError as exc:
                return ToolResult.fail(str(exc))
        cid = ctx.memory.save_contact(args["name"], email=email, alias=args.get("alias"))
        bits = [args["name"]]
        if args.get("alias"):
            bits.append(f"(your {args['alias']})")
        if email:
            bits.append(f"<{email}>")
        return ToolResult.ok("Saved contact " + " ".join(bits) + ".", name=args["name"], email=email, contact_id=cid)


PREF_LABELS = {"coding.language": "preferred coding language", "pref.email": "preferred e-mail", "pref.browser": "preferred browser",
               "pref.search": "preferred search engine", "pref.music": "preferred music app", "pref.editor": "preferred code editor"}
PREF_VALUE_NAMES = {"gmail": "Gmail", "outlook": "Outlook", "chrome": "Chrome", "edge": "Edge", "firefox": "Firefox", "brave": "Brave",
                    "google": "Google", "bing": "Bing", "duckduckgo": "DuckDuckGo", "spotify": "Spotify", "youtube": "YouTube",
                    "vscode": "VS Code", "pycharm": "PyCharm"}


class ListMemory(Tool):
    name = "list_memory"
    category = "memory"
    description = "Show what Genesis remembers."
    parameters = obj({})

    def run(self, args, ctx):
        snap = ctx.memory.snapshot()
        sections: list[tuple[str, list[str]]] = []
        ident = ctx.identity.get() if getattr(ctx, "identity", None) else None
        if ident and ident.canonical:
            sections.append(("You", [ctx.identity.describe()]))
        from ..services.coderunner import language_for

        def shown(k, v):
            if k == "coding.language" and isinstance(v, str) and language_for(v):
                return language_for(v).name
            return PREF_VALUE_NAMES.get(v, v) if isinstance(v, str) else v

        prefs = [f"{PREF_LABELS.get(k, k)}: {shown(k, v)}" for k, v in ctx.repos.preferences.all().items() if k in PREF_LABELS]
        sections.append(("Preferences", prefs))
        facts = snap["facts"]
        sections.append(("Instructions", [f["content"] for f in facts if f.get("kind") == "instruction"][:15]))
        sections.append(("Facts", [f["content"] for f in facts if f.get("kind") not in ("instruction", "preference")][:20]))
        sections.append(("Contacts", [c["name"] + (f" ({c['alias']})" if c.get("alias") else "") + (f" <{c['email']}>" if c.get("email") else "")
                                      for c in snap["contacts"][:20]]))
        sections.append(("Projects", [f"{p['name']} → {p['path']}" for p in snap["projects"][:10]]))
        sections.append(("Routines", [f"{r['name']}: {r['commands']}" for r in ctx.repos.routines.list()[:10]]))
        if ctx.settings.get("memory.track_usage", True):
            apps = [u["key"].split(":", 1)[1] for u in ctx.repos.usage.top(30) if u["key"].startswith("app:")][:5]
            sections.append(("Apps you use most", [", ".join(apps)] if apps else []))
        body = [f"{title}:\n" + "\n".join(f"• {x}" for x in items) for title, items in sections if items]
        if not body:
            state = "" if snap["enabled"] else " (memory is currently turned off)"
            return ToolResult.ok(f"I don't have anything stored about you{state}.", **snap)
        return ToolResult.ok("Here's what I remember (you can edit or delete any of it on the Memory page):\n" + "\n".join(body), **snap)


class RememberProject(Tool):
    name = "remember_project"
    category = "memory"
    description = "Remember that a named project lives in a folder (so 'open my <name> project' works)."
    parameters = obj({"name": s("Project name", maxLength=100), "path": s("Folder path", maxLength=1000)}, ["name", "path"])

    def run(self, args, ctx):
        from ..security.validators import validate_path

        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I didn't save that.")
        try:
            path = validate_path(args["path"], allowed_roots=ctx.allowed_roots() + _home_drives(), must_exist=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if not path.is_dir():
            return ToolResult.fail(f"{path} is not a folder.")
        ctx.repos.projects.set(args["name"], str(path))
        what = "your project folder is" if args["name"].lower() == "main" else f"your {args['name']} project is in"
        return ToolResult.ok(f"Got it — {what} {path}. (Say “forget that” to undo.)", project=args["name"].lower(), path=str(path))

    def verify(self, args, result, ctx):
        row = ctx.repos.projects.get(args["name"])
        return bool(result.success and row and row["path"] == result.data["path"])


def _home_drives():
    from pathlib import Path

    return [Path.home().resolve()]


class Recall(Tool):
    name = "recall"
    category = "memory"
    description = "Say what Genesis remembers about a topic (projects, people, facts, routines)."
    parameters = obj({"topic": s("Topic, e.g. 'my genesis project', 'Rahul'", maxLength=200)}, ["topic"])

    def run(self, args, ctx):
        topic = args["topic"].lower().removeprefix("my ").removesuffix(" folder").removesuffix(" project").strip()
        if topic in ("project", ""):
            topic = "main"
        found = []
        proj = ctx.repos.projects.get(topic)
        if proj:
            found.append(f"Your project folder is {proj['path']}." if proj["name"] == "main"
                         else f"Your {proj['name']} project is in {proj['path']}.")
        c = ctx.memory.find_contact(args["topic"])
        if c:
            found.append(f"{c['name']}" + (f" is your {c['alias']}" if c.get("alias") else "") + (f", e-mail {c['email']}" if c.get("email") else "") + ".")
        for f in ctx.repos.memory.list():
            if topic and topic in f["content"].lower():
                found.append(f["content"].rstrip(".") + ".")
        for r in ctx.repos.routines.list():
            if topic and (topic in r["name"].lower() or topic in r["commands"].lower()):
                found.append(f"Routine “{r['name']}”: {r['commands']}.")
        if not found:
            return ToolResult.ok(f"I don't have anything saved about {args['topic']}.", items=[])
        return ToolResult.ok("Here's what I remember: " + " ".join(found[:8]), items=found)


class ForgetItem(Tool):
    name = "forget_item"
    category = "memory"
    description = "Delete one specific memory item (the one just saved or mentioned)."
    parameters = obj({"kind": {"type": "string", "enum": ["fact", "contact", "project", "routine", "preference"]},
                      "ref": s("Item id, project name, or 'key|fact_id' for a preference", maxLength=200)}, ["kind", "ref"])

    def run(self, args, ctx):
        kind, ref = args["kind"], args["ref"]
        if kind == "fact":
            ok = ctx.repos.memory.delete(int(ref))
        elif kind == "contact":
            ok = ctx.repos.contacts.delete(int(ref))
        elif kind == "routine":
            ok = ctx.repos.routines.delete(int(ref))
        elif kind == "preference":
            key, _, fact_id = ref.partition("|")
            ok = ctx.repos.preferences.delete(key)
            if fact_id.isdigit():
                ctx.repos.memory.delete(int(fact_id))
        else:
            ok = ctx.repos.projects.get(ref) is not None
            ctx.repos.projects.delete(ref)
        if not ok:
            return ToolResult.fail("I couldn't find that item any more — it may already be deleted.")
        return ToolResult.ok(f"Forgotten — I deleted that {kind}.", kind=kind, ref=ref)


class RememberPreference(Tool):
    name = "remember_preference"
    category = "memory"
    description = "Remember a user preference Genesis should apply later (e.g. coding.language = python)."
    parameters = obj({"key": s("Preference key, e.g. coding.language", maxLength=100), "value": s("Value", maxLength=200),
                      "text": s("How the user said it", maxLength=500)}, ["key", "value"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I didn't save that.")
        ctx.memory.set_preference(args["key"], args["value"])
        if args["key"] == "pref.search":
            ctx.settings.set("search.engine", args["value"])  # the search tools read this setting
        label = PREF_LABELS.get(args["key"], args["key"])
        fact = f"My {label} is {args['value']}."
        for f in ctx.repos.memory.list():  # keep one fact per preference
            if f["content"].lower().startswith(f"my {label} is"):
                ctx.repos.memory.delete(f["id"])
        mid = ctx.repos.memory.add(fact, kind="preference")
        shown = PREF_VALUE_NAMES.get(args["value"], args["value"])
        if args["key"] == "coding.language":
            from ..services.coderunner import language_for

            lang = language_for(args["value"])
            shown = lang.name if lang else args["value"].capitalize()
        return ToolResult.ok(f"Got it — I'll use {shown} by default from now on. (Say “forget that” to undo.)",
                             memory_id=mid, key=args["key"], value=args["value"])

    def verify(self, args, result, ctx):
        return ctx.memory.get_preference(args["key"]) == args["value"] if result.success else False


class SetIdentity(Tool):
    name = "set_identity"
    category = "memory"
    description = "Store the user's name: set_name (full name), set_preferred (what to call them), add_nickname, " \
                  "learn_variant (another spelling the speech recogniser produces), forget."
    parameters = obj({"action": {"type": "string", "enum": ["set_name", "fix_spelling", "set_preferred", "add_nickname", "learn_variant",
                                                             "forget"]},
                      "name": s("Name", maxLength=120)}, ["action"])

    def run(self, args, ctx):
        if not ctx.memory.enabled:
            return ToolResult.fail("Memory is turned off, so I can't remember your name.")
        idm, a, name = ctx.identity, args["action"], (args.get("name") or "").strip()
        if a != "forget" and not name:
            return ToolResult.fail("What's the name?")
        if a == "set_name":
            ident = idm.set_name(name)
            msg = f"Nice to meet you, {ident.call_name}! I'll remember your name is {ident.canonical}."
        elif a == "fix_spelling":
            ident = idm.fix_spelling(name)
            msg = f"Thanks — fixed. Your name is {ident.canonical}."
        elif a == "set_preferred":
            ident = idm.set_preferred(name)
            msg = f"Okay — I'll call you {ident.preferred}."
        elif a == "add_nickname":
            ident = idm.get()
            ident.nicknames.append(name)
            idm.save(ident)
            msg = f"Got it — {name} is you too."
        elif a == "learn_variant":
            idm.learn_variant(name)
            ident = idm.get()
            msg = f"Thanks — I'll recognise “{name}” as you, {ident.call_name}."
        else:
            idm.forget()
            return ToolResult.ok("Done — I've forgotten your name.", action=a)
        return ToolResult.ok(msg, action=a, canonical=ident.canonical, preferred=ident.preferred)

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        ident = ctx.identity.get()
        return {"forget": not ident.canonical, "set_name": bool(ident.canonical), "set_preferred": bool(ident.preferred)}.get(
            args["action"], True)


TOOLS = [Remember(), Forget(), AddContact(), ListMemory(), RememberProject(), Recall(), ForgetItem(), RememberPreference(),
         SetIdentity()]
