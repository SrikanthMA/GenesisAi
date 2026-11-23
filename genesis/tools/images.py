"""Image tools: find a real picture or generate one, and actually show it (chat window image card)."""
from __future__ import annotations

from ..core.events import EventType
from ..services.images import ImageError
from .base import Tool, ToolResult, obj, s


def _show(ctx, item: dict, caption: str) -> None:
    try:  # v2 hook (2.1): a display problem must not turn a found/created picture into "something went wrong"
        ctx.events.emit(EventType.MEDIA_SHOW, kind="image", path=item["path"], caption=caption,
                        credit=" · ".join(x for x in (item.get("credit"), item.get("license"), item.get("source")) if x),
                        link=item.get("page", ""))
        ctx.events.emit(EventType.UI_ACTION, action="open_chat")
    except Exception:  # pragma: no cover - UI side
        from ..v2 import images as v2images

        v2images.open_file_fallback(ctx, item["path"])


def _fail(exc: ImageError) -> ToolResult:
    hint = {"not_configured": "Add HF_TOKEN to the .env file (the same token as the chat model).",
            "network": "Say “check my internet” and I'll look into it."}.get(exc.kind)
    return ToolResult.fail(str(exc), hint=hint, kind=exc.kind)


class ImageSearch(Tool):
    name = "image_search"
    category = "images"
    description = ("Find a real photo/picture of something (animals, places, objects, landmarks) and show it to the user "
                   "in the chat window.")
    parameters = obj({"query": s("What the picture should show, e.g. 'peacock', 'Eiffel Tower'", maxLength=200),
                      "count": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1}}, ["query"])
    output = {"images": "list[{path, title, credit, license, source, page}]", "image_path": "str"}
    remote = True

    def run(self, args, ctx):
        q = args["query"].strip()
        try:
            items = ctx.images.search(q, int(args.get("count", 1)))
        except ImageError as exc:
            from ..v2 import images as v2images  # v2 hook (2.1): nothing downloadable → open an image search instead

            return v2images.search_fallback(ctx, q, exc) or _fail(exc)
        for it in items:
            _show(ctx, it, it.get("title") or q)
        credit = items[0].get("credit") or items[0].get("source")
        more = f" ({len(items)} pictures)" if len(items) > 1 else ""
        return ToolResult.ok(f"Here's a picture of {q}{more} — it's in the chat window." + (f" Credit: {credit}." if credit else ""),
                             images=items, image_path=items[0]["path"], query=q)

    def verify(self, args, result, ctx):
        from pathlib import Path

        return all(Path(i["path"]).is_file() and Path(i["path"]).stat().st_size > 0 for i in result.data.get("images", [])) \
            if result.success else False


class ImageGenerate(Tool):
    name = "image_generate"
    category = "images"
    description = "Create a new image from a description with an image-generation model, and show it in the chat window."
    parameters = obj({"prompt": s("Description of the image to create", maxLength=1000)}, ["prompt"])
    output = {"image_path": "str", "model": "str"}
    remote = True

    def run(self, args, ctx):
        try:  # v2 hook (2.2.2): say it may take a moment (the free services queue requests)
            ctx.events.emit(EventType.STATE_CHANGED, state="EXECUTING",
                            detail="Creating your picture - this can take up to a minute…")
        except Exception:
            pass
        try:
            item = ctx.images.generate(args["prompt"].strip())
        except ImageError as exc:
            from ..v2 import images as v2images  # v2 hook (2.1.1): can't create one → show a real picture instead

            return v2images.generate_fallback(ctx, args["prompt"].strip(), exc, _show) or _fail(exc)
        _show(ctx, dict(item, credit=f"Generated with {item['model']}"), args["prompt"])
        return ToolResult.ok(f"Here's the image I created — it's in the chat window (made with {item['model']}).",
                             image_path=item["path"], images=[item], model=item["model"])

    def verify(self, args, result, ctx):
        from pathlib import Path

        return Path(result.data["image_path"]).is_file() if result.success else False


TOOLS = [ImageSearch(), ImageGenerate()]
