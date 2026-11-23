"""WhatsApp: open, read, draft and (after confirmation) send messages.

Two drivers, chosen automatically (setting ``whatsapp.mode`` = auto | web | desktop):

* :class:`WebWhatsApp` — WhatsApp Web in Genesis's own automated browser (Playwright). You link it once by scanning the
  QR code with your phone, exactly like any browser; Genesis never bypasses that login. It reads the DOM (chat list,
  header, messages, composer), so it can *verify* the right chat is open, that the draft is exactly what you approved,
  and that the message then appears as sent.
* :class:`DesktopWhatsApp` — the WhatsApp desktop app through the computer-control layer (UI Automation + keyboard).
  It only types after it has confirmed, from the window's accessibility tree, that the right chat is open.

Drafting never sends. Sending is a separate operation (HIGH risk → always confirmed by the executor), re-checks the
recipient and the text right before pressing Send, and then verifies the message left.
"""
from __future__ import annotations

import re
import time
from abc import ABC, abstractmethod
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("whatsapp")
WEB_URL = "https://web.whatsapp.com/"


class WhatsAppError(RuntimeError):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # login | not_found | ambiguous | unavailable | mismatch | error


def second_person(text: str) -> str:
    """'I'll call him in ten minutes' (said to Genesis) → 'I'll call you in ten minutes' (said to Rahul)."""
    t = text.strip().strip("\"“”")
    t = re.sub(r"\b(him|them)\b", "you", t, flags=re.I)
    t = re.sub(r"\bhis\b", "your", t, flags=re.I)
    t = re.sub(r"\btheir\b", "your", t, flags=re.I)
    t = re.sub(r"\bhe'?s\b|\bshe'?s\b", "you're", t, flags=re.I)
    t = re.sub(r"\b(he|she|they) (is|are)\b", "you are", t, flags=re.I)
    t = re.sub(r"\b(he|she|they) (was|were)\b", "you were", t, flags=re.I)
    t = re.sub(r"\b(he|she|they) (will|would|can|could|should|has|have|had|needs?|wants?)\b",
               lambda m: "you " + {"has": "have", "needs": "need", "wants": "want"}.get(m.group(2).lower(), m.group(2)), t, flags=re.I)
    # "her": object ("call her later") → you ; possessive ("her car") → your
    t = re.sub(r"\bher\b(?=\s*(?:$|[.,!?;]|in\b|at\b|on\b|later\b|tomorrow\b|today\b|tonight\b|soon\b|back\b|now\b|when\b|after\b|"
               r"before\b|about\b|that\b|if\b|and\b|up\b|again\b|there\b))", "you", t, flags=re.I)
    t = re.sub(r"\bher\b", "your", t, flags=re.I)
    t = t[:1].upper() + t[1:] if t else t
    if t and t[-1] not in ".!?":
        t += "."
    return t


class WhatsAppDriver(ABC):
    mode = "base"

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def open(self) -> dict: ...  # {"logged_in": bool}

    @abstractmethod
    def open_chat(self, name: str) -> str: ...  # returns the exact chat title opened

    @abstractmethod
    def current_chat(self) -> str: ...

    @abstractmethod
    def set_draft(self, text: str) -> None: ...

    @abstractmethod
    def draft_text(self) -> str: ...

    @abstractmethod
    def send(self) -> None: ...

    @abstractmethod
    def messages(self, n: int = 10) -> list[dict]: ...  # oldest→newest: {"incoming", "text", "time", "sender"}

    def unread(self) -> list[dict]:
        return []


