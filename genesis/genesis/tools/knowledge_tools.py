"""Knowledge-base (RAG) tools."""
from __future__ import annotations

from ..ai import prompts
from ..ai.llm.base import LLMError
from ..memory.knowledge import KnowledgeError
from ..security.injection import wrap_untrusted
from ..security.validators import ValidationError
from .base import Tool, ToolResult, obj, s
from .files import resolve_existing


class KnowledgeIngest(Tool):
    name = "knowledge_ingest"
    category = "knowledge"
    description = "Add a document or folder (txt, md, pdf, docx, code) to the local knowledge base."
    parameters = obj({"path": s("File or folder", maxLength=1000)}, ["path"])

    def run(self, args, ctx):
        try:
            path = resolve_existing(args["path"], ctx)
            info = ctx.knowledge.ingest(path)
        except (ValidationError, KnowledgeError, OSError) as exc:
            return ToolResult.fail(f"I couldn't add that to your knowledge base: {exc}")
        if info.get("already_indexed"):
            return ToolResult.ok(f"{path.name} was already in your knowledge base.", **info)
        if not info.get("chunks"):
            return ToolResult.fail(f"I didn't find any readable documents in {path.name}.")
        return ToolResult.ok(f"Added {info['documents']} document(s) from {path.name} to your knowledge base "
                             f"({info['chunks']} passages).", **info)


class KnowledgeQuery(Tool):
    name = "knowledge_query"
    category = "knowledge"
    description = "Answer a question from the user's own indexed documents (retrieval-augmented generation)."
    parameters = obj({"question": s("Question", maxLength=2000), "k": {"type": "integer", "minimum": 1, "maximum": 10, "default": 4}},
                     ["question"])

    def run(self, args, ctx):
        if ctx.knowledge.count() == 0:
            return ToolResult.fail("Your knowledge base is empty. Say “add <file> to your knowledge base” first.")
        hits = ctx.knowledge.search(args["question"], k=args.get("k", 4))
        if not hits:
            return ToolResult.ok("I couldn't find anything about that in your documents.", sources=[])
        sources = [{"title": h["title"], "path": h["path"], "score": round(h["score"], 3)} for h in hits]
        context = "\n\n".join(wrap_untrusted(h["text"], f"[{i + 1}] {h['title']}") for i, h in enumerate(hits))
        if getattr(ctx.llm, "is_generative", False):
            try:
                answer = ctx.llm.chat([{"role": "user", "content": prompts.RAG_PROMPT.format(context=context, question=args["question"])}],
                                      max_tokens=600, temperature=0.2)
                cites = "\n".join(f"[{i + 1}] {s['title']}" for i, s in enumerate(sources))
                return ToolResult.ok(f"{answer}\n\nSources:\n{cites}", answer=answer, sources=sources)
            except LLMError:
                pass
        best = hits[0]
        excerpt = best["text"][:600].strip()
        return ToolResult.ok(f"The most relevant passage (from {best['title']}):\n“{excerpt}”", answer=excerpt, sources=sources)


TOOLS = [KnowledgeIngest(), KnowledgeQuery()]
