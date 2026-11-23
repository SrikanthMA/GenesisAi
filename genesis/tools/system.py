"""System-control tools (power, volume, info, notifications).

There is deliberately NO generic "run shell command" tool. Each OS operation
is a narrow, typed tool with a fixed risk level.
"""
from __future__ import annotations

from ..security.permissions import RiskLevel
from ..services.platform import PlatformError
from .base import Tool, ToolResult, obj, s


class _Power(Tool):
    category = "system"
    action = ""

    def run(self, args, ctx):
        try:
            fn = getattr(ctx.backend, self.action)
            if self.action in ("shutdown", "restart"):
                fn(int(args.get("delay_seconds", 10)))
            else:
                fn()
        except (PlatformError, OSError, AttributeError) as exc:
            return ToolResult.fail(f"I couldn't {self.action} the computer: {exc}")
        simulated = getattr(ctx.backend, "dry_run_power", False)
        suffix = " (dry-run mode: simulated, nothing actually happened)" if simulated else ""
        return ToolResult.ok(self.done_message(args) + suffix, action=self.action, simulated=simulated)

    def done_message(self, args) -> str:
        return "Done."


class ShutdownComputer(_Power):
    name = "shutdown_computer"
    action = "shutdown"
    description = "Shut down the computer (HIGH risk — always asks for explicit confirmation)."
    parameters = obj({"delay_seconds": {"type": "integer", "minimum": 0, "maximum": 3600, "default": 10}})
    risk = RiskLevel.HIGH

    def confirmation_prompt(self, args):
        return "This will shut down your computer. Unsaved work will be lost. Do you want me to continue?"

    def done_message(self, args):
        return f"Shutting down in {args.get('delay_seconds', 10)} seconds. Say 'cancel shutdown' to abort."


class RestartComputer(_Power):
    name = "restart_computer"
    action = "restart"
    description = "Restart the computer (HIGH risk — always asks for explicit confirmation)."
    parameters = obj({"delay_seconds": {"type": "integer", "minimum": 0, "maximum": 3600, "default": 10}})
    risk = RiskLevel.HIGH

    def confirmation_prompt(self, args):
        return "This will restart your computer. Unsaved work will be lost. Do you want me to continue?"

    def done_message(self, args):
        return f"Restarting in {args.get('delay_seconds', 10)} seconds."


class CancelShutdown(_Power):
    name = "cancel_shutdown"
    action = "cancel_shutdown"
    description = "Abort a pending shutdown/restart."
    parameters = obj({})
    risk = RiskLevel.LOW

    def done_message(self, args):
        return "Cancelled the pending shutdown."


class SleepComputer(_Power):
    name = "sleep_computer"
    action = "sleep"
    description = "Put the computer to sleep."
    parameters = obj({})
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return "Put the computer to sleep now?"

    def done_message(self, args):
        return "Putting the computer to sleep."


class LockComputer(_Power):
    name = "lock_computer"
    action = "lock"
    description = "Lock the workstation (you'll need your password/PIN to get back in)."
    parameters = obj({})
    risk = RiskLevel.LOW

    def done_message(self, args):
        return "Locked the computer."


class SetVolume(Tool):
    name = "set_volume"
    category = "system"
    description = "Change the system volume: up, down, mute toggle, or set it to a level (0-100 %)."
    parameters = obj({"action": {"type": "string", "enum": ["volume_up", "volume_down", "mute", "set"]},
                      "steps": {"type": "integer", "minimum": 1, "maximum": 25, "default": 5},
                      "level": {"type": "integer", "minimum": 0, "maximum": 100}}, ["action"])

    def run(self, args, ctx):
        try:
            if args["action"] == "set":
                level = int(args.get("level", 50))
                ctx.backend.media_key("volume_down", 50)  # each key press is 2 %: go to 0, then up to the level
                if level >= 2:
                    ctx.backend.media_key("volume_up", round(level / 2))
                return ToolResult.ok(f"Set the volume to {level}%.", level=level)
            ctx.backend.media_key(args["action"], 1 if args["action"] == "mute" else args.get("steps", 5))
        except PlatformError as exc:
            return ToolResult.fail(str(exc))
        words = {"volume_up": "Turned the volume up.", "volume_down": "Turned the volume down.", "mute": "Toggled mute."}
        return ToolResult.ok(words[args["action"]])


class SystemInfo(Tool):
    name = "system_info"
    category = "system"
    description = "Report battery, CPU, memory and disk usage."
    parameters = obj({})

    def run(self, args, ctx):
        info = ctx.backend.system_info()
        parts = []
        if "battery_percent" in info:
            parts.append(f"battery at {info['battery_percent']}%" + (" (charging)" if info.get("plugged_in") else ""))
        if "cpu_percent" in info:
            parts.append(f"CPU at {info['cpu_percent']:.0f}%")
        if "memory_percent" in info:
            parts.append(f"memory at {info['memory_percent']:.0f}%")
        if "disk_free_gb" in info:
            parts.append(f"{info['disk_free_gb']:.0f} GB of disk free")
        if not parts:
            return ToolResult.fail("System information isn't available on this machine.")
        return ToolResult.ok("Your computer: " + ", ".join(parts) + ".", **info)


class Notify(Tool):
    name = "notify"
    category = "system"
    description = "Show a desktop notification."
    parameters = obj({"title": s("Title", maxLength=100), "message": s("Message", maxLength=500)}, ["message"])

    def run(self, args, ctx):
        ctx.notify(args.get("title") or "Genesis", args["message"])
        return ToolResult.ok("Notification shown.")


TOOLS = [ShutdownComputer(), RestartComputer(), CancelShutdown(), SleepComputer(), LockComputer(), SetVolume(), SystemInfo(), Notify()]
