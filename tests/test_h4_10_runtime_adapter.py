"""
tests/test_h4_10_runtime_adapter.py
======================================
H4-10 Runtime Adapter tests.

Validates runtime.event_adapter.RuntimeEventAdapter against:
  - docs/H4_RUNTIME_EVENT_CONTRACT.md (v1.0, Frozen)
  - docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md (Resolved Decisions)

Every raw event fixture below is not a synthetic Contract-shaped event --
it is a literal, byte-for-byte reproduction of what a specific, real
_emit_event(...) call site in src/phantom_runtime.py actually constructs
(see docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md section 1's call
site table for the line numbers each fixture corresponds to). This module
never imports phantom_runtime, runtime.cloud_run_shell, or
runtime.transport_gateway -- per the Single Runtime Policy, the Cloud Run
Runtime is exercised only through the literal shape of the events it is
known (by reading its unmodified source) to emit, never invoked directly.
It does import the real, unmodified VerificationRuntime (H4-2) -- the
Runtime Adapter's whole purpose is to produce Contract-shaped events that
VerificationRuntime consumes, so that dependency is expected and required,
not a scope violation.

Uses unittest (stdlib), consistent with the rest of this project's test
suite (pytest is not a dependency).
"""

import ast
import os
import sys
import unittest
import uuid

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.event_adapter import RuntimeEventAdapter
from verification.verification_runtime import VerificationRuntime


# ---------------------------------------------------------------------------
# Raw wire-format fixtures -- literal reproductions of real _emit_event(...)
# call sites (src/phantom_runtime.py). See module docstring.
# ---------------------------------------------------------------------------

_TS = "2026-07-06T12:00:00+00:00"


def _raw_event(event_type, payload):
    """Exactly the envelope shape _emit_event() constructs (phantom_runtime.py:597-602)."""
    return {"version": 1, "type": event_type, "timestamp": _TS, "payload": payload}


# phantom_runtime.py:822 -- _set_state
RAW_STATUS = _raw_event("status", {"state": "recruiter_speaking", "previous": "idle"})

# phantom_runtime.py:892 -- show_err
RAW_ERROR = _raw_event("error", {"label": "Audio", "message": "fd read failed: EOF"})

# phantom_runtime.py:900 -- show_latency
RAW_LATENCY = _raw_event("latency", {"stt_ms": 120.0, "gpt_ms": 300.0, "total_ms": 420.0})

# phantom_runtime.py:2250 -- _emit_line, [JP] branch
RAW_REPLY_LINE = _raw_event("reply", {"lang": "ja", "text": "こんにちは", "speaker": "agent"})

# phantom_runtime.py:3097 -- reply_worker, heard speech
RAW_TRANSCRIPT = _raw_event(
    "transcript", {"text": "hello there", "lang": "en", "ts": 1720000000.0, "speaker": "user"}
)

# phantom_runtime.py:3222 -- reply_worker, final agent reply
RAW_REPLY_FINAL = _raw_event(
    "reply", {"text": "Nice to meet you", "lang": "en", "speaker": "agent", "ts": 1720000001.0}
)

# phantom_runtime.py:9134 -- generate_meeting_analysis
RAW_ANALYSIS = _raw_event("analysis", {"text": "User introduced themselves; no risks detected."})


# ---------------------------------------------------------------------------
# Envelope translation
# ---------------------------------------------------------------------------

