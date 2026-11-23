"""Genesis settings tools (voice, listening, memory, start-with-Windows)."""
from __future__ import annotations

from ..core.events import EventType
from ..core.startup import set_start_with_windows
from .base import Tool, ToolResult, obj

SETTING_KEYS = {
    "listening": "voice.wake_word_enabled",
    "tts": "voice.tts_enabled",
    "memory": "memory.enabled",
    "start_with_windows": "system.start_with_windows",
}
LABELS = {
    ("listening", True): "I'm listening for “Hey Genesis” again.",
    ("listening", False): "I've stopped listening. The microphone is off until you turn it back on.",
    ("tts", True): "Voice replies are on.",
    ("tts", False): "Okay, I'll stay quiet and reply in text only.",
    ("memory", True): "Memory is on.",
    ("memory", False): "Memory is off. I won't store new facts or conversation history.",
    ("start_with_windows", True): "Genesis will now start automatically when you sign in to Windows.",
    ("start_with_windows", False): "Genesis will no longer start with Windows.",
}


class SetSetting(Tool):
    name = "set_setting"
    category = "settings"
    description = "Change a Genesis setting: listening, tts (voice replies), memory, start_with_windows."
    parameters = obj({"setting": {"type": "string", "enum": list(SETTING_KEYS)}, "value": {"type": "boolean"}}, ["setting", "value"])

    def run(self, args, ctx):
        key = SETTING_KEYS[args["setting"]]
        value = bool(args["value"])
        if args["setting"] == "start_with_windows":
            ok, detail = set_start_with_windows(value)
            if not ok:
                return ToolResult.fail(f"I couldn't change the startup setting: {detail}")
        ctx.settings.set(key, value)
        ctx.events.emit(EventType.SETTINGS_CHANGED, key=key, value=value)
        return ToolResult.ok(LABELS[(args["setting"], value)], key=key, value=value)

    def verify(self, args, result, ctx):
        return ctx.settings.get(SETTING_KEYS[args["setting"]]) == bool(args["value"]) if result.success else False


class SetLanguage(Tool):
    name = "set_language"
    category = "settings"
    description = ("Change which languages Genesis understands and replies in. reply: 'same' (reply in the user's language), "
                   "'english', or a language code such as 'fr' or 'hi' (always reply in that language). mode: 'auto' "
                   "(understand any language) or 'english' (English only).")
    parameters = obj({"reply": {"type": "string", "maxLength": 12}, "mode": {"type": "string", "enum": ["auto", "english"]}})

    def run(self, args, ctx):
        from ..nlp.language import LANGUAGES, language_name

        if not args.get("reply") and not args.get("mode"):
            return ToolResult.fail("Tell me which language you'd like, e.g. “always reply in English”.")
        said = []
        if args.get("reply"):
            reply = str(args["reply"]).strip()
            if reply not in ("same", "english") and reply not in LANGUAGES:
                return ToolResult.fail(f"I don't know the language “{reply}”.")
            ctx.settings.set("language.reply", reply)
            ctx.events.emit(EventType.SETTINGS_CHANGED, key="language.reply", value=reply)
            said.append({"same": "Okay — I'll reply in whatever language you speak to me.",
                         "english": "Okay — I'll always reply in English."}.get(reply, f"Okay — I'll reply in {language_name(reply)}."))
        if args.get("mode"):
            ctx.settings.set("language.mode", args["mode"])
            ctx.events.emit(EventType.SETTINGS_CHANGED, key="language.mode", value=args["mode"])
            said.append("I'll understand any language again." if args["mode"] == "auto"
                        else "Okay — English only. I won't translate other languages.")
        return ToolResult.ok(" ".join(said), **{k: v for k, v in args.items() if v})

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        ok = True
        if args.get("reply"):
            ok &= ctx.settings.get("language.reply") == args["reply"]
        if args.get("mode"):
            ok &= ctx.settings.get("language.mode") == args["mode"]
        return ok


def _data(h: dict) -> dict:
    return {("detail" if k == "message" else k): v for k, v in h.items()}


class CheckAIConnection(Tool):
    name = "check_ai_connection"
    category = "settings"
    description = "Check (and reload) Genesis's AI model configuration: key present, provider reachable, key valid, model available."
    parameters = obj({})

    def run(self, args, ctx):
        health = getattr(ctx.llm, "health", None)
        if health is None:
            return ToolResult.ok("My AI model is built in for this session.", available=True)
        h = health(probe=True, reload=True)
        where = f" ({h['provider']} · {h['model']})" if h.get("model") else f" ({h['provider']})"
        if h["available"]:
            return ToolResult.ok(f"My AI model is connected{where} — replied in {h['latency_ms']} ms.", **_data(h))
        tips = {"not_configured": "Add your key to the .env file in the Genesis folder, e.g. HF_TOKEN=hf_… (get one at "
                                  "huggingface.co → Settings → Access Tokens). I'll pick it up without a restart.",
                "offline_selected": "Set GENESIS_LLM_PROVIDER=huggingface (or openai / anthropic / gemini / ollama) in .env.",
                "auth_invalid": "Create a new token/key and replace it in .env.",
                "model_not_found": "Fix the model name in .env (HF_MODEL_ID / LLM_MODEL).",
                "network": "Check your internet connection or firewall/proxy.",
                "timeout": "The service is slow — try again, or raise GENESIS_LLM_TIMEOUT in .env."}
        warn = ("\nNote: " + " ".join(h["warnings"])) if h.get("warnings") else ""
        return ToolResult.ok(f"My AI model isn't working{where}: {h['message']}\n{tips.get(h['error_kind'] or '', '')}{warn}".strip(),
                             **_data(h))


TOOLS = [SetSetting(), SetLanguage(), CheckAIConnection()]
