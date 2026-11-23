"""Generalized computer control: the "hands" Genesis uses for any desktop application.

    computer.observe()        foreground window + its named UI elements (accessibility tree) — no screenshot
    computer.find(...)        an element by name / role / ordinal ("the second button")
    computer.click(...)       Invoke via UI Automation → click the element's centre → raw coordinates (last resort)
    computer.type(...)        into the focused control or a named field (ValuePattern first, then keystrokes)
    computer.press(...)       one key / a hotkey ("ctrl+s")
    computer.scroll(...), copy(), paste(), select_all(), drag(...), switch_window(...), open_application (tools/apps.py)

Preference order (as required): accessibility/UI-Automation element → semantic selectors (name/role) → app APIs /
DOM (the browser, WhatsApp and Colab services) → visual understanding (tools/screen.py) → coordinates only as a
fallback, and only after confirmation.

Safety is enforced *here*, below the planner, so no plan (rule-based or LLM) can get around it:

* Genesis never types into or sends keys to terminals, the Run dialog, the registry editor or UAC prompts — commands go
  through the safe developer-command runner instead. It never types into password fields.
* Dangerous hotkeys (Win+R, Win+X, Ctrl+Alt+Del, …) are refused.
* "Send"-like actions (Enter in a chat/e-mail app, clicking Send/Submit/Pay/Delete …) are classified HIGH risk so the
  executor always asks first; see :func:`action_risk`.

The Windows driver uses ``SendInput`` / the clipboard API through ctypes (no extra packages) and the optional
``uiautomation`` package for the accessibility tree. A recording driver is used in tests.
"""
from __future__ import annotations

import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

from ..core.logging_setup import get_logger

log = get_logger("computer")


class ComputerUnavailable(RuntimeError):
    pass


class ComputerRefused(RuntimeError):
    """A safety rule forbids this action (message is user-facing)."""


# ------------------------------------------------------------------ data
@dataclass
class WindowInfo:
    title: str = ""
    process: str = ""
    class_name: str = ""
    handle: int = 0
    pid: int = 0

    def public(self) -> dict:
        return {"title": self.title, "process": self.process, "class_name": self.class_name}


@dataclass
class Element:
    id: int
    name: str
    role: str  # button | link | field | checkbox | radio | tab | menu item | list item | dropdown | text | item
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, width, height
    enabled: bool = True
    is_password: bool = False
    value: str = ""
    ref: Any = field(default=None, repr=False, compare=False)

    def public(self) -> dict:
        d = {"id": self.id, "name": self.name, "role": self.role, "enabled": self.enabled}
        if self.is_password:
            d["is_password"] = True
        return d

    @property
    def center(self) -> tuple[int, int]:
        left, top, w, h = self.bbox
        return left + w // 2, top + h // 2


ROLE_OF = {"Button": "button", "SplitButton": "button", "Hyperlink": "link", "Edit": "field", "Document": "field",
           "CheckBox": "checkbox", "RadioButton": "radio", "TabItem": "tab", "MenuItem": "menu item", "ListItem": "list item",
           "ComboBox": "dropdown", "Text": "text", "TreeItem": "item", "DataItem": "item"}

# ------------------------------------------------------------------ safety policy
# windows Genesis never types/sends keys into: shells, the Run box, system tools and the Start/search surfaces (each of those can
# run arbitrary commands, which must go through the safe command runner instead)
TERMINAL_PROCESSES = {"cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe", "conhost.exe", "bash.exe",
                      "wsl.exe", "mintty.exe", "regedit.exe", "mmc.exe", "gpedit.msc", "consent.exe", "openconsole.exe",
                      "searchhost.exe", "searchapp.exe", "searchui.exe", "startmenuexperiencehost.exe", "shellexperiencehost.exe",
                      "taskmgr.exe", "powershell_ise.exe", "cscript.exe", "wscript.exe"}
SYSTEM_TITLES = re.compile(r"^(run|user account control|administrator:.*|windows powershell.*|command prompt.*|registry editor|"
                           r"task manager|search|start)$", re.I)
# the Explorer address bar / search box also runs commands ("powershell …" typed there starts PowerShell)
EXPLORER_COMMAND_FIELDS = re.compile(r"\b(address|search box|search|location|run)\b", re.I)
MESSAGING_PROCESSES = {"whatsapp.exe", "whatsapp.root.exe", "telegram.exe", "slack.exe", "teams.exe", "ms-teams.exe", "discord.exe",
                       "outlook.exe", "olk.exe", "signal.exe", "skype.exe", "thunderbird.exe", "messenger.exe", "phoneexperiencehost.exe"}
MESSAGING_TITLES = re.compile(r"\b(whatsapp|telegram|slack|microsoft teams|teams|discord|outlook|gmail|mail|messenger|signal|skype|"
                              r"instagram|linkedin|google chat|chat|messages|phone link|inbox|x\.com|twitter)\b", re.I)
