"""
api/dashboard_view.py
========================
Renders the Dashboard HTML page from a DashboardResult (or its absence).

Markup lives entirely in src/api/templates/*.html; this module only reads
those files and fills in placeholders via string.Template -- it never
assembles HTML tags itself. Every substituted value is html.escape()'d
before insertion, since DashboardResult fields (e.g. review_reason,
warnings) may contain arbitrary text.

EXPORTED API:
  render_dashboard_html(dashboard_result: Optional[DashboardResult]) -> str
"""

import html
import os
import string
from typing import Optional

from dashboard.dashboard_result import DashboardResult

_TEMPLATES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")


def _read_template(name: str) -> str:
    with open(os.path.join(_TEMPLATES_DIR, name), "r", encoding="utf-8") as f:
        return f.read()


def _yes_no(value: bool) -> str:
    return "Yes" if value else "No"


def render_dashboard_html(dashboard_result: Optional[DashboardResult]) -> str:
    if dashboard_result is None:
        return _read_template("dashboard_empty.html")

    warnings_text = "; ".join(dashboard_result.warnings) if dashboard_result.warnings else "(none)"

    fields = {
        "trust_score":           f"{dashboard_result.trust_score:.3f}",
        "trust_level":           dashboard_result.trust_level,
        "reliability_score":     f"{dashboard_result.reliability_score:.3f}",
        "gap_detected":          _yes_no(dashboard_result.gap_detected),
        "fallback_detected":     _yes_no(dashboard_result.fallback_detected),
        "human_review_required": _yes_no(dashboard_result.human_review_required),
        "review_reason":         dashboard_result.review_reason or "—",
        "warnings":              warnings_text,
        "session_id":            dashboard_result.session_id or "—",
        "event_id":              dashboard_result.source_event_id or "—",
        "timestamp":             dashboard_result.timestamp.isoformat(),
    }
    escaped_fields = {key: html.escape(str(value)) for key, value in fields.items()}

    return string.Template(_read_template("dashboard.html")).substitute(**escaped_fields)