class EnvelopeTranslationTests(unittest.TestCase):
    def test_schema_version_maps_int_1_to_str_1_0(self):
        event = RuntimeEventAdapter().translate(RAW_STATUS)
        self.assertEqual(event["schema_version"], "1.0")
        self.assertIsInstance(event["schema_version"], str)

    def test_unmapped_version_falls_back_to_str_without_guessing_format(self):
        raw = dict(RAW_STATUS)
        raw["version"] = 2
        event = RuntimeEventAdapter().translate(raw)
        self.assertEqual(event["schema_version"], "2")

    def test_timestamp_passes_through_unchanged(self):
        event = RuntimeEventAdapter().translate(RAW_TRANSCRIPT)
        self.assertEqual(event["timestamp"], _TS)

    def test_type_passes_through_unchanged(self):
        for raw, expected_type in (
            (RAW_STATUS, "status"), (RAW_ERROR, "error"), (RAW_LATENCY, "latency"),
            (RAW_REPLY_LINE, "reply"), (RAW_TRANSCRIPT, "transcript"), (RAW_ANALYSIS, "analysis"),
        ):
            with self.subTest(event_type=expected_type):
                event = RuntimeEventAdapter().translate(raw)
                self.assertEqual(event["type"], expected_type)

    def test_event_id_is_generated_and_unique_per_event(self):
        adapter = RuntimeEventAdapter()
        e1 = adapter.translate(RAW_STATUS)
        e2 = adapter.translate(RAW_STATUS)
        self.assertTrue(uuid.UUID(e1["event_id"]))
        self.assertTrue(uuid.UUID(e2["event_id"]))
        self.assertNotEqual(e1["event_id"], e2["event_id"])

    def test_session_id_is_stable_across_events_from_the_same_adapter_instance(self):
        adapter = RuntimeEventAdapter()
        e1 = adapter.translate(RAW_STATUS)
        e2 = adapter.translate(RAW_LATENCY)
        self.assertTrue(uuid.UUID(e1["session_id"]))
        self.assertEqual(e1["session_id"], e2["session_id"])

    def test_session_id_can_be_pinned_explicitly(self):
        adapter = RuntimeEventAdapter(session_id="sess-fixed")
        event = adapter.translate(RAW_STATUS)
        self.assertEqual(event["session_id"], "sess-fixed")

    def test_two_adapter_instances_get_different_session_ids(self):
        e1 = RuntimeEventAdapter().translate(RAW_STATUS)
        e2 = RuntimeEventAdapter().translate(RAW_STATUS)
        self.assertNotEqual(e1["session_id"], e2["session_id"])

    def test_sequence_is_monotonically_increasing_from_1_per_instance(self):
        adapter = RuntimeEventAdapter()
        sequences = [adapter.translate(RAW_STATUS)["sequence"] for _ in range(4)]
        self.assertEqual(sequences, [1, 2, 3, 4])

    def test_sequence_restarts_for_a_new_adapter_instance(self):
        self.assertEqual(RuntimeEventAdapter().translate(RAW_STATUS)["sequence"], 1)
        self.assertEqual(RuntimeEventAdapter().translate(RAW_STATUS)["sequence"], 1)

    def test_raw_event_is_never_mutated(self):
        raw = _raw_event("status", {"state": "idle", "previous": "generating"})
        snapshot = {"version": 1, "type": "status", "timestamp": _TS,
                    "payload": {"state": "idle", "previous": "generating"}}
        RuntimeEventAdapter().translate(raw)
        self.assertEqual(raw, snapshot)


# ---------------------------------------------------------------------------
# Payload mapping -- per docs/H4_10_RUNTIME_EVENT_ANALYSIS_AND_MAPPING.md
# ---------------------------------------------------------------------------

class TranscriptPayloadMappingTests(unittest.TestCase):
    def setUp(self):
        self.payload = RuntimeEventAdapter().translate(RAW_TRANSCRIPT)["payload"]

    def test_text_is_direct(self):
        self.assertEqual(self.payload["text"], "hello there")

    def test_lang_renames_to_language(self):
        self.assertEqual(self.payload["language"], "en")
        self.assertNotIn("lang", self.payload)

    def test_ts_and_speaker_preserved_as_extra(self):
        self.assertEqual(self.payload["ts"], 1720000000.0)
        self.assertEqual(self.payload["speaker"], "user")

    def test_confidence_and_is_final_are_gaps_not_fabricated(self):
        self.assertNotIn("confidence", self.payload)
        self.assertNotIn("is_final", self.payload)


