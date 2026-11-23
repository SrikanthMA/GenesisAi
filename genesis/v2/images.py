"""Image generation that keeps working when one service is unavailable (2.1, 2.1.1).

Genesis 1.x could only generate images through Hugging Face (needs credits) or an OpenAI images endpoint. 2.1 tries,
in order (each only when it is set up):

1. **Gemini** image model - Google bills image output, so on a free key this fails quickly and is skipped for 30 minutes;
2. **Hugging Face / OpenAI** - the 1.x path, when a token is configured;
3. **Cloudflare Workers AI** (FLUX.1-schnell) - free daily allowance (about 170 images a day), needs a free Cloudflare
   account: ``CLOUDFLARE_ACCOUNT_ID`` + ``CLOUDFLARE_API_TOKEN`` (Setup_Images.bat asks for them and tests them);
4. **Pollinations** - with ``POLLINATIONS_API_KEY`` (or, only when ``GENESIS_IMAGE_PROVIDER=auto``, its old key-less
   endpoint, which may no longer answer). Your prompt text is sent to that service.

When nothing can create the picture, the tool shows a real photo of the subject instead (search) and says why.
Every provider's failure is remembered for a while so a request doesn't wait on a service that just said "no".
"""
from __future__ import annotations

import os
import re
import time
from typing import Any, Callable
from urllib.parse import quote

import requests

from ..core.logging_setup import get_logger
from . import gemini_native

log = get_logger("v2.images")
POLLINATIONS = "https://image.pollinations.ai/prompt/"
POLLINATIONS_GEN = "https://gen.pollinations.ai/image/"
CLOUDFLARE_MODEL = "@cf/black-forest-labs/flux-1-schnell"
HORDE = "https://aihorde.net/api/v2/generate"
HORDE_ANON_KEY = "0000000000"  # AI Horde's public anonymous key (lowest priority, no account needed)
HORDE_MAX_WAIT = 150.0
SKIP_SECONDS = {"quota": 1800, "auth": 1800, "not_found": 3600, "not_configured": 60, "network": 60, "error": 120}
_skip_until: dict[str, float] = {}


def _cloudflare_creds() -> tuple[str, str]:
    return (os.getenv("CLOUDFLARE_ACCOUNT_ID", "") or "").strip(), (os.getenv("CLOUDFLARE_API_TOKEN", "") or "").strip()


def provider_order(service: Any) -> list[str]:
    """Providers to try. Unset = every service *you* set up (Gemini, HF/OpenAI, Cloudflare, a Pollinations key);
    ``auto`` additionally tries Pollinations' key-less endpoint as the last resort."""
    choice = (os.getenv("GENESIS_IMAGE_PROVIDER", "") or "").strip().lower()
    if choice in ("off", "none", "disabled"):
        return ["off"]
    if choice not in ("", "auto"):
        return [{"hf": "legacy", "huggingface": "legacy", "openai": "legacy", "cf": "cloudflare"}.get(choice, choice)]
    order = []
    if gemini_native.gemini_key(service.config):
        order.append("gemini")
    if service.generator().get("configured"):
        order.append("legacy")
    if all(_cloudflare_creds()):
        order.append("cloudflare")
    if choice == "auto" or os.getenv("POLLINATIONS_API_KEY", "").strip():
        order.append("pollinations")
    if choice == "auto" or os.getenv("AIHORDE_API_KEY", "").strip():
        order.append("horde")  # free, no account: the last resort that still works when everything else says no
    return order


def _usable(name: str) -> bool:
    return time.monotonic() >= _skip_until.get(name, 0.0)


_last_kind: dict[str, str] = {}


def _remember_failure(name: str, kind: str) -> None:
    _skip_until[name] = time.monotonic() + SKIP_SECONDS.get(kind, 120)
    _last_kind[name] = kind


def reset() -> None:
    _skip_until.clear()


