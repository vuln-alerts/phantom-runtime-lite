"""
provider/speech_provider.py
=============================
Speech-to-Text Provider abstraction.

Mirrors provider/interface.py's ProviderInterface pattern for the
Speech-to-Text boundary: the Runtime Core (phantom_runtime.py) talks
only to SpeechToTextProvider, never to an SDK directly. Kept as a
single minimal file (interface + request/response models + the one
shared utility both implementations need) rather than mirroring the
LLM Provider layer's file-per-concern split -- there is only one
operation (transcribe) and no streaming variant here.

EXPORTED API:
  SpeechToTextRequest       -- provider-independent transcription request
  SpeechToTextResponse      -- provider-independent transcription response
  SpeechToTextProvider      -- abstract base class for STT implementations
  detect_language_from_text -- shared JP/EN language-detection fallback,
                                used by both implementations when the
                                underlying SDK does not return a
                                structured language field
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class SpeechToTextRequest:
    audio_wav: bytes
    sample_rate: int
    prompt: Optional[str] = None


@dataclass
class SpeechToTextResponse:
    text: str
    language: str


class SpeechToTextProvider(ABC):
    @abstractmethod
    def transcribe(self, request: SpeechToTextRequest) -> SpeechToTextResponse:
        """
        Transcribe the given audio to text.

        Raises RuntimeProviderError (see provider.errors) or a subclass
        thereof when the underlying provider implementation fails.
        """
        raise NotImplementedError


def detect_language_from_text(text: str) -> str:
    """
    Fix 5 (moved from phantom_runtime.py, unchanged): improved mixed
    JP/EN language detection.

    Strategy (no deps, <0.1ms):
      1. Any hiragana, katakana, or JP punctuation -> "japanese" immediately.
         These codepoints are exclusive to Japanese text.
      2. Kanji present with adaptive threshold:
           len <= 25 chars: any kanji -> "japanese"  (short fragments are almost always JP)
           len > 25 chars: kanji/total >= 5%  -> "japanese"
      3. Otherwise -> "english"

    Tested on 19 interview cases including mixed JP/EN with romaji-heavy text.
    """
    if not text:
        return "unknown"

    # Step 1: unambiguous JP codepoints (single-pass, early exit)
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F: return "japanese"   # hiragana
        if 0x30A0 <= cp <= 0x30FF: return "japanese"   # katakana
        if 0xFF65 <= cp <= 0xFF9F: return "japanese"   # halfwidth kana
        if 0x3000 <= cp <= 0x303F: return "japanese"   # JP punctuation 。、「」…

    # Step 2: kanji with adaptive threshold
    kanji = sum(1 for ch in text
                if 0x3400 <= ord(ch) <= 0x4DBF   # CJK Extension A
                or 0x4E00 <= ord(ch) <= 0x9FFF)  # CJK Unified (main block)

    if kanji == 0:
        return "english"

    if len(text) <= 25:
        return "japanese"   # any kanji in a short string -> JP

    if kanji / len(text) >= 0.05:
        return "japanese"

    return "english"
