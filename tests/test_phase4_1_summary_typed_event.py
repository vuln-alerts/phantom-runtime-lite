"""
tests/test_phase4_1_summary_typed_event.py
===============================================
Phase 4-1 regression test: generate_summary() (src/phantom_runtime.py)
must emit a Typed Event, the same way generate_meeting_analysis()
already does, so the Runtime Client actually receives Summary results
instead of them only landing in the Cloud Run container's own stdout
(the bug found during Phase 4's live E2E pass -- see
docs/MIGRATION_MATRIX.md's 2026-07-09 "Summary" row and
docs/RUNBOOK.md's Known Limitations for the discovery).

Per this project's Single Runtime Policy (see
tests/test_h4_10_runtime_adapter.py's module docstring), this module
never imports phantom_runtime.py directly. Two independent checks:

  1. AST-based structural check that generate_summary()'s body contains
     a call `_emit_event("analysis", ...)`, mirroring how the same
     check would be written for any other _emit_event call site in
     this monolith without executing it.
  2. A literal reproduction of the exact wire envelope that call site
     now produces, run through the real (unmodified) RuntimeEventAdapter
     -- confirming the fix's output is Contract-shaped, not just that
     the call exists syntactically.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import ast
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.event_adapter import RuntimeEventAdapter

_PHANTOM_RUNTIME_PATH = os.path.join(_SRC_DIR, "phantom_runtime.py")


def _find_function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function {name!r} not found in phantom_runtime.py")


def _emit_event_calls(func_node: ast.FunctionDef):
    """Yields (event_type_str_or_None, call_node) for every _emit_event(...)
    call anywhere in func_node's body (including nested blocks)."""
    for node in ast.walk(func_node):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_emit_event"
        ):
            event_type = None
            if node.args and isinstance(node.args[0], ast.Constant):
                event_type = node.args[0].value
            yield event_type, node


class GenerateSummaryEmitsTypedEventStructuralTests(unittest.TestCase):
    """Confirms the fix exists in source, without importing/running the
    Single-Runtime-Policy-protected monolith."""

    @classmethod
    def setUpClass(cls):
        with open(_PHANTOM_RUNTIME_PATH, "r", encoding="utf-8") as f:
            cls.tree = ast.parse(f.read())
        cls.generate_summary = _find_function(cls.tree, "generate_summary")
        cls.generate_meeting_analysis = _find_function(cls.tree, "generate_meeting_analysis")

    def test_generate_summary_calls_emit_event_with_analysis_type(self):
        calls = list(_emit_event_calls(self.generate_summary))
        analysis_calls = [c for etype, c in calls if etype == "analysis"]
        self.assertEqual(
            len(analysis_calls), 1,
            "generate_summary() must call _emit_event(\"analysis\", ...) exactly once "
            "(same event type generate_meeting_analysis() uses)",
        )

    def test_emit_event_call_passes_text_keyword(self):
        calls = list(_emit_event_calls(self.generate_summary))
        analysis_call = next(c for etype, c in calls if etype == "analysis")
        kwarg_names = {kw.arg for kw in analysis_call.keywords}
        self.assertIn(
            "text", kwarg_names,
            "_emit_event(\"analysis\", text=...) must pass the summary text as "
            "'text' -- RuntimeEventAdapter's _PAYLOAD_FIELD_MAP only renames "
            "'text' -> 'summary' for the analysis event type",
        )

    def test_same_event_type_as_meeting_analysis(self):
        # generate_meeting_analysis() is the existing, already-working
        # reference implementation this fix is required to mirror
        # ("Meeting Analysisと同様に" per the task spec).
        summary_calls = [
            etype for etype, _ in _emit_event_calls(self.generate_summary)
        ]
        meeting_calls = [
            etype for etype, _ in _emit_event_calls(self.generate_meeting_analysis)
        ]
        self.assertIn("analysis", summary_calls)
        self.assertIn("analysis", meeting_calls)

    def test_emit_event_happens_before_console_print(self):
        # Ordering check mirroring generate_meeting_analysis()'s own
        # shape: _emit_event() first, then the local console _print().
        # Walking function.body's top-level statements inside the `try`
        # block (both functions wrap the provider call + emit + print in
        # one try/except).
        try_node = next(
            n for n in ast.walk(self.generate_summary) if isinstance(n, ast.Try)
        )
        call_names_in_order = []
        for stmt in ast.walk(try_node):
            if (
                isinstance(stmt, ast.Call)
                and isinstance(stmt.func, ast.Name)
                and stmt.func.id in ("_emit_event", "_print")
            ):
                call_names_in_order.append((stmt.lineno, stmt.func.id))
        call_names_in_order.sort()
        names_only = [name for _, name in call_names_in_order]
        self.assertIn("_emit_event", names_only)
        self.assertIn("_print", names_only)
        self.assertLess(
            names_only.index("_emit_event"), names_only.index("_print"),
            "_emit_event() must run before the local console _print(), matching "
            "generate_meeting_analysis()'s existing order",
        )


# ---------------------------------------------------------------------------
# Literal reproduction of the new call site's wire envelope, translated
# through the real, unmodified RuntimeEventAdapter -- same convention as
# test_h4_10_runtime_adapter.py's RAW_* fixtures.
# ---------------------------------------------------------------------------

_TS = "2026-07-09T12:00:00+00:00"

# phantom_runtime.py:generate_summary() -- _emit_event("analysis", text=summary)
RAW_SUMMARY_ANALYSIS = {
    "version": 1,
    "type": "analysis",
    "timestamp": _TS,
    "payload": {"text": "リクルーターの発言: プロジェクトのタイムラインについて。次のステップ: 未定。"},
}


class SummaryAnalysisEventAdapterTranslationTests(unittest.TestCase):
    def setUp(self):
        self.payload = RuntimeEventAdapter().translate(RAW_SUMMARY_ANALYSIS)["payload"]

    def test_text_renames_to_summary_field(self):
        self.assertEqual(
            self.payload["summary"],
            "リクルーターの発言: プロジェクトのタイムラインについて。次のステップ: 未定。",
        )
        self.assertNotIn("text", self.payload)

    def test_translated_envelope_has_analysis_type(self):
        envelope = RuntimeEventAdapter().translate(RAW_SUMMARY_ANALYSIS)
        self.assertEqual(envelope["type"], "analysis")


if __name__ == "__main__":
    unittest.main()
