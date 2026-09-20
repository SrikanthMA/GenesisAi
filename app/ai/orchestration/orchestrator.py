from collections.abc import AsyncIterator

from app.ai.llm.base import ChatMessage, LLMProvider

SYSTEM_PROMPT = (
    "You are Genesis, a helpful personal AI assistant. Respond clearly and concisely."
)


class AIOrchestrator:
    """Coordinates a single response turn.

    Phase 1 scope is intentionally thin: build a prompt from conversation
    history and stream it through the configured LLMProvider. Memory, RAG,
    web search, tools and planning are separate phases - this class is where
    they will be wired in later, behind the same stream_response() call, so
    the API layer never has to change.
    """

    def __init__(self, llm_provider: LLMProvider) -> None:
        self._llm = llm_provider

    async def stream_response(self, history: list[ChatMessage]) -> AsyncIterator[str]:
        prompt = [ChatMessage(role="system", content=SYSTEM_PROMPT), *history]
        async for token in self._llm.stream(prompt):
            yield token
