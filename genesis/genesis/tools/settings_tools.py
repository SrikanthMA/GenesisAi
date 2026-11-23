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


TOOLS = [SetSetting()]
