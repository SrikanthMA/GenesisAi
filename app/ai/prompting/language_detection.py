from langdetect import DetectorFactory, LangDetectException, detect_langs

from app.ai.prompting.languages import SUPPORTED_LANGUAGES

# langdetect samples randomly internally; pin the seed so the same input
# always yields the same result. Without this, tests (and users) would see
# language detection flip-flop between runs.
DetectorFactory.seed = 0

MIN_CONFIDENCE = 0.5


class LanguageDetectionService:
    """Best-effort language detection for a single message of user text.

    Returns None on empty input, undetectable input, low-confidence
    detection, or a language outside SUPPORTED_LANGUAGES - callers must treat
    None as "unknown" and fall back accordingly rather than guessing. This is
    not accurate on very short strings (a couple of words); that's a known
    limitation of statistical language detection, not something this class
    can paper over.
    """

    def detect(self, text: str) -> str | None:
        text = text.strip()
        if not text:
            return None

        try:
            candidates = detect_langs(text)
        except LangDetectException:
            return None

        if not candidates:
            return None

        best = candidates[0]
        if best.prob < MIN_CONFIDENCE:
            return None

        return best.lang if best.lang in SUPPORTED_LANGUAGES else None
