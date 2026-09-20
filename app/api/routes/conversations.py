import json
import logging
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.ai.orchestration.orchestrator import AIOrchestrator
from app.ai.prompting.language_detection import LanguageDetectionService
from app.ai.prompting.languages import AUTO
from app.api.deps import get_db, get_language_detector, get_orchestrator
from app.core import generation_registry
from app.models.conversation import Conversation
from app.schemas.conversation import (
    ConversationCreate,
    ConversationDetail,
    ConversationRead,
    MessageCreate,
)
from app.schemas.language import LanguagePreferenceUpdate
from app.services.conversation_service import ConversationNotFoundError, ConversationService
from app.services.language_preference_service import LanguagePreferenceService
from app.services.user_service import UserService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def _service(db: Session = Depends(get_db)) -> ConversationService:
    return ConversationService(db)


def _current_user_id(db: Session = Depends(get_db)) -> str:
    return UserService(db).get_or_create_default_user().id


def _resolve_target_language(conversation: Conversation, detected: str | None) -> str | None:
    if conversation.preferred_language != AUTO:
        return conversation.preferred_language
    return detected


@router.post("", response_model=ConversationRead, status_code=201)
def create_conversation(
    payload: ConversationCreate,
    service: ConversationService = Depends(_service),
    user_id: str = Depends(_current_user_id),
) -> ConversationRead:
    conversation = service.create_conversation(user_id=user_id, title=payload.title)
    return ConversationRead.model_validate(conversation)


@router.get("", response_model=list[ConversationRead])
def list_conversations(
    service: ConversationService = Depends(_service),
    user_id: str = Depends(_current_user_id),
) -> list[ConversationRead]:
    conversations = service.list_conversations(user_id=user_id)
    return [ConversationRead.model_validate(c) for c in conversations]


@router.get("/{conversation_id}", response_model=ConversationDetail)
def get_conversation(
    conversation_id: str, service: ConversationService = Depends(_service)
) -> ConversationDetail:
    try:
        conversation = service.get_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    return ConversationDetail.model_validate(conversation)


@router.delete("/{conversation_id}", status_code=204)
def delete_conversation(
    conversation_id: str, service: ConversationService = Depends(_service)
) -> None:
    try:
        service.delete_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc


@router.patch("/{conversation_id}/language", response_model=ConversationRead)
def set_conversation_language(
    conversation_id: str,
    payload: LanguagePreferenceUpdate,
    db: Session = Depends(get_db),
) -> ConversationRead:
    try:
        conversation = LanguagePreferenceService(db).set_conversation_language(
            conversation_id, payload.language
        )
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc
    return ConversationRead.model_validate(conversation)


def _sse(event_type: str, data: dict) -> str:
    prefix = f"event: {event_type}\n" if event_type != "message" else ""
    return f"{prefix}data: {json.dumps(data)}\n\n"


async def _stream_and_persist(
    conversation_id: str,
    service: ConversationService,
    orchestrator: AIOrchestrator,
    history,
    target_language: str | None,
) -> AsyncIterator[str]:
    stop_event = generation_registry.start_generation(conversation_id)
    chunks: list[str] = []
    error_message: str | None = None
    try:
        async for token in orchestrator.stream_response(history, target_language=target_language):
            if stop_event.is_set():
                break
            chunks.append(token)
            yield _sse("message", {"token": token})
    except Exception as exc:  # noqa: BLE001 - must not leak the raw exception to the SSE stream
        logger.exception("LLM streaming failed for conversation %s", conversation_id)
        error_message = str(exc) or exc.__class__.__name__
    finally:
        generation_registry.finish_generation(conversation_id)
        content = "".join(chunks)
        if content:
            service.add_message(
                conversation_id=conversation_id,
                role="assistant",
                content=content,
                language=target_language,
            )
    if error_message is not None:
        yield _sse("error", {"message": error_message})
    yield _sse("done", {"stopped": stop_event.is_set()})


@router.post("/{conversation_id}/messages")
async def send_message(
    conversation_id: str,
    payload: MessageCreate,
    service: ConversationService = Depends(_service),
    orchestrator: AIOrchestrator = Depends(get_orchestrator),
    language_detector: LanguageDetectionService = Depends(get_language_detector),
) -> StreamingResponse:
    try:
        conversation = service.get_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc

    detected_language = language_detector.detect(payload.content)
    service.add_message(
        conversation_id=conversation_id,
        role="user",
        content=payload.content,
        language=detected_language,
    )
    target_language = _resolve_target_language(conversation, detected_language)
    history = service.get_history(conversation_id)

    return StreamingResponse(
        _stream_and_persist(conversation_id, service, orchestrator, history, target_language),
        media_type="text/event-stream",
    )


@router.post("/{conversation_id}/regenerate")
async def regenerate_message(
    conversation_id: str,
    service: ConversationService = Depends(_service),
    orchestrator: AIOrchestrator = Depends(get_orchestrator),
) -> StreamingResponse:
    try:
        conversation = service.get_conversation(conversation_id)
    except ConversationNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Conversation not found") from exc

    service.delete_last_assistant_message(conversation_id)
    last_user_message = service.get_last_user_message(conversation_id)
    detected_language = last_user_message.language if last_user_message else None
    target_language = _resolve_target_language(conversation, detected_language)
    history = service.get_history(conversation_id)

    return StreamingResponse(
        _stream_and_persist(conversation_id, service, orchestrator, history, target_language),
        media_type="text/event-stream",
    )


@router.post("/{conversation_id}/stop")
def stop_generation(conversation_id: str) -> dict[str, bool]:
    stopped = generation_registry.request_stop(conversation_id)
    return {"stopped": stopped}
