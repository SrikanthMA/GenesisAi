"""Embedding providers for the local knowledge base (RAG).

* ``SentenceTransformerEmbedder`` – all-MiniLM-L6-v2 running locally (optional dependency)
* ``HuggingFaceEmbedder``         – same model via Hugging Face Inference (remote)
* ``HashingEmbedder``             – dependency-light local fallback (hashed word + char n-grams)

All return L2-normalised float32 vectors so cosine similarity is a dot product.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
import requests

from ..core.logging_setup import get_logger

log = get_logger("embeddings")


def _normalize(m: np.ndarray) -> np.ndarray:
    m = np.asarray(m, dtype=np.float32)
    if m.ndim == 1:
        m = m[None, :]
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return m / norms


class EmbeddingProvider(ABC):
    name = "base"
    is_remote = False

    @abstractmethod
    def embed(self, texts: list[str]) -> np.ndarray:
        ...


class HashingEmbedder(EmbeddingProvider):
    name = "hashing-1024"

    def __init__(self, n_features: int = 1024) -> None:
        from sklearn.feature_extraction.text import HashingVectorizer

        self._word = HashingVectorizer(n_features=n_features, ngram_range=(1, 2), alternate_sign=False, norm=None,
                                       stop_words="english")
        self._char = HashingVectorizer(n_features=n_features, analyzer="char_wb", ngram_range=(3, 4), alternate_sign=False,
                                       norm=None)

    def embed(self, texts: list[str]) -> np.ndarray:
        w = self._word.transform(texts).toarray()
        c = self._char.transform(texts).toarray()
        w = np.log1p(w)
        c = np.log1p(c) * 0.5
        return _normalize(np.hstack([w, c]))


class SentenceTransformerEmbedder(EmbeddingProvider):
    def __init__(self, model_id: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # type: ignore

        self.model = SentenceTransformer(model_id)
        self.name = f"st:{model_id}"

    def embed(self, texts: list[str]) -> np.ndarray:
        return _normalize(self.model.encode(texts, batch_size=32, show_progress_bar=False))


class HuggingFaceEmbedder(EmbeddingProvider):
    is_remote = True

    def __init__(self, token: str, model_id: str = "sentence-transformers/all-MiniLM-L6-v2", timeout: float = 30.0) -> None:
        self.token = token
        self.model_id = model_id
        self.timeout = timeout
        self.name = f"hf:{model_id}"
        self.url = f"https://router.huggingface.co/hf-inference/models/{model_id}/pipeline/feature-extraction"

    def embed(self, texts: list[str]) -> np.ndarray:
        resp = requests.post(self.url, headers={"Authorization": f"Bearer {self.token}"}, json={"inputs": texts},
                             timeout=self.timeout)
        resp.raise_for_status()
        arr = np.asarray(resp.json(), dtype=np.float32)
        if arr.ndim == 3:  # token embeddings -> mean pool
            arr = arr.mean(axis=1)
        return _normalize(arr)


def create_embedder(kind: str = "auto", model_id: str = "sentence-transformers/all-MiniLM-L6-v2",
                    hf_token: Optional[str] = None) -> EmbeddingProvider:
    if kind in {"auto", "sentence_transformers"}:
        try:
            return SentenceTransformerEmbedder(model_id)
        except Exception as exc:
            if kind == "sentence_transformers":
                log.warning("sentence-transformers unavailable (%s); using hashing embedder", exc)
    if kind == "huggingface" and hf_token:
        return HuggingFaceEmbedder(hf_token, model_id)
    return HashingEmbedder()