def _pollinations(service: Any, prompt: str) -> tuple[bytes, str]:
    http = service.session or requests
    key = os.getenv("POLLINATIONS_API_KEY", "").strip()
    headers = {"User-Agent": "Genesis-Desktop-Assistant/2.1"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    urls = ([POLLINATIONS_GEN] if key else [POLLINATIONS, POLLINATIONS_GEN])
    last_status = 0
    for base in urls:
        try:
            r = http.get(base + quote(prompt[:900], safe=""), params={"width": 1024, "height": 1024, "nologo": "true",
                                                                      "model": "flux"}, timeout=120, headers=headers)
        except requests.RequestException as exc:
            raise _err("I couldn't reach Pollinations - check your internet connection.", "network") from exc
        ctype = (getattr(r, "headers", {}) or {}).get("Content-Type", "")
        if r.status_code == 200 and ctype.startswith("image/") and r.content:
            return r.content, ctype
        last_status = r.status_code
        if r.status_code == 429:
            raise _err("Pollinations is busy - try again shortly.", "quota")
    kind = "auth" if last_status in (401, 402, 403) else "error"
    raise _err(f"Pollinations didn't create the image (HTTP {last_status}"
               + ("; it now needs an account key)" if kind == "auth" and not key else ")"), kind)


def _cloudflare(service: Any, prompt: str) -> tuple[bytes, str]:
    """Cloudflare Workers AI, FLUX.1-schnell: free daily allowance on a free account."""
    import base64

    account, token = _cloudflare_creds()
    if not (account and token):
        raise _err("Cloudflare isn't set up (CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN).", "not_configured")
    http = service.session or requests
    url = f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/run/{CLOUDFLARE_MODEL}"
    try:
        r = http.post(url, json={"prompt": prompt[:2048], "steps": 4}, timeout=90,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    except requests.RequestException as exc:
        raise _err("I couldn't reach Cloudflare - check your internet connection.", "network") from exc
    text = (getattr(r, "text", "") or "")[:400].lower()
    if r.status_code in (401, 403) or "authentication" in text:
        raise _err("Cloudflare rejected the API token (it needs the Workers AI permission).", "auth")
    if r.status_code == 429 or (r.status_code >= 400 and any(w in text for w in ("allocation", "neurons", "limit"))):
        raise _err("Today's free Cloudflare image allowance is used up - it resets tomorrow.", "quota")
    if r.status_code != 200:
        raise _err(f"Cloudflare didn't create the image (HTTP {r.status_code}).", "error")
    try:
        img = (r.json().get("result") or {}).get("image")
        data = base64.b64decode(img) if img else b""
    except (ValueError, TypeError, AttributeError) as exc:
        raise _err("Cloudflare returned an unreadable answer.", "error") from exc
    if not data:
        raise _err("Cloudflare returned no image.", "error")
    return data, "image/jpeg"


def _horde(service: Any, prompt: str, *, sleep: Callable[[float], None] = time.sleep,
           clock: Callable[[], float] = time.monotonic) -> tuple[bytes, str]:
    """AI Horde: a free, volunteer-run image network. No account needed (anonymous requests wait in a queue, usually
    under a minute or two). The prompt is seen by the volunteer who renders it; nothing else is sent."""
    http = service.session or requests
    key = os.getenv("AIHORDE_API_KEY", "").strip() or HORDE_ANON_KEY
    headers = {"apikey": key, "Client-Agent": "Genesis-Desktop-Assistant:2.2:local", "Content-Type": "application/json",
               "Accept": "application/json"}
    body = {"prompt": prompt[:900], "nsfw": False, "censor_nsfw": True, "shared": False, "r2": True, "trusted_workers": False,
            "params": {"width": 512, "height": 512, "steps": 20, "n": 1, "sampler_name": "k_euler_a", "cfg_scale": 7}}
    try:
        r = http.post(HORDE + "/async", json=body, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise _err("I couldn't reach the free community image service - check your internet connection.", "network") from exc
    if r.status_code not in (200, 202):
        text = (getattr(r, "text", "") or "")[:200].lower()
        raise _err(f"The community image service refused (HTTP {r.status_code}).",
                   "quota" if r.status_code == 429 or "kudos" in text else "error")
    try:
        job = r.json().get("id")
    except (ValueError, AttributeError):
        job = None
    if not job:
        raise _err("The community image service didn't accept the request.", "error")
    t0 = clock()
    while clock() - t0 < HORDE_MAX_WAIT:
        sleep(3.0)
        try:
            st = http.get(f"{HORDE}/check/{job}", headers={"Client-Agent": headers["Client-Agent"]}, timeout=20).json()
        except Exception:
            continue
        if st.get("faulted") or st.get("is_possible") is False:
            raise _err("The community image service couldn't make that picture right now.", "error")
        if st.get("done"):
            break
    else:
        raise _err("The free community image service is very busy right now - try again in a few minutes.", "quota")
    try:
        res = http.get(f"{HORDE}/status/{job}", headers={"Client-Agent": headers["Client-Agent"]}, timeout=30).json()
        img = ((res.get("generations") or [{}])[0] or {}).get("img") or ""
    except Exception as exc:
        raise _err("The community image service returned no picture.", "error") from exc
    if img.startswith("https://"):
        try:
            g = http.get(img, timeout=60)
        except requests.RequestException as exc:
            raise _err("I couldn't download the finished picture.", "network") from exc
        ctype = (getattr(g, "headers", {}) or {}).get("Content-Type", "image/webp")
        if g.status_code != 200 or not g.content:
            raise _err("I couldn't download the finished picture.", "network")
        return g.content, ctype if ctype.startswith("image/") else "image/webp"
    if img:
        import base64

        return base64.b64decode(img), "image/webp"
    raise _err("The community image service returned no picture.", "error")


_LABEL = {"gemini": "Gemini", "legacy": "Hugging Face", "cloudflare": "Cloudflare", "pollinations": "Pollinations",
          "horde": "AI Horde"}
_WHY = {"quota": "no free image quota left", "auth": "not allowed with the current key", "network": "unreachable",
        "not_found": "model not available", "not_configured": "not set up", "error": "failed"}


def _err(msg: str, kind: str):
    from ..services.images import ImageError

    return ImageError(msg, kind)


def generate(service: Any, prompt: str, legacy: Callable[[str], dict]) -> dict:
    """Try each provider in turn; returns the same dict as ImageService.generate (path, model, provider, title)."""
    from ..services.images import ImageError

    order = provider_order(service)
    if order == ["off"]:
        raise ImageError("Image generation is switched off (GENESIS_IMAGE_PROVIDER=off).", "not_configured")
    if not order:
        return legacy(prompt)  # nothing configured: the 1.x message explains what to add
    prompt = prompt.strip().rstrip("?.!, ")
    errors: list[str] = []
    reasons: list[str] = []
    last: ImageError | None = None
    if not any(_usable(n) for n in order):
        reset()  # everything said "no" recently: ask again rather than give up without trying
    for name in order:
        if not _usable(name) and len(order) > 1:
            kind = _last_kind.get(name, "error")
            reasons.append(f"{_LABEL.get(name, name)}: {_WHY.get(kind, 'failed')} (a few minutes ago)")
            continue
        try:
            if name == "gemini":
                data, ctype, model = gemini_native.generate_image(service.config, prompt, session=service.session,
                                                                  model=os.getenv("GENESIS_GEMINI_IMAGE_MODEL") or None)
                ext = "jpg" if "jpeg" in ctype else "png"
                path = service._save(data, prompt, ext)
                return {"path": str(path), "model": model, "provider": "gemini", "title": prompt}
            if name == "legacy":
                return legacy(prompt)
            if name == "cloudflare":
                data, ctype = _cloudflare(service, prompt)
                path = service._save(data, prompt, "jpg")
                return {"path": str(path), "model": "FLUX.1-schnell via Cloudflare (free daily allowance)",
                        "provider": "cloudflare", "title": prompt}
            if name == "horde":
                data, ctype = _horde(service, prompt)
                ext = "webp" if "webp" in ctype else service._ext(ctype, "")
                path = service._save(data, prompt, ext)
                return {"path": str(path), "model": "Stable Diffusion via AI Horde (free community service)",
                        "provider": "horde", "title": prompt}
            if name == "pollinations":
                data, ctype = _pollinations(service, prompt)
                path = service._save(data, prompt, service._ext(ctype, ""))
                return {"path": str(path), "model": "FLUX via Pollinations (free)", "provider": "pollinations", "title": prompt}
            raise ImageError(f"Unknown image provider “{name}” (GENESIS_IMAGE_PROVIDER).", "not_configured")
        except gemini_native.GeminiError as exc:
            if exc.kind == "refused":
                raise ImageError("The image model declined that request.", "refused") from exc
            _remember_failure(name, exc.kind)
            errors.append(f"gemini: {exc.kind}")
            reasons.append(f"Gemini: {'needs billing for images' if exc.kind in ('quota', 'auth') else _WHY.get(exc.kind, 'failed')}")
            last = ImageError(str(exc), exc.kind)
        except ImageError as exc:
            if exc.kind == "refused":
                raise
            _remember_failure(name, exc.kind)
            errors.append(f"{name}: {exc.kind}")
            reasons.append(f"{_LABEL.get(name, name)}: {_WHY.get(exc.kind, 'failed')}")
            last = exc
    log.warning("image generation failed with every provider: %s", ", ".join(errors) or "none usable")
    if order == ["gemini"] and last is not None and last.kind in ("quota", "auth"):
        raise ImageError("Gemini only creates images on accounts with billing turned on. For free images, run "
                         "Setup_Images.bat (free Cloudflare account, about 170 images a day).", "unavailable")
    if last is not None and len(order) == 1 and order != ["pollinations"]:
        raise last
    why = "; ".join(dict.fromkeys(reasons)) or "no image service answered"
    tip = "" if "cloudflare" in order else (" (Tip: for faster, reliable free images run Setup_Images.bat once - a free "
                                            "Cloudflare account, about 170 images a day.)")
    err = ImageError(f"I couldn't create the picture right now ({why}).{tip}", "unavailable")
    err.why, err.tip = why, tip  # type: ignore[attr-defined]
    raise err


_ENGINES = {"google": "https://www.google.com/search?tbm=isch&q=", "bing": "https://www.bing.com/images/search?q=",
            "duckduckgo": "https://duckduckgo.com/?ia=images&iax=images&q="}


def search_url(query: str, engine: str = "google") -> str:
    """A browser image search, used when no picture could be downloaded."""
    return _ENGINES.get((engine or "google").lower(), _ENGINES["google"]) + quote(query)


def search_fallback(ctx: Any, query: str, exc: Exception):
    """No picture could be downloaded: open an image search in the browser so the user still sees pictures."""
    from ..tools.base import ToolResult

    if getattr(exc, "kind", "") not in ("not_found", "network"):
        return None
    engine = str(ctx.settings.get("search.engine", "google") or "google") if getattr(ctx, "settings", None) else "google"
    url = search_url(query, engine)
    try:
        opened = bool(ctx.backend.open_url(url))
    except Exception:
        opened = False
    if not opened:
        return None
    return ToolResult.ok(f"I couldn't find a picture of {query} to show here, so I opened an image search for it in your "
                         "browser.", url=url, query=query, images=[], fallback="browser")


def open_file_fallback(ctx: Any, path: str) -> None:
    try:
        ctx.backend.open_path(path)
    except Exception:
        log.info("couldn't open the image file either")


def generate_fallback(ctx: Any, prompt: str, exc: Exception, show: Callable[[Any, dict, str], None]):
    """Nothing could create the picture: show a real photo of the subject instead (and say why)."""
    from ..services.images import ImageError
    from ..tools.base import ToolResult

    if getattr(exc, "kind", "") == "refused":
        return None
    subject = re.sub(r"^(?:an?|the|some)\s+", "", prompt.strip().rstrip("?.!, "), flags=re.I)[:120]
    try:
        items = ctx.images.search(subject, 1)
    except ImageError:
        return None
    for it in items:
        show(ctx, it, it.get("title") or subject)
    why = getattr(exc, "why", "") or str(exc)
    tip = getattr(exc, "tip", "")
    return ToolResult.ok(f"I couldn't create a new picture right now, so here's a real photo of {subject} instead - it's in "
                         f"the chat window. (Why: {why}.){tip}",
                         images=items, image_path=items[0]["path"], substituted="search", reason=str(exc))

from ..v3.hooks import patch_v2_images as _v3_hook; _v3_hook(globals())  # v3 hook (Genesis 3.0)
