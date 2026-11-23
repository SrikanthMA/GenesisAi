"""Google Calendar (events) via REST, sharing Genesis's Google OAuth session."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional

import requests

from .google_auth import GoogleAuth, GoogleAuthError

API = "https://www.googleapis.com/calendar/v3/calendars/primary/events"


class CalendarError(RuntimeError):
    pass


class CalendarService:
    def __init__(self, auth: GoogleAuth) -> None:
        self.auth = auth

    def create_event(self, title: str, start: datetime, duration_minutes: int = 30,
                     description: str = "", timezone_name: Optional[str] = None) -> dict:
        if "https://www.googleapis.com/auth/calendar.events" not in self.auth.granted_scopes():
            raise GoogleAuthError("Calendar permission wasn't granted. Reconnect your Google account to allow it.")
        start_local = start.astimezone() if start.tzinfo is None else start
        end_local = start_local + timedelta(minutes=duration_minutes)
        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_local.isoformat()},
            "end": {"dateTime": end_local.isoformat()},
            "reminders": {"useDefault": True},
        }
        if timezone_name:
            body["start"]["timeZone"] = body["end"]["timeZone"] = timezone_name
        try:
            resp = self.auth.session.post(API, json=body, timeout=30,
                                          headers={"Authorization": f"Bearer {self.auth.access_token()}"})
        except requests.RequestException as exc:
            raise CalendarError(f"Could not reach Google Calendar ({type(exc).__name__}).") from exc
        if resp.status_code >= 400:
            raise CalendarError(f"Google Calendar error (HTTP {resp.status_code}).")
        data = resp.json()
        return {"event_id": data.get("id"), "url": data.get("htmlLink")}
