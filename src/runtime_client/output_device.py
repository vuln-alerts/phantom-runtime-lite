"""
runtime_client/output_device.py
==================================
macOS output-device resolution for the Runtime Client's TTS playback
(Phase 3). No SSoT equivalent exists -- the SSoT never selected an
output device at all (see src/runtime_client/tts.py's module docstring
for why). Mirrors audio/devices.py's resolve_device_id matching logic
(NFC-normalized exact match, then case-insensitive substring match) but
filtered to output-capable devices, plus index and "default" handling.

EXPORTED API:
  resolve_output_device_id(name_or_index) -- name/substring/index -> device id or None
  list_output_devices()                   -- [{"index": int, "name": str}, ...]
  print_output_devices(print_fn)          -- render list_output_devices() via print_fn
"""

import unicodedata
from typing import Callable, Optional

import sounddevice as sd

_DEFAULT_ALIASES = {"default", "system default", ""}


def resolve_output_device_id(name_or_index: Optional[str]) -> Optional[int]:
    """
    Resolve an --output-device value to a sounddevice output device
    index. None, "" , "default", "system default" (case-insensitive)
    all resolve to None -- sounddevice's own system-default semantics.
    Numeric strings resolve by index. Otherwise: exact NFC-normalized
    match first, then case-insensitive substring match, both restricted
    to devices with max_output_channels > 0. Returns None (with no
    devices matched) if nothing matches.
    """
    if name_or_index is None:
        return None
    if name_or_index.strip().lower() in _DEFAULT_ALIASES:
        return None
    if name_or_index.strip().lstrip("-").isdigit():
        return int(name_or_index.strip())

    name_nfc = unicodedata.normalize("NFC", name_or_index)
    try:
        devices = sd.query_devices()
    except Exception:
        return None

    for dev in devices:
        if dev["max_output_channels"] < 1:
            continue
        if unicodedata.normalize("NFC", dev["name"]) == name_nfc:
            return int(dev["index"])

    name_lower = name_nfc.lower()
    for dev in devices:
        if dev["max_output_channels"] < 1:
            continue
        if name_lower in unicodedata.normalize("NFC", dev["name"]).lower():
            return int(dev["index"])

    return None


def list_output_devices() -> list:
    """Returns [{"index": int, "name": str}, ...] for every output-capable device."""
    try:
        devices = sd.query_devices()
    except Exception:
        return []
    return [
        {"index": int(dev["index"]), "name": dev["name"]}
        for dev in devices
        if dev["max_output_channels"] > 0
    ]


def print_output_devices(print_fn: Callable[[str], None]) -> None:
    for dev in list_output_devices():
        print_fn(f"  [{dev['index']}] {dev['name']}")
