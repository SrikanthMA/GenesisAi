from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.user import User

DEFAULT_LOCAL_EMAIL = "local@genesis.local"


class UserService:
    """Phase 1 has no auth yet - every request runs as a single local user.

    Multi-user auth arrives once Genesis is actually deployed for more than
    one person; until then this keeps the schema/API shape (user_id on every
    conversation) correct without building login infrastructure early.
    """

    def __init__(self, db: Session) -> None:
        self.db = db

    def get_or_create_default_user(self) -> User:
        stmt = select(User).where(User.email == DEFAULT_LOCAL_EMAIL)
        user = self.db.scalars(stmt).first()
        if user is not None:
            return user

        user = User(email=DEFAULT_LOCAL_EMAIL, display_name="Local User")
        self.db.add(user)
        self.db.commit()
        self.db.refresh(user)
        return user
