"""Structured (JSON-lines) logging with secret redaction.

Log files (in ``logs/``):
    genesis.log         – application events
    errors.log          – WARNING and above
    tool_execution.log  – every tool invocation (logger ``genesis.tools``)
    security.log        – permission / confirmation / validation events (``genesis.security``)
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from datetime import datetime, timezone
from pathlib import Path

from ..security.redaction import RedactingFilter

_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        data = getattr(record, "data", None)
        if data is not None:
            payload["data"] = data
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def _file_handler(path: Path, level: int) -> logging.Handler:
    h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setLevel(level)
    h.setFormatter(JsonFormatter())
    h.addFilter(RedactingFilter())
    return h


def setup_logging(logs_dir: Path, level: str = "INFO", console: bool = True) -> None:
    global _CONFIGURED
    logs_dir.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger("genesis")
    if _CONFIGURED:
        return
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    root.propagate = False

    root.addHandler(_file_handler(logs_dir / "genesis.log", logging.DEBUG))
    root.addHandler(_file_handler(logs_dir / "errors.log", logging.WARNING))

    tools = logging.getLogger("genesis.tools")
    tools.addHandler(_file_handler(logs_dir / "tool_execution.log", logging.INFO))

    security = logging.getLogger("genesis.security")
    security.addHandler(_file_handler(logs_dir / "security.log", logging.INFO))

    if console and sys.stderr is not None:  # pythonw.exe has no console
        ch = logging.StreamHandler()
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%H:%M:%S"))
        ch.addFilter(RedactingFilter())
        root.addHandler(ch)
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("genesis"):
        name = f"genesis.{name}"
    return logging.getLogger(name)