# apps where Enter / a newline just inserts a line (safe to type multi-line text and press Enter)
# (IDEs such as VS Code / PyCharm / Visual Studio are NOT here: their built-in terminal runs whatever is typed + Enter.
#  Code goes into files through the coding tools instead.)
EDITOR_PROCESSES = {"notepad.exe", "notepad++.exe", "winword.exe", "wordpad.exe", "write.exe", "onenote.exe", "obsidian.exe",
                    "sublime_text.exe", "excel.exe", "powerpnt.exe", "notion.exe", "typora.exe"}
IDE_PROCESSES = {"code.exe", "code - insiders.exe", "cursor.exe", "pycharm64.exe", "idea64.exe", "devenv.exe", "webstorm64.exe",
                 "clion64.exe", "rider64.exe", "goland64.exe", "android studio.exe", "studio64.exe"}
TERMINAL_CONTROL = re.compile(r"\b(terminal|console|powershell|command prompt|cmd|bash|shell)\b", re.I)
EXECUTABLE_NAME = re.compile(r"\.(exe|bat|cmd|ps1|vbs|vbe|js|jse|wsf|msi|msix|appx|lnk|scr|com|hta|reg|jar)\b", re.I)
BLOCKED_COMBOS = [{"win", "r"}, {"win", "x"}, {"ctrl", "alt", "delete"}, {"ctrl", "alt", "del"}, {"win", "ctrl", "shift", "b"},
                  {"ctrl", "esc"}, {"ctrl", "shift", "esc"}, {"win", "s"}, {"win", "q"}]
EXPLORER_FOCUS_COMBOS = [{"alt", "d"}, {"ctrl", "l"}, {"f4"}, {"ctrl", "e"}, {"ctrl", "f"}]  # jump to the address/search bar
SENSITIVE_NAMES = re.compile(r"\b(send|submit|pay|payment|buy|purchase|order|checkout|place order|delete|remove|erase|confirm|"
                             r"transfer|post|publish|tweet|share|sign out|log ?out|uninstall|format|install|allow|accept|agree|"
                             r"authori[sz]e|approve|unsubscribe|empty (recycle )?bin|reset|discard|don'?t save|apply|run|yes)\b", re.I)
KEY_ALIASES = {"control": "ctrl", "ctl": "ctrl", "cmd": "win", "windows": "win", "super": "win", "meta": "win", "option": "alt",
               "return": "enter", "escape": "esc", "del": "delete", "ins": "insert", "pgup": "pageup", "page up": "pageup",
               "pgdn": "pagedown", "page down": "pagedown", "arrow up": "up", "arrow down": "down", "arrow left": "left",
               "arrow right": "right", "spacebar": "space", "back space": "backspace", "print screen": "printscreen", "caps": "capslock"}


def parse_keys(spec: str) -> list[str]:
    """'Ctrl + Shift + S' → ['ctrl', 'shift', 's']."""
    s = spec.strip().lower()
    for a, b in sorted(KEY_ALIASES.items(), key=lambda x: -len(x[0])):
        s = re.sub(rf"\b{re.escape(a)}\b", b, s)
    parts = [p.strip() for p in re.split(r"\s*\+\s*|\s+", s) if p.strip()]
    return parts


