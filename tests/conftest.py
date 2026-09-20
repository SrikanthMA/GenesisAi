from collections.abc import AsyncIterator, Iterator

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.ai.llm.base import ChatMessage, LLMProvider
from app.ai.orchestration.orchestrator import AIOrchestrator
from app.api.deps import get_db, get_orchestrator
from app.main import app
from app.models.base import Base


class FakeLLMProvider(LLMProvider):
    """Deterministic in-memory stand-in so tests never hit a real HF endpoint."""

    def __init__(self, tokens: list[str] | None = None) -> None:
        self.tokens = tokens if tokens is not None else ["Hello", ", ", "world", "!"]
        self.last_messages: list[ChatMessage] | None = None

    async def stream(self, messages: list[ChatMessage], **kwargs: object) -> AsyncIterator[str]:
        self.last_messages = messages
        for token in self.tokens:
            yield token


@pytest.fixture()
def db_session() -> Iterator[Session]:
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    session = TestingSessionLocal()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)


@pytest.fixture()
def fake_llm() -> FakeLLMProvider:
    return FakeLLMProvider()


@pytest.fixture()
def client(db_session: Session, fake_llm: FakeLLMProvider):
    from fastapi.testclient import TestClient

    def _get_db_override() -> Iterator[Session]:
        yield db_session

    def _get_orchestrator_override() -> AIOrchestrator:
        return AIOrchestrator(llm_provider=fake_llm)

    app.dependency_overrides[get_db] = _get_db_override
    app.dependency_overrides[get_orchestrator] = _get_orchestrator_override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()
