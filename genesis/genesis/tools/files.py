"""File-system tools. Every path is validated (see security/validators.py)."""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Optional

from ..core.paths import user_known_folders
from ..security.injection import scan_for_injection
from ..security.permissions import RiskLevel
from ..security.validators import ValidationError, validate_filename, validate_path
from ..services.platform import PlatformError
from .base import Tool, ToolResult, obj, s

SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "env", "appdata", "$recycle.bin", ".cache", ".idea",
             "site-packages", "dist", "build", ".tox", ".mypy_cache", "library", "windows", "program files"}
EXT_GROUPS = {"image": {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".heic"}, "video": {".mp4", ".mkv", ".mov", ".avi", ".webm"}}
MAX_READ_BYTES = 200_000


def known_folder(name: str) -> Optional[Path]:
    return user_known_folders().get((name or "").strip().lower().replace(" folder", "").removeprefix("my "))


def _candidates(name: str, ctx, base_dir: Optional[str]) -> list[Path]:
    """Places a bare/relative name might live, in priority order."""
    bases: list[Path] = []
    if base_dir:
        bases.append(Path(base_dir))
    for p in ctx.repos.projects.list():
        bases.append(Path(p["path"]))
    folders = user_known_folders()
    bases += [folders["desktop"], folders["documents"], folders["downloads"], folders["home"]]
    return [b / name for b in bases]


def resolve_existing(name: str, ctx, base_dir: Optional[str] = None, *, for_write: bool = False) -> Path:
    """Turn user text ('report.pdf', 'app/main.py', 'C:\\x\\y', 'downloads') into a validated existing path."""
    roots = ctx.allowed_roots()
    kf = known_folder(name)
    if kf is not None:
        return validate_path(kf, allowed_roots=roots, must_exist=True, for_write=for_write)
    raw = os.path.expandvars(os.path.expanduser(name.strip().strip('"')))
    if os.path.isabs(raw):
        return validate_path(raw, allowed_roots=roots, must_exist=True, for_write=for_write)
    for cand in _candidates(raw, ctx, base_dir):
        if cand.exists():
            return validate_path(cand, allowed_roots=roots, must_exist=True, for_write=for_write)
    # fall back to a shallow search by file name
    hits = search_files(ctx, Path(raw).name, roots=[Path(base_dir)] if base_dir else None, limit=5, exact=True)
    if hits:
        return validate_path(hits[0], allowed_roots=roots, must_exist=True, for_write=for_write)
    raise ValidationError(f"I couldn't find '{name}'.")


def resolve_folder_target(name: Optional[str], ctx, default: str = "documents", base_dir: Optional[str] = None) -> Path:
    roots = ctx.allowed_roots()
    if not name:
        return validate_path(Path(base_dir) if base_dir else user_known_folders()[default], allowed_roots=roots, for_write=True)
    kf = known_folder(name)
    if kf is not None:
        return validate_path(kf, allowed_roots=roots, for_write=True)
    p = resolve_existing(name, ctx, base_dir, for_write=True)
    if not p.is_dir():
        raise ValidationError(f"'{p}' is not a folder.")
    return p


def search_files(ctx, query: str = "", *, extension: Optional[str] = None, roots: Optional[list[Path]] = None, limit: int = 10,
                 sort: str = "relevance", exact: bool = False, time_budget: float = 6.0, max_depth: int = 7) -> list[Path]:
    folders = user_known_folders()
    roots = roots or [folders["downloads"], folders["desktop"], folders["documents"], folders["pictures"], folders["home"]]
    tokens = [t for t in (query or "").lower().replace("_", " ").replace("-", " ").split() if t]
    exts = EXT_GROUPS.get(extension or "", {extension.lower()} if extension else None)
    seen: set[str] = set()
    hits: list[tuple[float, float, Path]] = []
    deadline = time.time() + time_budget
    for root in roots:
        if not root.exists():
            continue
        stack = [(root, 0)]
        while stack and time.time() < deadline:
            d, depth = stack.pop()
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if depth < max_depth and not e.name.startswith(".") and e.name.lower() not in SKIP_DIRS:
                            stack.append((Path(e.path), depth + 1))
                        continue
                    if e.path in seen:
                        continue
                    name = e.name.lower()
                    if exts and os.path.splitext(name)[1] not in exts:
                        continue
                    if exact:
                        if name != query.lower():
                            continue
                        score = 1.0
                    else:
                        norm = name.replace("_", " ").replace("-", " ")
                        if tokens and not all(t in norm for t in tokens):
                            continue
                        score = sum(len(t) for t in tokens) / max(len(norm), 1) if tokens else 0.0
                    seen.add(e.path)
                    hits.append((score, e.stat().st_mtime, Path(e.path)))
                except OSError:
                    continue
    if sort == "newest":
        hits.sort(key=lambda h: -h[1])
    else:
        hits.sort(key=lambda h: (-h[0], -h[1]))
    return [h[2] for h in hits[:limit]]