# ------------------------------------------------------------------ WhatsApp Web (Playwright)
_JS_STATE = """() => {
  const logged = !!document.querySelector('#pane-side, [aria-label="Chat list"], [data-testid="chat-list"]');
  const qr = !!document.querySelector('canvas[aria-label*="Scan"], div[data-ref], [data-testid="qrcode"]');
  return {logged_in: logged, qr: qr};
}"""
_JS_HEADER = """() => {
  const h = document.querySelector('#main header');
  if (!h) return '';
  const t = h.querySelector('span[title], span[dir="auto"]');
  return t ? (t.getAttribute('title') || t.innerText || '').trim() : '';
}"""
_JS_MESSAGES = """(n) => {
  const rows = Array.from(document.querySelectorAll('#main div.message-in, #main div.message-out'));
  return rows.slice(-n).map(r => {
    const meta = r.querySelector('[data-pre-plain-text]');
    const pre = meta ? meta.getAttribute('data-pre-plain-text') : '';
    const txt = Array.from(r.querySelectorAll('span.selectable-text')).map(s => s.innerText).join('\\n').trim();
    const m = pre.match(/^\\[(.*?)\\]\\s*(.*?):\\s*$/);
    const status = r.querySelector('[data-icon^="msg-dblcheck"], [data-icon="msg-check"], [data-icon="msg-time"]');
    return {incoming: r.classList.contains('message-in'), text: txt, time: m ? m[1] : '', sender: m ? m[2] : '',
            status: status ? status.getAttribute('data-icon') : ''};
  });
}"""
_JS_UNREAD = """() => {
  const out = [];
  for (const row of document.querySelectorAll('#pane-side [role="listitem"], #pane-side [role="row"]')) {
    const badge = row.querySelector('[aria-label*="unread"]');
    if (!badge) continue;
    const t = row.querySelector('span[title]');
    out.push({chat: t ? t.getAttribute('title') : '', unread: parseInt(badge.innerText) || 1});
  }
  return out.slice(0, 20);
}"""
SEARCH_BOX = ['div[contenteditable="true"][data-tab="3"]', '[aria-label="Search input textbox"]', '[aria-label*="Search"][contenteditable="true"]',
              'input[aria-label*="Search"]']
COMPOSER = ['footer div[contenteditable="true"][data-tab="10"]', 'footer div[contenteditable="true"]',
            '[aria-label="Type a message"][contenteditable="true"]', '#main div[contenteditable="true"][role="textbox"]']


class WebWhatsApp(WhatsAppDriver):
    mode = "web"

    def __init__(self, browser: Any) -> None:
        self.browser = browser

    def available(self) -> bool:
        return bool(self.browser is not None and self.browser.available())

    def _page(self, page_fn, timeout: float = 60.0):
        return self.browser.run(lambda _p: page_fn(self.browser.page_for(WEB_URL, WEB_URL)), timeout=timeout)

    @staticmethod
    def _first(page, selectors: list[str]):
        for sel in selectors:
            loc = page.locator(sel)
            try:
                if loc.count() > 0:
                    return loc.first
            except Exception:
                continue
        return None

    def open(self) -> dict:
        def fn(page):
            deadline = time.time() + 25
            state = {"logged_in": False, "qr": False}
            while time.time() < deadline:
                state = page.evaluate(_JS_STATE)
                if state["logged_in"] or state["qr"]:
                    break
                page.wait_for_timeout(500)
            return state
        return self._page(fn)

    def _need_login(self, page) -> None:
        state = page.evaluate(_JS_STATE)
        if not state["logged_in"]:
            raise WhatsAppError("WhatsApp Web isn't linked yet. Open WhatsApp on your phone → Linked devices → Link a device, and "
                                "scan the QR code in the window I opened. I'll remember the login.", "login")

    def open_chat(self, name: str) -> str:
        def fn(page):
            self._need_login(page)
            box = self._first(page, SEARCH_BOX)
            if box is None:
                raise WhatsAppError("I couldn't find WhatsApp's search box (the page layout may have changed).", "error")
            box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            page.keyboard.insert_text(name)
            page.wait_for_timeout(1200)
            titles = page.evaluate("""() => Array.from(document.querySelectorAll('#pane-side span[title]'))
                                        .map(s => s.getAttribute('title')).filter(Boolean).slice(0, 12)""")
            exact = [t for t in titles if t.lower() == name.lower()]
            starts = [t for t in titles if t.lower().startswith(name.lower())]
            pick = (exact or starts or [None])[0]
            if pick is None:
                raise WhatsAppError(f"I couldn't find a chat called “{name}” in WhatsApp.", "not_found")
            if not exact and len({t.lower() for t in starts}) > 1:
                raise WhatsAppError(f"There are several chats matching “{name}”: {', '.join(starts[:5])}. Which one?", "ambiguous")
            page.locator(f'#pane-side span[title="{pick}"]').first.click()
            page.wait_for_timeout(700)
            head = page.evaluate(_JS_HEADER)
            if head.lower() != pick.lower():
                raise WhatsAppError(f"I tried to open “{pick}” but WhatsApp shows “{head or 'no chat'}”, so I stopped.", "mismatch")
            return head
        return self._page(fn)

    def current_chat(self) -> str:
        return self._page(lambda page: page.evaluate(_JS_HEADER))

    def set_draft(self, text: str) -> None:
        def fn(page):
            self._need_login(page)
            box = self._first(page, COMPOSER)
            if box is None:
                raise WhatsAppError("No chat is open, so there's nowhere to type.", "error")
            box.click()
            page.keyboard.press("Control+A")
            page.keyboard.press("Backspace")
            lines = text.split("\n")
            for i, line in enumerate(lines):  # Shift+Enter = newline; plain Enter would send
                page.keyboard.insert_text(line)
                if i < len(lines) - 1:
                    page.keyboard.press("Shift+Enter")
        self._page(fn)

    def draft_text(self) -> Optional[str]:
        def fn(page):
            box = self._first(page, COMPOSER)
            return box.inner_text().strip() if box is not None else None
        return self._page(fn)

    def send(self) -> None:
        def fn(page):
            box = self._first(page, COMPOSER)
            if box is None:
                raise WhatsAppError("The message box disappeared before sending, so I didn't send anything.", "error")
            btn = self._first(page, ['[aria-label="Send"]', 'button span[data-icon="send"]', 'span[data-icon="wds-ic-send-filled"]'])
            if btn is not None:
                btn.click()
            else:
                box.press("Enter")
            page.wait_for_timeout(1500)
        self._page(fn)

    def messages(self, n: int = 10) -> list[dict]:
        return self._page(lambda page: (self._need_login(page), page.evaluate(_JS_MESSAGES, n))[1])

    def unread(self) -> list[dict]:
        return self._page(lambda page: (self._need_login(page), page.evaluate(_JS_UNREAD))[1])


