from collections.abc import AsyncIterator

from app.ai.llm.base import ChatMessage, LLMProvider
from app.ai.prompting.languages import language_name

BASE_SYSTEM_PROMPT = (
    "You are Genesis, a helpful personal AI assistant. Respond clearly and concisely."
)


def _language_instruction(target_language: str | None) -> str:
    if target_language is None:
        return "Respond in the same language the user's message is written in."
    name = language_name(target_language)
    return f"Respond in {name}, regardless of what language the user writes in."


class AIOrchestrator:
    """Coordinates a single response turn.

    Phase 1 scope was intentionally thin: build a prompt from conversation
    history and stream it through the configured LLMProvider. Phase 2 adds
    one thing to that - which language the response should be in - without
    changing that shape. Memory, RAG, web search, tools and planning are
    later phases; they'll extend this same stream_response() call so the API
    layer never has to change.
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def stream_response(
        self, history: list[ChatMessage], target_language: str | None = None
    ) -> AsyncIterator[str]:
        system_content = f"{BASE_SYSTEM_PROMPT} {_language_instruction(target_language)}"
        prompt = [ChatMessage(role="system", content=system_content), *history]
        async for token in self._llm.stream(prompt):
            yield token
