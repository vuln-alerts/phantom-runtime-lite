"""
audio/devices.py
================
Audio input device resolution for Phantom Runtime Lite.

EXPORTED API:
  resolve_device_id(name)         — resolve device index, or name substring, to integer ID
  print_input_devices(warn_fn)    — enumerate available input devices via warn_fn
"""

import unicodedata
from typing import Callable, Optional

import sounddevice as sd


def resolve_device_id(name: str) -> Optional[int]:
    """
    Resolve a device index or name to an integer device index.

    Pass 0: numeric string, resolved by index (mirrors
    output_device.py's resolve_output_device_id -- see PV-1 Blocker Fix:
    prior to this, a numeric --input-device value like "1" fell through
    to Pass 2 as a literal substring search instead of being treated as
    an index).
    Pass 1: NFC-normalized exact match, input-only devices.
    Pass 2: case-insensitive NFC-normalized substring match, input-only devices.
    Returns None if no match found or sd.query_devices() raises.
    """
    if name.strip().lstrip("-").isdigit():
        return int(name.strip())

    name_nfc = unicodedata.normalize("NFC", name)
    try:
        devices = sd.query_devices()
    except Exception:
        return None
    for dev in devices:
        if dev["max_input_channels"] < 1:
            continue
        if unicodedata.normalize("NFC", dev["name"]) == name_nfc:
            return int(dev["index"])
    name_lower = name_nfc.lower()
    for dev in devices:
        if dev["max_input_channels"] < 1:
            continue
        if name_lower in unicodedata.normalize("NFC", dev["name"]).lower():
            return int(dev["index"])
    return None


def print_input_devices(warn_fn: Callable[[str], None]) -> None:
    """List all available input devices via warn_fn."""
    try:
        for dev in sd.query_devices():
            if dev["max_input_channels"] > 0:
                warn_fn(f"  [{dev['index']}] {dev['name']}")
    except Exception:
        pass
