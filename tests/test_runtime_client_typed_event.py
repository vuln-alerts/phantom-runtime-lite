"""
tests/test_runtime_client_typed_event.py
=============================================
Unit tests for src/runtime_client/typed_event.py's TypedEventStore --
the Client's local mirror/renderer for the Runtime's Typed Event stream
(H4 Runtime Event Contract, docs/H4_RUNTIME_EVENT_CONTRACT.md). Feeds
literal JSON-line envelopes (the exact wire shape
runtime.transport_gateway relays from event_fd_r, see
tests/test_h4_10_runtime_adapter.py's RAW_* fixtures for the
server-side equivalent) into handle_line() and asserts both the local
mirror state and the rendered console output.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.typed_event import TypedEventStore


def _line(event_type, payload):
    return json.dumps({"version": 1, "type": event_type, "payload": payload})


class TestTypedEventStoreTranscriptAndReply(unittest.TestCase):
    def setUp(self):
        self.store = TypedEventStore()

    def test_transcript_event_appended_to_log(self):
        with redirect_stdout(io.StringIO()):
            self.store.handle_line(
                _line("transcript", {"text": "hello there", "lang": "en", "speaker": "user", "ts": "12:00:00"})
            )
        self.assertEqual(len(self.store.transcript_log), 1)
        entry = self.store.transcript_log[0]
        self.assertEqual(entry.text, "hello there")
        self.assertEqual(entry.lang, "en")
        self.assertEqual(entry.speaker, "user")

    def test_reply_event_appended_to_log(self):
        with redirect_stdout(io.StringIO()):
            self.store.handle_line(_line("reply", {"text": "Nice to meet you", "lang": "en"}))
        self.assertEqual(len(self.store.transcript_log), 1)
        self.assertEqual(self.store.transcript_log[0].speaker, "agent")  # default when omitted

    def test_transcript_then_reply_both_land_in_order(self):
        with redirect_stdout(io.StringIO()):
            self.store.handle_line(_line("transcript", {"text": "Q", "lang": "en", "speaker": "user"}))
            self.store.handle_line(_line("reply", {"text": "A", "lang": "en"}))
        self.assertEqual([e.text for e in self.store.transcript_log], ["Q", "A"])

    def test_log_is_bounded_by_maxlen(self):
        store = TypedEventStore(maxlen=3)
        with redirect_stdout(io.StringIO()):
            for i in range(5):
                store.handle_line(_line("transcript", {"text": str(i), "lang": "en"}))
        self.assertEqual([e.text for e in store.transcript_log], ["2", "3", "4"])


class TestTypedEventStoreStatusAndLatency(unittest.TestCase):
    def setUp(self):
        self.store = TypedEventStore()

    def test_status_event_updates_last_status(self):
        with redirect_stdout(io.StringIO()):
            self.store.handle_line(_line("status", {"state": "recruiter_speaking", "previous": "idle"}))
        self.assertEqual(self.store.last_status, {"state": "recruiter_speaking", "previous": "idle"})
        self.assertEqual(self.store.status_line(), "state=recruiter_speaking")

    def test_status_line_before_any_status_event(self):
        self.assertEqual(self.store.status_line(), "state=(no status event received yet)")

    def test_latency_event_does_not_raise_and_does_not_touch_log(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.store.handle_line(_line("latency", {"stt_ms": 120.0, "total_ms": 420.0}))
        self.assertIn("STT=120.0ms", buf.getvalue())
        self.assertIn("TOTAL=420.0ms", buf.getvalue())
        self.assertEqual(len(self.store.transcript_log), 0)


class TestTypedEventStoreErrorAndAnalysis(unittest.TestCase):
    def setUp(self):
        self.store = TypedEventStore()

    def test_error_event_rendered_with_label_and_message(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.store.handle_line(_line("error", {"label": "Audio", "message": "fd read failed: EOF"}))
        out = buf.getvalue()
        self.assertIn("Audio", out)
        self.assertIn("fd read failed: EOF", out)

    def test_analysis_event_rendered(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.store.handle_line(_line("analysis", {"text": "User introduced themselves."}))
        self.assertIn("User introduced themselves.", buf.getvalue())


class TestTypedEventStoreMalformedAndUnknown(unittest.TestCase):
    def setUp(self):
        self.store = TypedEventStore()

    def test_unparseable_json_does_not_raise(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.store.handle_line("not json at all {{{")
        self.assertIn("unparseable event", buf.getvalue())
        self.assertEqual(len(self.store.transcript_log), 0)

    def test_non_dict_json_does_not_raise(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.store.handle_line(json.dumps([1, 2, 3]))
        self.assertIn("unexpected event shape", buf.getvalue())

    def test_unknown_event_type_shown_generically_not_dropped(self):
        with redirect_stdout(io.StringIO()) as buf:
            self.store.handle_line(_line("future_event_type", {"foo": "bar"}))
        self.assertIn("future_event_type", buf.getvalue())
        self.assertIn("foo", buf.getvalue())

    def test_missing_payload_defaults_to_empty_dict(self):
        with redirect_stdout(io.StringIO()):
            # Should not raise even though "payload" key is entirely absent.
            self.store.handle_line(json.dumps({"version": 1, "type": "status"}))
        self.assertEqual(self.store.last_status, {})


if __name__ == "__main__":
    unittest.main()
