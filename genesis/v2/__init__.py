"""Genesis 2.0 add-on layer.

Everything new in 2.0 lives here so the 1.x code stays as it was. The existing modules call into this package through a few
small, clearly marked hooks (search the code base for ``v2 hook``):

* ``nlu``      — phrase canonicalisation, precise overrides for known misparses, safer request splitting, dependency-aware
                 routing of multi-step goals to the planner.
* ``policy``   — approval binding, duplicate-action guard, retry rules, LLM-plan dependency check, whole-workflow review.
* ``tasks``    — persistent task journal (typed states, validated transitions, checkpoints, resume after restart).
* ``errors``   — structured error categories for every failed step.
* ``cancel``   — cancellation of a running multi-step task.
* ``tools``    — list / clear the task journal.        * ``voice`` — honest notice when no voice exists for a language.

(Classifier calibration and margin thresholds were *measured* — docs/GENESIS_EVALUATION.md — but not deployed: they did not
change routing quality enough to justify changing behaviour.)
"""


def _tune_regex_cache() -> None:
    """Performance fix. Importing scikit-learn pulls in ``numpy.f2py.crackfortran``, which sets the *global*
    ``re._MAXCACHE = 50``. The rule parser uses several hundred inline regular expressions, so with a 50-entry cache nearly
    every request recompiled them (measured: ~40–170 ms per utterance). Restoring a large cache makes parsing ~0.6 ms.
    Called at import and again before every parse (one integer comparison), because the library can shrink it later."""
    import re

    try:
        if getattr(re, "_MAXCACHE", 0) < 4096:
            re._MAXCACHE = 4096  # type: ignore[attr-defined]
        if hasattr(re, "_MAXCACHE2") and re._MAXCACHE2 < 1024:  # Python 3.12+ keeps a second, smaller LRU cache
            re._MAXCACHE2 = 1024  # type: ignore[attr-defined]
    except Exception:  # pragma: no cover - private attribute; a different Python simply keeps its default
        pass


_tune_regex_cache()
ensure_regex_cache = _tune_regex_cache
