"""Input validation for paths, URLs, e-mail addresses and file names.

Every tool that touches the file system or network validates its arguments
here. The LLM never gets to bypass these checks: they run inside the tool
layer, after planning.
"""
from __future__ import annotations

import ipaddress
import os
import re
import sys
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlparse


class ValidationError(ValueError):
    """Raised when an argument fails a security or format check."""


# ----------------------------------------------------------------- paths
def _protected_roots() -> list[Path]:
    roots: list[str] = []
    if sys.platform.startswith("win"):
        sysroot = os.getenv("SystemRoot", r"C:\Windows")
        roots += [
            sysroot,
            os.getenv("ProgramFiles", r"C:\Program Files"),
            os.getenv("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            os.getenv("ProgramData", r"C:\ProgramData"),
            r"C:\$Recycle.Bin",
            r"C:\System Volume Information",
        ]
    else:
        roots += ["/etc", "/bin", "/sbin", "/usr", "/boot", "/dev", "/proc", "/sys", "/var/lib", "/System", "/Library"]
    return [Path(r).resolve() for r in roots if r]


_SENSITIVE_NAMES = {
    ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker",
    "credentials", "id_rsa", "id_ed25519", ".env", ".git-credentials", ".netrc",
    "login data", "cookies", "ntuser.dat", "sam", "system", "security",
}

_INVALID_WIN_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_RESERVED_WIN = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}


def _is_relative_to(p: Path, root: Path) -> bool:
    try:
        p.relative_to(root)
        return True
    except ValueError:
        return False


def default_allowed_roots(extra: Iterable[str] = ()) -> list[Path]:
    roots = [Path.home().resolve()]
    for r in extra:
        try:
            roots.append(Path(r).expanduser().resolve())
        except OSError:
            continue
    return roots


def validate_path(
    raw: str | os.PathLike,
    *,
    allowed_roots: Optional[list[Path]] = None,
    must_exist: bool = False,
    for_write: bool = False,
    allow_sensitive: bool = False,
) -> Path:
    """Resolve and check a path.

    * no NUL bytes / device paths / UNC network shares
    * resolved (symlinks and ``..`` collapsed) path must be inside an allowed root
    * writes into OS/system directories are always refused
    * credential stores (``.ssh``, ``.env``...) are refused unless explicitly allowed
    """
    if raw is None or str(raw).strip() == "":
        raise ValidationError("A file or folder path is required.")
    text = str(raw).strip().strip('"').strip("'")
    if "\x00" in text:
        raise ValidationError("Path contains a NUL byte.")
    if text.startswith("\\\\") or text.startswith("//"):
        raise ValidationError("Network (UNC) paths are not allowed.")
    if re.match(r"^\\\\\?\\|^\\\\\.\\", text):
        raise ValidationError("Device paths are not allowed.")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    try:
        resolved = path.resolve(strict=False)
    except (OSError, RuntimeError) as exc:
        raise ValidationError(f"Invalid path: {exc}") from exc

    roots = allowed_roots if allowed_roots is not None else default_allowed_roots()
    if not any(_is_relative_to(resolved, r) for r in roots):
        raise ValidationError(f"'{resolved}' is outside the folders Genesis is allowed to access.")

    if for_write:
        for protected in _protected_roots():
            if _is_relative_to(resolved, protected):
                raise ValidationError(f"Writing inside system folder '{protected}' is not allowed.")
    if not allow_sensitive:
        lowered = {part.lower() for part in resolved.parts}
        hit = lowered & _SENSITIVE_NAMES
        if hit:
            raise ValidationError(f"Access to sensitive location '{sorted(hit)[0]}' is blocked.")
    if must_exist and not resolved.exists():
        raise ValidationError(f"'{resolved}' does not exist.")
    return resolved


def validate_filename(name: str) -> str:
    name = (name or "").strip().strip('"').strip("'")
    if not name:
        raise ValidationError("A file name is required.")
    if len(name) > 200:
        raise ValidationError("File name is too long.")
    if "/" in name or "\\" in name or name in {".", ".."}:
        raise ValidationError("File name must not contain path separators.")
    if _INVALID_WIN_CHARS.search(name):
        raise ValidationError("File name contains characters that are invalid on Windows.")
    if name.split(".")[0].lower() in _RESERVED_WIN:
        raise ValidationError("That file name is reserved by Windows.")
    if name.endswith(" ") or name.endswith("."):
        raise ValidationError("File name must not end with a space or a dot.")
    return name


# ------------------------------------------------------------------ URLs
_BLOCKED_SCHEMES = {"javascript", "data", "file", "vbscript", "ftp", "smb", "ms-msdt", "search-ms"}
_DOMAIN_RE = re.compile(r"^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I)


def validate_url(raw: str, *, allow_private: bool = False) -> str:
    """Return a normalized http(s) URL or raise :class:`ValidationError`."""
    text = (raw or "").strip()
    if not text:
        raise ValidationError("A URL is required.")
    if any(ch in text for ch in ("\n", "\r", "\x00", " ")):
        raise ValidationError("URL contains whitespace or control characters.")
    if "://" not in text:
        scheme_guess = text.split(":", 1)[0].lower()
        if ":" in text and scheme_guess in _BLOCKED_SCHEMES:
            raise ValidationError(f"The '{scheme_guess}:' scheme is not allowed.")
        text = "https://" + text
    parsed = urlparse(text)
    scheme = parsed.scheme.lower()
    if scheme in _BLOCKED_SCHEMES or scheme not in {"http", "https"}:
        raise ValidationError(f"The '{scheme}:' scheme is not allowed.")
    if parsed.username or parsed.password:
        raise ValidationError("URLs with embedded credentials are not allowed.")
    host = (parsed.hostname or "").lower()
    if not host:
        raise ValidationError("URL has no host.")
    try:
        ip = ipaddress.ip_address(host)
        if not allow_private and (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved):
            raise ValidationError("URLs pointing at private/loopback addresses are not allowed.")
    except ValueError:
        if host != "localhost" and not _DOMAIN_RE.match(host):
            raise ValidationError(f"'{host}' is not a valid domain name.")
        if host == "localhost" and not allow_private:
            raise ValidationError("URLs pointing at localhost are not allowed.")
    return parsed.geturl()


# ---------------------------------------------------------------- e-mail
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def validate_email(addr: str) -> str:
    addr = (addr or "").strip().strip("<>").strip()
    if not EMAIL_RE.match(addr) or ".." in addr:
        raise ValidationError(f"'{addr}' is not a valid e-mail address.")
    return addr


def validate_text(text: str, *, field: str = "text", max_len: int = 20_000) -> str:
    if text is None:
        raise ValidationError(f"{field} is required.")
    text = str(text)
    if len(text) > max_len:
        raise ValidationError(f"{field} is too long (>{max_len} characters).")
    return text.replace("\x00", "")