class ReplyPayloadMappingTests(unittest.TestCase):
    def test_emit_line_variant_text_direct_and_extras_preserved(self):
        payload = RuntimeEventAdapter().translate(RAW_REPLY_LINE)["payload"]
        self.assertEqual(payload["text"], "こんにちは")
        self.assertEqual(payload["lang"], "ja")
        self.assertEqual(payload["speaker"], "agent")

    def test_reply_worker_variant_text_direct_and_extras_preserved(self):
        payload = RuntimeEventAdapter().translate(RAW_REPLY_FINAL)["payload"]
        self.assertEqual(payload["text"], "Nice to meet you")
        self.assertEqual(payload["lang"], "en")
        self.assertEqual(payload["speaker"], "agent")
        self.assertEqual(payload["ts"], 1720000001.0)

    def test_provider_model_finish_reason_are_gaps_not_fabricated(self):
        payload = RuntimeEventAdapter().translate(RAW_REPLY_FINAL)["payload"]
        self.assertNotIn("provider", payload)
        self.assertNotIn("model", payload)
        self.assertNotIn("finish_reason", payload)


class AnalysisPayloadMappingTests(unittest.TestCase):
    def setUp(self):
        self.payload = RuntimeEventAdapter().translate(RAW_ANALYSIS)["payload"]

    def test_text_renames_to_summary(self):
        self.assertEqual(self.payload["summary"], "User introduced themselves; no risks detected.")
        self.assertNotIn("text", self.payload)

    def test_intent_and_metadata_are_gaps_not_fabricated(self):
        self.assertNotIn("intent", self.payload)
        self.assertNotIn("metadata", self.payload)


class LatencyPayloadMappingTests(unittest.TestCase):
    def setUp(self):
        self.payload = RuntimeEventAdapter().translate(RAW_LATENCY)["payload"]

    def test_stt_ms_and_total_ms_are_direct(self):
        self.assertEqual(self.payload["stt_ms"], 120.0)
        self.assertEqual(self.payload["total_ms"], 420.0)

    def test_gpt_ms_renames_to_provider_ms(self):
        self.assertEqual(self.payload["provider_ms"], 300.0)
        self.assertNotIn("gpt_ms", self.payload)

    def test_routing_ms_is_a_gap_not_fabricated(self):
        self.assertNotIn("routing_ms", self.payload)


class StatusPayloadMappingTests(unittest.TestCase):
    def setUp(self):
        self.payload = RuntimeEventAdapter().translate(RAW_STATUS)["payload"]

    def test_state_passes_through_untranslated_not_case_folded_or_remapped(self):
        # Resolved Decision 4: no vocabulary translation, no case folding --
        # the real value is carried verbatim, even though it is not a
        # member of the Contract's 5-state enum.
        self.assertEqual(self.payload["state"], "recruiter_speaking")

    def test_previous_preserved_as_extra(self):
        self.assertEqual(self.payload["previous"], "idle")

    def test_message_is_a_gap_not_fabricated(self):
        self.assertNotIn("message", self.payload)


class ErrorPayloadMappingTests(unittest.TestCase):
    def setUp(self):
        self.payload = RuntimeEventAdapter().translate(RAW_ERROR)["payload"]

    def test_message_is_direct(self):
        self.assertEqual(self.payload["message"], "fd read failed: EOF")

    def test_label_is_preserved_as_extra_not_renamed_to_code(self):
        # Resolved Decision 3: label -> code rename was rejected.
        self.assertEqual(self.payload["label"], "Audio")
        self.assertNotIn("code", self.payload)

    def test_recoverable_is_a_gap_not_fabricated(self):
        self.assertNotIn("recoverable", self.payload)


# ---------------------------------------------------------------------------
# Downstream integration -- adapter output handed to the real,
# already-approved VerificationRuntime (H4-2), unmodified.
# ---------------------------------------------------------------------------

