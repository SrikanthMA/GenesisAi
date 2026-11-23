"""Intent-routing evaluation (run: python -m eval.run_intent_eval [--json out.json]).

1. Offline NLU pipeline (rules → typo repair → classifier, no LLM) on the held-out set, per category.
2. Classifier alone on held-out examples whose label it knows: accuracy, macro-P/R/F1, calibration (ECE).
3. Threshold policy: chosen on 5-fold out-of-fold predictions of the *training* data, then evaluated once on held-out data.
4. Alternative classifiers on the same split: char-n-gram kNN, hashing-embedding kNN (and sentence-transformers if installed).
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np

from genesis.nlp.classifier import IntentClassifier, load_training_data
from genesis.nlp.intents import Intent
from genesis.nlp.pipeline import NLUPipeline

HELD = Path(__file__).parent / "data" / ("intent_heldout_b.jsonl" if "--set-b" in sys.argv else "intent_heldout.jsonl")
NO_ACTION = {"conversation", "information_request", "unknown", "search_web", "goal"}
ACTING = {i.value for i in Intent} - NO_ACTION - {"cancel", "ui_control"}


def load_heldout() -> list[dict]:
    return [json.loads(line) for line in HELD.read_text(encoding="utf-8").splitlines() if line.strip()]


def ok(pred: str, row: dict) -> bool:
    if "allowed" in row:
        return pred in row["allowed"]
    exp = row["expected"]
    return pred in NO_ACTION if exp == "NO_ACTION" else pred == exp


# ----------------------------------------------------------------------------------------------- 1. pipeline
def pipeline_eval(pipe: NLUPipeline, rows: list[dict]) -> dict:
    per_cat: dict[str, list[bool]] = defaultdict(list)
    failures, lat = [], []
    unsafe = []  # an acting intent where no action (or a different action) was expected
    for row in rows:
        t0 = time.perf_counter()
        parts = pipe.split(row["text"])
        preds = []
        for p in parts:
            r = p if not isinstance(p, str) else pipe.parse(p)
            preds.append(r.intent.value)
        lat.append((time.perf_counter() - t0) * 1000)
        if isinstance(row.get("expected"), list):
            good = preds == row["expected"]
            shown = preds
        else:
            good = len(preds) == 1 and ok(preds[0], row)
            shown = preds[0] if len(preds) == 1 else preds
            exp = row.get("expected")
            if exp == "NO_ACTION" and any(p in ACTING for p in preds):
                unsafe.append((row["text"], shown))
        per_cat[row["category"]].append(good)
        if not good:
            failures.append((row["category"], row["text"], row.get("expected", row.get("allowed")), shown))
    total = sum(sum(v) for v in per_cat.values()), sum(len(v) for v in per_cat.values())
    return {"accuracy": total[0] / total[1], "n": total[1],
            "per_category": {k: {"acc": round(sum(v) / len(v), 3), "n": len(v)} for k, v in per_cat.items()},
            "unsafe_actions": unsafe, "failures": failures,
            "latency_ms_p50": float(np.percentile(lat, 50)), "latency_ms_p95": float(np.percentile(lat, 95))}


# ----------------------------------------------------------------------------------------------- 2./3. classifier
def macro_prf(y_true: list[str], y_pred: list[str]) -> tuple[float, float, float]:
    labels = sorted(set(y_true))
    ps, rs, fs = [], [], []
    for lab in labels:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == lab and p != lab)
        p = tp / (tp + fp) if tp + fp else 0.0
        r = tp / (tp + fn) if tp + fn else 0.0
        ps.append(p)
        rs.append(r)
        fs.append(2 * p * r / (p + r) if p + r else 0.0)
    return float(np.mean(ps)), float(np.mean(rs)), float(np.mean(fs))


def ece(conf: list[float], correct: list[bool], bins: int = 10) -> float:
    conf_a, cor = np.array(conf), np.array(correct, dtype=float)
    total = 0.0
    for b in range(bins):
        lo, hi = b / bins, (b + 1) / bins
        m = (conf_a > lo) & (conf_a <= hi)
        if m.any():
            total += m.mean() * abs(conf_a[m].mean() - cor[m].mean())
    return float(total)


def oof_predictions(texts: list[str], labels: list[str], k: int = 5) -> list[tuple[str, float, float, str]]:
    """(label, p1, margin, pred) for every training utterance, predicted by a model that never saw it."""
    from sklearn.model_selection import StratifiedKFold

    out: list = [None] * len(texts)
    for tr, te in StratifiedKFold(k, shuffle=True, random_state=7).split(texts, labels):
        clf = _fit([texts[i] for i in tr], [labels[i] for i in tr])
        for i in te:
            (p1l, p1), (_, p2) = clf.predict(texts[i])[:2]
            out[i] = (labels[i], p1, p1 - p2, p1l.value)
    return out


def _fit(texts: list[str], labels: list[str]) -> IntentClassifier:
    clf = IntentClassifier.__new__(IntentClassifier)
    clf.texts, clf.labels, clf._model, clf._backend = texts, labels, None, "jaccard"
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import FeatureUnion, Pipeline

    feats = FeatureUnion([("word", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True)),
                          ("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True))])
    clf._model = Pipeline([("features", feats), ("clf", LogisticRegression(C=12.0, max_iter=2000))]).fit(texts, labels)
    clf._backend = "tfidf-logreg"
    return clf


def policy_table(points: list[tuple[str, float, float, str]]) -> list[dict]:
    rows = []
    for t in (0.30, 0.45, 0.60):
        for m in (0.0, 0.10, 0.20, 0.30):
            acc = [lab == pred for lab, p1, mg, pred in points if p1 >= t and mg >= m]
            rows.append({"min_prob": t, "min_margin": m, "coverage": round(len(acc) / len(points), 3),
                         "precision": round(sum(acc) / len(acc), 3) if acc else None})
    return rows


def classifier_eval(clf: IntentClassifier, rows: list[dict]) -> dict:
    known = set(clf.labels)
    sub = [r for r in rows if isinstance(r.get("expected"), str) and r["expected"] in known
           and r["category"] in ("paraphrase", "typo", "asr", "short")]
    y, yhat, conf, margin = [], [], [], []
    for r in sub:
        (l1, p1), (_, p2) = clf.predict(r["text"].lower())[:2]
        y.append(r["expected"])
        yhat.append(l1.value)
        conf.append(p1)
        margin.append(p1 - p2)
    correct = [a == b for a, b in zip(y, yhat)]
    p, rc, f = macro_prf(y, yhat)
    by_cat = defaultdict(list)
    for r, c in zip(sub, correct):
        by_cat[r["category"]].append(c)
    return {"n": len(sub), "accuracy": float(np.mean(correct)), "macro_p": p, "macro_r": rc, "macro_f1": f,
            "ece": ece(conf, correct), "mean_conf": float(np.mean(conf)),
            "per_category": {k: round(float(np.mean(v)), 3) for k, v in by_cat.items()},
            "confusions": Counter((a, b) for a, b in zip(y, yhat) if a != b).most_common(8),
            "points": list(zip(y, conf, margin, yhat))}


# ----------------------------------------------------------------------------------------------- 4. alternatives
def knn_eval(name: str, embed, train_t: list[str], train_y: list[str], rows: list[dict]) -> dict:
    X = embed(train_t)
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    known = set(train_y)
    sub = [r for r in rows if isinstance(r.get("expected"), str) and r["expected"] in known
           and r["category"] in ("paraphrase", "typo", "asr", "short")]
    Q = embed([r["text"].lower() for r in sub])
    Q = Q / (np.linalg.norm(Q, axis=1, keepdims=True) + 1e-9)
    t0 = time.perf_counter()
    sims = Q @ X.T
    preds = []
    for row in sims:
        top = np.argsort(-row)[:5]
        votes = Counter()
        for j in top:
            votes[train_y[j]] += float(row[j])
        preds.append(votes.most_common(1)[0][0])
    ms = (time.perf_counter() - t0) * 1000 / max(1, len(sub))
    y = [r["expected"] for r in sub]
    _, _, f = macro_prf(y, preds)
    return {"model": name, "accuracy": float(np.mean([a == b for a, b in zip(y, preds)])), "macro_f1": f, "ms_per_query": ms}


def alternatives(train_t, train_y, rows) -> list[dict]:
    out = []
    from genesis.nlp.tfidf_numpy import _Tfidf, char_wb_ngrams

    ch = _Tfidf(char_wb_ngrams)
    ch.fit_transform(train_t)
    out.append(knn_eval("char 3–5-gram TF-IDF kNN (k=5)", lambda ts: ch.transform(ts), train_t, train_y, rows))
    from genesis.memory.embeddings import HashingEmbedder

    he = HashingEmbedder()
    out.append(knn_eval("hashing embedding kNN (k=5)", lambda ts: np.array(he.embed(ts)), train_t, train_y, rows))
    try:  # only if the optional dependency is installed
        from sentence_transformers import SentenceTransformer  # type: ignore

        st = SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")
        out.append(knn_eval("MiniLM sentence-embedding kNN (k=5)", lambda ts: st.encode(ts), train_t, train_y, rows))
    except Exception as exc:
        out.append({"model": "MiniLM sentence-embedding kNN", "skipped": f"not available here ({type(exc).__name__})"})
    return out


def main() -> dict:
    rows = load_heldout()
    texts, labels = load_training_data()
    clf = IntentClassifier()
    pipe = NLUPipeline(None)
    res = {"heldout_n": len(rows), "classifier_backend": clf.backend}
    res["pipeline"] = pipeline_eval(pipe, rows)
    ce = classifier_eval(clf, rows)
    res["classifier"] = {k: v for k, v in ce.items() if k != "points"}
    oof = oof_predictions(texts, labels)
    res["policy_on_training_oof"] = policy_table(oof)
    res["policy_on_heldout"] = policy_table([(a, b, c, d) for a, b, c, d in ce["points"]])
    res["oof_ece"] = ece([p for _, p, _, _ in oof], [a == d for a, _, _, d in oof])
    res["alternatives"] = [{"model": "TF-IDF word+char + LogReg (current)", "accuracy": ce["accuracy"], "macro_f1": ce["macro_f1"]}] + \
        alternatives(texts, labels, rows)
    return res


if __name__ == "__main__":
    r = main()
    if "--json" in sys.argv:
        Path(sys.argv[sys.argv.index("--json") + 1]).write_text(json.dumps(r, indent=1, default=str))
    p = r["pipeline"]
    print(f"held-out examples: {r['heldout_n']}  (classifier backend: {r['classifier_backend']})")
    print(f"\nOFFLINE PIPELINE accuracy {p['accuracy']:.3f}  p50 {p['latency_ms_p50']:.2f} ms  p95 {p['latency_ms_p95']:.2f} ms")
    for k, v in p["per_category"].items():
        print(f"   {k:16} {v['acc']:.3f}  (n={v['n']})")
    print(f"   unsafe actions on no-action inputs: {len(p['unsafe_actions'])} {p['unsafe_actions']}")
    c = r["classifier"]
    print(f"\nCLASSIFIER on held-out (n={c['n']}): acc {c['accuracy']:.3f}  macro-P {c['macro_p']:.3f}  macro-R {c['macro_r']:.3f}  "
          f"macro-F1 {c['macro_f1']:.3f}  ECE {c['ece']:.3f}  mean confidence {c['mean_conf']:.3f}")
    print(f"   per category: {c['per_category']}")
    print(f"   top confusions: {c['confusions']}")
    print(f"   ECE on training out-of-fold predictions: {r['oof_ece']:.3f}")
    print("\nTHRESHOLD POLICY  (precision of accepted predictions / share accepted)")
    print("   min_prob min_margin | train-OOF prec cov | held-out prec cov")
    for a, b in zip(r["policy_on_training_oof"], r["policy_on_heldout"]):
        print(f"   {a['min_prob']:.2f}     {a['min_margin']:.2f}       | {a['precision']}  {a['coverage']} | {b['precision']}  {b['coverage']}")
    print("\nALTERNATIVES (same held-out subset)")
    for a in r["alternatives"]:
        print("  ", a)
    print("\nPIPELINE FAILURES")
    for f in p["failures"]:
        print("  ", f)
