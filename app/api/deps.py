from app.ai.llm.base import LLMProvider
from app.ai.llm.huggingface_provider import HuggingFaceProvider
from app.ai.orchestration.orchestrator import AIOrchestrator
from app.ai.prompting.language_detection import LanguageDetectionService
from app.core.config import get_settings
from app.database.session import get_db

_llm_provider: LLMProvider | None = None
_language_detector = LanguageDetectionService()


def get_llm_provider() -> LLMProvider:
    global _llm_provider
    if _llm_provider is None:
        settings = get_settings()
        _llm_provider = HuggingFaceProvider(model_id=settings.hf_model_id, token=settings.hf_token)
    return _llm_provider


def get_orchestrator() -> AIOrchestrator:
    return AIOrchestrator(llm_provider=get_llm_provider())


def get_language_detector() -> LanguageDetectionService:
    return _language_detector


__all__ = ["get_db", "get_llm_provider", "get_orchestrator", "get_language_detector"]
