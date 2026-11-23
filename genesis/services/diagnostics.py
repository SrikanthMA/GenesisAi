"""Self-diagnosis: when something fails, work out *why* before answering.

Each function returns a small dict ``{"cause": <code>, "detail": <user-facing sentence>, "fix": <what to do>}`` — never
stack traces or secrets. Network checks are bounded (a few seconds) and only run after a failure.

    diagnose_app(name, resolver, backend, errors)  → not_installed | missing_executable | permission_denied |
                                                      crashed_on_start | blocked | unknown
    diagnose_url(url)                               → offline | dns_failed | site_down | timeout | http_error |
                                                      site_ok (so the browser was the problem) | invalid_url
    diagnose_network()                              → online | offline | dns_failed
"""
from __future__ import annotations

import re
import socket
import time
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse

import requests

from ..core.logging_setup import get_logger

log = get_logger("diagnostics")
PROBE_HOSTS = [("www.google.com", 443), ("1.1.1.1", 443), ("www.microsoft.com", 443)]


def _resolve(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443)
        return True
    except (socket.gaierror, UnicodeError, OSError):
        return False


def _connect(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def diagnose_network() -> dict:
    for host, port in PROBE_HOSTS:
        if _connect(host, port):
            return {"cause": "online", "detail": "Your internet connection is working.", "fix": ""}
    if not _resolve("www.google.com") and _connect("1.1.1.1", 443):
        return {"cause": "dns_failed", "detail": "You're online, but website names can't be looked up (DNS problem).",
                "fix": "Restarting your router or setting DNS to automatic usually fixes it."}
    return {"cause": "offline", "detail": "This computer isn't connected to the internet right now.",
            "fix": "Check Wi-Fi or the network cable, then try again."}


def diagnose_url(url: str, session: Any = None, timeout: float = 6.0) -> dict:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    host = parsed.hostname or ""
    if not host:
        return {"cause": "invalid_url", "detail": "That doesn't look like a valid web address.", "fix": "Check the address."}
    if not _resolve(host):
        net = diagnose_network()
        if net["cause"] != "online":
            return net
        return {"cause": "dns_failed", "detail": f"The address {host} doesn't exist or can't be found.",
                "fix": "Check the spelling of the website."}
    if not _connect(host, parsed.port or (443 if parsed.scheme == "https" else 80)):
        net = diagnose_network()
        if net["cause"] != "online":
            return net
        return {"cause": "site_down", "detail": f"{host} isn't answering — the website seems to be down.",
                "fix": "Try again in a few minutes."}
    sess = session or requests
    t0 = time.monotonic()
    try:
        r = sess.get(parsed.geturl(), timeout=timeout, allow_redirects=True, headers={"User-Agent": "Mozilla/5.0 Genesis"})
    except requests.Timeout:
        return {"cause": "timeout", "detail": f"{host} is taking too long to respond.", "fix": "Try again in a moment."}
    except requests.RequestException:
        return {"cause": "site_down", "detail": f"{host} refused the connection.", "fix": "Try again later."}
    status = getattr(r, "status_code", 0)
    if status >= 500:
        return {"cause": "site_down", "detail": f"{host} is having problems on its side (error {status}).", "fix": "Try again later."}
    if status in (401, 403):
        return {"cause": "http_error", "detail": f"{host} wants you to sign in or blocked automated access (error {status}).",
                "fix": "Open it yourself and sign in — I don't bypass logins or CAPTCHAs."}
    if status == 404:
        return {"cause": "http_error", "detail": "That page doesn't exist on the site (error 404).", "fix": "Check the link."}
    if status >= 400:
        return {"cause": "http_error", "detail": f"{host} returned error {status}.", "fix": ""}
    return {"cause": "site_ok", "detail": f"{host} is up (answered in {int((time.monotonic() - t0) * 1000)} ms), so the problem was "
                                           "on the browser side.", "fix": "I'll try opening it another way."}


def diagnose_app(name: str, resolver: Any = None, backend: Any = None, errors: Optional[list[str]] = None,
                 target: Optional[str] = None) -> dict:
    errs = " ".join(errors or []).lower()
    if resolver is not None and target is None:
        try:
            cands = resolver.resolve_all(name) if hasattr(resolver, "resolve_all") else [r for r in [resolver.resolve(name)] if r]
        except Exception:
            cands = []
        if not cands:
            return {"cause": "not_installed", "detail": f"{name} doesn't seem to be installed — I couldn't find it in the Start "
                                                        "menu, the usual install folders or the registry.",
                    "fix": f"Install {name}, or tell me where it is (“{name} is in C:\\…”)."}
        target = next((c.target for c in cands if getattr(c, "kind", "") == "exe"), None)
    if re.search(r"access is denied|winerror 5\b|permission|elevation|requires elevation|740", errs):
        return {"cause": "permission_denied", "detail": f"Windows blocked {name} from starting (it needs permissions I don't have "
                                                        "— possibly administrator rights).",
                "fix": f"Start {name} yourself once (right-click → Run as administrator if it needs it)."}
    if target and re.search(r"[\\/]", target) and not Path(target).exists():
        return {"cause": "missing_executable", "detail": f"{name}'s program file is missing ({Path(target).name}) — it may have been "
                                                         "uninstalled or moved.", "fix": f"Reinstall {name}."}
    if re.search(r"exited immediately|crash|terminated", errs):
        return {"cause": "crashed_on_start", "detail": f"{name} started but closed straight away (it crashed on start-up).",
                "fix": f"Try opening {name} from the Start menu — if Windows shows an error there, reinstalling usually fixes it."}
    if re.search(r"blocked|smartscreen|policy|antivirus|defender", errs):
        return {"cause": "blocked", "detail": f"Something (security software or a policy) blocked {name}.",
                "fix": "Check Windows Security's protection history."}
    return {"cause": "unknown", "detail": f"{name} didn't start and Windows didn't say why.",
            "fix": f"Try starting {name} from the Start menu once."}


def explain(diag: dict) -> str:
    """One natural sentence for the user."""
    return (diag.get("detail", "") + (" " + diag["fix"] if diag.get("fix") else "")).strip()
