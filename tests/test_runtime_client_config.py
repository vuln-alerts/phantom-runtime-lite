"""
tests/test_runtime_client_config.py
=======================================
Unit tests for src/runtime_client/config.py: CLI parsing (parse_args)
and Cloud Run URL -> WebSocket URL construction (build_ws_url).

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.config import build_ws_url, parse_args


class TestBuildWsUrl(unittest.TestCase):
    def test_https_becomes_wss(self):
        self.assertEqual(
            build_ws_url("https://xxxxx.run.app", "openai"),
            "wss://xxxxx.run.app/ws?provider=openai",
        )

    def test_http_becomes_ws(self):
        self.assertEqual(
            build_ws_url("http://localhost:8080", "gemini"),
            "ws://localhost:8080/ws?provider=gemini",
        )

    def test_existing_path_and_query_are_discarded(self):
        self.assertEqual(
            build_ws_url("https://xxxxx.run.app/some/path?foo=bar", "openai"),
            "wss://xxxxx.run.app/ws?provider=openai",
        )

    def test_ws_and_wss_schemes_pass_through(self):
        self.assertEqual(
            build_ws_url("ws://localhost:8080", "openai"),
            "ws://localhost:8080/ws?provider=openai",
        )
        self.assertEqual(
            build_ws_url("wss://xxxxx.run.app", "gemini"),
            "wss://xxxxx.run.app/ws?provider=gemini",
        )

    def test_invalid_scheme_raises(self):
        with self.assertRaises(ValueError):
            build_ws_url("ftp://xxxxx.run.app", "openai")

    def test_missing_netloc_raises(self):
        with self.assertRaises(ValueError):
            build_ws_url("https://", "openai")


class TestParseArgs(unittest.TestCase):
    def test_url_and_provider_required(self):
        with self.assertRaises(SystemExit):
            parse_args([])

    def test_url_required_even_with_provider(self):
        with self.assertRaises(SystemExit):
            parse_args(["--provider", "openai"])

    def test_provider_required_even_with_url(self):
        with self.assertRaises(SystemExit):
            parse_args(["--url", "https://xxxxx.run.app"])

    def test_list_devices_bypasses_required_args(self):
        config = parse_args(["--list-devices"])
        self.assertTrue(config.list_devices)
        self.assertEqual(config.url, "")
        self.assertEqual(config.provider, "")

    def test_list_output_devices_bypasses_required_args(self):
        config = parse_args(["--list-output-devices"])
        self.assertTrue(config.list_output_devices)

    def test_minimal_valid_invocation_defaults(self):
        config = parse_args(["--url", "https://xxxxx.run.app", "--provider", "openai"])
        self.assertEqual(config.url, "https://xxxxx.run.app")
        self.assertEqual(config.provider, "openai")
        self.assertEqual(config.sample_rate, 16000)
        self.assertEqual(config.channels, 1)
        self.assertEqual(config.block_size, 1600)
        self.assertEqual(config.max_reconnect_attempts, 3)
        self.assertEqual(config.backoff_base_seconds, 1.0)
        self.assertFalse(config.manual_flush)
        self.assertEqual(config.tts, "none")
        self.assertIsNone(config.input_device)
        self.assertIsNone(config.output_device)
        self.assertEqual(config.voice, "Samantha")
        self.assertIsNone(config.rate)
        self.assertEqual(config.volume, 1.0)

    def test_invalid_provider_choice_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(["--url", "https://xxxxx.run.app", "--provider", "claude"])

    def test_invalid_tts_choice_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(
                ["--url", "https://xxxxx.run.app", "--provider", "openai", "--tts", "elevenlabs"]
            )

    def test_all_overrides_applied(self):
        config = parse_args(
            [
                "--url", "https://xxxxx.run.app",
                "--provider", "gemini",
                "--input-device", "MacBook",
                "--output-device", "BlackHole",
                "--sample-rate", "48000",
                "--block-size", "800",
                "--max-reconnect-attempts", "5",
                "--backoff-base-seconds", "2.5",
                "--manual-flush",
                "--tts", "say",
                "--voice", "Daniel",
                "--rate", "220",
                "--volume", "0.6",
            ]
        )
        self.assertEqual(config.input_device, "MacBook")
        self.assertEqual(config.output_device, "BlackHole")
        self.assertEqual(config.sample_rate, 48000)
        self.assertEqual(config.block_size, 800)
        self.assertEqual(config.max_reconnect_attempts, 5)
        self.assertEqual(config.backoff_base_seconds, 2.5)
        self.assertTrue(config.manual_flush)
        self.assertEqual(config.tts, "say")
        self.assertEqual(config.voice, "Daniel")
        self.assertEqual(config.rate, 220)
        self.assertEqual(config.volume, 0.6)

    def test_volume_above_one_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(
                ["--url", "https://xxxxx.run.app", "--provider", "openai", "--volume", "1.5"]
            )

    def test_volume_below_zero_rejected(self):
        with self.assertRaises(SystemExit):
            parse_args(
                ["--url", "https://xxxxx.run.app", "--provider", "openai", "--volume", "-0.1"]
            )

    def test_volume_boundary_values_accepted(self):
        config = parse_args(
            ["--url", "https://xxxxx.run.app", "--provider", "openai", "--volume", "0.0"]
        )
        self.assertEqual(config.volume, 0.0)
        config = parse_args(
            ["--url", "https://xxxxx.run.app", "--provider", "openai", "--volume", "1.0"]
        )
        self.assertEqual(config.volume, 1.0)


if __name__ == "__main__":
    unittest.main()
