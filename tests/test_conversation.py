"""Upgrade tests: conversation sessions, follow-ups, task continuity, recovery, memory, routines, screen, coding.

Everything runs against fakes (RecordingBackend, FakeGoogle, ScriptedLLM, fake screen providers) — no real
microphone, network or Windows APIs are touched.
"""
from __future__ import annotations

import json
import logging
import time
import unittest
from datetime import datetime

from genesis.core.assistant import GenesisCore
from genesis.core.events import AssistantState, EventBus, EventType
from genesis.core.session import SessionManager
from genesis.core.settings import Settings
from genesis.database.db import Database
from genesis.database.repositories import Repositories
from genesis.nlp import rules
from genesis.services.platform import RecordingBackend
from genesis.services.routines import RoutineScheduler
from genesis.services.screen import ScreenElement, ScreenService

from .helpers import GenesisTestCase, ScriptedLLM

INBOX = [
    {"id": "m3", "thread_id": "t3", "from": "Prof Rao <rao@university.edu>", "subject": "Assignment deadline",
     "body": "Can you submit by tomorrow?", "unread": True, "message_id_header": "<abc123@university.edu>"},
    {"id": "m2", "thread_id": "t2", "from": "Rahul Sharma <rahul@example.com>", "subject": "Lunch?", "body": "Lunch today?",
     "unread": True},
    {"id": "m1", "thread_id": "t1", "from": "Newsletter <news@example.com>", "subject": "Weekly digest", "body": "News",
     "unread": True},
]


def tools_of(resp) -> list[tuple[str, str]]:
    return [(s["tool"], s["status"]) for p in resp.plans for s in p["steps"]]


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


