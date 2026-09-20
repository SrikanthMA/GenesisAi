from collections.abc import AsyncIterator
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from app.ai.llm.base import ChatMessage
from app.ai.llm.huggingface_provider import HuggingFaceProvider


@dataclass
class _FakeDelta:
    content: str | None


def _fake_chunk(content: str | None) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(delta=_FakeDelta(content=content))])


async def _fake_stream() -> AsyncIterator[SimpleNamespace]:
    for content in ["Hi", " there", None]:
        yield _fake_chunk(content)


@pytest.mark.asyncio
async def test_stream_yields_only_non_empty_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = HuggingFaceProvider(model_id="Qwen/Qwen2.5-7B-Instruct", token="fake-token")

    captured_kwargs: dict[str, object] = {}

    async def fake_chat_completion(**kwargs: object):
        captured_kwargs.update(kwargs)
        return _fake_stream()

    monkeypatch.setattr(provider._client, "chat_completion", fake_chat_completion)

    tokens = [
        token async for token in provider.stream([ChatMessage(role="user", content="hello")])
    ]

    assert tokens == ["Hi", " there"]
    assert captured_kwargs["stream"] is True
    assert captured_kwargs["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_generate_concatenates_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = HuggingFaceProvider(model_id="Qwen/Qwen2.5-7B-Instruct", token="fake-token")

    async def fake_chat_completion(**kwargs: object):
        return _fake_stream()

    monkeypatch.setattr(provider._client, "chat_completion", fake_chat_completion)

    result = await provider.generate([ChatMessage(role="user", content="hello")])

    assert result == "Hi there"
