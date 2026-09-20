from collections.abc import AsyncIterator

import pytest

from app.ai.llm.base import ChatMessage, LLMProvider
from app.ai.orchestration.orchestrator import AIOrchestrator


class RecordingProvider(LLMProvider):
    def __init__(self) -> None:
        self.last_messages: list[ChatMessage] | None = None

    async def stream(self, messages: list[ChatMessage], **kwargs: object) -> AsyncIterator[str]:
        self.last_messages = messages
        yield "ok"


@pytest.mark.asyncio
async def test_no_target_language_asks_to_mirror_user() -> None:
    provider = RecordingProvider()
    orchestrator = AIOrchestrator(provider)

    async for _ in orchestrator.stream_response([ChatMessage(role="user", content="hi")]):
        pass

    assert provider.last_messages is not None
    system_message = provider.last_messages[0]
    assert system_message.role == "system"
    assert "same language the user's message is written in" in system_message.content


@pytest.mark.asyncio
async def test_target_language_is_named_explicitly() -> None:
    provider = RecordingProvider()
    orchestrator = AIOrchestrator(provider)

    async for _ in orchestrator.stream_response(
        [ChatMessage(role="user", content="hi")], target_language="hi"
    ):
        pass

    assert provider.last_messages is not None
    system_message = provider.last_messages[0]
    assert "Respond in Hindi" in system_message.content
