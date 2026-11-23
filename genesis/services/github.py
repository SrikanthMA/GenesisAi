"""GitHub: open pages in the browser, and — with a personal access token — list, create and delete repositories.

Token: ``GITHUB_TOKEN`` in ``.env`` (read at call time, so editing .env needs no restart; never logged or shown).
  * list / create: a fine-grained token with "Administration: Read and write" on your repositories (or classic ``repo``)
  * delete:        the same fine-grained permission (or classic ``delete_repo``)

Deleting a repository is irreversible, so the tool that does it demands the repository's name typed/spoken back AND a
yes/no confirmation, then verifies that GitHub no longer has it.
"""
from __future__ import annotations

import os
import re
from typing import Any, Optional

import requests

from ..core.logging_setup import get_logger

log = get_logger("github")
API = "https://api.github.com"
WEB = "https://github.com"
REPO_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class GitHubError(Exception):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # not_configured | auth | forbidden | not_found | ambiguous | network | invalid | error


class GitHubService:
    def __init__(self, session: Optional[requests.Session] = None, token: Optional[str] = None) -> None:
        self.session = session or requests.Session()
        self._token = token  # tests inject one; otherwise GITHUB_TOKEN is read on every call
        self._login: Optional[str] = None

    # ---------------------------------------------------------------- plumbing
    def token(self) -> Optional[str]:
        t = self._token if self._token is not None else os.getenv("GITHUB_TOKEN", "")
        t = (t or "").strip().strip('"').strip("'")
        return t or None

    def configured(self) -> bool:
        return self.token() is not None

    def _req(self, method: str, path: str, **kw) -> Any:
        tok = self.token()
        if not tok:
            raise GitHubError("I need a GitHub token for that. Add GITHUB_TOKEN=… to your .env file (github.com → Settings → "
                              "Developer settings → Personal access tokens), then ask again — no restart needed.", "not_configured")
        headers = {"Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        try:
            r = self.session.request(method, API + path, headers=headers, timeout=20, **kw)
        except requests.RequestException as exc:
            raise GitHubError("I couldn't reach GitHub — check your internet connection.", "network") from exc
        status = getattr(r, "status_code", 0)
        if status == 401:
            raise GitHubError("GitHub rejected the token (it may be wrong or expired). Create a new one and update GITHUB_TOKEN.",
                              "auth")
        if status == 403:
            raise GitHubError("Your GitHub token isn't allowed to do that. Give it “Administration: Read and write” on your "
                              "repositories (classic tokens: the repo and delete_repo scopes).", "forbidden")
        if status == 404:
            raise GitHubError("GitHub says that repository doesn't exist (or your token can't see it).", "not_found")
        if status == 422:
            try:
                msg = "; ".join(e.get("message", "") for e in r.json().get("errors", [])) or r.json().get("message", "")
            except Exception:
                msg = ""
            raise GitHubError(f"GitHub didn't accept that{': ' + msg if msg else ''}.", "invalid")
        if status >= 400:
            raise GitHubError(f"GitHub returned an error (HTTP {status}).", "error")
        if status == 204 or not getattr(r, "content", b"x"):
            return None
        try:
            return r.json()
        except ValueError:
            return None

    # ---------------------------------------------------------------- read
    def login(self) -> str:
        if self._login is None:
            self._login = str(self._req("GET", "/user")["login"])
        return self._login

    def list_repos(self, limit: int = 30) -> list[dict]:
        data = self._req("GET", "/user/repos", params={"per_page": min(100, max(1, limit)), "sort": "updated",
                                                          "affiliation": "owner"}) or []
        return [{"name": d["name"], "full_name": d["full_name"], "private": bool(d.get("private")),
                 "url": d.get("html_url") or f"{WEB}/{d['full_name']}", "description": d.get("description") or "",
                 "updated": (d.get("pushed_at") or d.get("updated_at") or "")[:10]} for d in data]

    def find_repo(self, name: str) -> dict:
        """Resolve 'genesis', 'Genesis', 'srika/genesis' to exactly one of the user's repositories."""
        want = name.strip().strip("“”\"'").rstrip(".").lower()
        want = re.sub(r"^(?:https?://)?(?:www\.)?github\.com/", "", want)
        repos = self.list_repos(100)
        exact = [r for r in repos if want in (r["name"].lower(), r["full_name"].lower())]
        if len(exact) == 1:
            return exact[0]
        norm = lambda s: re.sub(r"[\s_.-]+", "", s.lower())  # noqa: E731
        close = [r for r in repos if norm(r["name"]) == norm(want)] or [r for r in repos if norm(want) and norm(want) in norm(r["name"])]
        if len(close) == 1:
            return close[0]
        if len(close) > 1:
            raise GitHubError("Several of your repositories match: " + ", ".join(r["name"] for r in close[:6]) + ". Which one?",
                              "ambiguous")
        raise GitHubError(f"I couldn't find a repository called “{name}” in your GitHub account.", "not_found")

    def exists(self, full_name: str) -> bool:
        try:
            self._req("GET", f"/repos/{full_name}")
            return True
        except GitHubError as exc:
            if exc.kind == "not_found":
                return False
            raise

    # ---------------------------------------------------------------- write
    def create_repo(self, name: str, private: bool = True, description: str = "") -> dict:
        name = re.sub(r"\s+", "-", name.strip())
        if not REPO_NAME.match(name):
            raise GitHubError("Repository names can only use letters, numbers, '-', '_' and '.'.", "invalid")
        d = self._req("POST", "/user/repos", json={"name": name, "private": bool(private), "description": description[:300],
                                                    "auto_init": True})
        return {"name": d["name"], "full_name": d["full_name"], "private": bool(d.get("private")),
                "url": d.get("html_url") or f"{WEB}/{d['full_name']}"}

    def delete_repo(self, full_name: str) -> None:
        if "/" not in full_name:
            raise GitHubError("I need the full repository name (owner/name).", "invalid")
        self._req("DELETE", f"/repos/{full_name}")


__all__ = ["GitHubService", "GitHubError", "WEB"]
