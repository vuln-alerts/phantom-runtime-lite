"""
profiles/schema.py
===================
Profile schema definition and validation for the Phantom Runtime.

Supports both .md (Markdown with ## sections) and .json profiles.

PROFILE SCHEMA (all fields optional — safe defaults applied for missing fields):
  identity            str   Who the assistant represents
  positioning         str   How to frame experience
  recruiter_context   str   Nature of the conversation
  communication_style str   Tone and style rules
  technical_focus     str   Domain vocabulary
  forbidden_phrases   str   Extra banned phrases
  language_behavior   dict  {interview_lang, english_level} overrides
  summary_tone        str   Summary style guidance
  career_summary      str   Key career facts (injected into prompt)
  topic_memory        str   Topics/skills to emphasise
  response_examples   str   Example answers (few-shot, truncated)
  delay_phrases_en    list  Custom EN delay phrases
  delay_phrases_jp    list  Custom JP delay phrases

EXPORTED API:
  PROFILE_DEFAULTS          — dict of default values for all fields
  validate_profile(d)       → list[str]  warning strings (empty = valid)
  normalise_profile(d)      → dict       fills missing fields with defaults
"""

from typing import Any

# ── Defaults applied when a profile field is absent ──────────────────────────
PROFILE_DEFAULTS: dict[str, Any] = {
    "identity":           "You are a real-time conversational assistant.",
    "positioning":        "Support the operator in a live conversation.",
    "recruiter_context":  "General professional conversation.",
    "communication_style":"Keep replies short, natural, and spoken.",
    "technical_focus":    "",
    "forbidden_phrases":  "Certainly / Of course / Absolutely / Great question / As an AI",
    "language_behavior":  {"interview_lang": "mixed", "english_level": "natural"},
    "summary_tone":       "Summarize in plain Japanese. Focus on what was discussed.",
    "career_summary":     "",
    "topic_memory":       "",
    "response_examples":  "",
    "delay_phrases_en":   [],
    "delay_phrases_jp":   [],
}

_VALID_INTERVIEW_LANGS = {"en", "ja", "mixed"}
_VALID_ENGLISH_LEVELS  = {"beginner", "simple", "natural", "fluent"}


def validate_profile(profile: dict, name: str = "unnamed") -> list:
    """
    Validate a loaded profile dict.
    Returns a list of warning strings. Empty list = valid.
    Does NOT abort or raise — warnings are informational.
    """
    warnings = []

    lb = profile.get("language_behavior", {})
    if isinstance(lb, dict):
        lang = lb.get("interview_lang", "")
        if lang and lang not in _VALID_INTERVIEW_LANGS:
            warnings.append(
                f"profile '{name}': language_behavior.interview_lang='{lang}' "
                f"invalid. Valid: {sorted(_VALID_INTERVIEW_LANGS)}"
            )
        level = lb.get("english_level", "")
        if level and level not in _VALID_ENGLISH_LEVELS:
            warnings.append(
                f"profile '{name}': language_behavior.english_level='{level}' "
                f"invalid. Valid: {sorted(_VALID_ENGLISH_LEVELS)}"
            )
    elif isinstance(lb, str):
        # Markdown-style "interview_lang: en" line — parsed by loader, not schema
        pass

    # Warn on over-long response_examples (token bloat risk)
    examples = profile.get("response_examples", "")
    if isinstance(examples, str) and len(examples) > 600:
        warnings.append(
            f"profile '{name}': response_examples is {len(examples)} chars — "
            f"consider trimming to <400 chars to reduce token overhead"
        )

    return warnings


def normalise_profile(profile: dict) -> dict:
    """
    Fill missing fields with PROFILE_DEFAULTS.
    Returns a new dict — does not mutate input.
    """
    result = dict(PROFILE_DEFAULTS)
    result.update({k: v for k, v in profile.items() if v not in (None, "", [], {})})
    return result
