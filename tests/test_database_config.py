"""Database, settings and configuration tests."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from genesis.core.config import GenesisConfig
from genesis.core.events import EventBus, EventType
from genesis.core.settings import Settings
from genesis.database.db import MIGRATIONS, Database
from genesis.database.repositories import Repositories


class TestDatabase(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "t.db"
        self.db = Database(self.path)
        self.r = Repositories(self.db)

    def tearDown(self):
        self.db.close()

    def test_schema(self):
        self.assertEqual(self.db.schema_version, MIGRATIONS[-1][0])
        tables = {r["name"] for r in self.db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for t in ("settings", "preferences", "conversations", "messages", "memory", "contacts", "tasks", "tool_executions",
                  "permissions", "connected_services", "reminders", "projects", "knowledge_documents", "knowledge_chunks"):
            self.assertIn(t, tables)

    def test_migrations_idempotent(self):
        self.db.close()
        db2 = Database(self.path)
        self.assertEqual(db2.schema_version, MIGRATIONS[-1][0])
        db2.close()
        self.db = Database(self.path)

    def test_conversations_cascade(self):
        cid = self.r.conversations.create()
        self.r.conversations.add_message(cid, "user", "hi", {"source": "text"})
        self.assertEqual(self.r.conversations.messages(cid)[0]["metadata"], {"source": "text"})
        self.r.conversations.delete(cid)
        self.assertEqual(self.db.query("SELECT * FROM messages"), [])

    def test_contacts_lookup(self):
        self.r.contacts.upsert("Professor Rao", email="rao@uni.edu", alias="Professor")
        for q in ("my professor", "professor", "Professor Rao", "rao", "RAO@UNI.EDU"):
            self.assertEqual(self.r.contacts.find(q)["email"], "rao@uni.edu", q)
        self.assertIsNone(self.r.contacts.find("someone else"))

    def test_role_constraint(self):
        cid = self.r.conversations.create()
        import sqlite3

        with self.assertRaises(sqlite3.IntegrityError):
            self.r.conversations.add_message(cid, "hacker", "x")

    def test_transaction_rollback(self):
        with self.assertRaises(RuntimeError):
            with self.db.transaction() as c:
                c.execute("INSERT INTO memory(kind,content,source,created_at) VALUES('fact','x','user','now')")
                raise RuntimeError
        self.assertEqual(self.r.memory.list(), [])

    def test_services_and_permissions(self):
        self.r.services.set("google", "connected", account="a@b.co", scopes=["x"])
        self.assertEqual(self.r.services.get("google")["scopes"], ["x"])
        self.r.permissions.set("open_url", "deny")
        self.assertEqual(self.r.permissions.get("open_url"), "deny")


class TestSettings(unittest.TestCase):
    def test_defaults_persist_and_types(self):
        db = Database(":memory:")
        s = Settings(Repositories(db).settings)
        self.assertTrue(s.get("voice.enabled"))
        s.set("ui.auto_hide_seconds", 3)
        self.assertEqual(Settings(Repositories(db).settings).get("ui.auto_hide_seconds"), 3)
        with self.assertRaises(TypeError):
            s.set("voice.enabled", "yes")
        s.set("voice.wake_sensitivity", 1)  # int accepted for float
        self.assertEqual(s.get("voice.wake_sensitivity"), 1.0)


class TestConfig(unittest.TestCase):
    def test_from_env(self):
        env = {"GENESIS_LLM_PROVIDER": "ollama", "HF_TOKEN": "hf_x", "HF_MODEL_ID": "Qwen/Qwen2.5-7B-Instruct",
               "GENESIS_SYSTEM_DRY_RUN": "1", "GOOGLE_CLIENT_ID": "id", "GOOGLE_CLIENT_SECRET": "s"}
        with mock.patch.dict(os.environ, env, clear=False):
            cfg = GenesisConfig.from_env(env_file=Path("/nonexistent/.env"), home=Path(tempfile.mkdtemp()))
        self.assertEqual(cfg.llm_provider, "ollama")
        self.assertTrue(cfg.system_dry_run)
        d = cfg.describe()
        self.assertTrue(d["google_oauth_configured"])
        self.assertNotIn("hf_x", str(d))  # describe() never includes secrets

    def test_env_example_lists_every_variable(self):
        example = (Path(__file__).resolve().parents[1] / ".env.example").read_text()
        for var in ("HF_TOKEN", "HF_MODEL_ID", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GENESIS_LLM_PROVIDER",
                    "GENESIS_SYSTEM_DRY_RUN", "VOSK_MODEL_PATH", "PICOVOICE_ACCESS_KEY", "LLM_MODEL", "LLM_API_KEY",
                    "LLM_BASE_URL", "LLM_TEMPERATURE", "LLM_MAX_TOKENS", "LLM_TIMEOUT", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
            self.assertIn(var + "=", example)
        self.assertNotRegex(example, r"hf_[A-Za-z0-9]{20,}")


class TestEventBus(unittest.TestCase):
    def test_broken_subscriber_is_isolated(self):
        bus = EventBus()
        got = []
        bus.subscribe(lambda e: 1 / 0, EventType.MESSAGE)
        bus.subscribe(lambda e: got.append(e.payload["x"]), EventType.MESSAGE)
        bus.emit(EventType.MESSAGE, x=1)
        self.assertEqual(got, [1])


if __name__ == "__main__":
    unittest.main()
