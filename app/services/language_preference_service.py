from sqlalchemy.orm import Session

from app.models.conversation import Conversation
from app.models.user import User
from app.services.conversation_service import ConversationNotFoundError


class LanguagePreferenceService:
    """Owns reading/writing the user's and a conversation's language preference.

    Kept separate from ConversationService/UserService (even though it's thin)
    because language preference has its own validation rule (must be "auto" or
    a SUPPORTED_LANGUAGES code) that has nothing to do with conversation or
    user CRUD, and Phase 3+ (STT/TTS language selection) will grow this class
    independently of the other two.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def set_conversation_language(self, conversation_id: str, language: str) -> Conversation:
        conversation = self.db.get(Conversation, conversation_id)
        if conversation is None:
            raise ConversationNotFoundError(conversation_id)
        conversation.preferred_language = language
        self.db.commit()
        self.db.refresh(conversation)
        return conversation

    def set_user_language(self, user_id: str, language: str) -> User:
        user = self.db.get(User, user_id)
        if user is None:
            raise ValueError(f"User not found: {user_id}")
        user.preferred_language = language
        self.db.commit()
        self.db.refresh(user)
        return user
