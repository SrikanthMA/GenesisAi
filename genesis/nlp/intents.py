"""Intent inventory, slot requirements and NLU data structures."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Optional


class Intent(str, Enum):
    CONVERSATION = "conversation"
    OPEN_APPLICATION = "open_application"
    CLOSE_APPLICATION = "close_application"
    OPEN_FOLDER = "open_folder"
    OPEN_URL = "open_url"
    SEARCH_WEB = "search_web"
    SEND_EMAIL = "send_email"
    DRAFT_EMAIL = "draft_email"
    READ_EMAIL = "read_email"
    CREATE_REMINDER = "create_reminder"
    FILE_OPERATION = "file_operation"
    SYSTEM_CONTROL = "system_control"
    BROWSER_ACTION = "browser_action"
    CODE_ACTION = "code_action"
    INFORMATION_REQUEST = "information_request"
    SHUTDOWN = "shutdown"
    RESTART = "restart"
    SETTINGS = "settings"
    MEMORY = "memory"
    KNOWLEDGE = "knowledge"
    UI_CONTROL = "ui_control"
    CANCEL = "cancel"
    # conversational-agent intents (v2)
    REPLY_EMAIL = "reply_email"
    REVISE_DRAFT = "revise_draft"
    FOLLOW_UP = "follow_up"  # a reference resolved to a concrete tool call ("open the second one")
    SESSION = "session_control"  # "go to sleep", "that's all"
    ROUTINE = "routine"
    SCREEN = "screen"
    RESEARCH = "research"
    YOUTUBE = "youtube"
    MEDIA = "media_control"
    WINDOW = "window_control"
    CALENDAR = "calendar"
    HISTORY = "history"
    IDENTITY = "identity"
    CORRECTION = "correction"
    TASKS = "tasks"
    NOTES = "notes"
    GOAL = "goal"
    # 1.4 desktop agent
    WHATSAPP = "whatsapp"
    COLAB = "colab"
    IMAGE = "image"
    COMPUTER = "computer_control"
    FORM = "form"
    # 1.5
    GITHUB = "github"
    OFFICE = "office"
    UNKNOWN = "unknown"


INTENT_DESCRIPTIONS: dict[Intent, str] = {
    Intent.CONVERSATION: "small talk, opinions, general chat",
    Intent.OPEN_APPLICATION: "launch a desktop or web application (Chrome, VS Code, Gmail, Spotify...)",
    Intent.CLOSE_APPLICATION: "close/quit a running application",
    Intent.OPEN_FOLDER: "open a folder such as Downloads or Documents in the file manager",
    Intent.OPEN_URL: "open a specific website or URL",
    Intent.SEARCH_WEB: "search the internet for something",
    Intent.SEND_EMAIL: "send an e-mail (or send a previously drafted one)",
    Intent.DRAFT_EMAIL: "write/compose/draft an e-mail without sending",
    Intent.READ_EMAIL: "check, read or find e-mails in the inbox",
    Intent.CREATE_REMINDER: "remind the user of something at a time",
    Intent.FILE_OPERATION: "create/find/read/open/move/copy/rename/delete files or folders",
    Intent.SYSTEM_CONTROL: "lock, sleep, volume, battery/system information",
    Intent.BROWSER_ACTION: "interact with a web page: read it, click, fill a form",
    Intent.CODE_ACTION: "write/create and run a program, run code, open a project or file in VS Code, check or fix code, explain an error",
    Intent.INFORMATION_REQUEST: "factual question the assistant can answer (time, date, general knowledge)",
    Intent.SHUTDOWN: "shut down / power off the computer",
    Intent.RESTART: "restart / reboot the computer",
    Intent.SETTINGS: "change Genesis settings (voice, listening, memory...)",
    Intent.MEMORY: "remember/forget facts, contacts; show memory",
    Intent.KNOWLEDGE: "add documents to or ask questions about the user's local knowledge base",
    Intent.UI_CONTROL: "open the chat window, hide Genesis",
    Intent.CANCEL: "cancel the current request",
    Intent.REPLY_EMAIL: "reply to the e-mail currently being discussed",
    Intent.REVISE_DRAFT: "change the e-mail draft currently being discussed",
    Intent.FOLLOW_UP: "act on something referred to earlier (it, that, the second one)",
    Intent.SESSION: "end the conversation / go to sleep",
    Intent.ROUTINE: "create, list, run or delete a scheduled routine",
    Intent.SCREEN: "look at / read / explain what is on the screen",
    Intent.RESEARCH: "find several things online and compare or summarise them, or answer a question from web results",
    Intent.YOUTUBE: "search YouTube or play a video/song on YouTube",
    Intent.MEDIA: "pause, resume, skip or go back in whatever is playing",
    Intent.WINDOW: "switch to/minimize/maximize windows, show the desktop, list windows, take a screenshot",
    Intent.CALENDAR: "create, list, move or cancel calendar events/meetings",
    Intent.HISTORY: "find something from an earlier conversation (e.g. 'that tutorial we talked about yesterday')",
    Intent.IDENTITY: "the user tells Genesis their name / what to call them, or asks what their name is",
    Intent.TASKS: "add, list, complete or delete to-do list items",
    Intent.NOTES: "take a note, list notes, find or read a note",
    Intent.GOAL: "a goal that needs several steps Genesis must work out itself (e.g. 'prepare my computer for coding')",
    Intent.WHATSAPP: "open WhatsApp, read WhatsApp messages from someone, or write/send a WhatsApp message (\"message Rahul saying…\")",
    Intent.COLAB: "Google Colab: open it, create a notebook, write code into a cell, run it",
    Intent.IMAGE: "show a picture/photo of something, or generate/draw a new image",
    Intent.COMPUTER: "operate the active app directly: type text, press keys/shortcuts, copy, paste, select, right/double-click, drag",
    Intent.FORM: "fill in a form or job application with the user's resume/details, or submit it",
    Intent.GITHUB: "GitHub: open GitHub or a repository, list the user's repositories, create or delete a repository",
    Intent.OFFICE: "Microsoft Office: create a Word document, Excel spreadsheet or PowerPoint presentation, or write/add content "
                   "into the one that is open",
    Intent.UNKNOWN: "cannot be classified",
}

# Slots the dialogue manager must fill before planning, with the question to ask.
REQUIRED_SLOTS: dict[Intent, list[tuple[str, str]]] = {
    Intent.OPEN_APPLICATION: [("app", "Which application should I open?")],
    Intent.CLOSE_APPLICATION: [("app", "Which application should I close?")],
    Intent.OPEN_FOLDER: [("folder", "Which folder should I open?")],
    Intent.OPEN_URL: [("url", "Which website should I open?")],
    Intent.SEARCH_WEB: [("query", "What should I search for?")],
    Intent.DRAFT_EMAIL: [("recipient", "Who should the e-mail go to?"), ("message", "What should I say?")],
    Intent.SEND_EMAIL: [("recipient", "Who should the e-mail go to?"), ("message", "What should I say?")],
    Intent.CREATE_REMINDER: [("reminder_text", "What should I remind you about?"), ("datetime", "When should I remind you?")],
    Intent.REPLY_EMAIL: [("message", "What should I say in the reply?")],
    Intent.YOUTUBE: [("query", "What should I look for on YouTube?")],
}


@dataclass
class Entity:
    type: str
    value: Any
    text: str = ""
    start: int = -1
    end: int = -1
    confidence: float = 1.0

    def to_dict(self) -> dict:
        d = asdict(self)
        if hasattr(self.value, "isoformat"):
            d["value"] = self.value.isoformat()
        return d


@dataclass
class NLUResult:
    text: str
    intent: Intent
    confidence: float
    slots: dict[str, Any] = field(default_factory=dict)
    entities: list[Entity] = field(default_factory=list)
    source: str = "rules"  # rules | classifier | llm | dialogue
    alternatives: list[tuple[str, float]] = field(default_factory=list)

    def slot(self, name: str, default: Any = None) -> Any:
        v = self.slots.get(name)
        return default if v in (None, "") else v

    def to_dict(self) -> dict:
        slots = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in self.slots.items()}
        return {
            "text": self.text,
            "intent": self.intent.value,
            "confidence": round(self.confidence, 3),
            "slots": slots,
            "entities": [e.to_dict() for e in self.entities],
            "source": self.source,
            "alternatives": [(a, round(c, 3)) for a, c in self.alternatives],
        }

    def missing_slots(self) -> list[tuple[str, str]]:
        return [(s, q) for s, q in REQUIRED_SLOTS.get(self.intent, []) if self.slot(s) is None]


def intent_from_str(value: Optional[str]) -> Intent:
    try:
        return Intent((value or "").strip().lower())
    except ValueError:
        return Intent.UNKNOWN
