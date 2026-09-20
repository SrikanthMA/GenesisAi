import logging
from collections.abc import AsyncIterator

from huggingface_hub import AsyncInferenceClient

from app.ai.llm.base import ChatMessage, LLMProvider

logger = logging.getLogger(__name__)


class HuggingFaceProvider(LLMProvider):
    """Cloud inference via Hugging Face Inference Providers.

    Requires no local GPU - this is the Phase 1 default so Genesis is usable
    on any developer machine before local/quantized inference is wired up.
    """

    def __init__(self, model_id: str, token: str | None = None, max_tokens: int = 1024) -> None:
        self._model_id = model_id
        self._max_tokens = max_tokens
        self._client = AsyncInferenceClient(model=model_id, token=token)

    async def stream(self, messages: list[ChatMessage], **kwargs: object) -> AsyncIterator[str]:
        max_tokens_arg = kwargs.get("max_tokens", self._max_tokens)
        max_tokens = max_tokens_arg if isinstance(max_tokens_arg, int) else self._max_tokens
        payload = [{"role": m.role, "content": m.content} for m in messages]

        try:
            completion_stream = await self._client.chat_completion(
                messages=payload, stream=True, max_tokens=max_tokens
            )
            async for chunk in completion_stream:
                delta = chunk.choices[0].delta.content
                if delta:
                    yield delta
        except Exception:
            logger.exception("HuggingFace streaming call failed (model=%s)", self._model_id)
            raise
