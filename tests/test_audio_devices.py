"""
tests/test_audio_devices.py
===============================================
Unit tests for src/audio/devices.py's resolve_device_id(): input-device
resolution for --input-device.

PV-1 Blocker Fix: prior to this, a numeric string like "1" fell through
resolve_device_id()'s exact-match/substring-match passes as a literal
substring search (never matching a device index), so `--input-device 1`
silently resolved to None and fell back to the system default input
device even when device index 1 existed. This adds the missing Pass 0
(numeric string -> index), mirroring runtime_client/output_device.py's
resolve_output_device_id() -- see tests/test_runtime_client_output_device.py
for the equivalent output-device coverage. Existing exact-match/
substring-match/no-match behavior (Pass 1/Pass 2) is unchanged and is
re-verified here to guard against regression.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import os
import sys
import unittest
from unittest.mock import patch

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from audio.devices import print_input_devices, resolve_device_id

_FIXTURE_DEVICES = [
    {"index": 0, "name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
    {"index": 1, "name": "外部マイク", "max_input_channels": 1, "max_output_channels": 0},
    {"index": 2, "name": "外部ヘッドフォン", "max_input_channels": 0, "max_output_channels": 2},
    {"index": 3, "name": "MacBook Proのマイク", "max_input_channels": 1, "max_output_channels": 0},
    {"index": 4, "name": "MacBook Proのスピーカー", "max_input_channels": 0, "max_output_channels": 2},
]


def _patched_query_devices():
    return patch("audio.devices.sd.query_devices", return_value=_FIXTURE_DEVICES)


class TestResolveDeviceId(unittest.TestCase):
    def test_numeric_string_resolves_by_index(self):
        # PV-1 root cause: "1" must resolve as index 1 ("外部マイク"),
        # not as a substring search that never matches.
        with _patched_query_devices():
            self.assertEqual(resolve_device_id("1"), 1)
            self.assertEqual(resolve_device_id("0"), 0)

    def test_numeric_string_resolves_by_index_without_querying_devices(self):
        # Index resolution needs no device list at all.
        with patch("audio.devices.sd.query_devices") as mock_query:
            self.assertEqual(resolve_device_id("3"), 3)
            mock_query.assert_not_called()

    def test_exact_name_match(self):
        with _patched_query_devices():
            self.assertEqual(resolve_device_id("外部マイク"), 1)
            self.assertEqual(resolve_device_id("MacBook Proのマイク"), 3)

    def test_case_insensitive_substring_match(self):
        with _patched_query_devices():
            self.assertEqual(resolve_device_id("blackhole"), 0)

    def test_substring_match_on_japanese_name(self):
        with _patched_query_devices():
            self.assertEqual(resolve_device_id("外部"), 1)

    def test_output_only_device_is_not_matched(self):
        # 外部ヘッドフォン has max_input_channels == 0 -- must never
        # resolve as an input device even though the name matches exactly.
        with _patched_query_devices():
            self.assertIsNone(resolve_device_id("外部ヘッドフォン"))

    def test_no_match_returns_none(self):
        with _patched_query_devices():
            self.assertIsNone(resolve_device_id("nonexistent device"))

    def test_query_devices_failure_returns_none(self):
        with patch("audio.devices.sd.query_devices", side_effect=OSError("boom")):
            self.assertIsNone(resolve_device_id("外部マイク"))


class TestPrintInputDevices(unittest.TestCase):
    def test_lists_input_capable_devices_only(self):
        lines = []
        with _patched_query_devices():
            print_input_devices(lines.append)
        self.assertIn("  [1] 外部マイク", lines)
        self.assertIn("  [3] MacBook Proのマイク", lines)
        self.assertNotIn("  [2] 外部ヘッドフォン", lines)

    def test_query_failure_is_silent(self):
        with patch("audio.devices.sd.query_devices", side_effect=OSError("boom")):
            lines = []
            print_input_devices(lines.append)
        self.assertEqual(lines, [])


if __name__ == "__main__":
    unittest.main()
