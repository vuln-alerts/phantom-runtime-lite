"""
conversation.speaker_inference
================================
Speaker inference engine — Phantom Runtime Lite.

Public API
----------
infer_speaker(lang, text, conv_state, effective_lang, trace_fn) -> str
reset_speaker_state() -> None

This module is independently importable.
It carries no dependency on the main runtime file.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

# ── Language detection constants ──────────────────────────────────────────────

_JP_LANGS: frozenset[str] = frozenset({"japanese", "ja", "jpn"})

# ── Filler patterns (anti-oscillation guard) ─────────────────────────────────

_FILLER_PATTERNS: tuple[str, ...] = (
    "um", "uh", "ah", "oh", "hmm", "mm", "mhm",
    "うん", "えー", "あー", "はい", "ええ",
)

# ── Conversation states that force speaker → "user" ───────────────────────────
# Matches ConversationState enum values from the runtime.

_AGENT_ACTIVE_STATES: frozenset[str] = frozenset({"generating", "speaking"})

# ── Module-owned speaker inference state (anti-oscillation) ──────────────────

_speaker_lock:            threading.Lock = threading.Lock()
_last_inferred_speaker:   str            = "recruiter"
_speaker_consecutive_lang: str           = ""
_speaker_flip_threshold:  int            = 2   # reserved; not consumed by current logic


def _is_japanese_lang(lang: str) -> bool:
    lc = lang.lower()
    return lc in _JP_LANGS or lc.startswith("ja")


def infer_speaker(
    lang:           str,
    text:           str,
    conv_state:     str,
    effective_lang: str,
    trace_fn:       Optional[Callable[[str], None]] = None,
) -> str:
    """
    Infer the speaker ("recruiter" | "user") from language and conversation context.

    Parameters
    ----------
    lang:           Whisper-detected language string (e.g. "japanese", "english").
    text:           Transcribed utterance text.
    conv_state:     Current ConversationState value string (e.g. "generating").
    effective_lang: Runtime-configured effective language ("en" | "ja" | ...).
                    Accepted for interface stability; reserved for future use.
    trace_fn:       Optional callback invoked with a trace message string.

    Returns
    -------
    str — one of "recruiter", "user".
    """
    global _last_inferred_speaker, _speaker_consecutive_lang

    # During agent-active states any audio is the user side.
    if conv_state in _AGENT_ACTIVE_STATES:
        return "user"

    text_stripped = text.strip()
    text_lower    = text_stripped.lower().rstrip(".,!?")
    words         = text.split()

    # Filler short-circuits: keep the last inferred speaker.
    if text_lower in _FILLER_PATTERNS:
        return _last_inferred_speaker

    # Very short utterances carry no reliable lang signal.
    if len(words) <= 2 and len(text_stripped) < 8:
        return _last_inferred_speaker

    candidate = "user" if _is_japanese_lang(lang) else "recruiter"

    with _speaker_lock:
        if candidate == _last_inferred_speaker:
            _speaker_consecutive_lang = lang
            result = candidate
        else:
            if lang == _speaker_consecutive_lang:
                result = _last_inferred_speaker
            else:
                _speaker_consecutive_lang = lang
                result = candidate
        _last_inferred_speaker = result

    if trace_fn is not None:
        trace_fn(f"[speaker] {lang[:2]} → {result}  (text={text[:30]!r})")

    return result


def reset_speaker_state() -> None:
    """Reset speaker inference state to the initial default."""
    global _last_inferred_speaker, _speaker_consecutive_lang
    with _speaker_lock:
        _last_inferred_speaker    = "recruiter"
        _speaker_consecutive_lang = ""