class AdapterIntoVerificationRuntimeTests(unittest.TestCase):
    """Confirms the adapter's output is legitimately Contract-shaped by
    handing it to the real VerificationRuntime, and confirms the expected
    (honest, documented) gaps are reported -- not hidden by the adapter.
    """

    def test_identity_propagates_into_verification_result(self):
        adapter = RuntimeEventAdapter(session_id="sess-h4-10")
        event = adapter.translate(RAW_LATENCY)
        result = VerificationRuntime().handle(event)
        self.assertEqual(result.source_event_id, event["event_id"])
        self.assertEqual(result.session_id, "sess-h4-10")

    def test_reply_event_reports_gap_for_missing_provider_model_finish_reason(self):
        event = RuntimeEventAdapter().translate(RAW_REPLY_FINAL)
        result = VerificationRuntime().handle(event)
        self.assertTrue(result.gap_detected)
        self.assertIn("provider", result.gap_reason)
        self.assertIn("model", result.gap_reason)
        self.assertIn("finish_reason", result.gap_reason)

    def test_status_event_reports_undefined_state_even_for_idle(self):
        # Resolved Decision 4: passing "idle" through untranslated means it
        # still doesn't match the Contract's uppercase IDLE -- an accepted,
        # honest signal, not an adapter defect.
        raw = _raw_event("status", {"state": "idle", "previous": "generating"})
        event = RuntimeEventAdapter().translate(raw)
        result = VerificationRuntime().handle(event)
        self.assertTrue(result.gap_detected)
        self.assertIn("undefined state", result.gap_reason)

    def test_error_event_reports_gap_for_missing_code_and_recoverable(self):
        event = RuntimeEventAdapter().translate(RAW_ERROR)
        result = VerificationRuntime().handle(event)
        self.assertTrue(result.gap_detected)
        self.assertIn("code", result.gap_reason)
        self.assertIn("recoverable", result.gap_reason)

    def test_ordered_sequence_from_shared_adapter_causes_no_ordering_gap(self):
        adapter = RuntimeEventAdapter(session_id="sess-order")
        shared_verification_runtime = VerificationRuntime()
        last_result = None
        for raw in (RAW_TRANSCRIPT, RAW_TRANSCRIPT, RAW_TRANSCRIPT):
            event = adapter.translate(raw)
            last_result = shared_verification_runtime.handle(event)
        self.assertNotIn("sequence regression", last_result.gap_reason or "")
        self.assertNotIn("timestamp moved backwards", last_result.gap_reason or "")


# ---------------------------------------------------------------------------
# Single Runtime Policy / no-duplicate-contract guardrails
# ---------------------------------------------------------------------------

class AdapterScopeGuardrailTests(unittest.TestCase):
    def test_event_adapter_module_defines_no_new_dataclass(self):
        adapter_path = os.path.join(_SRC_DIR, "runtime", "event_adapter.py")
        with open(adapter_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                decorator_names = {
                    (d.id if isinstance(d, ast.Name) else getattr(d, "attr", ""))
                    for d in node.decorator_list
                }
                self.assertNotIn(
                    "dataclass", decorator_names,
                    "Runtime Adapter must not introduce a new dataclass/DTO",
                )

    def test_event_adapter_module_imports_nothing_from_cloud_run_runtime_or_providers(self):
        # The Runtime Adapter's job is to produce Contract-shaped events
        # for VerificationRuntime to consume -- that is the only permitted
        # downstream H4 dependency:
        #
        #   Runtime Adapter -> VerificationRuntime (allowed)
        #                         -> Trust / Dashboard / Aggregator / API
        #                            (not imported by the adapter)
        #
        # It must not itself reach into the Cloud Run Runtime, a Provider,
        # or any *other* downstream H4 component (that would be a real
        # scope violation: the adapter doing Trust/Dashboard/Aggregation/
        # API work rather than pure field mapping). It legitimately has
        # zero imports from any of these today; this guards against that
        # changing silently.
        adapter_path = os.path.join(_SRC_DIR, "runtime", "event_adapter.py")
        with open(adapter_path, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
                names.update(alias.name for alias in node.names)

        forbidden_substrings = (
            "phantom_runtime", "cloud_run_shell", "transport_gateway",
            "provider", "trust_runtime", "dashboard_runtime",
            "event_aggregator", "api_server",
        )
        for name in names:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden, lowered,
                    f"Runtime Adapter imported forbidden module: {name}",
                )

    def test_this_test_module_never_imports_cloud_run_runtime_directly(self):
        with open(__file__, "r", encoding="utf-8") as f:
            tree = ast.parse(f.read())

        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    names.add(node.module)
                names.update(alias.name for alias in node.names)

        forbidden_substrings = ("phantom_runtime", "cloud_run_shell", "transport_gateway")
        for name in names:
            lowered = name.lower()
            for forbidden in forbidden_substrings:
                self.assertNotIn(
                    forbidden, lowered,
                    f"H4-10 adapter test imported forbidden module: {name}",
                )


if __name__ == "__main__":
    unittest.main()