# ======================================================================= tools
class OpenFolder(Tool):
    name = "open_folder"
    category = "files"
    description = "Open a folder in File Explorer (known names like downloads/documents/desktop, or a path)."
    parameters = obj({"folder": s("Folder name or path", maxLength=500)}, ["folder"])

    def run(self, args, ctx):
        try:
            path = resolve_existing(args["folder"], ctx)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if not path.is_dir():
            return ToolResult.fail(f"'{path.name}' is a file, not a folder.")
        try:
            ctx.backend.open_path(str(path))
        except PlatformError as exc:
            return ToolResult.fail(f"I couldn't open the folder: {exc}")
        return ToolResult.ok(f"Opened your {path.name} folder.", path=str(path), is_dir=True)

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_dir() if result.success else False


class SearchFiles(Tool):
    name = "search_files"
    category = "files"
    description = "Search the user's folders for files by name keywords and/or extension; can sort newest first."
    parameters = obj({
        "query": s("Keywords in the file name", maxLength=200),
        "extension": s("Extension like .pdf, or 'image'/'video'", maxLength=10),
        "folder": s("Limit to a folder (downloads, documents, a path)", maxLength=500),
        "sort": {"type": "string", "enum": ["relevance", "newest"], "default": "relevance"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
    })

    def run(self, args, ctx):
        roots = None
        if args.get("folder"):
            try:
                roots = [resolve_existing(args["folder"], ctx)]
            except ValidationError as exc:
                return ToolResult.fail(str(exc))
        hits = search_files(ctx, args.get("query") or "", extension=args.get("extension"), roots=roots,
                            limit=args.get("limit", 10), sort=args.get("sort", "relevance"))
        allowed = []
        for h in hits:
            try:
                allowed.append(validate_path(h, allowed_roots=ctx.allowed_roots()))
            except ValidationError:
                continue
        if not allowed:
            what = " ".join(x for x in (args.get("query"), args.get("extension")) if x) or "matching"
            return ToolResult.fail(f"I couldn't find any {what} files.")
        first = allowed[0]
        label = "the most recent match" if args.get("sort") == "newest" else "the best match"
        return ToolResult.ok(f"Found {len(allowed)} file(s); {label} is {first.name}.", path=str(first),
                             results=[str(p) for p in allowed])


class OpenFile(Tool):
    name = "open_file"
    category = "files"
    description = "Open a file with its default application."
    parameters = obj({"path": s("File name or path", maxLength=1000), "base_dir": s("Folder to resolve relative names in")}, ["path"])

    _BLOCKED_EXTS = {".exe", ".bat", ".cmd", ".ps1", ".vbs", ".js", ".msi", ".scr", ".com", ".lnk", ".reg", ".hta", ".jar", ".wsf"}

    def risk_for(self, args, ctx=None):
        return RiskLevel.HIGH if Path(str(args.get("path", ""))).suffix.lower() in self._BLOCKED_EXTS else RiskLevel.LOW

    def confirmation_prompt(self, args):
        return f"'{Path(args['path']).name}' is an executable/script. Opening it will RUN it. Are you sure?"

    def run(self, args, ctx):
        try:
            path = resolve_existing(args["path"], ctx, args.get("base_dir"))
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        try:
            ctx.backend.open_path(str(path))
        except PlatformError as exc:
            return ToolResult.fail(f"I couldn't open {path.name}: {exc}")
        return ToolResult.ok(f"Opened {path.name}.", path=str(path), is_dir=path.is_dir())


class ReadFile(Tool):
    name = "read_file"
    category = "files"
    description = "Read a text/PDF/DOCX file the user asked about (content is treated as untrusted data)."
    parameters = obj({"path": s("File name or path", maxLength=1000), "base_dir": s("Folder to resolve relative names in")}, ["path"])

    def run(self, args, ctx):
        from ..memory.knowledge import KnowledgeError, read_document

        try:
            path = resolve_existing(args["path"], ctx, args.get("base_dir"))
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if path.is_dir():
            return ToolResult.fail(f"{path.name} is a folder.")
        try:
            text = read_document(path)
        except (KnowledgeError, OSError, UnicodeError) as exc:
            try:
                text = path.read_bytes()[:MAX_READ_BYTES].decode("utf-8")
            except Exception:
                return ToolResult.fail(f"I can't read {path.name}: {exc}")
        truncated = len(text) > MAX_READ_BYTES
        text = text[:MAX_READ_BYTES]
        scan = scan_for_injection(text)
        res = ToolResult.ok(f"Read {path.name} ({len(text):,} characters{', truncated' if truncated else ''}).",
                            path=str(path), content=text, suspicious=scan.suspicious)
        res.untrusted_content = True
        return res


class CreateFile(Tool):
    name = "create_file"
    category = "files"
    description = "Create a new file (never overwrites). Location defaults to the active project or Documents."
    parameters = obj({
        "name": s("File name, e.g. test.py", maxLength=200),
        "location": s("Folder (desktop, documents, a path)", maxLength=1000),
        "content": s("Initial content", maxLength=200_000),
        "base_dir": s("Active project folder"),
    }, ["name"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Create the file '{args.get('name')}' in {args.get('location') or args.get('base_dir') or 'Documents'}?"

    def run(self, args, ctx):
        try:
            name = validate_filename(args["name"])
            folder = resolve_folder_target(args.get("location"), ctx, base_dir=args.get("base_dir"))
            target = validate_path(folder / name, allowed_roots=ctx.allowed_roots(), for_write=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if target.exists():
            return ToolResult.fail(f"{name} already exists in {folder.name}; I won't overwrite it.", path=str(target))
        try:
            with open(target, "x", encoding="utf-8", newline="") as f:
                f.write(args.get("content") or "")
        except OSError as exc:
            return ToolResult.fail(f"I couldn't create {name}: {exc.strerror or exc}")
        return ToolResult.ok(f"Created {name} in {folder.name}.", path=str(target), folder_path=str(folder))

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_file() if result.success else False


class CreateFolder(Tool):
    name = "create_folder"
    category = "files"
    description = "Create a new folder (location defaults to Documents)."
    parameters = obj({"name": s("Folder name", maxLength=200), "location": s("Parent folder", maxLength=1000),
                      "base_dir": s("Active project folder")}, ["name"])
    risk = RiskLevel.LOW

    def run(self, args, ctx):
        try:
            name = validate_filename(args["name"])
            parent = resolve_folder_target(args.get("location"), ctx, base_dir=args.get("base_dir"))
            target = validate_path(parent / name, allowed_roots=ctx.allowed_roots(), for_write=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if target.exists():
            return ToolResult.fail(f"A folder named {name} already exists in {parent.name}.", path=str(target), is_dir=True)
        try:
            target.mkdir(parents=False)
        except OSError as exc:
            return ToolResult.fail(f"I couldn't create the folder: {exc.strerror or exc}")
        return ToolResult.ok(f"Created the folder {name} in {parent.name}.", path=str(target), is_dir=True)

    def verify(self, args, result, ctx):
        return Path(result.data["path"]).is_dir() if result.success else False


class RenamePath(Tool):
    name = "rename_path"
    category = "files"
    description = "Rename a file or folder (keeps it in the same folder; keeps the extension if omitted)."
    parameters = obj({"path": s("Existing file", maxLength=1000), "new_name": s("New name", maxLength=200),
                      "base_dir": s("Folder to resolve relative names in")}, ["path", "new_name"])
    risk = RiskLevel.MEDIUM

    def confirmation_prompt(self, args):
        return f"Rename '{Path(args['path']).name}' to '{args['new_name']}'?"

    def run(self, args, ctx):
        try:
            src = resolve_existing(args["path"], ctx, args.get("base_dir"), for_write=True)
            new_name = validate_filename(args["new_name"])
            if src.is_file() and not Path(new_name).suffix and src.suffix:
                new_name += src.suffix
            dst = validate_path(src.parent / new_name, allowed_roots=ctx.allowed_roots(), for_write=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if dst.exists():
            return ToolResult.fail(f"{new_name} already exists there.")
        try:
            src.rename(dst)
        except OSError as exc:
            return ToolResult.fail(f"I couldn't rename it: {exc.strerror or exc}")
        return ToolResult.ok(f"Renamed {src.name} to {dst.name}.", path=str(dst), old_path=str(src), is_dir=dst.is_dir())

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        return Path(result.data["path"]).exists() and not Path(result.data["old_path"]).exists()


class _Transfer(Tool):
    category = "files"
    risk = RiskLevel.MEDIUM
    verb = "move"
    parameters = obj({"path": s("File or folder", maxLength=1000), "destination": s("Destination folder", maxLength=1000),
                      "base_dir": s("Folder to resolve relative names in")}, ["path", "destination"])

    def confirmation_prompt(self, args):
        return f"{self.verb.capitalize()} '{Path(args['path']).name}' to {args['destination']}?"

    def run(self, args, ctx):
        try:
            src = resolve_existing(args["path"], ctx, args.get("base_dir"), for_write=self.verb == "move")
            dest_dir = resolve_folder_target(args["destination"], ctx)
            dst = validate_path(dest_dir / src.name, allowed_roots=ctx.allowed_roots(), for_write=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if dst.exists():
            return ToolResult.fail(f"{src.name} already exists in {dest_dir.name}.")
        try:
            if self.verb == "move":
                shutil.move(str(src), str(dst))
            elif src.is_dir():
                shutil.copytree(src, dst)
            else:
                shutil.copy2(src, dst)
        except OSError as exc:
            return ToolResult.fail(f"I couldn't {self.verb} it: {exc.strerror or exc}")
        past = "Moved" if self.verb == "move" else "Copied"
        return ToolResult.ok(f"{past} {src.name} to {dest_dir.name}.", path=str(dst), old_path=str(src), is_dir=dst.is_dir())

    def verify(self, args, result, ctx):
        if not result.success:
            return False
        ok = Path(result.data["path"]).exists()
        if self.verb == "move":
            ok = ok and not Path(result.data["old_path"]).exists()
        return ok


class MovePath(_Transfer):
    name = "move_path"
    verb = "move"
    description = "Move a file or folder into another folder."


class CopyPath(_Transfer):
    name = "copy_path"
    verb = "copy"
    description = "Copy a file or folder into another folder."


class DeletePath(Tool):
    name = "delete_path"
    category = "files"
    description = "Move a file or folder to the Recycle Bin (recoverable). Always asks for confirmation."
    parameters = obj({"path": s("File or folder", maxLength=1000), "base_dir": s("Folder to resolve relative names in")}, ["path"])
    risk = RiskLevel.MEDIUM
    always_confirm = True

    def risk_for(self, args, ctx=None):
        if ctx is None:
            return self.risk
        try:
            p = resolve_existing(args.get("path") or "", ctx, args.get("base_dir"))
            if p.is_dir():
                count = 0
                for _ in p.rglob("*"):
                    count += 1
                    if count > 20:
                        return RiskLevel.HIGH  # bulk deletion
        except (ValidationError, OSError):
            pass
        return self.risk

    def confirmation_prompt(self, args):
        return f"Move '{Path(args['path']).name}' to the Recycle Bin?"

    def run(self, args, ctx):
        try:
            target = resolve_existing(args["path"], ctx, args.get("base_dir"), for_write=True)
        except ValidationError as exc:
            return ToolResult.fail(str(exc))
        if target in {p for p in user_known_folders().values()}:
            return ToolResult.fail("I won't delete one of your main user folders.")
        try:
            try:
                from send2trash import send2trash  # type: ignore

                send2trash(str(target))
                where = "the Recycle Bin"
            except ImportError:
                trash = ctx.config.paths.data_dir / "trash" / time.strftime("%Y%m%d-%H%M%S")
                trash.mkdir(parents=True, exist_ok=True)
                shutil.move(str(target), str(trash / target.name))
                where = f"Genesis's trash folder ({trash})"
        except OSError as exc:
            return ToolResult.fail(f"I couldn't delete {target.name}: {exc.strerror or exc}")
        return ToolResult.ok(f"Moved {target.name} to {where}.", old_path=str(target))

    def verify(self, args, result, ctx):
        return (not Path(result.data["old_path"]).exists()) if result.success else False


TOOLS = [OpenFolder(), SearchFiles(), OpenFile(), ReadFile(), CreateFile(), CreateFolder(), RenamePath(), MovePath(),
         CopyPath(), DeletePath()]