# ------------------------------------------------------------------ WhatsApp desktop (computer control)
class DesktopWhatsApp(WhatsAppDriver):
    """WhatsApp for Windows through UI Automation + keyboard. Conservative: it verifies the open chat before typing."""

    mode = "desktop"

    def __init__(self, backend: Any, computer: Any) -> None:
        self.backend = backend
        self.computer = computer

    def available(self) -> bool:
        return bool(self.computer is not None and self.computer.available())

    @staticmethod
    def _is_app_window(w: dict) -> bool:
        return (w.get("title") or "").strip().lower() == "whatsapp"  # the app's own window, not e.g. "whatsapp-bot – VS Code"

    def _focus(self) -> None:
        wins = self.backend.list_windows()
        hit = next((w for w in wins if self._is_app_window(w)), None)
        if hit is None:
            self.backend.open_uri("whatsapp:")
            for _ in range(20):
                time.sleep(0.5)
                hit = next((w for w in self.backend.list_windows() if self._is_app_window(w)), None)
                if hit:
                    break
        if hit is None:
            raise WhatsAppError("The WhatsApp app didn't open. Is it installed? (Microsoft Store → WhatsApp.)", "unavailable")
        self.backend.window_action(hit["id"], "focus")
        time.sleep(0.4)

    def open(self) -> dict:
        self._focus()
        return {"logged_in": True}

    def _names(self) -> list[str]:
        self.computer.observe()
        return [e.name for e in self.computer.last_elements]

    def open_chat(self, name: str) -> str:
        self._focus()
        d = self.computer.driver
        d.keys(["ctrl", "f"])  # focus "Search or start a new chat"
        time.sleep(0.3)
        focused = d.focused_element()
        if focused is None or not re.search(r"search", focused.name or "", re.I):
            raise WhatsAppError("I couldn't get to WhatsApp's search box, so I stopped (I won't type into the wrong place).", "error")
        d.keys(["ctrl", "a"])
        d.type_text(name)
        time.sleep(1.2)
        d.keys(["down"])
        d.keys(["enter"])  # opens the highlighted search result (never sends: the composer isn't focused yet)
        time.sleep(0.8)
        hit = self._header_match(name)
        if hit is None:
            raise WhatsAppError(f"I couldn't confirm that the chat with “{name}” opened, so I didn't type anything.", "mismatch")
        self._chat = hit
        return hit

    def _header_match(self, name: str) -> Optional[str]:
        """The open conversation's name as shown in its header — above the message box, right of the chat list (the
        sidebar always lists every chat, so a name *somewhere* in the window proves nothing)."""
        self.computer.observe()
        els = self.computer.last_elements
        box = next((e for e in els if e.role == "field" and re.search(r"type a message|message", e.name or "", re.I)), None)
        if box is None or box.bbox[2] <= 0:
            return None
        left, top = box.bbox[0], box.bbox[1]
        low = name.lower()
        for e in els:
            n = (e.name or "").strip()
            if not n or e is box or e.bbox[2] <= 0:
                continue
            if (n.lower() == low or n.lower().startswith(low + " ")) and e.bbox[0] >= left - 8 and e.bbox[1] < top:
                return n
        return None

    def current_chat(self) -> str:
        """The chat whose name is shown in the conversation header right now (re-read from the window, not remembered)."""
        want = getattr(self, "_chat", "")
        if not want:
            return ""
        return want if self._header_match(want) else ""

    def _composer(self):
        self.computer.observe()
        return next((e for e in self.computer.last_elements if e.role == "field" and re.search(r"type a message|message", e.name, re.I)),
                    None)

    def set_draft(self, text: str) -> None:
        box = self._composer()
        if box is None:
            raise WhatsAppError("I can't find the message box in WhatsApp.", "error")
        if not self.computer.driver.set_value(box, text):
            self.computer.driver.focus_element(box)
            self.computer.driver.keys(["ctrl", "a"])
            self.computer.driver.type_text(text, ("shift", "enter"))

    def draft_text(self) -> Optional[str]:
        box = self._composer()
        return (box.value or "").strip() if box else None

    def send(self) -> None:
        self._focus()
        if not self.current_chat():
            raise WhatsAppError("I can't see the right chat open in WhatsApp any more, so I didn't send anything.", "mismatch")
        box = self._composer()
        if box is None:
            raise WhatsAppError("The message box disappeared, so I didn't send anything.", "error")
        self.computer.driver.focus_element(box)
        if not self.computer.driver.foreground().title.strip().lower() == "whatsapp":
            raise WhatsAppError("WhatsApp isn't the active window any more, so I didn't press Send.", "mismatch")
        self.computer.driver.keys(["enter"])  # the executor already obtained confirmation for this send
        time.sleep(0.8)

    def messages(self, n: int = 10) -> list[dict]:
        self._focus()
        self.computer.observe()
        names = [e for e in self.computer.last_elements if e.role in ("list item", "text")]
        # the desktop app doesn't expose who wrote a line, so direction is unknown (never counted as proof of sending)
        return [{"incoming": None, "text": e.name, "time": "", "sender": ""} for e in names[-n:]]