# =====================================================================================
# 1. Session state machine
# =====================================================================================
class TestSessionStateMachine(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.settings = Settings(Repositories(self.db).settings)
        self.bus = EventBus()
        self.events: list[tuple] = []
        self.bus.subscribe(lambda e: self.events.append((e.type, dict(e.payload))))
        self.clock = FakeClock()
        self.s = SessionManager(self.settings, self.bus, clock=self.clock)

    def test_starts_sleeping_and_wakes(self) -> None:
        self.assertEqual(self.s.phase, AssistantState.SLEEPING)
        self.assertTrue(self.s.wake("wake_word"))
        self.assertEqual(self.s.phase, AssistantState.ACTIVE_CONVERSATION)
        self.assertEqual([h[1] for h in self.s.history], ["WAKING", "ACTIVE_CONVERSATION"])
        self.assertTrue(any(t == EventType.SESSION_CHANGED and p["active"] for t, p in self.events))
        self.assertFalse(self.s.wake("wake_word"))  # already awake → same session, no restart

    def test_no_sleep_while_talking_or_transcribing(self) -> None:
        """The first transcription can take ~30 s (model download); the session must not end under the user."""
        self.settings.set("conversation.timeout_seconds", 20)
        self.s.wake()
        self.s.hold()  # speech started
        self.clock.advance(45)
        self.assertFalse(self.s.check_timeout())
        self.s.release()  # transcribed — the countdown restarts now
        self.clock.advance(19)
        self.assertFalse(self.s.check_timeout())
        self.clock.advance(2)
        self.assertTrue(self.s.check_timeout())

    def test_inactivity_sleep_after_timeout(self) -> None:
        self.settings.set("conversation.timeout_seconds", 20)
        self.s.wake()
        self.clock.advance(19)
        self.assertFalse(self.s.check_timeout())
        self.clock.advance(2)
        self.assertTrue(self.s.check_timeout())
        self.assertEqual(self.s.phase, AssistantState.SLEEPING)
        self.assertEqual([h[1] for h in self.s.history][-2:], ["RETURNING_TO_SLEEP", "SLEEPING"])
        self.assertEqual(self.s.session.ended_reason, "inactivity")

    def test_timer_paused_while_busy_and_restarts_after_response(self) -> None:
        self.settings.set("conversation.timeout_seconds", 10)
        self.s.wake()
        self.s.begin_turn("voice")
        self.clock.advance(60)  # a long-running task: must NOT fall asleep mid-task
        self.assertIsNone(self.s.remaining())
        self.assertFalse(self.s.check_timeout())
        self.assertFalse(self.s.sleep("inactivity"))  # refuses while busy
        self.s.end_turn(awaiting_reply=False)
        self.clock.advance(8)  # e.g. TTS speaking the answer
        self.s.response_finished()  # countdown starts only now
        self.clock.advance(9)
        self.assertFalse(self.s.check_timeout())
        self.clock.advance(2)
        self.assertTrue(self.s.check_timeout())

    def test_longer_timeout_when_waiting_for_answer(self) -> None:
        self.settings.set("conversation.timeout_seconds", 10)
        self.settings.set("conversation.question_timeout_seconds", 40)
        self.s.wake()
        self.s.begin_turn()
        self.s.end_turn(awaiting_reply=True)
        self.clock.advance(30)
        self.assertFalse(self.s.check_timeout())
        self.clock.advance(11)
        self.assertTrue(self.s.check_timeout())

    def test_user_activity_keeps_session_alive(self) -> None:
        self.settings.set("conversation.timeout_seconds", 10)
        self.s.wake()
        for _ in range(5):
            self.clock.advance(8)
            self.s.user_activity()
            self.assertFalse(self.s.check_timeout())

    def test_max_session_cap_even_without_auto_sleep(self) -> None:
        self.settings.set("conversation.auto_sleep", False)
        self.settings.set("conversation.max_session_minutes", 1)
        self.s.wake()
        self.clock.advance(45)
        self.assertFalse(self.s.check_timeout())
        self.clock.advance(20)
        self.assertTrue(self.s.check_timeout())
        self.assertEqual(self.s.session.ended_reason, "max_session")

    def test_rewake_after_sleep_creates_new_session(self) -> None:
        self.s.wake()
        first = self.s.session.id
        self.s.sleep("user_request")
        self.assertTrue(self.s.sleeping)
        self.assertTrue(self.s.wake("wake_word"))
        self.assertNotEqual(self.s.session.id, first)

    def test_invalid_transitions_ignored(self) -> None:
        self.assertFalse(self.s.sleep())  # sleeping → sleeping is not a transition
        self.assertEqual(self.s.history, [])


# =====================================================================================
# 2. Core + session integration
# =====================================================================================
class TestCoreSession(GenesisTestCase):
    def test_starts_sleeping_text_wakes_and_follow_ups_share_session(self) -> None:
        self.assertEqual(self.core.session.phase, AssistantState.SLEEPING)
        self.say("open notepad")
        self.assertTrue(self.core.session.active)
        sid = self.core.session.session.id
        self.say("open spotify")
        self.assertEqual(self.core.session.session.id, sid)
        self.assertEqual(self.core.session.session.turns, 2)

    def test_go_to_sleep_and_thats_all(self) -> None:
        for phrase in ("go to sleep", "that's all", "thanks, that's it for now", "goodbye genesis"):
            self.say("what time is it")
            self.assertTrue(self.core.session.active, phrase)
            r = self.say(phrase)
            self.assertIn("Hey Genesis", r.text)
            self.assertTrue(self.core.session.sleeping, phrase)

    def test_state_events_reflect_activity(self) -> None:
        seen: list[str] = []
        self.svc.events.subscribe(lambda e: seen.append(e.payload.get("state")) if e.type == EventType.STATE_CHANGED else None)
        self.say("open notepad")
        for st in ("WAKING", "ACTIVE_CONVERSATION", "THINKING", "EXECUTING", "VERIFYING"):
            self.assertIn(st, seen)

    def test_session_times_out_then_new_command_rewakes(self) -> None:
        self.svc.settings.set("conversation.timeout_seconds", 3)
        self.say("open notepad")
        self.core.session._idle_since -= 10  # simulate 10 s of silence
        self.assertTrue(self.core.session.check_timeout())
        self.assertTrue(self.core.session.sleeping)
        self.say("open spotify")
        self.assertTrue(self.core.session.active)

    def test_routine_source_does_not_wake_session(self) -> None:
        self.core.handle("open notepad", source="routine")
        self.assertTrue(self.core.session.sleeping)

    def test_utterance_content_not_logged_by_default(self) -> None:
        records: list[logging.LogRecord] = []

        class H(logging.Handler):
            def emit(self, record):
                records.append(record)

        h = H()
        logging.getLogger("genesis").addHandler(h)
        old = logging.getLogger("genesis").level
        logging.getLogger("genesis").setLevel(logging.INFO)
        try:
            self.say("remind me in 10 minutes to call my secret contact")
            dumped = json.dumps([getattr(r, "data", None) for r in records], default=str)
            self.assertNotIn("secret contact", dumped)
            self.assertTrue(any(r.getMessage() == "utterance" for r in records))
            self.svc.settings.set("debug.log_content", True)
            records.clear()
            self.say("remind me in 10 minutes to water the plants")
            dumped = json.dumps([getattr(r, "data", None) for r in records], default=str)
            self.assertIn("water the plants", dumped)
        finally:
            logging.getLogger("genesis").removeHandler(h)
            logging.getLogger("genesis").setLevel(old)


# =====================================================================================
# 3. Follow-ups and context
# =====================================================================================
class TestFollowUps(GenesisTestCase):
    connect_gmail = True

    def setUp(self) -> None:
        super().setUp()
        self.google.inbox = [dict(m) for m in INBOX]

    def test_ordinals_and_the_other_one(self) -> None:
        self.say("check my unread emails")
        r = self.say("open the second one")
        self.assertEqual(tools_of(r), [("gmail_open_message", "succeeded")])
        self.assertIn("Lunch?", r.text)
        r = self.say("no, the other one")
        self.assertIn("Weekly digest", r.text)
        r = self.say("open the latest one")
        self.assertIn("Assignment deadline", r.text)
        self.assertTrue(self.urls_opened()[-1].endswith("#inbox/m3"))

    def test_open_the_one_from_person(self) -> None:
        self.say("check my unread emails")
        r = self.say("open the one from Rahul")
        self.assertIn("Lunch?", r.text)

    def test_ambiguous_references_ask_instead_of_guessing(self) -> None:
        r = self.say("open the second one")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("Which list", r.text)
        r = self.say("close that")
        self.assertEqual(r.status, "awaiting_input")
        self.assertEqual(tools_of(r), [])
        self.say("check my unread emails")
        r = self.say("open the fifth one")
        self.assertEqual(r.status, "awaiting_input")
        self.assertIn("only 3", r.text)
        r = self.say("reply saying ok")
        self.assertEqual(r.status, "awaiting_input")  # no e-mail selected yet
        self.assertEqual(self.google.drafts, {})

    def test_open_it_close_that_keeps_confirmation(self) -> None:
        self.say("open notepad")
        self.backend.existing.append("notepad.exe")
        r = self.say("close that")
        self.assertEqual(r.status, "awaiting_confirmation")  # follow-ups never bypass the permission layer
        self.assertEqual(tools_of(r), [("close_application", "awaiting_confirmation")])
        r = self.say("yes")
        self.assertEqual(tools_of(r), [("close_application", "succeeded")])
        r = self.say("open it")
        self.assertEqual(tools_of(r), [("open_application", "succeeded")])
        self.assertIn("Notepad", r.text)

    def test_do_that_for_someone_else(self) -> None:
        self.say("draft an email to rahul@example.com saying the meeting is at 5")
        r = self.say("do that for priya@example.com too")
        self.assertEqual(r.status, "done", r.text)
        tos = sorted(self.google.parsed(d)["To"] for d in self.google.drafts)
        self.assertEqual(tos, ["priya@example.com", "rahul@example.com"])

    def test_do_that_for_contact_by_name(self) -> None:
        self.svc.repos.contacts.upsert("Rahul", email="rahul@example.com")
        self.say("draft an email to priya@example.com saying the report is ready")
        r = self.say("do that for Rahul too")
        self.assertEqual(r.status, "done", r.text)
        self.assertIn("rahul@example.com", [self.google.parsed(d)["To"] for d in self.google.drafts])

    def test_repeat_and_try_again(self) -> None:
        r = self.say("do the same thing again")
        self.assertEqual(r.status, "awaiting_input")  # nothing to repeat yet
        self.say("open notepad")
        n = len(self.launched())
        self.say("do the same thing again")
        self.assertEqual(len(self.launched()), n + 1)
        self.google.fail_reads = 9
        r = self.say("check my unread emails")
        self.assertEqual(r.status, "failed")
        self.google.fail_reads = 0
        r = self.say("try again")
        self.assertEqual(r.status, "done")
        self.assertIn("unread", r.text)

    def test_go_back_and_discussed_earlier(self) -> None:
        self.say("open my documents folder")
        self.say("open the downloads folder")
        self.say("open my pictures folder")
        r = self.say("go back")
        self.assertIn("Downloads", r.text)
        r = self.say("go back")
        self.assertIn("Documents", r.text)
        self.say("open the downloads folder")
        r = self.say("use the one we discussed earlier")
        self.assertIn("Documents", r.text)

    def test_interruption_phrase_then_new_command(self) -> None:
        r = self.say("wait, open notepad")
        self.assertEqual(tools_of(r), [("open_application", "succeeded")])
        self.backend.existing.append("notepad.exe")
        r = self.say("close notepad")
        self.assertEqual(r.status, "awaiting_confirmation")
        r = self.say("stop, open spotify instead")  # a new command replaces the pending confirmation…
        self.assertNotIn(("terminate", ("notepad.exe",)), self.backend.actions)  # …and nothing was closed
        self.assertIn("Spotify", r.text)

    def test_multiple_apps_in_one_command(self) -> None:
        r = self.say("open notepad and spotify")
        self.assertEqual(tools_of(r), [("open_application", "succeeded"), ("open_application", "succeeded")])


# =====================================================================================
# 4. Task continuity: the full e-mail workflow
# =====================================================================================
class TestEmailWorkflow(GenesisTestCase):
    connect_gmail = True

    def test_open_check_read_reply_revise_send(self) -> None:
        self.google.inbox = [dict(m) for m in INBOX]
        r = self.say("open gmail")
        self.assertIn("Gmail is open", r.text)
        r = self.say("check my unread emails")
        self.assertIn("three unread", r.text)
        r = self.say("open the latest one")
        self.assertIn("Can you submit by tomorrow?", r.text)
        r = self.say("reply saying I'll submit it tomorrow")
        self.assertEqual(r.status, "done", r.text)
        self.assertEqual(len(self.google.drafts), 1)
        did = next(iter(self.google.drafts))
        msg = self.google.parsed(did)
        self.assertEqual(msg["To"], "rao@university.edu")
        self.assertEqual(msg["Subject"], "Re: Assignment deadline")
        self.assertEqual(msg["In-Reply-To"], "<abc123@university.edu>")
        self.assertEqual(self.google.drafts[did]["thread_id"], "t3")
        self.assertIn("tomorrow", msg.get_body(("plain",)).get_content())

        r = self.say("actually change tomorrow to Friday")
        self.assertEqual(tools_of(r), [("revise_text", "succeeded"), ("gmail_update_draft", "succeeded")])
        body = self.google.parsed(did).get_body(("plain",)).get_content()
        self.assertIn("Friday", body)
        self.assertNotIn("tomorrow", body)
        self.assertEqual(len(self.google.drafts), 1)  # updated in place, not duplicated
        self.assertEqual(self.google.drafts[did]["thread_id"], "t3")

        r = self.say("send it")
        self.assertEqual(r.status, "awaiting_confirmation")  # sending always asks
        self.assertEqual(self.google.sent, [])
        r = self.say("yes")
        self.assertEqual(self.google.sent, [did])
        self.assertIn("Sent", r.text)

    def test_declining_send_keeps_draft(self) -> None:
        self.google.inbox = [dict(m) for m in INBOX]
        self.say("check my unread emails")
        self.say("open the first one")
        self.say("reply saying thanks")
        self.say("send it")
        r = self.say("no")
        self.assertIn("Cancelled", r.text)
        self.assertEqual(self.google.sent, [])
        self.assertEqual(len(self.google.drafts), 1)

    def test_style_revision_offline_is_honest(self) -> None:
        self.say("draft an email to rahul@example.com saying the meeting is at 5")
        r = self.say("make it shorter")
        self.assertEqual(r.status, "failed")
        self.assertIn("language model", r.text)
        r = self.say("change the subject to Meeting time")
        self.assertEqual(self.google.parsed(next(iter(self.google.drafts)))["Subject"], "Meeting time")


class TestEmailRevisionWithLLM(GenesisTestCase):
    connect_gmail = True

    def setUp(self) -> None:
        def reply(messages, json_mode):
            last = messages[-1]["content"]
            if "shorter" in last.lower() and json_mode:
                return json.dumps({"subject": "Meeting", "body": "Hi Rahul,\n\nMeeting at 5.\n\nThanks"})
            return "{}" if json_mode else "OK"

        self.llm = ScriptedLLM(reply)
        super().setUp()

    def test_make_it_shorter_updates_same_draft(self) -> None:
        self.say("draft an email to rahul@example.com saying the meeting is at 5 and please bring the slides")
        r = self.say("actually make it shorter")
        self.assertEqual(r.status, "done", r.text)
        self.assertEqual(len(self.google.drafts), 1)
        self.assertIn("Meeting at 5", self.google.parsed(next(iter(self.google.drafts))).get_body(("plain",)).get_content())


# =====================================================================================
# 5. Recovery & verification
# =====================================================================================
class StubbornBackend(RecordingBackend):
    def terminate_processes(self, names, timeout=5.0):
        self.actions.append(("terminate", tuple(names)))
        return 1  # claims success, but the process is still running


class TestRecovery(GenesisTestCase):
    connect_gmail = True

    def test_transient_read_failure_is_retried(self) -> None:
        self.google.inbox = [dict(m) for m in INBOX]
        self.google.fail_reads = 1
        r = self.say("check my unread emails")
        self.assertEqual(r.status, "done", r.text)

    def test_send_is_never_retried_automatically(self) -> None:
        self.say("draft an email to rahul@example.com saying hi")
        calls_before = len(self.google.calls)
        orig = self.google.request

        def flaky(method, url, **kw):
            if "/drafts/send" in url:
                self.google.calls.append((method, url))
                from .helpers import FakeResponse

                return FakeResponse(503, {"error": {"message": "unavailable"}})
            return orig(method, url, **kw)

        self.google.request = flaky
        self.say("send it")
        r = self.say("yes")
        self.assertNotEqual(r.status, "done")
        sends = [c for c in self.google.calls[calls_before:] if "/drafts/send" in c[1]]
        self.assertEqual(len(sends), 1)

    def test_alternative_launch_method(self) -> None:
        from genesis.services.app_resolver import ResolvedApp

        class TwoWays:
            def resolve_all(self, name):
                return [ResolvedApp("notepad", "Notepad", "exe", "C:/broken/notepad.exe", ["ghost.exe"]),
                        ResolvedApp("notepad", "Notepad", "exe", "C:/Windows/notepad.exe", ["notepad.exe"])]

        class Backend(RecordingBackend):
            def launch(self, argv, cwd=None):
                pid = super().launch(argv, cwd)
                if "Windows" in argv[0]:
                    self.existing.append("notepad.exe")
                return pid

            def process_alive(self, pid):
                return False

        self.svc.apps = TwoWays()
        self.svc.backend = Backend()
        r = self.say("open notepad")
        self.assertIn("tried another way", r.text)

    def test_verification_failure_is_reported_not_claimed(self) -> None:
        self.svc.backend.__class__ = StubbornBackend
        self.backend.existing.append("notepad.exe")
        self.say("close notepad")
        r = self.say("yes")
        self.assertEqual(r.status, "failed")
        self.assertIn("couldn't verify", r.text)

    def test_unknown_app_fails_honestly(self) -> None:
        r = self.say("open flurbometer")
        self.assertNotEqual(r.status, "done")
        self.assertEqual(self.launched(), [])


# =====================================================================================
# 6. Memory
# =====================================================================================
class TestLongTermMemory(GenesisTestCase):
    def test_remember_recall_forget_project_folder(self) -> None:
        r = self.say("remember that my project folder is ~/Documents")
        self.assertEqual(tools_of(r), [("remember_project", "succeeded")])
        r = self.say("what do you remember about my project folder")
        self.assertIn("Documents", r.text)
        r = self.say("open my project")
        self.assertIn("project", r.text.lower())
        r = self.say("forget that")
        self.assertIn("Forgotten", r.text)
        r = self.say("what do you remember about my project folder")
        self.assertIn("don't have anything", r.text)

    def test_remember_fact_and_forget_that(self) -> None:
        self.say("remember that I prefer short emails")
        self.assertTrue(any("short emails" in f["content"] for f in self.svc.repos.memory.list()))
        self.say("forget that")
        self.assertFalse(any("short emails" in f["content"] for f in self.svc.repos.memory.list()))

    def test_memory_persists_across_restart(self) -> None:
        self.say("remember that my project folder is ~/Documents")
        self.core.stop()
        core2 = GenesisCore(self.svc)
        core2.start()
        try:
            self.assertEqual(core2.session.phase, AssistantState.SLEEPING)
            r = core2.handle("what do you remember about my project folder")
            self.assertIn("Documents", r.text)
        finally:
            core2.stop()


# =====================================================================================
# 7. Routines
# =====================================================================================
class TestRoutines(GenesisTestCase):
    def test_parse_routine_is_not_split(self) -> None:
        self.assertEqual(len(rules.split_requests("every monday at 8 am, open vs code and my genesis project")), 1)
        sl = rules.routine_slots("on weekdays at 9, open gmail and check my unread emails")
        self.assertEqual(sl["days"], [0, 1, 2, 3, 4])
        self.assertEqual(sl["time"], "09:00")
        sl = rules.routine_slots("create a routine called focus that opens notepad and spotify")
        self.assertEqual(sl["commands"], "open notepad and spotify")

    def test_create_requires_confirmation_then_runs_on_request(self) -> None:
        r = self.say("every monday at 8 am, open vs code and my genesis project")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.svc.repos.routines.list(), [])
        self.say("yes")
        rows = self.svc.repos.routines.list()
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]["days"], rows[0]["time_of_day"]), ([0], "08:00"))
        r = self.say("run my monday routine")
        self.assertIn(("vscode_open_project", "succeeded"), tools_of(r))

    def test_scheduler_fires_through_normal_pipeline(self) -> None:
        self.svc.repos.routines.add("focus", "open notepad", [0], "08:00")
        ran: list[str] = []
        sched = RoutineScheduler(self.svc.repos.routines, lambda r: ran.append(self.core.handle(r["commands"],
                                                                                              source="routine").status),
                                 now=lambda: datetime(2026, 9, 21, 8, 3))  # a Monday, 3 min late (PC was busy)
        self.assertEqual(len(sched.check_now()), 1)
        self.assertEqual(ran, ["done"])
        self.assertEqual(sched.check_now(), [])  # only once per day
        sched.now = lambda: datetime(2026, 9, 22, 8, 0)  # Tuesday → not scheduled
        self.assertEqual(sched.due(), [])

    def test_routine_with_risky_step_still_asks(self) -> None:
        self.backend.existing.append("notepad.exe")
        self.svc.repos.routines.add("cleanup", "close notepad", [], None)
        r = self.say("run my cleanup routine")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertNotIn(("terminate", ("notepad.exe",)), self.backend.actions)

    def test_routine_cannot_start_routine(self) -> None:
        self.svc.repos.routines.add("loop", "run my loop routine", [], None)
        r = self.say("run my loop routine")
        self.assertIn("can't start another routine", r.text)

    def test_repeated_pattern_suggests_routine_and_saves_it(self) -> None:
        tip = None
        for _ in range(3):
            self.say("open notepad")
            r = self.say("open spotify")
            tip = tip or ("often do" in r.text and r.text)
        self.assertTrue(tip, "expected a routine suggestion after the pattern repeated")
        r = self.say("save that as a routine")
        if r.status == "awaiting_confirmation":
            r = self.say("yes")
        self.assertEqual(len(self.svc.repos.routines.list()), 1)
        self.assertIn("open notepad", self.svc.repos.routines.list()[0]["commands"])

    def test_usage_tracking_respects_setting(self) -> None:
        self.svc.settings.set("memory.track_usage", False)
        self.say("open notepad")
        self.assertEqual(self.svc.repos.usage.top(), [])
        self.svc.settings.set("memory.track_usage", True)
        self.say("open notepad")
        self.assertIn("tool:open_application", [u["key"] for u in self.svc.repos.usage.top()])


