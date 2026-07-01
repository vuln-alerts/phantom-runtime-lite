"""
profiles/loader.py
===================
Unified profile loader supporting both .md and .json formats.

RESOLUTION ORDER (per profile name):
  1. profiles/<name>.json   (preferred — structured, validatable)
  2. profiles/<name>.md     (legacy — Markdown with ## sections)
  3. profiles/default.json  (fallback)
  4. profiles/default.md    (fallback)
  5. Built-in minimal dict  (never crashes startup)

EXPORTED API:
  load_profile(name, profiles_dir) → (dict, resolved_name)
  parse_md_profile(text)           → dict
  parse_json_profile(text)         → dict
"""

import json
import os
from typing import Optional

from profiles.schema import validate_profile, normalise_profile, PROFILE_DEFAULTS


# ─────────────────────────────────────────────────────────────────────────────
# Markdown parser (legacy format — ## section headers)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_phrase_list(raw: str) -> list:
    return [ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


def _parse_language_behavior(raw: str) -> dict:
    result = {}
    for line in raw.splitlines():
        line = line.strip()
        if ":" in line:
            k, _, v = line.partition(":")
            k = k.strip().lower().replace("-", "_")
            v = v.strip().lower()
            if k in ("interview_lang", "english_level"):
                result[k] = v
    return result


def parse_md_profile(text: str) -> dict:
    """Parse a Markdown profile (## section headers) into a dict."""
    sections: dict = {}
    current_key: Optional[str] = None
    current_lines: list = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key   = line[3:].strip().lower().replace(" ", "_")
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    # Normalise language_behavior from string → dict
    if "language_behavior" in sections and isinstance(sections["language_behavior"], str):
        sections["language_behavior"] = _parse_language_behavior(sections["language_behavior"])

    # Normalise phrase lists from string → list
    for field in ("delay_phrases_en", "delay_phrases_jp"):
        if field in sections and isinstance(sections[field], str):
            sections[field] = _parse_phrase_list(sections[field])

    # Parse intent: lines from response_examples into INTENT_CACHE seeds
    # (kept in response_examples string; intent: prefix parsed elsewhere)

    return sections


def parse_json_profile(text: str) -> dict:
    """Parse a JSON profile. Returns empty dict on parse error."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# Main loader
# ─────────────────────────────────────────────────────────────────────────────

def load_profile(
    name:         str,
    profiles_dir: str,
    warn_fn       = None,
    info_fn       = None,
) -> tuple:
    """
    Load and return (profile_dict, resolved_name).

    Resolution order: <name>.json → <name>.md → default.json → default.md → built-in.
    Profile is normalised (missing fields filled with defaults) and validated.
    Warnings printed via warn_fn if provided.
    """
    _warn = warn_fn or (lambda s: None)
    _info = info_fn or (lambda s: None)

    # Path-traversal protection
    safe_name = os.path.basename(name).replace("..", "").strip()

    # Try .json first, then .md
    for ext, parser in [(".json", parse_json_profile), (".md", parse_md_profile)]:
        path = os.path.join(profiles_dir, f"{safe_name}{ext}")
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = f.read()
                sections = parser(raw)
                if sections:
                    sections = normalise_profile(sections)
                    for w in validate_profile(sections, safe_name):
                        _warn(f"[profile] {w}")
                    _info(f"[profile] loaded {safe_name}{ext}")
                    return sections, safe_name
            except Exception as e:
                _warn(f"[profile] Could not read '{safe_name}{ext}': {e}")

    # Fallback to default
    if safe_name != "default":
        _warn(f"[profile] '{safe_name}' not found in {profiles_dir}/ — using default")
        return load_profile("default", profiles_dir, warn_fn=_warn, info_fn=_info)

    # Hardcoded minimal fallback — startup never crashes
    _warn("[profile] default profile not found — using built-in minimal profile")
    return dict(PROFILE_DEFAULTS), "built-in"