# ------------------------------------------------------------------ service
class WhatsAppService:
    def __init__(self, browser: Any = None, backend: Any = None, computer: Any = None, *, drivers: Optional[list] = None,
                 settings: Any = None) -> None:
        self.backend = backend
        self.settings = settings
        self.drivers: list[WhatsAppDriver] = drivers if drivers is not None else [WebWhatsApp(browser),
                                                                                 DesktopWhatsApp(backend, computer)]

    def _call(self, fn, *args):
        try:
            return fn(*args)
        except WhatsAppError:
            raise
        except Exception as exc:
            from .browser import BrowserUnavailable

            log.warning("whatsapp driver error: %s", type(exc).__name__)
            if isinstance(exc, BrowserUnavailable):
                raise WhatsAppError(str(exc), "unavailable") from exc
            raise WhatsAppError(f"WhatsApp didn't respond as expected ({type(exc).__name__}). Please check the WhatsApp window.",
                                "error") from exc

    def driver(self) -> WhatsAppDriver:
        mode = (self.settings.get("whatsapp.mode", "auto") if self.settings is not None else "auto") or "auto"
        for d in self.drivers:
            if (mode == "auto" or d.mode == mode) and d.available():
                return d
        raise WhatsAppError("To use WhatsApp for you I need browser automation (pip install playwright, then "
                            "playwright install chromium) or the WhatsApp desktop app on Windows.", "unavailable")

    def automation_available(self) -> bool:
        try:
            self.driver()
            return True
        except WhatsAppError:
            return False

    def open(self) -> dict:
        d = self.driver()
        info = self._call(d.open)
        return dict(info, mode=d.mode)

    def read(self, chat: Optional[str], n: int = 5) -> dict:
        d = self.driver()
        title = self._call(d.open_chat, chat) if chat else self._call(d.current_chat)
        if not title:
            raise WhatsAppError("Which chat should I read?", "not_found")
        msgs = self._call(d.messages, max(1, min(n, 30)))
        return {"chat": title, "messages": msgs, "mode": d.mode}

    def unread(self) -> list[dict]:
        return self._call(self.driver().unread)

    def draft(self, chat: str, text: str) -> dict:
        d = self.driver()
        title = self._call(d.open_chat, chat)
        self._call(d.set_draft, text)
        typed = self._call(d.draft_text)
        if typed is None or _norm(typed) != _norm(text):
            raise WhatsAppError("The message box doesn't contain exactly what I typed, so I stopped before sending.", "mismatch")
        return {"chat": title, "text": text, "mode": d.mode}

    def send(self, chat: str, text: str) -> dict:
        return self._call(self._send, chat, text)

    def _send(self, chat: str, text: str) -> dict:
        """Re-verify recipient + exact text, send, then confirm it shows as an outgoing message."""
        d = self.driver()
        current = d.current_chat()
        if not current or current.lower() != chat.lower():
            current = d.open_chat(chat)
        if _norm(d.draft_text() or "") != _norm(text):
            d.set_draft(text)
            if _norm(d.draft_text() or "") != _norm(text):
                raise WhatsAppError("I couldn't put exactly the approved text in the message box, so I didn't send it.", "mismatch")
        before = [m for m in d.messages(5) if m.get("incoming") is False]
        d.send()
        sent, status = False, ""
        for _ in range(6):
            outgoing = [m for m in d.messages(5) if m.get("incoming") is False]
            last = outgoing[-1] if outgoing else None
            if last and _norm(last.get("text", "")) == _norm(text) and outgoing != before:
                sent, status = True, last.get("status", "")
                break
            time.sleep(0.5)
        left = d.draft_text() == ""  # None = box not found → not evidence of anything
        return {"chat": current, "text": text, "sent": sent, "status": status or ("pending" if left else ""), "mode": d.mode,
                "composer_empty": left}


