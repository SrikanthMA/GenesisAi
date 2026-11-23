"""Statistical intent classifier (TF-IDF features + logistic regression).

This is the classic supervised approach to intent detection used in
task-oriented dialogue systems: bag-of-n-grams features (word 1–2-grams plus
character 3–5-grams for robustness to typos and speech-recognition errors)
fed to a linear classifier. It is trained in well under a second at startup
from ``nlp/data/intents.json`` and runs fully offline.

If scikit-learn can't be loaded (not installed, or its DLLs blocked by Windows Smart App Control), the same model is
trained with Genesis's own NumPy implementation (``tfidf_numpy.py``, identical features and objective, same accuracy).
Only if NumPy is unavailable too does it fall back to nearest-neighbour token overlap (Jaccard).
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Optional

from ..core.logging_setup import get_logger
from .intents import Intent

log = get_logger("nlp.classifier")
DATA_FILE = Path(__file__).parent / "data" / "intents.json"


def load_training_data(path: Path = DATA_FILE) -> tuple[list[str], list[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    texts, labels = [], []
    for intent, examples in data.items():
        for ex in examples:
            texts.append(ex.lower())
            labels.append(intent)
    return texts, labels


class IntentClassifier:
    def __init__(self, data_path: Path = DATA_FILE) -> None:
        self.texts, self.labels = load_training_data(data_path)
        self._model = None
        self._backend = "jaccard"
        try:
            from sklearn.feature_extraction.text import TfidfVectorizer
            from sklearn.linear_model import LogisticRegression
            from sklearn.pipeline import FeatureUnion, Pipeline

            features = FeatureUnion([
                ("word", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True, min_df=1)),
                ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True, min_df=1)),
            ])
            self._model = Pipeline([("features", features), ("clf", LogisticRegression(C=12.0, max_iter=2000))])
            self._model.fit(self.texts, self.labels)
            self._backend = "tfidf-logreg"
        except Exception as exc:  # pragma: no cover - depends on environment
            self._fallback(exc)

    def _fallback(self, exc: BaseException) -> None:
        try:
            from .tfidf_numpy import NumpyTfidfLogReg

            self._model = NumpyTfidfLogReg(C=12.0, iterations=100, lr=0.18).fit(self.texts, self.labels)
            self._backend = "numpy-tfidf-logreg"
            log.info("scikit-learn can't be loaded (%s) — using Genesis's built-in NumPy TF-IDF + logistic regression "
                     "(same features, same accuracy)", str(exc).split("\n")[0][:160])
        except Exception as exc2:  # pragma: no cover
            self._model = None
            log.warning("scikit-learn and NumPy unavailable (%s; %s); using Jaccard nearest-neighbour classifier", exc, exc2)

    @property
    def backend(self) -> str:
        return self._backend

    def predict(self, text: str, top_k: int = 3) -> list[tuple[Intent, float]]:
        t = (text or "").lower().strip()
        if not t:
            return [(Intent.UNKNOWN, 0.0)]
        if self._model is not None:
            probs = self._model.predict_proba([t])[0]
            classes = self._model.classes_
            ranked = sorted(zip(classes, probs), key=lambda x: -x[1])[:top_k]
            return [(Intent(c), float(p)) for c, p in ranked]
        return self._jaccard(t, top_k)

    def _jaccard(self, t: str, top_k: int) -> list[tuple[Intent, float]]:
        toks = set(re.findall(r"\w+", t))
        best: dict[str, float] = {}
        for ex, label in zip(self.texts, self.labels):
            et = set(re.findall(r"\w+", ex))
            if not et:
                continue
            score = len(toks & et) / len(toks | et)
            best[label] = max(best.get(label, 0.0), score)
        ranked = sorted(best.items(), key=lambda x: -x[1])[:top_k]
        return [(Intent(lbl), s) for lbl, s in ranked] or [(Intent.UNKNOWN, 0.0)]


_instance: Optional[IntentClassifier] = None


def get_classifier() -> IntentClassifier:
    global _instance
    if _instance is None:
        _instance = IntentClassifier()
    return _instance
