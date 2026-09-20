from pydantic import BaseModel, field_validator

from app.ai.prompting.languages import is_valid_preference


class LanguageInfo(BaseModel):
    code: str
    name: str


class LanguagePreferenceUpdate(BaseModel):
    language: str

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        if not is_valid_preference(value):
            raise ValueError(f"Unsupported language code: {value!r}")
        return value
