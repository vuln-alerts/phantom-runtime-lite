"""
tests/test_dashboard_service.py
==================================
Unit tests for api.dashboard_service.DashboardService.

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

from api.dashboard_service import DashboardService
from dashboard.dashboard_result import DashboardResult


def _dashboard_result(trust_score=0.87, source_event_id="evt-1"):
    return DashboardResult(
        schema_version="1.0",
        source_event_id=source_event_id,
        session_id="sess-1",
        timestamp=datetime.datetime.now(datetime.timezone.utc),
        gap_detected=False,
        gap_reason=None,
        fallback_detected=False,
        reliability_score=1.0,
        reliable=True,
        warnings=[],
        trust_score=trust_score,
        trust_level="TRUSTED",
        human_review_required=False,
        review_reason=None,
        contributing_factors=[],
    )


class DashboardServiceTests(unittest.TestCase):
    def test_get_latest_is_none_initially(self):
        self.assertIsNone(DashboardService().get_latest())

    def test_set_latest_is_reflected_by_get_latest(self):
        service = DashboardService()
        dr = _dashboard_result(trust_score=0.42)
        service.set_latest(dr)
        self.assertIs(service.get_latest(), dr)

    def test_second_set_latest_replaces_the_first(self):
        service = DashboardService()
        first = _dashboard_result(source_event_id="evt-1")
        second = _dashboard_result(source_event_id="evt-2")
        service.set_latest(first)
        service.set_latest(second)
        self.assertIs(service.get_latest(), second)


if __name__ == "__main__":
    unittest.main()
