"""
tests/test_runtime_client_output_device.py
===============================================
Unit tests for src/runtime_client/output_device.py: macOS output-device
resolution for Phase 3 TTS playback. No SSoT equivalent exists (see
tts.py's module docstring) -- this mirrors audio/devices.py's
resolve_device_id matching logic (exact NFC match, then case-insensitive
substring match) but filtered to output-capable devices, plus index and
"default" handling.

Fixture device list matches what `sd.query_devices()` actually returns
on the development machine this was written against (BlackHole 2ch +
several other input/output devices), so these tests exercise the real
shape of that data, not an invented one.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import io
import os
import sys
import unittest
from unittest.mock import patch

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.output_device import (
    list_output_devices,
    print_output_devices,
    resolve_output_device_id,
)

_FIXTURE_DEVICES = [
    {"index": 0, "name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
    {"index": 1, "name": "外部マイク", "max_input_channels": 1, "max_output_channels": 0},
    {"index": 2, "name": "外部ヘッドフォン", "max_input_channels": 0, "max_output_channels": 2},
    {"index": 3, "name": "MacBook Proのマイク", "max_input_channels": 1, "max_output_channels": 0},
    {"index": 4, "name": "MacBook Proのスピーカー", "max_input_channels": 0, "max_output_channels": 2},
    {"index": 5, "name": "tw2s (2)のマイク", "max_input_channels": 1, "max_output_channels": 0},
    {"index": 6, "name": "Microsoft Teams Audio", "max_input_channels": 1, "max_output_channels": 1},
    {"index": 7, "name": "複数出力装置", "max_input_channels": 0, "max_output_channels": 2},
]


def _patched_query_devices():
    return patch("runtime_client.output_device.sd.query_devices", return_value=_FIXTURE_DEVICES)


class TestResolveOutputDeviceId(unittest.TestCase):
    def test_none_resolves_to_default(self):
        self.assertIsNone(resolve_output_device_id(None))

    def test_empty_string_resolves_to_default(self):
        self.assertIsNone(resolve_output_device_id(""))

    def test_default_aliases_case_insensitive(self):
        for alias in ("default", "DEFAULT", "Default", "system default", "SYSTEM DEFAULT"):
            self.assertIsNone(resolve_output_device_id(alias))

    def test_numeric_string_resolves_by_index(self):
        with _patched_query_devices():
            self.assertEqual(resolve_output_device_id("0"), 0)
            self.assertEqual(resolve_output_device_id("7"), 7)

    def test_exact_name_match(self):
        with _patched_query_devices():
            self.assertEqual(resolve_output_device_id("BlackHole 2ch"), 0)
            self.assertEqual(resolve_output_device_id("外部ヘッドフォン"), 2)

    def test_case_insensitive_substring_match(self):
        with _patched_query_devices():
            self.assertEqual(resolve_output_device_id("blackhole"), 0)
            self.assertEqual(resolve_output_device_id("teams"), 6)

    def test_substring_match_on_japanese_name(self):
        with _patched_query_devices():
            self.assertEqual(resolve_output_device_id("スピーカー"), 4)

    def test_multi_output_device_matches(self):
        with _patched_query_devices():
            self.assertEqual(resolve_output_device_id("複数出力装置"), 7)

    def test_input_only_device_is_not_matched(self):
        # 外部マイク has max_output_channels == 0 -- must never resolve
        # as an output device even though the name matches exactly.
        with _patched_query_devices():
            self.assertIsNone(resolve_output_device_id("外部マイク"))

    def test_no_match_returns_none(self):
        with _patched_query_devices():
            self.assertIsNone(resolve_output_device_id("nonexistent device"))

    def test_query_devices_failure_returns_none(self):
        with patch("runtime_client.output_device.sd.query_devices", side_effect=OSError("boom")):
            self.assertIsNone(resolve_output_device_id("BlackHole"))


class TestListAndPrintOutputDevices(unittest.TestCase):
    def test_list_excludes_input_only_devices(self):
        with _patched_query_devices():
            names = [d["name"] for d in list_output_devices()]
        self.assertNotIn("外部マイク", names)
        self.assertNotIn("MacBook Proのマイク", names)
        self.assertNotIn("tw2s (2)のマイク", names)
        self.assertIn("BlackHole 2ch", names)
        self.assertIn("複数出力装置", names)

    def test_list_includes_correct_indices(self):
        with _patched_query_devices():
            devices = list_output_devices()
        by_name = {d["name"]: d["index"] for d in devices}
        self.assertEqual(by_name["BlackHole 2ch"], 0)
        self.assertEqual(by_name["複数出力装置"], 7)

    def test_query_failure_returns_empty_list(self):
        with patch("runtime_client.output_device.sd.query_devices", side_effect=OSError("boom")):
            self.assertEqual(list_output_devices(), [])

    def test_print_output_devices_formats_index_and_name(self):
        lines = []
        with _patched_query_devices():
            print_output_devices(lines.append)
        self.assertIn("  [0] BlackHole 2ch", lines)
        self.assertIn("  [7] 複数出力装置", lines)


if __name__ == "__main__":
    unittest.main()
