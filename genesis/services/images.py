"""Images: find a real photo (search) or create one (generation), save it locally and hand it to the UI to display.

Search uses open, key-free image APIs with clear licensing — Wikimedia Commons first, then Openverse — so Genesis
doesn't scrape search engines. Generation uses a text-to-image model through Hugging Face Inference Providers with the
same HF token as the chat model (``GENESIS_IMAGE_MODEL``, default FLUX.1-schnell), or an OpenAI-compatible images
endpoint when that is the configured provider.

Files go to ``data/images/`` (a small cache, pruned to the newest 60 files) — nothing is uploaded.
"""
from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

import requests

from ..core.logging_setup import get_logger

log = get_logger("images")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"
OPENVERSE_API = "https://api.openverse.org/v1/images/"
UA = {"User-Agent": "Genesis-Desktop-Assistant/1.4 (personal assistant; https://github.com/)"}
MAX_BYTES = 12 * 1024 * 1024
KEEP = 60


class ImageError(RuntimeError):
    def __init__(self, message: str, kind: str = "error") -> None:
        super().__init__(message)
        self.kind = kind  # not_found | network | not_configured | refused | error


_UNSAFE = re.compile(r"\b(nude|naked|nsfw|porn|sexual|gore|beheading|child\s+(?:nude|sexual))\b", re.I)