def is_terminal(win: WindowInfo) -> bool:
    return (win.process or "").lower() in TERMINAL_PROCESSES or bool(SYSTEM_TITLES.match((win.title or "").strip())) \
        or (win.class_name or "") in ("ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "#32770-run")


def is_messaging(win: WindowInfo) -> bool:
    return (win.process or "").lower() in MESSAGING_PROCESSES or bool(MESSAGING_TITLES.search(win.title or ""))


def is_editor(win: WindowInfo) -> bool:
    return (win.process or "").lower() in EDITOR_PROCESSES


def action_risk(action: str, *, window: Optional[WindowInfo] = None, target_name: str = "", keys: Optional[list[str]] = None,
                coordinates: bool = False, double: bool = False) -> str:
    """'low' | 'medium' | 'high' for one computer action (the executor turns medium/high into confirmations)."""
    win = window or WindowInfo()
    keys = keys or []
    if action == "click":
        if SENSITIVE_NAMES.search(target_name or "") or EXECUTABLE_NAME.search(target_name or ""):
            return "high"  # send/pay/delete … or opening a program/script file
        return "medium" if coordinates or double else "low"  # a double-click opens/runs things
    if action == "press":
        ks = set(keys)
        if ks & {"enter"}:
            if is_messaging(win):
                return "high"  # Enter (or Ctrl+Enter) sends the message
            return "low" if is_editor(win) and ks == {"enter"} else "medium"  # elsewhere Enter may submit a form
        if ks == {"alt", "s"} or ks == {"ctrl", "enter"}:
            return "high"  # Outlook/Thunderbird "send" shortcuts
        if ks == {"shift", "delete"}:
            return "high"  # permanent delete in Explorer
        if ks & {"delete"} or ks == {"alt", "f4"}:
            return "medium"
        return "low"
    if action == "drag":
        return "medium"
    return "low"


def check_allowed(action: str, win: WindowInfo, *, keys: Optional[list[str]] = None, element: Optional[Element] = None) -> None:
    """Raise ComputerRefused for actions Genesis must never perform."""
    if action in ("type", "press", "paste") and is_terminal(win):
        raise ComputerRefused("I don't type into terminals, the Run box, search or system dialogs. Tell me the command instead — "
                              "I'll run it through my safe command runner (it shows you the command first).")
    proc = (win.process or "").lower()
    if proc in IDE_PROCESSES and action in ("type", "paste", "press") and element is not None \
            and TERMINAL_CONTROL.search(f"{element.name} {element.role}"):
        raise ComputerRefused("That's the editor's built-in terminal — I don't type into terminals. Tell me the command and I'll "
                              "run it through my safe command runner (it shows you the command first).")
    if proc == "explorer.exe" and action in ("type", "paste") and element is None:
        raise ComputerRefused("I can't see which box is focused in File Explorer, so I won't type there (the address bar can "
                              "start programs). Tell me which folder or file to open instead.")
    if proc == "explorer.exe" and action in ("type", "paste") and element is not None and element.role == "field" \
            and EXPLORER_COMMAND_FIELDS.search(element.name or ""):
        raise ComputerRefused("I don't type into File Explorer's address or search bar (it can start programs). Tell me which "
                              "folder to open instead.")
    if keys:
        ks = set(keys)
        if ks == {"win"}:
            raise ComputerRefused("I don't open the Start menu to type into it — ask me to open the app instead.")
        for combo in BLOCKED_COMBOS:
            if combo <= ks:
                raise ComputerRefused(f"I don't use the {'+'.join(keys)} shortcut — it opens system tools outside my safety checks.")
        if proc == "explorer.exe" and any(combo == ks for combo in EXPLORER_FOCUS_COMBOS):
            raise ComputerRefused("I don't jump to File Explorer's address bar — tell me which folder to open instead.")
    if action in ("type", "paste") and element is not None and element.is_password:
        raise ComputerRefused("That's a password field — please type passwords yourself. I never enter or store them.")


# ------------------------------------------------------------------ drivers
class InputDriver(ABC):
    """Low-level input + window/clipboard access. All coordinates are physical screen pixels."""

    name = "base"

    @abstractmethod
    def available(self) -> bool: ...

    @abstractmethod
    def foreground(self) -> WindowInfo: ...

    @abstractmethod
    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None: ...

    @abstractmethod
    def move(self, x: int, y: int) -> None: ...

    @abstractmethod
    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None: ...

    @abstractmethod
    def scroll(self, clicks: int, horizontal: bool = False) -> None: ...

    @abstractmethod
    def type_text(self, text: str, newline_keys: tuple[str, ...] = ("enter",)) -> None: ...

    @abstractmethod
    def keys(self, combo: list[str]) -> None: ...

    @abstractmethod
    def clipboard_get(self) -> str: ...

    @abstractmethod
    def clipboard_set(self, text: str) -> None: ...

    def screen_size(self) -> tuple[int, int]:
        return (1920, 1080)

    # accessibility tree (optional)
    def elements(self, limit: int = 120) -> list[Element]:
        return []

    def focused_element(self) -> Optional[Element]:
        return None

    def invoke(self, el: Element) -> bool:
        """Activate an element through accessibility (no mouse). False = not supported for this element."""
        return False

    def set_value(self, el: Element, text: str) -> bool:
        return False

    def focus_element(self, el: Element) -> bool:
        return False


class UnavailableDriver(InputDriver):
    name = "none"

    def __init__(self, reason: str = "Controlling the mouse and keyboard is only supported on Windows.") -> None:
        self.reason = reason

    def available(self) -> bool:
        return False

    def _no(self, *a, **k):
        raise ComputerUnavailable(self.reason)

    foreground = click = move = drag = scroll = type_text = keys = clipboard_get = clipboard_set = _no  # type: ignore[assignment]


class WindowsInputDriver(InputDriver):
    """SendInput + Win32 clipboard via ctypes; UI Automation through the optional ``uiautomation`` package."""

    name = "windows"
    VK = {"backspace": 0x08, "tab": 0x09, "enter": 0x0D, "shift": 0x10, "ctrl": 0x11, "alt": 0x12, "pause": 0x13,
          "capslock": 0x14, "esc": 0x1B, "space": 0x20, "pageup": 0x21, "pagedown": 0x22, "end": 0x23, "home": 0x24,
          "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28, "printscreen": 0x2C, "insert": 0x2D, "delete": 0x2E,
          "win": 0x5B, "apps": 0x5D, "menu": 0x5D, "numlock": 0x90, "scrolllock": 0x91,
          "volumemute": 0xAD, "volumedown": 0xAE, "volumeup": 0xAF, "browserback": 0xA6, "browserforward": 0xA7}
    EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0x5B, 0x5D}

    def __init__(self) -> None:
        self._ok = sys.platform.startswith("win")
        if self._ok:
            self._setup()

    def available(self) -> bool:
        return self._ok

    # ---------------------------------------------------------------- ctypes plumbing
    def _setup(self) -> None:
        import ctypes

        c = ctypes
        ULONG_PTR = c.c_size_t

        class MOUSEINPUT(c.Structure):
            _fields_ = [("dx", c.c_int32), ("dy", c.c_int32), ("mouseData", c.c_uint32), ("dwFlags", c.c_uint32),
                        ("time", c.c_uint32), ("dwExtraInfo", ULONG_PTR)]

        class KEYBDINPUT(c.Structure):
            _fields_ = [("wVk", c.c_uint16), ("wScan", c.c_uint16), ("dwFlags", c.c_uint32), ("time", c.c_uint32),
                        ("dwExtraInfo", ULONG_PTR)]

        class HARDWAREINPUT(c.Structure):
            _fields_ = [("uMsg", c.c_uint32), ("wParamL", c.c_uint16), ("wParamH", c.c_uint16)]

        class _U(c.Union):
            _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]

        class INPUT(c.Structure):
            _anonymous_ = ("u",)
            _fields_ = [("type", c.c_uint32), ("u", _U)]

        self._c = c
        self._INPUT, self._MOUSEINPUT, self._KEYBDINPUT = INPUT, MOUSEINPUT, KEYBDINPUT
        self._user32 = c.WinDLL("user32", use_last_error=True)
        self._kernel32 = c.WinDLL("kernel32", use_last_error=True)
        u, k = self._user32, self._kernel32
        u.SendInput.argtypes = [c.c_uint, c.POINTER(INPUT), c.c_int]
        u.SendInput.restype = c.c_uint
        u.GetForegroundWindow.restype = c.c_void_p
        u.GetWindowTextW.argtypes = [c.c_void_p, c.c_wchar_p, c.c_int]
        u.GetClassNameW.argtypes = [c.c_void_p, c.c_wchar_p, c.c_int]
        u.GetWindowThreadProcessId.argtypes = [c.c_void_p, c.POINTER(c.c_ulong)]
        u.SetCursorPos.argtypes = [c.c_int, c.c_int]
        u.OpenClipboard.argtypes = [c.c_void_p]
        u.GetClipboardData.argtypes = [c.c_uint]
        u.GetClipboardData.restype = c.c_void_p
        u.SetClipboardData.argtypes = [c.c_uint, c.c_void_p]
        u.SetClipboardData.restype = c.c_void_p
        u.VkKeyScanW.argtypes = [c.c_wchar]
        u.VkKeyScanW.restype = c.c_short
        k.GlobalAlloc.argtypes = [c.c_uint, c.c_size_t]
        k.GlobalAlloc.restype = c.c_void_p
        k.GlobalLock.argtypes = [c.c_void_p]
        k.GlobalLock.restype = c.c_void_p
        k.GlobalUnlock.argtypes = [c.c_void_p]
        k.GlobalFree.argtypes = [c.c_void_p]

    def _send(self, inputs: list) -> None:
        arr = (self._INPUT * len(inputs))(*inputs)
        sent = self._user32.SendInput(len(inputs), arr, self._c.sizeof(self._INPUT))
        if sent != len(inputs):
            raise ComputerUnavailable("Windows blocked the input (another program or an admin window may have focus).")

    def _key_input(self, vk: int = 0, scan: int = 0, up: bool = False, unicode: bool = False):
        flags = (0x0002 if up else 0) | (0x0004 if unicode else 0) | (0x0001 if vk in self.EXTENDED else 0)
        inp = self._INPUT(type=1)
        inp.ki = self._KEYBDINPUT(wVk=vk, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    def _mouse_input(self, flags: int, data: int = 0):
        inp = self._INPUT(type=0)
        inp.mi = self._MOUSEINPUT(dx=0, dy=0, mouseData=data & 0xFFFFFFFF, dwFlags=flags, time=0, dwExtraInfo=0)
        return inp

    def _vk(self, key: str) -> int:
        key = key.lower()
        if key in self.VK:
            return self.VK[key]
        m = re.fullmatch(r"f(\d{1,2})", key)
        if m and 1 <= int(m.group(1)) <= 24:
            return 0x6F + int(m.group(1))
        if len(key) == 1:
            if key.isalnum():
                return ord(key.upper())
            res = self._user32.VkKeyScanW(key)
            if res != -1:
                return res & 0xFF
        raise ComputerRefused(f"I don't know the key “{key}”.")

    # ---------------------------------------------------------------- InputDriver
    def foreground(self) -> WindowInfo:
        c, u = self._c, self._user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return WindowInfo()
        buf = c.create_unicode_buffer(512)
        u.GetWindowTextW(hwnd, buf, 512)
        cls = c.create_unicode_buffer(256)
        u.GetClassNameW(hwnd, cls, 256)
        pid = c.c_ulong(0)
        u.GetWindowThreadProcessId(hwnd, c.byref(pid))
        proc = ""
        try:
            import psutil

            proc = psutil.Process(pid.value).name()
        except Exception:
            pass
        return WindowInfo(buf.value, proc, cls.value, int(hwnd), int(pid.value))

    def screen_size(self) -> tuple[int, int]:
        return (self._user32.GetSystemMetrics(0), self._user32.GetSystemMetrics(1))

    def move(self, x: int, y: int) -> None:
        self._user32.SetCursorPos(int(x), int(y))

    def click(self, x: int, y: int, button: str = "left", count: int = 1) -> None:
        down, up = {"left": (0x0002, 0x0004), "right": (0x0008, 0x0010), "middle": (0x0020, 0x0040)}[button]
        self.move(x, y)
        time.sleep(0.03)
        for _ in range(max(1, min(count, 3))):
            self._send([self._mouse_input(down), self._mouse_input(up)])
            time.sleep(0.05)

    def drag(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.move(x1, y1)
        self._send([self._mouse_input(0x0002)])
        steps = 12
        for i in range(1, steps + 1):
            self.move(x1 + (x2 - x1) * i // steps, y1 + (y2 - y1) * i // steps)
            time.sleep(0.015)
        self._send([self._mouse_input(0x0004)])

    def scroll(self, clicks: int, horizontal: bool = False) -> None:
        # positive = up/right (Windows convention); WHEEL_DELTA = 120
        self._send([self._mouse_input(0x01000 if horizontal else 0x0800, int(clicks) * 120)])

    def type_text(self, text: str, newline_keys: tuple[str, ...] = ("enter",)) -> None:
        batch: list = []
        for ch in text.replace("\r\n", "\n"):
            if ch == "\n":
                if batch:
                    self._send(batch)
                    batch = []
                self.keys(list(newline_keys))
                continue
            if ch == "\t":
                batch += [self._key_input(vk=0x09), self._key_input(vk=0x09, up=True)]
                continue
            data = ch.encode("utf-16-le")
            for i in range(0, len(data), 2):  # surrogate pairs → two units
                unit = int.from_bytes(data[i:i + 2], "little")
                batch += [self._key_input(scan=unit, unicode=True), self._key_input(scan=unit, unicode=True, up=True)]
            if len(batch) >= 200:
                self._send(batch)
                batch = []
        if batch:
            self._send(batch)

    def keys(self, combo: list[str]) -> None:
        vks = [self._vk(k) for k in combo]
        seq = [self._key_input(vk=v) for v in vks] + [self._key_input(vk=v, up=True) for v in reversed(vks)]
        self._send(seq)

    def clipboard_get(self) -> str:
        c, u, k = self._c, self._user32, self._kernel32
        for _ in range(10):
            if u.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            raise ComputerUnavailable("The clipboard is busy (another program is using it).")
        try:
            h = u.GetClipboardData(13)  # CF_UNICODETEXT
            if not h:
                return ""
            p = k.GlobalLock(h)
            try:
                return c.wstring_at(p) if p else ""
            finally:
                k.GlobalUnlock(h)
        finally:
            u.CloseClipboard()

    def clipboard_set(self, text: str) -> None:
        c, u, k = self._c, self._user32, self._kernel32
        data = (text or "").encode("utf-16-le") + b"\x00\x00"
        for _ in range(10):
            if u.OpenClipboard(None):
                break
            time.sleep(0.05)
        else:
            raise ComputerUnavailable("The clipboard is busy (another program is using it).")
        try:
            u.EmptyClipboard()
            h = k.GlobalAlloc(0x0002, len(data))  # GMEM_MOVEABLE
            p = k.GlobalLock(h)
            c.memmove(p, data, len(data))
            k.GlobalUnlock(h)
            if not u.SetClipboardData(13, h):
                k.GlobalFree(h)
                raise ComputerUnavailable("Windows didn't accept the clipboard text.")
        finally:
            u.CloseClipboard()

    # ---------------------------------------------------------------- UI Automation
    def _uia(self):
        try:
            import uiautomation as auto  # type: ignore

            return auto
        except ImportError:
            return None

    def _to_element(self, i: int, ctrl) -> Optional[Element]:
        name = (ctrl.Name or "").strip()
        ctype = ctrl.ControlTypeName.replace("Control", "")
        role = ROLE_OF.get(ctype)
        if role is None or (not name and role != "field"):
            return None
        r = ctrl.BoundingRectangle
        value = ""
        if role == "field":
            try:
                vp = ctrl.GetValuePattern()
                value = (vp.Value or "")[:200] if vp else ""
            except Exception:
                pass
        return Element(i, name or "(unnamed field)", role, (r.left, r.top, r.width(), r.height()), bool(ctrl.IsEnabled),
                       bool(getattr(ctrl, "IsPassword", False)), value, ctrl)

    def elements(self, limit: int = 120) -> list[Element]:
        auto = self._uia()
        if auto is None:
            return []
        root = auto.GetForegroundControl()
        if root is None:
            return []
        try:  # climb to the top-level window so the whole window is listed, not just the focused pane
            while root.GetParentControl() is not None and root.GetParentControl().ControlTypeName != "PaneControl" \
                    and root.ControlTypeName != "WindowControl":
                root = root.GetParentControl()
        except Exception:
            pass
        out: list[Element] = []

        def walk(ctrl, depth: int = 0) -> None:
            if len(out) >= limit or depth > 14:
                return
            try:
                children = ctrl.GetChildren()
            except Exception:
                return
            for ch in children:
                try:
                    if ch.IsOffscreen:
                        continue
                except Exception:
                    pass
                el = self._to_element(len(out) + 1, ch)
                if el is not None:
                    out.append(el)
                walk(ch, depth + 1)

        walk(root)
        return out

    def focused_element(self) -> Optional[Element]:
        auto = self._uia()
        if auto is None:
            return None
        try:
            return self._to_element(0, auto.GetFocusedControl())
        except Exception:
            return None

    def invoke(self, el: Element) -> bool:
        ctrl = el.ref
        for getter in ("GetInvokePattern", "GetTogglePattern", "GetSelectionItemPattern", "GetExpandCollapsePattern"):
            try:
                pat = getattr(ctrl, getter)()
            except Exception:
                pat = None
            if pat is None:
                continue
            for method in ("Invoke", "Toggle", "Select", "Expand"):
                if hasattr(pat, method):
                    getattr(pat, method)()
                    return True
        return False

    def set_value(self, el: Element, text: str) -> bool:
        try:
            vp = el.ref.GetValuePattern()
            if vp is None or getattr(vp, "IsReadOnly", False):
                return False
            vp.SetValue(text)
            return True
        except Exception:
            return False

    def focus_element(self, el: Element) -> bool:
        try:
            el.ref.SetFocus()
            return True
        except Exception:
            return False


class RecordingInputDriver(InputDriver):
    """Test double: records every action; the 'screen' is a scripted window + element list."""

    name = "recording"

    def __init__(self, window: Optional[WindowInfo] = None, elements: Optional[list[Element]] = None) -> None:
        self.window = window or WindowInfo("Untitled - Notepad", "notepad.exe", "Notepad")
        self._elements = elements or []
        self.actions: list[tuple] = []
        self.clipboard = ""
        self.selection = ""
        self.values: dict[int, str] = {}
        self.invokable = True

    def available(self) -> bool:
        return True

    def foreground(self) -> WindowInfo:
        return self.window

    def click(self, x, y, button="left", count=1):
        self.actions.append(("click", x, y, button, count))

    def move(self, x, y):
        self.actions.append(("move", x, y))

    def drag(self, x1, y1, x2, y2):
        self.actions.append(("drag", x1, y1, x2, y2))

    def scroll(self, clicks, horizontal=False):
        self.actions.append(("scroll", clicks, horizontal))

    def type_text(self, text, newline_keys=("enter",)):
        self.actions.append(("type", text, tuple(newline_keys)))

    def keys(self, combo):
        self.actions.append(("keys", tuple(combo)))
        if combo == ["ctrl", "c"]:
            self.clipboard = self.selection

    def clipboard_get(self):
        return self.clipboard

    def clipboard_set(self, text):
        self.clipboard = text
        self.actions.append(("clipboard_set", text))

    def elements(self, limit=120):
        return list(self._elements)[:limit]

    def invoke(self, el):
        if not self.invokable:
            return False
        self.actions.append(("invoke", el.name))
        return True

    def set_value(self, el, text):
        self.values[el.id] = text
        self.actions.append(("set_value", el.name, text))
        return True

    def focus_element(self, el):
        self.actions.append(("focus", el.name))
        return True


def create_driver() -> InputDriver:
    if sys.platform.startswith("win"):
        try:
            return WindowsInputDriver()
        except Exception as exc:  # pragma: no cover - Windows only
            log.warning("input driver unavailable: %s", exc)
            return UnavailableDriver(f"Mouse/keyboard control couldn't start: {exc}")
    return UnavailableDriver()


# ------------------------------------------------------------------ controller
ORDINAL_WORDS = {"first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6, "seventh": 7, "eighth": 8, "ninth": 9,
                 "tenth": 10, "last": -1}
ROLE_WORDS = {"button": "button", "buttons": "button", "link": "link", "links": "link", "field": "field", "box": "field",
              "textbox": "field", "text box": "field", "input": "field", "checkbox": "checkbox", "check box": "checkbox",
              "tab": "tab", "menu": "menu item", "item": "list item", "option": "radio", "dropdown": "dropdown"}


class ComputerController:
    """High-level, safety-checked operations over an :class:`InputDriver`."""

    def __init__(self, driver: Optional[InputDriver] = None, backend: Any = None) -> None:
        self.driver = driver or create_driver()
        self.backend = backend
        self.last_elements: list[Element] = []
        self.last_window: Optional[WindowInfo] = None

    def available(self) -> bool:
        return self.driver.available()

    def _need(self) -> None:
        if not self.driver.available():
            raise ComputerUnavailable(getattr(self.driver, "reason", "Mouse/keyboard control isn't available here."))
        self._leave_own_window()

    def _leave_own_window(self) -> None:
        """When you type a command into Genesis's own window, act on the app you were using before — never on Genesis."""
        try:
            win = self.driver.foreground()
        except Exception:
            return
        if not win.pid or win.pid != os.getpid() or self.backend is None:
            return
        try:
            wins = self.backend.list_windows()  # Z-order: most recently used first
        except Exception:
            wins = []
        for w in wins:
            title = (w.get("title") or "").strip()
            if title and not title.lower().startswith("genesis") and w.get("id") != win.handle:
                self.backend.window_action(w["id"], "focus")
                time.sleep(0.35)
                return
        raise ComputerRefused("There's no other app window to work in — open the app first.")

    # ---------------------------------------------------------------- observe
    def observe(self, limit: int = 80) -> dict:
        self._need()
        win = self.driver.foreground()
        els = self.driver.elements(limit=limit)
        self.last_elements, self.last_window = els, win
        return {"window": win.public(), "elements": [e.public() for e in els], "accessible": bool(els),
                "is_terminal": is_terminal(win), "is_messaging": is_messaging(win)}

    def window(self) -> WindowInfo:
        self._need()
        return self.driver.foreground()

    # ---------------------------------------------------------------- find
    def find(self, target: str = "", role: str = "", nth: Optional[int] = None, element_id: Optional[int] = None,
             refresh: bool = True) -> Element:
        """Resolve "Save", "the second button", "the search box", element #7 → an element of the foreground window."""
        self._need()
        if refresh or not self.last_elements:
            self.observe()
        els = [e for e in self.last_elements if e.enabled]
        if element_id is not None:
            hit = next((e for e in self.last_elements if e.id == int(element_id)), None)
            if hit is None:
                raise ComputerRefused("That control isn't in the window any more — it may have changed.")
            return hit
        t = (target or "").strip()
        m = re.match(r"^(?:the\s+)?(?P<ord>first|second|third|fourth|fifth|sixth|seventh|eighth|ninth|tenth|last|\d+(?:st|nd|rd|th)?)"
                     r"\s+(?P<role>.+)$", t, re.I)
        if m and nth is None:
            o = m.group("ord").lower()
            nth = ORDINAL_WORDS.get(o) or int(re.sub(r"\D", "", o))
            rw = m.group("role").lower().rstrip("s") if m.group("role").lower() not in ROLE_WORDS else m.group("role").lower()
            role = role or ROLE_WORDS.get(rw, ROLE_WORDS.get(rw + "s", ""))
            t = "" if role else m.group("role")
        if not role:
            m2 = re.search(r"\b(button|link|field|box|textbox|text box|checkbox|tab|dropdown)$", t, re.I)
            if m2:
                role = ROLE_WORDS[m2.group(1).lower()]
                t = t[:m2.start()].strip()
        t = re.sub(r"^(?:the|a|an)\s+", "", t, flags=re.I)
        pool = [e for e in els if not role or e.role == role]
        if t:
            from ..tools.web import rank_elements

            exact = [e for e in pool if e.name.lower() == t.lower()]
            if exact:
                pool = exact
            else:
                ranked = rank_elements([{"name": e.name, "role": e.role, "_e": e} for e in pool], t, role)
                if not ranked:
                    where = self.last_window.title if self.last_window else "the active window"
                    raise ComputerRefused(f"I can't see anything called “{t}” in {where or 'the active window'}.")
                best = ranked[0][0]
                pool = [d["_e"] for sc, d in ranked if sc >= best - 1e-9]
        if not pool:
            raise ComputerRefused(f"I can't see any {role or 'matching control'}s in the active window.")
        if nth is not None:
            idx = nth - 1 if nth > 0 else len(pool) + nth
            if not 0 <= idx < len(pool):
                raise ComputerRefused(f"I can only see {len(pool)} {role or 'control'}{'s' if len(pool) != 1 else ''} there.")
            return pool[idx]
        if len(pool) > 1 and len({e.name.lower() for e in pool}) > 1:
            names = ", ".join(sorted({e.name for e in pool})[:6])
            raise ComputerRefused(f"There are several matches: {names}. Which one?")
        return pool[0]

    # ---------------------------------------------------------------- act
    def click(self, el: Optional[Element] = None, *, x: Optional[int] = None, y: Optional[int] = None, button: str = "left",
              count: int = 1) -> dict:
        self._need()
        if el is not None:
            if button == "left" and count == 1 and self.driver.invoke(el):
                return {"method": "accessibility", "element": el.public()}
            if el.bbox[2] <= 0 or el.bbox[3] <= 0:
                raise ComputerRefused(f"“{el.name}” can't be clicked (it has no position on screen).")
            cx, cy = el.center
            self.driver.click(cx, cy, button, count)
            return {"method": "element-position", "element": el.public()}
        if x is None or y is None:
            raise ComputerRefused("What should I click?")
        w, h = self.driver.screen_size()
        if not (0 <= x < w and 0 <= y < h):
            raise ComputerRefused("Those coordinates are off the screen.")
        self.driver.click(int(x), int(y), button, count)
        return {"method": "coordinates", "x": x, "y": y}

    def type(self, text: str, el: Optional[Element] = None, *, replace: bool = False) -> dict:
        self._need()
        win = self.driver.foreground()
        target = el or self.driver.focused_element()
        check_allowed("type", win, element=target)
        newline = ("shift", "enter") if is_messaging(win) else ("enter",)  # a newline must never send a chat message
        if "\n" in text.strip("\n") and not (is_messaging(win) or is_editor(win)):
            # in other apps a newline is Enter, which can submit a form — type one line at a time instead
            raise ComputerRefused("I can only type one line at a time in this app (Enter might submit something). "
                                  "Tell me the lines separately, or say “press enter” when you want it.")
        text = text.strip("\n") if not is_editor(win) else text
        if el is not None:
            if replace and self.driver.set_value(el, text):
                return {"method": "accessibility-value", "field": el.name, "chars": len(text)}
            self.driver.focus_element(el) or (el.bbox[2] > 0 and self.driver.click(*el.center))
            if replace:
                self.driver.keys(["ctrl", "a"])
        self.driver.type_text(text, newline)
        return {"method": "keystrokes", "field": el.name if el else (target.name if target else ""), "chars": len(text),
                "window": win.title}

    def press(self, keys: list[str]) -> dict:
        self._need()
        win = self.driver.foreground()
        focused = self.driver.focused_element() if (win.process or "").lower() in IDE_PROCESSES else None
        check_allowed("press", win, keys=keys, element=focused)
        self.driver.keys(keys)
        return {"keys": "+".join(keys), "window": win.title}

    def scroll(self, direction: str = "down", amount: int = 3) -> dict:
        self._need()
        n = max(1, min(int(amount), 30))
        horizontal = direction in ("left", "right")
        self.driver.scroll(n if direction in ("up", "left") else -n, horizontal)
        return {"direction": direction, "amount": n}

    def copy(self) -> dict:
        self._need()
        before = self.driver.clipboard_get()
        self.driver.keys(["ctrl", "c"])
        time.sleep(0.15)
        text = self.driver.clipboard_get()
        return {"text": text, "changed": text != before}

    def paste(self, text: Optional[str] = None) -> dict:
        self._need()
        win = self.driver.foreground()
        check_allowed("paste", win, element=self.driver.focused_element())
        if text is None and "\n" in (self.driver.clipboard_get() or "").strip() and not (is_messaging(win) or is_editor(win)):
            raise ComputerRefused("The clipboard has several lines, and pasting them here could submit something. "
                                  "Paste it yourself, or ask me to type one line.")
        if text is not None:
            self.driver.clipboard_set(text)
        self.driver.keys(["ctrl", "v"])
        return {"pasted_chars": len(text) if text is not None else None, "window": win.title}

    def select_all(self) -> dict:
        self._need()
        self.driver.keys(["ctrl", "a"])
        return {"selected": "all"}

    def drag(self, src: Element, dst: Element) -> dict:
        self._need()
        (x1, y1), (x2, y2) = src.center, dst.center
        self.driver.drag(x1, y1, x2, y2)
        return {"from": src.name, "to": dst.name}

    def switch_window(self, name: str) -> dict:
        """Bring a window to the front by (partial) title or app name."""
        if self.backend is None:
            raise ComputerUnavailable("Window switching isn't available.")
        wins = self.backend.list_windows()
        want = name.lower().strip()
        hits = [w for w in wins if want in (w.get("title", "") + " " + w.get("process", "")).lower()]
        if not hits:
            raise ComputerRefused(f"I can't find an open window for “{name}”.")
        self.backend.window_action(hits[0]["id"], "focus")
        return {"window": hits[0].get("title", name)}