def _norm(s: str) -> str:
    return " ".join((s or "").split()).strip().lower()


class FakeWhatsAppDriver(WhatsAppDriver):
    """In-memory WhatsApp for tests and demos-with-no-network (never used in production code paths)."""

    mode = "web"

    def __init__(self, chats: Optional[dict[str, list[dict]]] = None, logged_in: bool = True) -> None:
        self.chats = chats if chats is not None else {}
        self.logged_in = logged_in
        self.current = ""
        self.composer = ""
        self.sent: list[tuple[str, str]] = []
        self.fail_send = False

    def available(self) -> bool:
        return True

    def open(self) -> dict:
        return {"logged_in": self.logged_in, "qr": not self.logged_in}

    def _login(self):
        if not self.logged_in:
            raise WhatsAppError("WhatsApp Web isn't linked yet — scan the QR code.", "login")

    def open_chat(self, name: str) -> str:
        self._login()
        exact = [c for c in self.chats if c.lower() == name.lower()]
        starts = [c for c in self.chats if c.lower().startswith(name.lower())]
        if not exact and len(starts) > 1:
            raise WhatsAppError(f"There are several chats matching “{name}”: {', '.join(starts)}. Which one?", "ambiguous")
        pick = (exact or starts or [None])[0]
        if pick is None:
            raise WhatsAppError(f"I couldn't find a chat called “{name}” in WhatsApp.", "not_found")
        self.current, self.composer = pick, ""
        return pick

    def current_chat(self) -> str:
        return self.current

    def set_draft(self, text: str) -> None:
        self.composer = text

    def draft_text(self) -> str:
        return self.composer

    def send(self) -> None:
        if self.fail_send:
            return
        self.chats.setdefault(self.current, []).append({"incoming": False, "text": self.composer, "time": "now", "sender": "You",
                                                        "status": "msg-check"})
        self.sent.append((self.current, self.composer))
        self.composer = ""

    def messages(self, n: int = 10) -> list[dict]:
        self._login()
        return list(self.chats.get(self.current, []))[-n:]

    def unread(self) -> list[dict]:
        return [{"chat": c, "unread": sum(1 for m in ms if m.get("incoming") and m.get("unread"))} for c, ms in self.chats.items()
                if any(m.get("unread") for m in ms)]
