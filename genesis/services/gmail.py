"""Gmail REST API client (drafts, send, search, read).

Uses https://gmail.googleapis.com/gmail/v1 directly with OAuth bearer tokens
from :class:`GoogleAuth`. Message bodies read from the inbox are *untrusted*
content and are wrapped accordingly by the tools.
"""
from __future__ import annotations

import base64
import mimetypes
import urllib.parse
from email.message import EmailMessage
from pathlib import Path
from typing import Optional

import requests

from ..core.logging_setup import get_logger
from .google_auth import GoogleAuth, GoogleAuthError

log = get_logger("gmail")
API = "https://gmail.googleapis.com/gmail/v1/users/me"
MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024


class GmailError(RuntimeError):
    def __init__(self, message: str, *, transient: bool = False) -> None:
        super().__init__(message)
        self.transient = transient  # network problem / rate limit / 5xx — safe to retry a read


def build_mime(to: list[str], subject: str, body: str, *, cc: Optional[list[str]] = None, sender: Optional[str] = None,
               attachments: Optional[list[Path]] = None, in_reply_to: Optional[str] = None) -> EmailMessage:
    msg = EmailMessage()
    if in_reply_to:  # threading headers so the reply lands in the same conversation
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    if to:
        msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    if sender:
        msg["From"] = sender
    msg["Subject"] = subject
    msg.set_content(body)
    total = 0
    for path in attachments or []:
        data = Path(path).read_bytes()
        total += len(data)
        if total > MAX_ATTACHMENT_BYTES:
            raise GmailError("Attachments exceed Gmail's 25 MB limit.")
        ctype, _ = mimetypes.guess_type(str(path))
        maintype, subtype = (ctype or "application/octet-stream").split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=Path(path).name)
    return msg


def compose_url(to: list[str], subject: str, body: str) -> str:
    """Gmail web compose URL (fallback when the API isn't connected)."""
    q = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": ",".join(to), "su": subject, "body": body},
                               quote_via=urllib.parse.quote)
    return f"https://mail.google.com/mail/u/0/?{q}"


def draft_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"


def message_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#inbox/{message_id}"


def _decode_body(payload: dict) -> str:
    """Extract the text/plain body from a Gmail 'full' payload (falls back to stripped HTML)."""
    def walk(part):
        yield part
        for p in part.get("parts") or []:
            yield from walk(p)

    plain, html = None, None
    for part in walk(payload or {}):
        data = (part.get("body") or {}).get("data")
        if not data:
            continue
        text = base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", errors="replace")
        if part.get("mimeType") == "text/plain" and plain is None:
            plain = text
        elif part.get("mimeType") == "text/html" and html is None:
            html = text
    if plain:
        return plain
    if html:
        from .web import html_to_text

        return html_to_text(html)[1]
    return ""


