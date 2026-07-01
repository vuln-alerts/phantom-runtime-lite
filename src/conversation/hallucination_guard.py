"""
conversation.hallucination_guard
=================================
Hallucination Guard — Phantom Runtime Lite.

Public API
----------
is_meaningful(text, *, trace_fn=None) -> bool

This module is independently importable.
It carries no dependency on the main runtime file.
"""

from __future__ import annotations

import os as _os
import re as _re
from typing import Callable, Optional

# ── Hallucination noise patterns ──────────────────────────────────────────────

_HALLUCINATIONS: frozenset[str] = frozenset({
    "ご視聴ありがとうございました", "字幕は自動生成されています", "この動画は", "翻訳",
    "thank you for watching", "please subscribe", "like and subscribe",
    "thanks for watching", "[music]", "(music)", "♪", "…", ".", "。",
})

_FILLER_PATTERNS: tuple[str, ...] = (
    "um", "uh", "ah", "oh", "hmm", "mm", "mhm",
    "うん", "えー", "あー", "はい", "ええ",
)

_MIN_CHARS: int = 6

# ── Guard enabled flag — read from environment at import time ─────────────────

def _parse_bool_env(name: str, default: str = "1") -> bool:
    val = _os.environ.get(name, default).lower()
    return val not in ("0", "false", "no", "off", "")

_GUARD_ENABLED: bool = _parse_bool_env("ENABLE_HALLUCINATION_GUARD", "1")


# ── Internal: language detection (stdlib-only, no runtime dependency) ─────────

def _detect_language(text: str) -> str:
    """
    Detect language from text without external dependencies.
    Identical logic to detect_language_from_text() in the runtime.
    """
    if not text:
        return "unknown"

    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F: return "japanese"   # hiragana
        if 0x30A0 <= cp <= 0x30FF: return "japanese"   # katakana
        if 0xFF65 <= cp <= 0xFF9F: return "japanese"   # halfwidth kana
        if 0x3000 <= cp <= 0x303F: return "japanese"   # JP punctuation

    kanji = sum(
        1 for ch in text
        if 0x3400 <= ord(ch) <= 0x4DBF
        or 0x4E00 <= ord(ch) <= 0x9FFF
    )

    if kanji == 0:
        return "english"
    if len(text) <= 25:
        return "japanese"
    if kanji / len(text) >= 0.05:
        return "japanese"
    return "english"


# ── Public API ────────────────────────────────────────────────────────────────

def is_meaningful(
    text: str,
    *,
    trace_fn: Optional[Callable[[str, str], None]] = None,
) -> bool:
    """
    Noise filter for interview runtime transcripts.

    Added guards:
      - Single-word captures ("OK", "yeah", "right") below 10 chars → False
      - Repeated-token guard: ≤ 2 unique words in 4+ token utterance → False
      - All-punctuation / all-space → False

    Parameters
    ----------
    text:      Transcribed utterance text.
    trace_fn:  Optional callable(stage, detail) for pipeline tracing.
               Matches the _trace(stage, detail) signature in the runtime.
    """
    if not _GUARD_ENABLED:
        return True   # guard disabled — pass everything through

    stripped = (text or "").strip()

    if len(stripped) < _MIN_CHARS:
        return False

    # Punctuation-only
    if all(not c.isalpha() and not '぀' <= c <= '鿿' for c in stripped):
        return False

    tl = stripped.lower()

    # ── Language detection ─────────────────────────────────────────────────

    try:
        lang_guess = _detect_language(stripped)
    except Exception:
        lang_guess = "english" if _re.search(r"[a-zA-Z]", stripped) else "japanese"

    if lang_guess == "english":

        # Explicit question
        if "?" in stripped:
            if trace_fn is not None:
                trace_fn("meaningful/check", f"decision=True text={stripped[:80]!r}")
            return True

        # Recruiter-style prompts
        if len(stripped.split()) >= 4:
            if trace_fn is not None:
                trace_fn("meaningful/check", f"decision=True text={stripped[:80]!r}")
            return True

    # ── Hallucination patterns ─────────────────────────────────────────────

    if tl in _HALLUCINATIONS:
        if trace_fn is not None:
            trace_fn("meaningful/check", f"decision=False text={stripped[:80]!r}")
        return False

    # ── Filler patterns ────────────────────────────────────────────────────

    if tl.rstrip(".,!?") in _FILLER_PATTERNS:
        if trace_fn is not None:
            trace_fn("meaningful/check", f"decision=False text={stripped[:80]!r}")
        return False

    # ── Single-word short captures ─────────────────────────────────────────

    words = stripped.split()

    if len(words) == 1 and len(stripped) < 10:
        if trace_fn is not None:
            trace_fn("meaningful/check", f"decision=False text={stripped[:80]!r}")
        return False

    # ── Repetition guard ───────────────────────────────────────────────────

    if len(words) >= 4 and len(set(w.lower().rstrip(".,!?") for w in words)) <= 2:
        if trace_fn is not None:
            trace_fn("meaningful/check", f"decision=False text={stripped[:80]!r}")
        return False

    if trace_fn is not None:
        trace_fn("meaningful/check", f"decision=True text={stripped[:80]!r}")

    return True
