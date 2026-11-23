"""Service container: builds and wires every subsystem (dependency injection).

Tests construct it with fakes (RecordingBackend, MemorySecretStore, mocked
HTTP sessions); the desktop app constructs it with real implementations.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from ..ai.llm.base import LLMProvider
from ..ai.llm.factory import ResilientLLM, build_provider
from ..database.db import Database
from ..database.repositories import Repositories
from ..memory.embeddings import create_embedder
from ..memory.knowledge import KnowledgeBase
from ..memory.manager import MemoryManager
from ..security.confirmation import ConfirmationManager
from ..security.permissions import PermissionManager
from ..security.secrets import SecretStore, create_secret_store
from ..security.validators import default_allowed_roots
from ..services.app_resolver import AppResolver
from ..services.browser import BrowserController
from ..services.calendar import CalendarService
from ..services.gmail import GmailService
from ..services.google_auth import GoogleAuth, load_client_config
from ..services.platform import PlatformBackend, create_backend
from .config import GenesisConfig
from .events import EventBus, EventType
from .settings import Settings


@dataclass
class ServiceContainer:
    config: GenesisConfig
    db: Database
    repos: Repositories
    settings: Settings
    events: EventBus
    backend: PlatformBackend
    apps: AppResolver
    secrets: SecretStore
    llm: LLMProvider
    google: GoogleAuth
    gmail: GmailService
    calendar: CalendarService
    knowledge: KnowledgeBase
    memory: MemoryManager
    permissions: PermissionManager
    confirmations: ConfirmationManager
    browser: BrowserController
    extras: dict[str, Any] = field(default_factory=dict)
    scheduler: Any = None  # ReminderScheduler, attached by GenesisCore

    def allowed_roots(self) -> list[Path]:
        roots = default_allowed_roots(self.settings.get("security.extra_allowed_roots", []) or [])
        for p in self.repos.projects.list():
            try:
                roots.append(Path(p["path"]).resolve())
            except OSError:
                pass
        return roots

    def notify(self, title: str, message: str) -> None:
        self.events.emit(EventType.NOTIFICATION, title=title, message=message)


def build_container(config: GenesisConfig, *, backend: Optional[PlatformBackend] = None, secrets: Optional[SecretStore] = None,
                    llm: Optional[LLMProvider] = None, db: Optional[Database] = None, events: Optional[EventBus] = None,
                    http_session: Any = None, embedder_factory: Optional[Callable] = None,
                    apps: Optional[AppResolver] = None) -> ServiceContainer:
    config.paths.ensure()
    events = events or EventBus()
    db = db or Database(config.paths.db_path)
    repos = Repositories(db)
    settings = Settings(repos.settings)
    secrets = secrets or create_secret_store(config.paths.credentials_dir)

    def _llm_status(status: dict) -> None:
        events.emit(EventType.LLM_STATUS, **status)

    if llm is None:
        llm = ResilientLLM(build_provider(config), status_callback=_llm_status)

    def _google_changed(connected: bool, email: Optional[str]) -> None:
        repos.services.set("google", "connected" if connected else "disconnected", account=email)
        events.emit(EventType.SERVICE_CHANGED, service="google", connected=connected, account=email)

    client = load_client_config(config.google_client_id, config.google_client_secret, config.google_client_secrets_file)
    google = GoogleAuth(client, secrets, session=http_session, on_change=_google_changed)
    gmail = GmailService(google)
    calendar = CalendarService(google)

    embedder_factory = embedder_factory or (lambda: create_embedder(config.embedding_provider, config.embedding_model, config.hf_token))
    knowledge = KnowledgeBase(db, embedder_factory)
    memory = MemoryManager(repos, settings)
    permissions = PermissionManager(repos.permissions, settings, repos.security_events)
    return ServiceContainer(
        config=config, db=db, repos=repos, settings=settings, events=events,
        backend=backend or create_backend(config.system_dry_run), apps=apps or AppResolver(), secrets=secrets, llm=llm,
        google=google, gmail=gmail, calendar=calendar, knowledge=knowledge, memory=memory, permissions=permissions,
        confirmations=ConfirmationManager(), browser=BrowserController(config.paths.data_dir / "browser-profile"),
    )
