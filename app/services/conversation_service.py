from sqlalchemy import select
from sqlalchemy.orm import Session

from app.ai.llm.base import ChatMessage
from app.models.conversation import Conversation
from app.models.message import Message

DEFAULT_HISTORY_LIMIT = 20


class ConversationNotFoundError(Exception):
    pass


class ConversationService:
    def __init__(self, db: Session) -> None:
        self.db = db

    def create_conversation(
        self, user_id: str, title: str | None = None, model_name: str | None = None
    ) -> Conversation:
        conversation = Conversation(user_id=user_id, title=title, model_name=model_name)
        self.db.add(conversation)
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def list_conversations(self, user_id: str) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
        )
        return list(self.db.scalars(stmt))

    def get_conversation(self, conversation_id: str) -> Conversation:
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        return conversation

    def delete_conversation(self, conversation_id: str) -> None:
        conversation = self.get_conversation(conversation_id)
        self.db.delete(conversation)
        self.db.commit()

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        model_name: str | None = None,
        language: str | None = None,
    ) -> Message:
        message = Message(
            conversation_id=conversation_id,
            role=role,
            content=content,
            model_name=model_name,
            language=language,
        )
        self.db.add(message)
        # Touch the conversation so conversation lists sort by recency.
        conversation = self.get_conversation(conversation_id)
        self.db.flush()
        self.db.add(conversation)
        self.db.commit()
        self.db.refresh(message)
        return message

    def get_history(
        self, conversation_id: str, limit: int = DEFAULT_HISTORY_LIMIT
    ) -> list[ChatMessage]:
        conversation = self.get_conversation(conversation_id)
        messages = conversation.messages[-limit:]
        return [ChatMessage(role=m.role, content=m.content) for m in messages]

    def delete_last_assistant_message(self, conversation_id: str) -> None:
        conversation = self.get_conversation(conversation_id)
        if conversation.messages and conversation.messages[-1].role == "assistant":
            self.db.delete(conversation.messages[-1])
            self.db.commit()

    def get_last_user_message(self, conversation_id: str) -> Message | None:
        conversation = self.get_conversation(conversation_id)
        for message in reversed(conversation.messages):
            if message.role == "user":
                return message
        return None
