"""Typed data-access helpers on top of :class:`Database`."""
from __future__ import annotations

import uuid
from typing import Any, Optional

from .db import Database, dumps, loads, utcnow


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def set(self, key: str, value: Any) -> None:
        self.db.execute(
            "INSERT INTO settings(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, dumps(value), utcnow()),
        )

    def all(self) -> dict[str, Any]:
        return {r["key"]: loads(r["value"]) for r in self.db.query("SELECT key,value FROM settings")}


class ConversationRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, title: str = "New conversation") -> str:
        cid = uuid.uuid4().hex
        now = utcnow()
        self.db.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", (cid, title, now, now))
        return cid

    def exists(self, cid: str) -> bool:
        return self.db.query_one("SELECT 1 FROM conversations WHERE id=?", (cid,)) is not None

    def rename(self, cid: str, title: str) -> None:
        self.db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?", (title[:80], utcnow(), cid))

    def add_message(self, cid: str, role: str, content: str, metadata: Optional[dict] = None) -> int:
        cur = self.db.execute(
            "INSERT INTO messages(conversation_id,role,content,metadata,created_at) VALUES(?,?,?,?,?)",
            (cid, role, content, dumps(metadata or {}), utcnow()),
        )
        self.db.execute("UPDATE conversations SET updated_at=? WHERE id=?", (utcnow(), cid))
        return int(cur.lastrowid)

    def messages(self, cid: str, limit: int = 200) -> list[dict]:
        rows = self.db.query(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
            (cid, limit),
        )
        for r in rows:
            r["metadata"] = loads(r["metadata"], {})
        return rows

    def list(self, limit: int = 100) -> list[dict]:
        return self.db.query(
            "SELECT c.*, (SELECT COUNT(*) FROM messages m WHERE m.conversation_id=c.id) AS message_count "
            "FROM conversations c ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        )

    def delete(self, cid: str) -> None:
        self.db.execute("DELETE FROM conversations WHERE id=?", (cid,))

    def clear_messages(self, cid: str) -> None:
        self.db.execute("DELETE FROM messages WHERE conversation_id=?", (cid,))

    def delete_all(self) -> None:
        self.db.execute("DELETE FROM conversations")


class MemoryRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, content: str, kind: str = "fact", source: str = "user") -> int:
        cur = self.db.execute(
            "INSERT INTO memory(kind,content,source,created_at) VALUES(?,?,?,?)", (kind, content, source, utcnow())
        )
        return int(cur.lastrowid)

    def list(self, kind: Optional[str] = None) -> list[dict]:
        if kind:
            return self.db.query("SELECT * FROM memory WHERE kind=? ORDER BY id DESC", (kind,))
        return self.db.query("SELECT * FROM memory ORDER BY id DESC")

    def delete(self, mid: int) -> bool:
        return self.db.execute("DELETE FROM memory WHERE id=?", (mid,)).rowcount > 0

    def delete_matching(self, text: str) -> int:
        return self.db.execute("DELETE FROM memory WHERE lower(content) LIKE ?", (f"%{text.lower()}%",)).rowcount

    def clear(self) -> None:
        self.db.execute("DELETE FROM memory")


class PreferenceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def set(self, key: str, value: Any, source: str = "user") -> None:
        self.db.execute(
            "INSERT INTO preferences(key,value,source,updated_at) VALUES(?,?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, source=excluded.source, updated_at=excluded.updated_at",
            (key, dumps(value), source, utcnow()),
        )

    def get(self, key: str, default: Any = None) -> Any:
        row = self.db.query_one("SELECT value FROM preferences WHERE key=?", (key,))
        return loads(row["value"], default) if row else default

    def all(self) -> dict[str, Any]:
        return {r["key"]: loads(r["value"]) for r in self.db.query("SELECT key,value FROM preferences ORDER BY key")}

    def delete(self, key: str) -> bool:
        return self.db.execute("DELETE FROM preferences WHERE key=?", (key,)).rowcount > 0

    def clear(self) -> None:
        self.db.execute("DELETE FROM preferences")


class ContactRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def upsert(self, name: str, email: Optional[str] = None, alias: Optional[str] = None, notes: str = "") -> int:
        existing = self.db.query_one("SELECT * FROM contacts WHERE lower(name)=lower(?)", (name,))
        if existing:
            self.db.execute(
                "UPDATE contacts SET email=COALESCE(?,email), alias=COALESCE(?,alias), notes=? WHERE id=?",
                (email, alias.lower() if alias else None, notes or existing.get("notes") or "", existing["id"]),
            )
            return int(existing["id"])
        cur = self.db.execute(
            "INSERT INTO contacts(name,alias,email,notes,created_at) VALUES(?,?,?,?,?)",
            (name, alias.lower() if alias else None, email, notes, utcnow()),
        )
        return int(cur.lastrowid)

    def find(self, text: str) -> Optional[dict]:
        """Find by exact name, alias ('professor', 'my boss'), first name or email."""
        t = (text or "").strip().lower()
        if not t:
            return None
        t_noposs = t[3:] if t.startswith("my ") else t
        for sql, arg in (
            ("SELECT * FROM contacts WHERE lower(name)=?", t),
            ("SELECT * FROM contacts WHERE lower(email)=?", t),
            ("SELECT * FROM contacts WHERE alias=?", t_noposs),
            ("SELECT * FROM contacts WHERE lower(name)=?", t_noposs),
        ):
            row = self.db.query_one(sql, (arg,))
            if row:
                return row
        # first / last name match ("rahul" -> "Rahul Sharma", "rao" -> "Professor Rao")
        for row in self.db.query("SELECT * FROM contacts"):
            parts = [p.strip(".").lower() for p in row["name"].split()]
            if t_noposs in parts:
                return row
        return None

    def list(self) -> list[dict]:
        return self.db.query("SELECT * FROM contacts ORDER BY name")

    def delete(self, cid: int) -> bool:
        return self.db.execute("DELETE FROM contacts WHERE id=?", (cid,)).rowcount > 0

    def clear(self) -> None:
        self.db.execute("DELETE FROM contacts")


class TaskRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create(self, task_id: str, conversation_id: Optional[str], request: str, plan: dict) -> None:
        now = utcnow()
        self.db.execute(
            "INSERT INTO tasks(id,conversation_id,request,status,plan,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (task_id, conversation_id, request, "created", dumps(plan), now, now),
        )

    def update(self, task_id: str, status: str, plan: Optional[dict] = None, result: Optional[dict] = None) -> None:
        sets, params = ["status=?", "updated_at=?"], [status, utcnow()]
        if plan is not None:
            sets.append("plan=?")
            params.append(dumps(plan))
        if result is not None:
            sets.append("result=?")
            params.append(dumps(result))
        params.append(task_id)
        self.db.execute(f"UPDATE tasks SET {', '.join(sets)} WHERE id=?", params)

    def get(self, task_id: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row:
            row["plan"] = loads(row["plan"], {})
            row["result"] = loads(row["result"], {})
        return row

    def recent(self, limit: int = 50) -> list[dict]:
        return self.db.query("SELECT id,request,status,created_at,updated_at FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))


class ToolExecutionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, *, task_id: Optional[str], tool: str, args: dict, risk: str, status: str, confirmed: bool,
               message: str = "", error: Optional[str] = None, duration_ms: int = 0) -> int:
        from ..security.redaction import redact

        cur = self.db.execute(
            "INSERT INTO tool_executions(task_id,tool,args,risk,status,confirmed,message,error,duration_ms,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, tool, dumps(redact(args)), risk, status, int(confirmed), message[:1000], (error or "")[:1000] or None,
             duration_ms, utcnow()),
        )
        return int(cur.lastrowid)

    def recent(self, limit: int = 100) -> list[dict]:
        rows = self.db.query("SELECT * FROM tool_executions ORDER BY id DESC LIMIT ?", (limit,))
        for r in rows:
            r["args"] = loads(r["args"], {})
        return rows


class PermissionRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, tool: str) -> Optional[str]:
        row = self.db.query_one("SELECT policy FROM permissions WHERE tool=?", (tool,))
        return row["policy"] if row else None

    def set(self, tool: str, policy: str) -> None:
        self.db.execute(
            "INSERT INTO permissions(tool,policy,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(tool) DO UPDATE SET policy=excluded.policy, updated_at=excluded.updated_at",
            (tool, policy, utcnow()),
        )

    def clear(self, tool: str) -> None:
        self.db.execute("DELETE FROM permissions WHERE tool=?", (tool,))

    def all(self) -> dict[str, str]:
        return {r["tool"]: r["policy"] for r in self.db.query("SELECT * FROM permissions")}


class ServiceRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def set(self, service: str, status: str, account: Optional[str] = None, scopes: Optional[list] = None,
            metadata: Optional[dict] = None) -> None:
        self.db.execute(
            "INSERT INTO connected_services(service,status,account,scopes,metadata,updated_at) VALUES(?,?,?,?,?,?) "
            "ON CONFLICT(service) DO UPDATE SET status=excluded.status, account=excluded.account, "
            "scopes=excluded.scopes, metadata=excluded.metadata, updated_at=excluded.updated_at",
            (service, status, account, dumps(scopes or []), dumps(metadata or {}), utcnow()),
        )

    def get(self, service: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM connected_services WHERE service=?", (service,))
        if row:
            row["scopes"] = loads(row["scopes"], [])
            row["metadata"] = loads(row["metadata"], {})
        return row

    def all(self) -> list[dict]:
        return self.db.query("SELECT * FROM connected_services ORDER BY service")


class ReminderRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, text: str, due_at_iso: str) -> int:
        cur = self.db.execute(
            "INSERT INTO reminders(text,due_at,status,created_at) VALUES(?,?,'pending',?)", (text, due_at_iso, utcnow())
        )
        return int(cur.lastrowid)

    def get(self, rid: int) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM reminders WHERE id=?", (rid,))

    def due(self, now_iso: str) -> list[dict]:
        return self.db.query("SELECT * FROM reminders WHERE status='pending' AND due_at<=? ORDER BY due_at", (now_iso,))

    def pending(self) -> list[dict]:
        return self.db.query("SELECT * FROM reminders WHERE status='pending' ORDER BY due_at")

    def mark(self, rid: int, status: str) -> None:
        self.db.execute("UPDATE reminders SET status=?, fired_at=? WHERE id=?", (status, utcnow(), rid))


class ProjectRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def set(self, name: str, path: str) -> None:
        self.db.execute(
            "INSERT INTO projects(name,path,created_at) VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET path=excluded.path",
            (name.lower(), path, utcnow()),
        )

    def get(self, name: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM projects WHERE name=?", (name.lower(),))

    def list(self) -> list[dict]:
        return self.db.query("SELECT * FROM projects ORDER BY name")

    def delete(self, name: str) -> None:
        self.db.execute("DELETE FROM projects WHERE name=?", (name.lower(),))


class SecurityEventRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, event: str, detail: dict) -> None:
        from ..security.redaction import redact

        self.db.execute("INSERT INTO security_events(event,detail,created_at) VALUES(?,?,?)", (event, dumps(redact(detail)), utcnow()))

    def recent(self, limit: int = 100) -> list[dict]:
        return self.db.query("SELECT * FROM security_events ORDER BY id DESC LIMIT ?", (limit,))


class Repositories:
    """Convenience bundle."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.settings = SettingsRepo(db)
        self.conversations = ConversationRepo(db)
        self.memory = MemoryRepo(db)
        self.preferences = PreferenceRepo(db)
        self.contacts = ContactRepo(db)
        self.tasks = TaskRepo(db)
        self.tool_executions = ToolExecutionRepo(db)
        self.permissions = PermissionRepo(db)
        self.services = ServiceRepo(db)
        self.reminders = ReminderRepo(db)
        self.projects = ProjectRepo(db)
        self.security_events = SecurityEventRepo(db)