class GmailService:
    def __init__(self, auth: GoogleAuth, session: Optional[requests.Session] = None) -> None:
        self.auth = auth
        self.session = session or auth.session

    @property
    def connected(self) -> bool:
        return self.auth.connected

    def _request(self, method: str, path: str, **kw) -> dict:
        for attempt in (1, 2):
            token = self.auth.access_token()  # raises GoogleAuthError when not connected / expired
            headers = kw.pop("headers", {})
            headers["Authorization"] = f"Bearer {token}"
            try:
                resp = self.session.request(method, f"{API}{path}", headers=headers, timeout=30, **kw)
            except requests.RequestException as exc:
                raise GmailError(f"Could not reach Gmail ({type(exc).__name__}). Check your internet connection.",
                                 transient=True) from exc
            if resp.status_code == 401 and attempt == 1:
                self.auth.invalidate_access_token()
                continue
            if resp.status_code == 401:
                raise GoogleAuthError("Gmail rejected the saved authorization. Please reconnect your Google account.")
            if resp.status_code == 403:
                raise GmailError("Gmail denied the request (missing permission scope?). Reconnect your Google account.")
            if resp.status_code >= 400:
                try:
                    detail = resp.json().get("error", {}).get("message", "")
                except ValueError:
                    detail = resp.text[:200]
                raise GmailError(f"Gmail API error (HTTP {resp.status_code}): {detail}",
                                 transient=resp.status_code == 429 or resp.status_code >= 500)
            return resp.json() if resp.content else {}
        raise GmailError("Gmail request failed.")

    # ------------------------------------------------------------ drafts
    def create_draft(self, to: list[str], subject: str, body: str, *, cc: Optional[list[str]] = None,
                     attachments: Optional[list[Path]] = None, thread_id: Optional[str] = None,
                     in_reply_to: Optional[str] = None) -> dict:
        mime = build_mime(to, subject, body, cc=cc, attachments=attachments, in_reply_to=in_reply_to)
        raw = base64.urlsafe_b64encode(mime.as_bytes()).decode()
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        data = self._request("POST", "/drafts", json={"message": message})
        draft_id = data.get("id")
        message_id = (data.get("message") or {}).get("id")
        if not draft_id:
            raise GmailError("Gmail did not return a draft id.")
        log.info("created Gmail draft", extra={"data": {"draft_id": draft_id}})
        return {"draft_id": draft_id, "message_id": message_id, "url": draft_url(message_id) if message_id else
                "https://mail.google.com/mail/u/0/#drafts"}

    def update_draft(self, draft_id: str, to: list[str], subject: str, body: str, *, thread_id: Optional[str] = None,
                     in_reply_to: Optional[str] = None) -> dict:
        mime = build_mime(to, subject, body, in_reply_to=in_reply_to)
        message = {"raw": base64.urlsafe_b64encode(mime.as_bytes()).decode()}
        if thread_id:
            message["threadId"] = thread_id
        data = self._request("PUT", f"/drafts/{urllib.parse.quote(draft_id)}", json={"id": draft_id, "message": message})
        message_id = (data.get("message") or {}).get("id")
        return {"draft_id": data.get("id") or draft_id, "message_id": message_id,
                "url": draft_url(message_id) if message_id else "https://mail.google.com/mail/u/0/#drafts"}

    def get_message(self, message_id: str) -> dict:
        from email.utils import parseaddr

        msg = self._request("GET", f"/messages/{urllib.parse.quote(message_id)}", params={"format": "full"})
        payload = msg.get("payload", {})
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", [])}
        name, addr = parseaddr(headers.get("from", ""))
        attachments = []

        def walk(part):
            if part.get("filename"):
                attachments.append({"filename": part["filename"][:200], "mime_type": part.get("mimeType", ""),
                                    "size": (part.get("body") or {}).get("size", 0)})
            for p in part.get("parts") or []:
                walk(p)

        walk(payload)
        return {"id": msg.get("id"), "thread_id": msg.get("threadId"), "subject": headers.get("subject", "(no subject)"),
                "attachments": attachments,
                "from": headers.get("from", ""), "from_name": name or addr, "from_email": addr, "date": headers.get("date", ""),
                "message_id_header": headers.get("message-id"), "snippet": msg.get("snippet", ""),
                "body": _decode_body(payload)[:20_000], "url": message_url(msg.get("id", message_id))}

    def get_draft(self, draft_id: str) -> dict:
        data = self._request("GET", f"/drafts/{urllib.parse.quote(draft_id)}", params={"format": "metadata"})
        headers = {h["name"].lower(): h["value"] for h in (data.get("message", {}).get("payload", {}).get("headers") or [])}
        return {"draft_id": data.get("id"), "to": headers.get("to", ""), "subject": headers.get("subject", "")}

    def send_draft(self, draft_id: str) -> dict:
        data = self._request("POST", "/drafts/send", json={"id": draft_id})
        return {"message_id": data.get("id"), "thread_id": data.get("threadId"), "labels": data.get("labelIds", [])}

    # ------------------------------------------------------------ reading
    def search(self, query: str = "", max_results: int = 5) -> list[dict]:
        params = {"maxResults": max(1, min(max_results, 20))}
        if query:
            params["q"] = query
        listing = self._request("GET", "/messages", params=params)
        out = []
        for m in listing.get("messages", [])[:max_results]:
            msg = self._request("GET", f"/messages/{m['id']}", params={"format": "metadata",
                                                                       "metadataHeaders": ["From", "Subject", "Date"]})
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            from email.utils import parseaddr

            name, addr = parseaddr(headers.get("from", ""))
            out.append({
                "id": msg.get("id"), "thread_id": msg.get("threadId"), "from_name": name or addr, "from_email": addr,
                "from": headers.get("from", ""), "subject": headers.get("subject", "(no subject)"),
                "date": headers.get("date", ""), "snippet": msg.get("snippet", ""),
                "unread": "UNREAD" in (msg.get("labelIds") or []),
            })
        return out