class ImageService:
    def __init__(self, config: Any, folder: Path, session: Any = None) -> None:
        self._config = config  # a config object, or a callable returning the current one (picks up .env edits)
        self.folder = Path(folder)
        self.session = session
        self.timeout = 20

    @property
    def config(self) -> Any:
        return self._config() if callable(self._config) else self._config

    # ---------------------------------------------------------------- helpers
    def _http(self):
        return self.session or requests

    def _get(self, url: str, **kw):
        try:
            return self._http().get(url, timeout=self.timeout, headers=UA, **kw)
        except requests.RequestException as exc:
            raise ImageError("I couldn't reach the image service — check your internet connection.", "network") from exc

    def _save(self, data: bytes, stem: str, ext: str) -> Path:
        self.folder.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:40] or "image"
        path = self.folder / f"{safe}-{hashlib.sha1(data).hexdigest()[:8]}.{ext}"
        path.write_bytes(data)
        self._prune()
        return path

    def _prune(self) -> None:
        try:
            files = sorted(self.folder.glob("*.*"), key=lambda p: p.stat().st_mtime, reverse=True)
            for old in files[KEEP:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _ext(content_type: str, url: str) -> str:
        ct = (content_type or "").lower()
        for k, e in (("png", "png"), ("jpeg", "jpg"), ("jpg", "jpg"), ("webp", "webp"), ("gif", "gif")):
            if k in ct or url.lower().split("?")[0].endswith("." + k):
                return e
        return "jpg"

    def _download(self, url: str, stem: str) -> Path:
        if not url.lower().startswith("https://"):
            raise ImageError("I only download pictures over a secure (https) connection.", "error")
        from urllib.parse import urljoin

        r = None
        for _ in range(4):  # follow at most 3 redirects, each one checked *before* it is requested
            r = self._get(url, stream=True, allow_redirects=False)
            loc = (getattr(r, "headers", {}) or {}).get("Location") if getattr(r, "status_code", 0) in (301, 302, 303, 307, 308) else None
            if not loc:
                break
            url = urljoin(url, loc)
            if not url.lower().startswith("https://"):
                raise ImageError("That image redirected to an insecure address, so I didn't download it.", "error")
        else:
            raise ImageError("The image couldn't be downloaded (too many redirects).", "network")
        if getattr(r, "status_code", 0) != 200:
            raise ImageError("The image couldn't be downloaded.", "network")
        headers = getattr(r, "headers", {}) or {}
        try:
            if int(headers.get("Content-Length") or 0) > MAX_BYTES:
                raise ImageError("That image is too large to show.", "error")
        except ValueError:
            pass
        if hasattr(r, "iter_content"):
            chunks, total = [], 0
            for chunk in r.iter_content(65536):
                total += len(chunk)
                if total > MAX_BYTES:
                    r.close()
                    raise ImageError("That image is too large to show.", "error")
                chunks.append(chunk)
            data = b"".join(chunks)
        else:
            data = getattr(r, "content", b"") or b""
        if not data:
            raise ImageError("The image couldn't be downloaded.", "network")
        ctype = headers.get("Content-Type", "")
        if ctype and not ctype.startswith("image/"):
            raise ImageError("The link didn't return an image.", "error")
        return self._save(data, stem, self._ext(ctype, url))

    # ---------------------------------------------------------------- search
    def _commons(self, query: str, n: int) -> list[dict]:
        r = self._get(COMMONS_API, params={"action": "query", "format": "json", "generator": "search", "gsrsearch": f"{query} filetype:bitmap",
                                           "gsrnamespace": 6, "gsrlimit": max(n * 3, 10), "prop": "imageinfo",
                                           "iiprop": "url|mime|extmetadata", "iiurlwidth": 1024})
        if r.status_code != 200:
            return []
        pages = (r.json().get("query") or {}).get("pages") or {}
        out = []
        for p in sorted(pages.values(), key=lambda p: p.get("index", 99)):
            info = (p.get("imageinfo") or [{}])[0]
            if not str(info.get("mime", "")).startswith("image/") or info.get("mime") == "image/svg+xml":
                continue
            meta = info.get("extmetadata") or {}
            out.append({"url": info.get("thumburl") or info.get("url"), "page": info.get("descriptionurl", ""),
                        "title": re.sub(r"^File:|\.\w+$", "", p.get("title", "")),
                        "credit": re.sub(r"<[^>]+>", "", (meta.get("Artist") or {}).get("value", ""))[:80],
                        "license": (meta.get("LicenseShortName") or {}).get("value", ""), "source": "Wikimedia Commons"})
        return out[:n]

    def _openverse(self, query: str, n: int) -> list[dict]:
        r = self._get(OPENVERSE_API, params={"q": query, "page_size": n, "mature": "false"})
        if r.status_code != 200:
            return []
        return [{"url": it.get("thumbnail") or it.get("url"), "page": it.get("foreign_landing_url", ""), "title": it.get("title", ""),
                 "credit": it.get("creator", "") or "", "license": (it.get("license") or "").upper(), "source": "Openverse"}
                for it in (r.json().get("results") or [])[:n]]

    def search(self, query: str, n: int = 1) -> list[dict]:
        """Find and download up to ``n`` images; returns dicts with a local ``path``."""
        q = query.strip()
        if _UNSAFE.search(q):
            raise ImageError("I can't look for that kind of image.", "refused")
        found: list[dict] = []
        for source in (self._commons, self._openverse):
            try:
                found = [c for c in source(q, n) if c.get("url")]
            except ImageError:
                raise
            except Exception as exc:
                log.info("image source failed: %s", exc)
                found = []
            if found:
                break
        if not found:
            raise ImageError(f"I couldn't find a picture of {q}.", "not_found")
        out = []
        for cand in found[:n]:
            try:
                out.append(dict(cand, path=str(self._download(cand["url"], q))))
            except ImageError as exc:
                log.info("download failed: %s", exc)
        if not out:
            raise ImageError(f"I found pictures of {q} but couldn't download them.", "network")
        return out

    # ---------------------------------------------------------------- generation
    def generator(self) -> dict:
        model = os.getenv("GENESIS_IMAGE_MODEL", "").strip() or "black-forest-labs/FLUX.1-schnell"
        token = getattr(self.config, "hf_token", None)
        if token:
            return {"provider": "huggingface", "model": model, "configured": True}
        if getattr(self.config, "llm_provider", "") == "openai_compatible" and getattr(self.config, "openai_api_key", None):
            return {"provider": "openai", "model": os.getenv("GENESIS_IMAGE_MODEL", "").strip() or "gpt-image-1", "configured": True}
        return {"provider": None, "model": model, "configured": False}

    def generate(self, prompt: str) -> dict:
        if _UNSAFE.search(prompt):
            raise ImageError("I can't create that kind of image.", "refused")
        from ..v2 import images as v2images  # v2 hook (2.1): Gemini / Hugging Face / free fallback, in turn

        return v2images.generate(self, prompt, legacy=self._generate_legacy)

    def _generate_legacy(self, prompt: str) -> dict:
        gen = self.generator()
        if not gen["configured"]:
            raise ImageError("Creating images needs an image model: add HF_TOKEN to .env (the same token as the chat model).",
                             "not_configured")
        http = self._http()
        try:
            if gen["provider"] == "huggingface":
                url = f"https://router.huggingface.co/hf-inference/models/{gen['model']}"
                r = http.post(url, json={"inputs": prompt}, timeout=120,
                              headers={"Authorization": f"Bearer {self.config.hf_token}", "Accept": "image/png"})
                if r.status_code == 200 and r.content and (r.headers or {}).get("Content-Type", "image/").startswith("image/"):
                    data = r.content
                else:
                    raise ImageError(_gen_error(r.status_code), "auth" if r.status_code in (401, 403) else "error")
            else:
                base = (getattr(self.config, "openai_base_url", "") or "https://api.openai.com/v1").rstrip("/")
                r = http.post(f"{base}/images/generations", json={"model": gen["model"], "prompt": prompt, "size": "1024x1024"},
                              timeout=120, headers={"Authorization": f"Bearer {self.config.openai_api_key}"})
                if r.status_code != 200:
                    raise ImageError(_gen_error(r.status_code), "error")
                import base64

                item = (r.json().get("data") or [{}])[0]
                data = base64.b64decode(item["b64_json"]) if item.get("b64_json") else self._get(item["url"]).content
        except requests.RequestException as exc:
            raise ImageError("I couldn't reach the image model — check your internet connection.", "network") from exc
        path = self._save(data, prompt, "png")
        return {"path": str(path), "model": gen["model"], "provider": gen["provider"], "title": prompt}


def _gen_error(status: int) -> str:
    return {401: "The image model rejected the token (HTTP 401) — check HF_TOKEN.",
            403: "The token isn't allowed to use the image model (HTTP 403) — enable “Make calls to Inference Providers”.",
            404: "That image model isn't available (HTTP 404) — set GENESIS_IMAGE_MODEL to another text-to-image model.",
            429: "The image service is busy or the free quota is used up (HTTP 429) — try again later.",
            503: "The image model is starting up (HTTP 503) — try again in a minute."}.get(status, f"The image model failed (HTTP {status}).")
