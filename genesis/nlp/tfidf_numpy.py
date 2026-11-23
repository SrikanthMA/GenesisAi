"""TF-IDF + multinomial logistic regression implemented from scratch with NumPy.

Used when scikit-learn can't be loaded — for example when Windows Smart App Control / an Application Control policy blocks
scikit-learn's compiled extension modules ("DLL load failed … An Application Control policy has blocked this file").
It reproduces the scikit-learn pipeline Genesis normally uses, so accuracy is the same:

* word features: tokens of 2+ word characters, uni- and bi-grams  (TfidfVectorizer(ngram_range=(1, 2)))
* char features: character 3–5-grams inside word boundaries, each word padded with spaces (analyzer="char_wb")
* sublinear tf  = 1 + ln(tf);  smooth idf = ln((1 + N) / (1 + df)) + 1;  each block L2-normalised, then concatenated
* softmax regression  P(k|x) = exp(w_k·x + b_k) / Σ_j exp(w_j·x + b_j), trained by minimising
  Σ_i −ln P(y_i|x_i) + ‖W‖² / (2C)   (the same objective as LogisticRegression(C=12)) with full-batch Adam.
"""
from __future__ import annotations

import math
import re
from collections import Counter

import numpy as np

_TOKEN = re.compile(r"(?u)\b\w\w+\b")
_WS = re.compile(r"\s\s+")


def word_ngrams(text: str) -> list[str]:
    toks = _TOKEN.findall(text.lower())
    return toks + [f"{a} {b}" for a, b in zip(toks, toks[1:])]


def char_wb_ngrams(text: str, lo: int = 3, hi: int = 5) -> list[str]:
    out: list[str] = []
    for w in _WS.sub(" ", text.lower()).split():
        w = f" {w} "
        for n in range(lo, hi + 1):
            if len(w) < n:
                out.append(w)
                continue
            out.extend(w[i:i + n] for i in range(len(w) - n + 1))
    return out


class _Tfidf:
    def __init__(self, analyzer) -> None:
        self.analyzer = analyzer
        self.vocab: dict[str, int] = {}
        self.idf: np.ndarray = np.zeros(0)

    def fit_transform(self, docs: list[str]) -> np.ndarray:
        grams = [Counter(self.analyzer(d)) for d in docs]
        df: Counter = Counter()
        for g in grams:
            df.update(g.keys())
        self.vocab = {t: i for i, t in enumerate(sorted(df))}
        n = len(docs)
        self.idf = np.array([math.log((1 + n) / (1 + df[t])) + 1.0 for t in sorted(df)])
        return np.vstack([self._row(g) for g in grams]) if docs else np.zeros((0, len(self.vocab)))

    def transform(self, docs: list[str]) -> np.ndarray:
        return np.vstack([self._row(Counter(self.analyzer(d))) for d in docs])

    def _row(self, counts: Counter) -> np.ndarray:
        v = np.zeros(len(self.vocab))
        for t, c in counts.items():
            j = self.vocab.get(t)
            if j is not None:
                v[j] = (1.0 + math.log(c)) * self.idf[j]
        norm = np.linalg.norm(v)
        return v / norm if norm > 0 else v


class NumpyTfidfLogReg:
    """Drop-in replacement for the scikit-learn pipeline: fit(texts, labels), predict_proba([text]), classes_."""

    def __init__(self, C: float = 12.0, iterations: int = 400, lr: float = 0.08, seed: int = 0) -> None:
        self.C, self.iterations, self.lr, self.seed = C, iterations, lr, seed
        self.word = _Tfidf(word_ngrams)
        self.char = _Tfidf(char_wb_ngrams)
        self.classes_: list[str] = []
        self.W = np.zeros((0, 0))
        self.b = np.zeros(0)

    def _features(self, texts: list[str], fit: bool = False) -> np.ndarray:
        if fit:
            return np.hstack([self.word.fit_transform(texts), self.char.fit_transform(texts)])
        return np.hstack([self.word.transform(texts), self.char.transform(texts)])

    def fit(self, texts: list[str], labels: list[str]) -> "NumpyTfidfLogReg":
        X = self._features(texts, fit=True)
        self.classes_ = sorted(set(labels))
        idx = {c: i for i, c in enumerate(self.classes_)}
        Y = np.zeros((len(labels), len(self.classes_)))
        Y[np.arange(len(labels)), [idx[lbl] for lbl in labels]] = 1.0
        n, d = X.shape
        k = len(self.classes_)
        W, b = np.zeros((d, k)), np.zeros(k)
        mW, vW, mb, vb = np.zeros_like(W), np.zeros_like(W), np.zeros_like(b), np.zeros_like(b)
        beta1, beta2, eps = 0.9, 0.999, 1e-8
        for t in range(1, self.iterations + 1):
            P = self._softmax(X @ W + b)
            G = P - Y  # gradient of the summed cross-entropy w.r.t. the scores
            gW = X.T @ G + W / self.C
            gb = G.sum(axis=0)
            for g, m, v, p in ((gW, mW, vW, W), (gb, mb, vb, b)):
                m *= beta1
                m += (1 - beta1) * g
                v *= beta2
                v += (1 - beta2) * g * g
                p -= self.lr * (m / (1 - beta1 ** t)) / (np.sqrt(v / (1 - beta2 ** t)) + eps)
        self.W, self.b = W, b
        return self

    @staticmethod
    def _softmax(Z: np.ndarray) -> np.ndarray:
        Z = Z - Z.max(axis=1, keepdims=True)
        E = np.exp(Z)
        return E / E.sum(axis=1, keepdims=True)

    def predict_proba(self, texts: list[str]) -> np.ndarray:
        return self._softmax(self._features(texts) @ self.W + self.b)
