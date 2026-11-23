"""2.1 tools: Google Meet, meeting minutes (MOM) and voice chat mode."""
from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path

from ..core.logging_setup import get_logger
from ..security.permissions import RiskLevel
from ..tools.base import Tool, ToolResult, obj, s
from . import meeting as mt
from . import voice_ai

log = get_logger("v2.meet_tools")
STATE_KEY = "v2_meeting"


def _out_dir(ctx) -> Path:
    base = getattr(getattr(ctx, "office", None), "out_dir", None) or (Path.home() / "Documents" / "Genesis Documents")
    return Path(base) / "Meeting Minutes"


class MeetOpen(Tool):
    name = "meet_open"
    category = "meetings"
    description = "Open Google Meet in the browser: the Meet home page, a brand-new meeting, or a meeting by its code."
    parameters = obj({"op": {"type": "string", "enum": ["home", "new", "join"], "default": "home"},
                      "code": s("Meeting code like abc-defg-hij or a meet.google.com link", maxLength=200)})

    def run(self, args, ctx):
        op = args.get("op", "home")
        code = (args.get("code") or "").strip()
        if op == "join" and not mt.normalise_code(code) and not code.startswith("https://meet.google.com/"):
            return ToolResult.fail("Which meeting? Tell me its code, like abc-defg-hij.", kind="needs_input")
        url = mt.meet_url(op, code)
        if not ctx.backend.open_url(url):
            return ToolResult.fail("I couldn't open the browser.")
        msg = {"new": "Starting a new Google Meet meeting in your browser.",
               "join": f"Opening meeting {mt.normalise_code(code) or code} in Google Meet."}.get(op, "Opening Google Meet.")
        return ToolResult.ok(msg + " Say “take meeting notes” when it starts and I'll write the minutes.", url=url)

    def verify(self, args, result, ctx):
        return result.success


class MeetingNotesStart(Tool):
    name = "meeting_notes_start"
    category = "meetings"
    description = ("Start recording the current meeting on this PC (computer sound + microphone) to write minutes of "
                   "meeting (MOM) afterwards.")
    parameters = obj({"title": s("Meeting title", maxLength=120)})
    risk = RiskLevel.MEDIUM
    always_confirm = True  # recording other people needs their agreement - always ask

    def confirmation_prompt(self, args):
        return ("I'll record this meeting's audio on your PC (your voice and the other participants') to write the "
                "minutes. Please make sure everyone in the meeting agrees to being recorded. Start recording?")

    def run(self, args, ctx):
        cur = ctx.extras.get(STATE_KEY)
        if cur and cur.running:
            return ToolResult.ok(f"I'm already taking notes ({int(cur.elapsed() // 60)} min so far). "
                                 "Say “stop meeting notes” to finish.", already=True)
        factory = ctx.extras.get("v2_meeting_sources") or mt.default_sources
        sources, notes = factory(ctx.config)
        if not sources:
            return ToolResult.fail("I can't record: no audio source is available. " + " ".join(notes))
        transcriber = ctx.extras.get("v2_meeting_transcriber") or mt.make_transcriber(ctx.config)
        folder = ctx.config.paths.data_dir / "meetings" / datetime.now().strftime("%Y%m%d-%H%M%S")
        rec = mt.MeetingRecorder(folder=folder, transcriber=transcriber, sources=sources,
                                 title=(args.get("title") or "Meeting").strip(),
                                 chunk_seconds=int(ctx.extras.get("v2_meeting_chunk_seconds", mt.CHUNK_SECONDS)))
        try:
            rec.start()
        except Exception as exc:
            return ToolResult.fail(f"I couldn't start recording: {exc}")
        ctx.extras[STATE_KEY] = rec
        tracks = " and ".join(sorted(src.track.lower().replace("others", "the computer's sound").replace("you", "your microphone")
                                     for src in rec.sources))
        extra = (" " + " ".join(notes)) if notes else ""
        return ToolResult.ok(f"Taking meeting notes now (recording {tracks}).{extra} Say “stop meeting notes” when the "
                             "meeting ends and I'll write the minutes.", tracks=[src.track for src in rec.sources])

    def verify(self, args, result, ctx):
        rec = ctx.extras.get(STATE_KEY)
        return bool(result.success and rec is not None and rec.running)


