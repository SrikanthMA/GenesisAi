"""Google Calendar (events) via REST, sharing Genesis's Google OAuth session."""
from __future__ import annotations

from datetime import datetime, timedelta
import urllib.parse
from typing import Optional

import requests

from .google_auth import GoogleAuth, GoogleAuthError

API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarError(RuntimeError):
    pass


class CalendarService:
    SCOPE = "https://www.googleapis.com/auth/calendar.events"

    def __init__(self, auth: GoogleAuth) -> None:
        self.auth = auth

    def _check_scope(self) -> None:
        if self.SCOPE not in self.auth.granted_scopes():
            raise GoogleAuthError("Calendar permission wasn't granted. Reconnect your Google account to allow it.")

    def _request(self, method: str, url: str, **kw) -> dict:
        self._check_scope()
        for attempt in (1, 2):
            try:
                resp = self.auth.session.request(method, url, timeout=30,
                                                 headers={"Authorization": f"Bearer {self.auth.access_token()}"}, **kw)
            except requests.RequestException as exc:
                raise CalendarError(f"Could not reach Google Calendar ({type(exc).__name__}).") from exc
            if resp.status_code == 401 and attempt == 1:
                self.auth.invalidate_access_token()
                continue
            if resp.status_code == 404:
                raise CalendarError("That event doesn't exist any more.")
            if resp.status_code >= 400:
                raise CalendarError(f"Google Calendar error (HTTP {resp.status_code}).")
            return resp.json() if getattr(resp, "content", b"") else {}
        raise CalendarError("Google Calendar rejected the saved authorization. Please reconnect your Google account.")

    @staticmethod
    def _iso(dt: datetime) -> str:
        return (dt.astimezone() if dt.tzinfo is None else dt).isoformat()

    def create_event(self, title: str, start: datetime, duration_minutes: int = 30,
                     description: str = "", timezone_name: Optional[str] = None, attendees: Optional[list[str]] = None) -> dict:
        end = start + timedelta(minutes=duration_minutes)
        body = {"summary": title, "description": description, "start": {"dateTime": self._iso(start)},
                "end": {"dateTime": self._iso(end)}, "reminders": {"useDefault": True}}
        if attendees:
            body["attendees"] = [{"email": a} for a in attendees]
        if timezone_name:
            body["start"]["timeZone"] = body["end"]["timeZone"] = timezone_name
        data = self._request("POST", API, json=body)
        return {"event_id": data.get("id"), "url": data.get("htmlLink"), "title": title, "start": start.isoformat(),
                "end": end.isoformat()}

    def list_events(self, start: datetime, end: datetime, query: str = "", max_results: int = 25) -> list[dict]:
        params = {"timeMin": self._iso(start), "timeMax": self._iso(end), "singleEvents": "true", "orderBy": "startTime",
                  "maxResults": max(1, min(max_results, 100))}
        if query:
            params["q"] = query
        data = self._request("GET", API, params=params)
        return [_event(e) for e in data.get("items", []) if e.get("status") != "cancelled"]

    def get_event(self, event_id: str) -> dict:
        return _event(self._request("GET", f"{API}/{urllib.parse.quote(event_id)}"))

    def update_event(self, event_id: str, *, start: Optional[datetime] = None, duration_minutes: Optional[int] = None,
                     title: Optional[str] = None) -> dict:
        current = self.get_event(event_id)
        body: dict = {}
        if title:
            body["summary"] = title
        if start is not None or duration_minutes:
            old_start = datetime.fromisoformat(current["start"]) if current.get("start") else None
            old_end = datetime.fromisoformat(current["end"]) if current.get("end") else None
            new_start = start or old_start
            if new_start is None:
                raise CalendarError("That is an all-day event; I can only move timed events.")
            if duration_minutes:
                length = timedelta(minutes=duration_minutes)
            elif old_start is not None and old_end is not None:
                length = old_end - old_start
            else:
                length = timedelta(minutes=30)
            if new_start.tzinfo is None and old_start is not None and old_start.tzinfo is not None:
                new_start = new_start.replace(tzinfo=old_start.tzinfo)
            body["start"] = {"dateTime": self._iso(new_start)}
            body["end"] = {"dateTime": self._iso(new_start + length)}
        data = self._request("PATCH", f"{API}/{urllib.parse.quote(event_id)}", json=body)
        return _event(data)

    def delete_event(self, event_id: str) -> None:
        self._request("DELETE", f"{API}/{urllib.parse.quote(event_id)}")


def _event(e: dict) -> dict:
    start = (e.get("start") or {}).get("dateTime") or (e.get("start") or {}).get("date")
    end = (e.get("end") or {}).get("dateTime") or (e.get("end") or {}).get("date")
    return {"event_id": e.get("id"), "title": e.get("summary") or "(no title)", "start": start, "end": end,
            "all_day": "dateTime" not in (e.get("start") or {}), "location": e.get("location", ""),
            "url": e.get("htmlLink"), "attendees": [a.get("email") for a in e.get("attendees", []) if a.get("email")]}
