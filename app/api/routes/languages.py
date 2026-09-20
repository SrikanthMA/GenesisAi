from fastapi import APIRouter

from app.ai.prompting.languages import SUPPORTED_LANGUAGES
from app.schemas.language import LanguageInfo

router = APIRouter(prefix="/languages", tags=["languages"])


@router.get("", response_model=list[LanguageInfo])
def list_languages() -> list[LanguageInfo]:
    return [LanguageInfo(code=code, name=name) for code, name in SUPPORTED_LANGUAGES.items()]
