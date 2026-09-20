from sqlalchemy.orm import Session

from app.services.conversation_service import ConversationNotFoundError, ConversationService
from app.services.user_service import UserService


def _make_user_id(db_session: Session) -> str:
    return UserService(db_session).get_or_create_default_user().id


def test_create_conversation(db_session: Session) -> None:
    user_id = _make_user_id(db_session)
    service = ConversationService(db_session)

    conversation = service.create_conversation(user_id=user_id, title="My chat")

    assert conversation.id
    assert conversation.title == "My chat"
    assert conversation.user_id == user_id


def test_add_message_and_get_history(db_session: Session) -> None:
    user_id = _make_user_id(db_session)
    service = ConversationService(db_session)
    conversation = service.create_conversation(user_id=user_id)

    service.add_message(conversation.id, role="user", content="hi")
    service.add_message(conversation.id, role="assistant", content="hello!")

    history = service.get_history(conversation.id)

    assert [m.role for m in history] == ["user", "assistant"]
    assert [m.content for m in history] == ["hi", "hello!"]


def test_get_missing_conversation_raises(db_session: Session) -> None:
    service = ConversationService(db_session)
    try:
        service.get_conversation("does-not-exist")
        assert False, "expected ConversationNotFoundError"
    except ConversationNotFoundError:
        pass


def test_delete_last_assistant_message(db_session: Session) -> None:
    user_id = _make_user_id(db_session)
    service = ConversationService(db_session)
    conversation = service.create_conversation(user_id=user_id)
    service.add_message(conversation.id, role="user", content="hi")
    service.add_message(conversation.id, role="assistant", content="hello!")

    service.delete_last_assistant_message(conversation.id)

    history = service.get_history(conversation.id)
    assert [m.role for m in history] == ["user"]


def test_list_conversations_scoped_to_user(db_session: Session) -> None:
    user_id = _make_user_id(db_session)
    service = ConversationService(db_session)
    service.create_conversation(user_id=user_id, title="A")
    service.create_conversation(user_id=user_id, title="B")

    conversations = service.list_conversations(user_id=user_id)

    assert {c.title for c in conversations} == {"A", "B"}
