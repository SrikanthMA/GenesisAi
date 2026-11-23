"""Genesis visual identity: near-black glass, crimson energy accents."""
from __future__ import annotations

from PySide6.QtGui import QColor, QFont, QFontDatabase

BG = "#0A0A0E"
PANEL = "#111117"
SURFACE = "#17171F"
SURFACE_2 = "#1E1E28"
BORDER = "#2A2A36"
TEXT = "#ECECF1"
MUTED = "#8C8C9C"
ACCENT = "#E0314B"  # crimson
ACCENT_2 = "#FF5A6E"
ACCENT_DIM = "#5A1422"
AMBER = "#F5A524"
GREEN = "#2FD18B"
VIOLET = "#9B5CF6"
ERROR = "#FF3B3B"

CYAN = "#2EC4D6"

STATE_COLORS = {
    "SLEEPING": (QColor("#6B2A36"), QColor("#1C0A0F")),
    "WAKING": (QColor(ACCENT_2), QColor("#FFD0D6")),
    "ACTIVE_CONVERSATION": (QColor(ACCENT), QColor("#1E8A5A")),
    "RETURNING_TO_SLEEP": (QColor("#8A3A47"), QColor("#2A0E14")),
    "VERIFYING": (QColor(CYAN), QColor(ACCENT)),
    "IDLE": (QColor(ACCENT), QColor("#3A0C16")),
    "LISTENING": (QColor(ACCENT_2), QColor(ACCENT)),
    "THINKING": (QColor(VIOLET), QColor(ACCENT)),
    "PLANNING": (QColor("#7C6CF6"), QColor(CYAN)),
    "RETRYING": (QColor(AMBER), QColor("#FF7A45")),
    "EXECUTING": (QColor("#FF7A45"), QColor(ACCENT)),
    "SPEAKING": (QColor(ACCENT_2), QColor("#FF9AA8")),
    "WAITING_FOR_CONFIRMATION": (QColor(AMBER), QColor("#7A4E00")),
    "WAITING_FOR_INPUT": (QColor(ACCENT_2), QColor(VIOLET)),
    "ERROR": (QColor(ERROR), QColor("#5A0000")),
    "SUCCESS": (QColor(GREEN), QColor("#0E4D33")),
}

STATE_LABELS = {
    "SLEEPING": "😴 Sleeping — say “Hey Genesis”",
    "WAKING": "Waking up…",
    "ACTIVE_CONVERSATION": "💬 Active conversation — just keep talking",
    "RETURNING_TO_SLEEP": "Going to sleep…",
    "IDLE": "Ready",
    "LISTENING": "🎙️ Listening…",
    "THINKING": "🧠 Thinking…",
    "PLANNING": "🗺️ Planning…",
    "RETRYING": "🔁 Retrying…",
    "EXECUTING": "⚙️ Executing…",
    "VERIFYING": "🔍 Verifying…",
    "SPEAKING": "Speaking",
    "WAITING_FOR_CONFIRMATION": "⏳ Waiting for your confirmation",
    "WAITING_FOR_INPUT": "⏳ Waiting for your answer",
    "ERROR": "⚠️ Something went wrong",
    "SUCCESS": "✓ Done",
}
# states in which the microphone is actively capturing speech for Genesis (privacy indicator)
LISTENING_STATES = {"ACTIVE_CONVERSATION", "LISTENING", "WAITING_FOR_INPUT", "WAITING_FOR_CONFIRMATION"}


def ui_font_family() -> str:
    fams = set(QFontDatabase.families())
    for f in ("Segoe UI Variable Text", "Segoe UI", "Inter", "Helvetica Neue", "Arial"):
        if f in fams:
            return f
    return QFont().defaultFamily()


def icon_font_family() -> str | None:
    fams = set(QFontDatabase.families())
    for f in ("Segoe Fluent Icons", "Segoe MDL2 Assets"):
        if f in fams:
            return f
    return None


# Segoe MDL2 / Fluent glyph code points, with plain-text fallbacks.
GLYPHS = {
    "mic": ("", "🎙"),
    "mic_off": ("", "🔇"),
    "send": ("", "➤"),
    "expand": ("", "⤢"),
    "close": ("", "✕"),
    "minimize": ("", "—"),
    "settings": ("", "⚙"),
    "chat": ("", "💬"),
    "memory": ("", "🧠"),
    "tools": ("", "🛠"),
    "activity": ("", "🕘"),
    "services": ("", "🔌"),
    "add": ("", "+"),
    "delete": ("", "🗑"),
    "stop": ("", "■"),
    "check": ("", "✓"),
    "warning": ("", "⚠"),
}