# =====================================================================================
# 8. Screen understanding (fake providers)
# =====================================================================================
class _Img:
    size = (1920, 1080)


class FakeCapturer:
    name = "fake-capture"

    def __init__(self) -> None:
        self.captures = 0

    def available(self):
        return True

    def capture(self):
        self.captures += 1
        return _Img()


class FakeOcr:
    name = "fake-ocr"

    def available(self):
        return True

    def read(self, image):
        return ["Build output", "Error: Cannot find module 'react'"]


class FakeElements:
    name = "fake-uia"

    def __init__(self) -> None:
        self.clicked: list[str] = []

    def available(self):
        return True

    def window_title(self):
        return "Visual Studio Code"

    def list(self, max_elements=150):
        return [ScreenElement("Save", "Button"), ScreenElement("Cancel", "Button"), ScreenElement("Run", "Button", enabled=False)]

    def invoke(self, element):
        self.clicked.append(element.name)


class FakeVision:
    name = "fake-vision"
    is_remote = True

    def available(self):
        return True

    def describe(self, image, question):
        return "A code editor with a red error banner."


class TestScreen(GenesisTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.cap, self.els = FakeCapturer(), FakeElements()
        self.svc.screen = ScreenService(capturer=self.cap, ocr=FakeOcr(), elements=self.els)

    def test_capture_only_on_request_and_structured(self) -> None:
        self.say("open notepad")
        self.assertEqual(self.cap.captures, 0)
        r = self.say("what's on my screen")
        self.assertEqual(self.cap.captures, 1)
        self.assertIn("Cannot find module", r.text)
        step = r.plans[0]["steps"][0]
        self.assertEqual(step["tool"], "analyze_screen")

    def test_click_by_name_requires_confirmation(self) -> None:
        r = self.say("click the Save button")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.els.clicked, [])
        self.say("yes")
        self.assertEqual(self.els.clicked, ["Save"])
        self.say("click Run")
        r = self.say("yes")
        self.assertIn("can't find an enabled control", r.text)

    def test_remote_vision_asks_first(self) -> None:
        self.svc.screen.vision = FakeVision()
        r = self.say("what's on my screen")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertEqual(self.cap.captures, 0)
        r = self.say("yes")
        self.assertIn("red error banner", r.text)

    def test_screen_disabled_setting(self) -> None:
        self.svc.settings.set("screen.enabled", False)
        r = self.say("what's on my screen")
        self.assertIn("turned off", r.text)
        self.assertEqual(self.cap.captures, 0)


