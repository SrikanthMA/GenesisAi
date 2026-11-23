"""Reproducible NLU evaluation used in the CO1 report.

    python scripts/evaluate_nlu.py

1. 5-fold stratified cross-validation of the intent classifier (word n-grams vs word+char n-grams).
2. Rule-layer accuracy on the specification's command set (tests/test_nlu.py).
3. Per-intent F1 table.
"""
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
warnings.filterwarnings("ignore")

from sklearn.feature_extraction.text import TfidfVectorizer  # noqa: E402
from sklearn.linear_model import LogisticRegression  # noqa: E402
from sklearn.metrics import accuracy_score, classification_report, f1_score  # noqa: E402
from sklearn.model_selection import StratifiedKFold, cross_val_predict  # noqa: E402
from sklearn.pipeline import FeatureUnion, Pipeline  # noqa: E402

from genesis.nlp import rules  # noqa: E402
from genesis.nlp.classifier import load_training_data  # noqa: E402


def model(char: bool) -> Pipeline:
    feats = [("word", TfidfVectorizer(ngram_range=(1, 2), sublinear_tf=True))]
    if char:
        feats.append(("char", TfidfVectorizer(analyzer="char_wb", ngram_range=(3, 5), sublinear_tf=True)))
    return Pipeline([("features", FeatureUnion(feats)), ("clf", LogisticRegression(C=12.0, max_iter=2000))])


def main() -> None:
    X, y = load_training_data()
    print(f"Dataset: {len(X)} utterances, {len(set(y))} intents\n")
    cv = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    best = None
    for name, char in (("TF-IDF word 1-2 grams", False), ("TF-IDF word 1-2 + char 3-5 grams", True)):
        pred = cross_val_predict(model(char), X, y, cv=cv)
        print(f"{name:<34} accuracy={accuracy_score(y, pred):.3f}  macro-F1={f1_score(y, pred, average='macro'):.3f}")
        best = pred
    print("\nPer-intent report (word+char):")
    print(classification_report(y, best, digits=2))

    from tests.test_nlu import TestIntentRules

    cases = TestIntentRules.CASES
    ok = sum(1 for t, i, _ in cases if (r := rules.parse(t)) is not None and r.intent == i)
    print(f"Rule layer on specification commands: {ok}/{len(cases)} correct intents")


if __name__ == "__main__":
    main()