class MeetingNotesStop(Tool):
    name = "meeting_notes_stop"
    category = "meetings"
    description = "Stop recording the meeting and write the minutes of meeting (MOM) as a Word document, then open it."
    parameters = obj({"start_if_idle": {"type": "boolean"}})

    def run(self, args, ctx):
        rec = ctx.extras.get(STATE_KEY)
        if rec is None or not rec.running:
            if args.get("start_if_idle"):  # "write the MOM" before anything was recorded = start taking notes
                r = ToolResult.ok("I'm not recording this meeting yet, so I'll start taking notes now.")
                r.followup = {"tool": "meeting_notes_start", "args": {}, "description": "Take meeting notes"}
                return r
            return ToolResult.fail("I'm not taking meeting notes right now. Say “take meeting notes” during a meeting.")
        minutes = max(1, round(rec.elapsed() / 60))

        def finish():
            try:
                lines = rec.stop()
                res = mt.write_minutes(llm=ctx.llm, lines=lines, out_dir=_out_dir(ctx), started=rec.started_at,
                                       minutes=minutes, chunk_seconds=rec.chunk_seconds)
                rec.cleanup_audio()
                ctx.extras["v2_meeting_last"] = res
                try:
                    ctx.backend.open_path(res["docx"])
                except Exception:
                    pass
                ctx.notify("Meeting minutes ready", f"{res['title']} - saved in Documents\\Genesis Documents\\Meeting Minutes")
                return res
            except Exception as exc:
                log.exception("writing minutes failed")
                ctx.notify("Meeting minutes failed", f"I couldn't write the minutes: {exc}")
                ctx.extras["v2_meeting_last"] = {"error": str(exc)}
                return None

        if ctx.extras.get("v2_meeting_sync"):
            res = finish()
            if not res:
                return ToolResult.fail("I stopped recording but couldn't write the minutes.")
            how = "" if res["llm_ok"] else " (the AI model wasn't available, so it has the transcript only)"
            return ToolResult.ok(f"Done - the minutes are open{how}: {Path(res['docx']).name}.", **res)
        threading.Thread(target=finish, name="meeting-minutes", daemon=True).start()
        return ToolResult.ok(f"Stopped recording ({minutes} min). I'm writing the minutes now and will open them in Word "
                             "when they're ready - usually within a minute.", minutes=minutes)

    def verify(self, args, result, ctx):
        return result.success


class MeetingNotesStatus(Tool):
    name = "meeting_notes_status"
    category = "meetings"
    description = "Say whether meeting notes are being taken and for how long."
    parameters = obj({})

    def run(self, args, ctx):
        rec = ctx.extras.get(STATE_KEY)
        if rec is None or not rec.running:
            last = ctx.extras.get("v2_meeting_last") or {}
            if last.get("docx"):
                return ToolResult.ok(f"I'm not recording. The last minutes are in {last['docx']}.")
            return ToolResult.ok("I'm not taking meeting notes right now.")
        return ToolResult.ok(f"I'm taking meeting notes - {int(rec.elapsed() // 60)} min so far, "
                             f"{len(rec.lines)} part(s) transcribed.")


class VoiceChat(Tool):
    name = "voice_chat"
    category = "voice"
    description = "Turn voice chat mode on or off (a natural back-and-forth spoken conversation)."
    parameters = obj({"on": {"type": "boolean"}}, ["on"])

    def run(self, args, ctx):
        if args.get("on"):
            voice_ai.CHAT.start()
            return ToolResult.ok("Voice chat is on - just talk to me, no need to say “Hey Genesis” between turns. "
                                 "Say “stop voice chat” when you're done.", on=True)
        voice_ai.CHAT.stop()
        return ToolResult.ok("Voice chat is off.", on=False)

    def verify(self, args, result, ctx):
        return voice_ai.CHAT.active == bool(args.get("on"))


TOOLS = [MeetOpen(), MeetingNotesStart(), MeetingNotesStop(), MeetingNotesStatus(), VoiceChat()]
