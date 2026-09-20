from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass


@dataclass(frozen=True)
class ChatMessage:
    role: str
    content: str


class LLMProvider(ABC):
    """Abstraction every LLM backend (Hugging Face, local, custom) must implement.

    Nothing outside app/ai/llm should import a concrete provider directly -
    callers depend on this interface so the backing model/provider can be
    swapped through config alone.
    """

    @abstractmethod
    def stream(self, messages: list[ChatMessage], **kwargs: object) -> AsyncIterator[str]:
        """Yield response text incrementally, token-by-token or chunk-by-chunk."""
        raise NotImplementedError

    async def generate(self, messages: list[ChatMessage], **kwargs: object) -> str:
        """Convenience non-streaming call built on top of stream()."""
        parts = [chunk async for chunk in self.stream(messages, **kwargs)]
        return "".join(parts)