def glyph(name: str) -> tuple[str, str | None]:
    code, fallback = GLYPHS[name]
    fam = icon_font_family()
    return (code, fam) if fam else (fallback, None)


def stylesheet() -> str:
    f = ui_font_family()
    return f"""
    * {{ font-family: '{f}'; color: {TEXT}; }}
    QWidget#ChatRoot, QWidget#Page {{ background: {BG}; }}
    QToolTip {{ background: {SURFACE_2}; color: {TEXT}; border: 1px solid {BORDER}; padding: 6px; border-radius: 6px; }}
    QScrollArea {{ border: none; background: transparent; }}
    QScrollBar:vertical {{ background: transparent; width: 8px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 24px; }}
    QScrollBar::handle:vertical:hover {{ background: {ACCENT_DIM}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
    QLineEdit, QPlainTextEdit, QTextEdit, QComboBox, QSpinBox {{
        background: {SURFACE}; border: 1px solid {BORDER}; border-radius: 10px; padding: 8px 12px;
        selection-background-color: {ACCENT};
    }}
    QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus {{ border: 1px solid {ACCENT}; }}
    QComboBox QAbstractItemView {{ background: {SURFACE}; border: 1px solid {BORDER}; selection-background-color: {ACCENT_DIM}; }}
    QPushButton {{
        background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 10px; padding: 8px 14px;
    }}
    QPushButton:hover {{ border-color: {ACCENT}; }}
    QPushButton:pressed {{ background: {ACCENT_DIM}; }}
    QPushButton:disabled {{ color: {MUTED}; }}
    QPushButton#Primary {{ background: {ACCENT}; border: 1px solid {ACCENT}; font-weight: 600; }}
    QPushButton#Primary:hover {{ background: {ACCENT_2}; }}
    QPushButton#Danger {{ background: transparent; border: 1px solid {ERROR}; color: {ERROR}; }}
    QPushButton#Ghost {{ background: transparent; border: none; color: {MUTED}; padding: 6px; }}
    QPushButton#Ghost:hover {{ color: {TEXT}; }}
    QPushButton#Nav {{
        background: transparent; border: none; border-radius: 10px; padding: 10px 14px; text-align: left; color: {MUTED};
    }}
    QPushButton#Nav:hover {{ background: {SURFACE}; color: {TEXT}; }}
    QPushButton#Nav:checked {{ background: {SURFACE_2}; color: {TEXT}; border-left: 3px solid {ACCENT}; }}
    QListWidget, QTableWidget {{
        background: {PANEL}; border: 1px solid {BORDER}; border-radius: 12px; padding: 4px; gridline-color: {BORDER};
    }}
    QListWidget::item {{ padding: 8px; border-radius: 8px; }}
    QListWidget::item:selected {{ background: {SURFACE_2}; color: {TEXT}; }}
    QListWidget::item:hover {{ background: {SURFACE}; }}
    QHeaderView::section {{ background: {SURFACE}; color: {MUTED}; border: none; padding: 6px; }}
    QTableWidget::item {{ padding: 4px; }}
    QCheckBox {{ spacing: 10px; }}
    QCheckBox::indicator {{ width: 36px; height: 20px; border-radius: 10px; background: {SURFACE_2}; border: 1px solid {BORDER}; }}
    QCheckBox::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
    QSlider::groove:horizontal {{ height: 4px; background: {BORDER}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ background: {ACCENT}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
    QLabel#H1 {{ font-size: 20px; font-weight: 600; }}
    QLabel#H2 {{ font-size: 14px; font-weight: 600; color: {TEXT}; }}
    QLabel#Muted {{ color: {MUTED}; }}
    QLabel#Badge {{ background: {SURFACE_2}; border: 1px solid {BORDER}; border-radius: 9px; padding: 2px 8px; font-size: 11px; color: {MUTED}; }}
    QFrame#Card {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 14px; }}
    QFrame#Sidebar {{ background: {PANEL}; border-right: 1px solid {BORDER}; }}
    QFrame#Composer {{ background: {PANEL}; border: 1px solid {BORDER}; border-radius: 18px; }}
    QMenu {{ background: {SURFACE}; border: 1px solid {BORDER}; padding: 6px; border-radius: 8px; }}
    QMenu::item {{ padding: 7px 22px; border-radius: 6px; }}
    QMenu::item:selected {{ background: {ACCENT_DIM}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
    """