# =====================================================================================
# 9. Coding agent loop
# =====================================================================================
class TestCodingAgent(GenesisTestCase):
    def setUp(self) -> None:
        def reply(messages, json_mode):
            if "Fix the problem" in messages[-1]["content"]:
                return json.dumps({"summary": "complete the unfinished expression",
                                   "edits": [{"find": "return a +\n", "replace": "return a + b\n"}]})
            return "{}" if json_mode else "OK"

        self.llm = ScriptedLLM(reply)
        super().setUp()
        self.proj = self.home / "Projects" / "calc"
        (self.proj / "tests").mkdir(parents=True)
        (self.proj / "calc.py").write_text("def add(a, b):\n    return a +\n")
        (self.proj / "tests" / "__init__.py").write_text("")
        (self.proj / "tests" / "test_calc.py").write_text(
            "import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n")
        self.svc.repos.projects.set("calc", str(self.proj))

    def test_inspect_diagnose_propose_confirm_modify_test_undo(self) -> None:
        r = self.say("check my calc project for errors")
        self.assertIn("SyntaxError", r.text)
        r = self.say("fix the first one")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.assertIn("return a +\n", (self.proj / "calc.py").read_text())  # nothing changed before permission
        r = self.say("yes")
        self.assertEqual(tools_of(r), [("propose_fix", "succeeded"), ("apply_patch", "succeeded"), ("run_tests", "succeeded")])
        self.assertIn("return a + b", (self.proj / "calc.py").read_text())
        self.assertIn("tests pass", r.text)
        r = self.say("undo that")
        self.assertEqual(r.status, "awaiting_confirmation")
        self.say("yes")
        self.assertIn("return a +\n", (self.proj / "calc.py").read_text())

    def test_declined_fix_changes_nothing(self) -> None:
        self.say("check my calc project for errors")
        self.say("fix the first one")
        self.say("no")
        self.assertIn("return a +\n", (self.proj / "calc.py").read_text())


