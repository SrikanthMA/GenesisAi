import json
from collections.abc import AsyncIterator

from fastapi.testclient import TestClient

from app.ai.llm.base import ChatMessage, LLMProvider
from app.ai.orchestration.orchestrator import AIOrchestrator
from app.api.deps import get_orchestrator
from app.main import app


def _create_conversation(client: TestClient, title: str = "Test chat") -> str:
    response = client.post("/api/v1/conversations", json={"title": title})
    assert response.status_code == 201
    return response.json()["id"]


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_create_and_get_conversation(client: TestClient) -> None:
    conversation_id = _create_conversation(client)

    response = client.get(f"/api/v1/conversations/{conversation_id}")

    assert response.status_code == 200
    body = response.json()
    assert body["id"] == conversation_id
    assert body["title"] == "Test chat"
    assert body["messages"] == []


def test_get_missing_conversation_returns_404(client: TestClient) -> None:
    response = client.get("/api/v1/conversations/does-not-exist")
    assert response.status_code == 404


def test_list_conversations(client: TestClient) -> None:
    _create_conversation(client, title="First")
    _create_conversation(client, title="Second")

    response = client.get("/api/v1/conversations")

    assert response.status_code == 200
    titles = {c["title"] for c in response.json()}
    assert titles == {"First", "Second"}


def test_send_message_streams_and_persists(client: TestClient, fake_llm) -> None:
    conversation_id = _create_conversation(client)

    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "hi there"},
    ) as response:
        assert response.status_code == 200
        body = "".join(response.iter_text())

    for token in fake_llm.tokens:
        assert json.dumps({"token": token}) in body
    assert "event: done" in body

    detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
    roles = [m["role"] for m in detail["messages"]]
    contents = [m["content"] for m in detail["messages"]]
    assert roles == ["user", "assistant"]
    assert contents == ["hi there", "".join(fake_llm.tokens)]

    # The orchestrator should have seen the user's message in the history it sent to the LLM.
    assert fake_llm.last_messages is not None
    assert any(m.content == "hi there" for m in fake_llm.last_messages)


def test_regenerate_replaces_last_assistant_message(client: TestClient, fake_llm) -> None:
    conversation_id = _create_conversation(client)
    with client.stream(
        "POST",
        f"/api/v1/conversations/{conversation_id}/messages",
        json={"content": "hi"},
    ) as response:
        list(response.iter_text())

    fake_llm.tokens = ["different", " answer"]

    with client.stream(
        "POST", f"/api/v1/conversations/{conversation_id}/regenerate"
    ) as response:
        list(response.iter_text())

    detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
    assert len(detail["messages"]) == 2
    assert detail["messages"][-1]["content"] == "different answer"


def test_stop_generation_on_unknown_conversation_returns_false(client: TestClient) -> None:
    response = client.post("/api/v1/conversations/unknown-id/stop")
    assert response.status_code == 200
    assert response.json() == {"stopped": False}


def test_send_message_surfaces_provider_error_instead_of_hanging(client: TestClient) -> None:
    conversation_id = _create_conversation(client)

    class BrokenProvider(LLMProvider):
        async def stream(
            self, messages: list[ChatMessage], **kwargs: object
        ) -> AsyncIterator[str]:
            yield "partial"
            raise RuntimeError("upstream inference call failed")

    app.dependency_overrides[get_orchestrator] = lambda: AIOrchestrator(BrokenProvider())
    try:
        with client.stream(
            "POST",
            f"/api/v1/conversations/{conversation_id}/messages",
            json={"content": "hi"},
        ) as response:
            body = "".join(response.iter_text())
    finally:
        del app.dependency_overrides[get_orchestrator]

    assert "event: error" in body
    assert "upstream inference call failed" in body
    assert "event: done" in body

    # The partial output that streamed before the failure is still persisted,
    # matching what the user actually saw.
    detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
    assert detail["messages"][-1]["content"] == "partial"


def test_delete_conversation(client: TestClient) -> None:
    conversation_id = _create_conversation(client)

    response = client.delete(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 204

    response = client.get(f"/api/v1/conversations/{conversation_id}")
    assert response.status_code == 404
