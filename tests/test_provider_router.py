"""
tests/test_provider_router.py
=================================
H5-1 unit tests for runtime.provider_router: the Provider Router's
validation/selection logic. No SDK calls, no transport, no subprocess --
pure function behavior only.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.provider_router import (
    SUPPORTED_PROVIDERS,
    ProviderRejected,
    select_provider_from_query,
)


class TestProviderRouter(unittest.TestCase):
    def test_supported_providers_are_openai_and_gemini(self):
        self.assertEqual(SUPPORTED_PROVIDERS, frozenset({"openai", "gemini"}))

    def test_valid_provider_openai_selected(self):
        self.assertEqual(select_provider_from_query("provider=openai"), "openai")

    def test_valid_provider_gemini_selected(self):
        self.assertEqual(select_provider_from_query("provider=gemini"), "gemini")

    def test_provider_is_case_normalized(self):
        self.assertEqual(select_provider_from_query("provider=GEMINI"), "gemini")

    def test_missing_provider_rejected(self):
        with self.assertRaises(ProviderRejected) as ctx:
            select_provider_from_query("")
        self.assertIn("missing provider", str(ctx.exception))

    def test_blank_provider_rejected(self):
        with self.assertRaises(ProviderRejected) as ctx:
            select_provider_from_query("provider=")
        self.assertIn("missing provider", str(ctx.exception))

    def test_unknown_provider_rejected(self):
        with self.assertRaises(ProviderRejected) as ctx:
            select_provider_from_query("provider=claude")
        self.assertIn("unknown provider", str(ctx.exception))

    def test_provider_param_absent_from_other_query_keys(self):
        with self.assertRaises(ProviderRejected) as ctx:
            select_provider_from_query("other=1")
        self.assertIn("missing provider", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