# =====================================================================================
# 10. Persistence / migrations
# =====================================================================================
class TestMigrations(unittest.TestCase):
    def test_v2_migration_idempotent_and_preserves_data(self) -> None:
        import tempfile
        from pathlib import Path

        path = Path(tempfile.mkdtemp()) / "g.db"
        db = Database(path)
        repos = Repositories(db)
        repos.memory.add("fact", "I like tea") if hasattr(repos.memory, "add") else None
        rid = repos.routines.add("x", "open notepad", [0], "08:00")
        repos.usage.bump("tool:open_application")
        db.close()
        db2 = Database(path)  # re-running migrations must not fail or drop data
        repos2 = Repositories(db2)
        self.assertEqual(repos2.routines.get(rid)["commands"], "open notepad")
        self.assertEqual(repos2.usage.top()[0]["count"], 1)
        self.assertGreaterEqual(db2.schema_version, 2)
        db2.close()


# =====================================================================================
# 11. Voice: continuous conversation, sleep, re-wake, barge-in
# =====================================================================================
class TestVoiceConversation(unittest.TestCase):
    def setUp(self) -> None:
        from .test_voice import FakeSTT, FakeWake

        self.FakeSTT, self.FakeWake = FakeSTT, FakeWake
        self.db = Database(":memory:")
        self.settings = Settings(Repositories(self.db).settings)
        self.settings.set("conversation.timeout_seconds", 3)
        self.bus = EventBus()
        self.session = SessionManager(self.settings, self.bus)

    def make(self, frames, texts, handler, tts=None):
        from genesis.core.config import GenesisConfig
        from genesis.voice.manager import VoiceManager

        from .test_voice import FakeMic, FakeTTS

        self.mic = FakeMic(frames)
        self.tts = tts or FakeTTS()
        self.stt = self.FakeSTT(texts)
        return VoiceManager(GenesisConfig(), self.settings, self.bus, handler, mic=self.mic, wake=self.FakeWake(refractory_s=0),
                            stt=self.stt, tts=self.tts, session=self.session)

    @staticmethod
    def wait(cond, timeout=12.0) -> bool:
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return True
            time.sleep(0.02)
        return False

    def test_wake_once_follow_ups_then_sleep_then_rewake(self) -> None:
        from .test_voice import WAKE_MARK, silence, tone

        heard: list[str] = []

        def handler(text):
            heard.append(text)
            return f"ok {len(heard)}", False

        frames = [WAKE_MARK] + tone(700) + silence(1400) + tone(600) + silence(1400)
        vm = self.make(frames, ["open gmail", "check my unread emails", "open notepad"], handler)
        vm.start()
        try:
            self.assertTrue(self.wait(lambda: len(heard) >= 2))
            self.assertEqual(heard[:2], ["open gmail", "check my unread emails"])  # 2nd needed no wake word
            self.assertTrue(self.session.active)
            self.assertTrue(self.wait(lambda: self.session.sleeping), "should sleep after the silence timeout")
            self.assertEqual(self.session.session.ended_reason, "inactivity")
            for f in tone(300) + silence(300):  # speech while sleeping is ignored (no wake word)
                self.mic.q.put(f)
            time.sleep(0.6)
            self.assertEqual(len(heard), 2)
            for f in [WAKE_MARK] + tone(600) + silence(1400):
                self.mic.q.put(f)
            self.assertTrue(self.wait(lambda: len(heard) >= 3), "re-wake must work without restarting")
            self.assertEqual(heard[2], "open notepad")
        finally:
            vm.stop()

    def test_go_to_sleep_by_voice(self) -> None:
        from .test_voice import WAKE_MARK, silence, tone

        heard: list[str] = []

        def handler(text):
            heard.append(text)
            if text == "go to sleep":
                self.session.sleep("user_request")
            return "ok", False

        frames = [WAKE_MARK] + tone(600) + silence(1400) + tone(600) + silence(1400)
        vm = self.make(frames, ["open notepad", "go to sleep", "SHOULD NOT BE HEARD"], handler)
        vm.start()
        try:
            self.assertTrue(self.wait(lambda: len(heard) >= 2))
            self.assertTrue(self.wait(lambda: self.session.sleeping))
            for f in tone(600) + silence(1400):
                self.mic.q.put(f)
            time.sleep(0.8)
            self.assertEqual(heard, ["open notepad", "go to sleep"])
        finally:
            vm.stop()

    def test_single_command_mode(self) -> None:
        from .test_voice import WAKE_MARK, silence, tone

        self.settings.set("conversation.enabled", False)
        heard: list[str] = []
        frames = [WAKE_MARK] + tone(600) + silence(1400) + tone(600) + silence(1400)
        vm = self.make(frames, ["open notepad", "SHOULD NOT BE HEARD"], lambda t: (heard.append(t) or "ok", False))
        vm.start()
        try:
            self.assertTrue(self.wait(lambda: len(heard) >= 1))
            self.assertTrue(self.wait(lambda: self.session.sleeping))
            time.sleep(0.5)
            self.assertEqual(heard, ["open notepad"])
        finally:
            vm.stop()

    def test_barge_in_with_wake_word_while_speaking(self) -> None:
        from .test_voice import WAKE_MARK, FakeTTS, silence, tone

        class SpeakingTTS(FakeTTS):
            def speak(self, text):
                super().speak(text)
                self._speaking = True

            def stop(self):
                super().stop()
                self._speaking = False

        heard: list[str] = []

        def handler(text):
            heard.append(text)
            return ("here is a very long answer " * 20) if len(heard) == 1 else "ok", False

        tts = SpeakingTTS()
        frames = [WAKE_MARK] + tone(600) + silence(1400)
        vm = self.make(frames, ["tell me about the project", "open notepad"], handler, tts=tts)
        vm.start()
        try:
            self.assertTrue(self.wait(lambda: tts.speaking))
            for f in [WAKE_MARK] + tone(600) + silence(1400):  # "Hey Genesis — open notepad" over the reply
                self.mic.q.put(f)
            self.assertTrue(self.wait(lambda: len(heard) >= 2))
            self.assertEqual(heard[1], "open notepad")
            self.assertGreaterEqual(vm.interruptions, 1)
            self.assertGreaterEqual(tts.stopped, 1)
        finally:
            vm.stop()


if __name__ == "__main__":
    unittest.main()
