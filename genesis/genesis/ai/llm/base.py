"""Provider-agnostic LLM interface."""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any, Optional


class LLMError(RuntimeError):
    """Any provider failure."""


class LLMUnavailable(LLMError):
    """Provider not configured or unreachable (network down, no token...)."""


class LLMAuthError(LLMError):
    """Credentials rejected."""


Message = dict  # {"role": "system"|"user"|"assistant", "content": str}


class LLMProvider(ABC):
    name: str = "base"
    is_remote: bool = True  # shown on the privacy page
    is_generative: bool = True  # False for the offline rule-based fallback

    @abstractmethod
    def chat(self, messages: list[Message], *, temperature: Optional[float] = None, max_tokens: int = 700,
             json_mode: bool = False) -> str:
        """Return the assistant's reply text."""

    def is_configured(self) -> bool:
        return True

    def describe(self) -> dict:
        return {"name": self.name, "remote": self.is_remote, "configured": self.is_configured()}

    def chat_json(self, messages: list[Message], *, max_tokens: int = 700, temperature: float = 0.1) -> Any:
        text = self.chat(messages, temperature=temperature, max_tokens=max_tokens, json_mode=True)
        data = extract_json(text)
        if data is None:
            raise LLMError("Model did not return valid JSON.")
        return data


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> Optional[Any]:
    """Pull the first JSON object/array out of model output (tolerates code fences and prose)."""
    if not text:
        return None
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        try:
            return json.loads(cand)
        except ValueError:
            pass
        for open_ch, close_ch in (("{", "}"), ("[", "]")):
            start = cand.find(open_ch)
            while start != -1:
                depth, in_str, esc = 0, False, False
                for i in range(start, len(cand)):
                    ch = cand[i]
                    if in_str:
                        if esc:
                            esc = False
                        elif ch == "\\":
                            esc = True
                        elif ch == '"':
                            in_str = False
                        continue
                    if ch == '"':
                        in_str = True
                    elif ch == open_ch:
                        depth += 1
                    elif ch == close_ch:
                        depth -= 1
                        if depth == 0:
                            try:
                                return json.loads(cand[start:i + 1])
                            except ValueError:
                                break
                start = cand.find(open_ch, start + 1)
    return None
