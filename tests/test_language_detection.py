from app.ai.prompting.language_detection import LanguageDetectionService

# Verified against langdetect directly before writing this test - each sample
# detects as its expected language with >0.99 confidence.
SAMPLES = {
    "en": "The quick brown fox jumps over the lazy dog and runs into the forest.",
    "hi": "यह एक परीक्षण वाक्य है जो हिंदी भाषा में लिखा गया है।",
    "kn": "ಇದು ಕನ್ನಡ ಭಾಷೆಯಲ್ಲಿ ಬರೆಯಲಾದ ಒಂದು ಪರೀಕ್ಷಾ ವಾಕ್ಯವಾಗಿದೆ.",
    "te": "ఇది తెలుగు భాషలో వ్రాయబడిన ఒక పరీక్ష వాక్యం.",
    "ta": "இது தமிழ் மொழியில் எழுதப்பட்ட ஒரு சோதனை வாக்கியம் ஆகும்.",
    "ml": "ഇത് മലയാളം ഭാഷയിൽ എഴുതിയ ഒരു പരീക്ഷണ വാക്യമാണ്.",
    "mr": "हे मराठी भाषेत लिहिलेले एक चाचणी वाक्य आहे.",
    "bn": "এটি বাংলা ভাষায় লেখা একটি পরীক্ষামূলক বাক্য।",
    "gu": "આ ગુજરાતી ભાષામાં લખાયેલું એક પરીક્ષણ વાક્ય છે.",
    "pa": "ਇਹ ਪੰਜਾਬੀ ਭਾਸ਼ਾ ਵਿੱਚ ਲਿਖਿਆ ਗਿਆ ਇੱਕ ਟੈਸਟ ਵਾਕ ਹੈ।",
    "ur": "یہ اردو زبان میں لکھا گیا ایک ٹیسٹ جملہ ہے۔",
}


def test_detects_each_supported_language() -> None:
    service = LanguageDetectionService()
    for expected_code, text in SAMPLES.items():
        assert service.detect(text) == expected_code


def test_empty_input_returns_none() -> None:
    service = LanguageDetectionService()
    assert service.detect("") is None
    assert service.detect("   ") is None


def test_unsupported_language_returns_none() -> None:
    # Japanese is not in SUPPORTED_LANGUAGES.
    service = LanguageDetectionService()
    assert service.detect("これは日本語で書かれたテスト文です。") is None
