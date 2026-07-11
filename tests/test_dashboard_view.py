"""
tests/test_dashboard_view.py
===============================
Unit tests for api.dashboard_view.render_dashboard_html.

Uses unittest (stdlib), consistent with the rest of this project's test
suite.
"""

import datetime
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from api.dashboard_view import render_dashboard_html
from dashboard.dashboard_result import DashboardResult


def _dashboard_result(**overrides):
    fields = dict(
        schema_version="1.0",
        source_event_id="evt-1",
        session_id="sess-1",
        timestamp=datetime.datetime(2026, 7, 11, 12, 0, 0, tzinfo=datetime.timezone.utc),
        gap_detected=True,
        gap_reason="missing field",
        fallback_detected=False,
        reliability_score=0.5,
        reliable=False,
        warnings=["w1", "w2"],
        trust_score=0.42,
        trust_level="UNTRUSTED",
        human_review_required=True,
        review_reason="low trust score",
        contributing_factors=["f1"],
        conversation_line=31,
        speaker="YOU",
        transcript="現在、利用人数はどのくらいを想定されていますか？",
    )
    fields.update(overrides)
    return DashboardResult(**fields)


class RenderDashboardHtmlTests(unittest.TestCase):
    def test_none_renders_empty_state(self):
        html = render_dashboard_html(None)
        self.assertIn("No DashboardResult yet", html)

    def test_renders_all_required_display_fields(self):
        html = render_dashboard_html(_dashboard_result())
        self.assertIn("0.420", html)          # Trust Score
        self.assertIn("UNTRUSTED", html)      # Trust Level
        self.assertIn("0.500", html)          # Reliability Score
        self.assertIn("Yes", html)            # Gap Detected / Human Review Required
        self.assertIn("No", html)             # Fallback Detected
        self.assertIn("low trust score", html)  # Review Reason
        self.assertIn("w1", html)             # Warnings
        self.assertIn("w2", html)
        self.assertIn("sess-1", html)         # Session ID
        self.assertIn("evt-1", html)          # Event ID
        self.assertIn("2026-07-11", html)     # Timestamp

    def test_missing_optional_fields_render_as_placeholder(self):
        html = render_dashboard_html(_dashboard_result(session_id=None, review_reason=None))
        self.assertIn("—", html)

    def test_values_are_html_escaped(self):
        html = render_dashboard_html(_dashboard_result(review_reason="<script>alert(1)</script>"))
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;", html)

    def test_renders_conversation_traceability_alongside_event_and_session_id(self):
        # Conversation Traceability additions must not remove Event ID /
        # Session ID (docs/H4_RUNTIME_EVENT_CONTRACT.md, "Runtime Event
        # Metadata").
        html = render_dashboard_html(_dashboard_result())
        self.assertIn("Conversation", html)
        self.assertIn("#31", html)            # conversation_line
        self.assertIn("Speaker", html)
        self.assertIn("YOU", html)
        self.assertIn("Transcript", html)
        self.assertIn("現在、利用人数はどのくらいを想定されていますか？", html)
        self.assertIn("Event ID", html)
        self.assertIn("evt-1", html)
        self.assertIn("Session ID", html)
        self.assertIn("sess-1", html)

    def test_missing_conversation_fields_render_as_placeholder(self):
        html = render_dashboard_html(
            _dashboard_result(conversation_line=None, speaker=None, transcript=None)
        )
        self.assertIn("—", html)


if __name__ == "__main__":
    unittest.main()
