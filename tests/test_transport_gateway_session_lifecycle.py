"""
tests/test_transport_gateway_session_lifecycle.py
=====================================================
Regression tests for the Session Lifecycle fix in TransportGateway._handler.

Root cause this guards against: reader_thread.join() and
session_teardown() (RuntimeSession.teardown()'s blocking child.wait()/
child.kill(), see runtime.cloud_run_shell) used to run inline, on the
gateway's own single asyncio event loop, inside _handler's finally
block. Since this Shell serves every connection -- /healthz and every
/ws session -- on that one loop, a single session's ordinary disconnect
could stall it for several seconds, during which the loop could not
service Ping/Pong keepalive (-> client-observed "keepalive ping
timeout", close code 1011) or process any other connection's handshake
(-> a stale/abandoned reconnect attempt could claim the just-freed
single-session slot ahead of the client's real reconnect -> 409).

These tests exercise the real `_handler` coroutine (same fixture shape
as test_h6_control_event_relay.py) with a deliberately slow
session_teardown stand-in to prove, without guessing:
  1. _active_connection/_active_session are released as soon as the
     relay loop ends, not after teardown finishes -- and stay released
     regardless of how long teardown takes.
  2. teardown runs off the event-loop thread (a background executor
     thread), never on the thread the loop itself runs on.
  3. teardown fires exactly once per connection.
  4. the four Session Lifecycle trace stages (START / DISCONNECT /
     TEARDOWN START / TEARDOWN END) fire in that order, carrying the
     documented lifecycle_state, only when tracing is enabled.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import asyncio
import dataclasses
import os
import sys
import threading
import time
import unittest
from unittest import mock

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.transport_gateway import TransportGateway


class _FakeRequest:
    def __init__(self, path: str):
        self.path = path


class _FakeWebSocketConnection:
    """Same minimal ServerConnection stand-in as
    test_h6_control_event_relay.py: async-iterable over a fixed message
    list; exhausting it ends the relay loop like a clean disconnect."""

    def __init__(self, path: str, messages):
        self.request = _FakeRequest(path)
        self._messages = list(messages)
        self.sent = []

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        pass


@dataclasses.dataclass
class _FakeSession:
    audio_fd_w: int
    event_fd_r: int
    control_fd_w: int


def _make_pipes_with_immediate_eof_event_pipe():
    """audio/control pipes the handler writes to (unused by these tests,
    just need to exist), plus an event pipe whose write end is already
    closed -- gives the reader thread an instant EOF so no test pays the
    reader thread's 2s join timeout."""
    audio_r, audio_w = os.pipe()
    control_r, control_w = os.pipe()
    event_r, event_w = os.pipe()
    os.close(event_w)
    return audio_r, audio_w, control_r, control_w, event_r


class _PipeFixtureMixin:
    def setUp(self):
        (
            self.audio_r, self.audio_w,
            self.control_r, self.control_w,
            self.event_r,
        ) = _make_pipes_with_immediate_eof_event_pipe()
        self.session = _FakeSession(
            audio_fd_w=self.audio_w, event_fd_r=self.event_r, control_fd_w=self.control_w
        )

    def tearDown(self):
        for fd in (self.audio_r, self.audio_w, self.control_r, self.control_w, self.event_r):
            try:
                os.close(fd)
            except OSError:
                pass


class TestSessionTeardownRunsOffEventLoop(_PipeFixtureMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.main_thread_name = threading.current_thread().name
        self.teardown_calls = []
        self.teardown_thread_names = []

    def _slow_teardown(self, session):
        self.teardown_thread_names.append(threading.current_thread().name)
        time.sleep(0.2)
        self.teardown_calls.append(session)

    def _make_gateway(self):
        return TransportGateway(
            host="127.0.0.1", port=0, is_ready=lambda: True,
            session_factory=lambda provider: self.session,
            session_teardown=self._slow_teardown,
        )

    def test_active_slot_is_free_immediately_even_while_teardown_is_still_running(self):
        """
        This is the exact mechanism behind the 1011 -> 409 bug, made
        directly observable: a reconnect must be able to see the slot
        as free the instant the old relay loop ends -- without waiting
        for that old session's (potentially multi-second) subprocess
        shutdown. Pre-fix, this same check (running inline on the same
        blocked loop as teardown) could not even execute until teardown
        finished.
        """
        gateway = self._make_gateway()
        ws = _FakeWebSocketConnection("/ws?provider=openai", [])

        async def _run_and_check():
            await gateway._handler(ws)
            # _handler has returned: per the fixed finally block this
            # must already be true, well before _slow_teardown's 0.2s
            # sleep has elapsed.
            self.assertIsNone(gateway._active_connection)
            self.assertIsNone(gateway._active_session)
            # ... and teardown must NOT have completed yet at this exact
            # instant -- proves release is concurrent with (not merely
            # faster than) teardown, not just coincidentally fast.
            self.assertEqual(self.teardown_calls, [])
            # Let the background job finish so the test doesn't leak a
            # running thread past its own lifetime.
            await asyncio.sleep(0.4)
            self.assertEqual(self.teardown_calls, [self.session])

        asyncio.run(_run_and_check())

    def test_teardown_runs_on_a_different_thread_than_the_event_loop(self):
        gateway = self._make_gateway()
        ws = _FakeWebSocketConnection("/ws?provider=openai", [])
        # asyncio.run() waits for the loop's default executor to drain
        # before returning (shutdown_default_executor), so this blocks
        # until _slow_teardown has actually completed.
        asyncio.run(gateway._handler(ws))
        self.assertEqual(self.teardown_calls, [self.session])
        self.assertEqual(len(self.teardown_thread_names), 1)
        self.assertNotEqual(self.teardown_thread_names[0], self.main_thread_name)

    def test_teardown_called_exactly_once_never_twice(self):
        gateway = self._make_gateway()
        ws = _FakeWebSocketConnection("/ws?provider=openai", [])
        asyncio.run(gateway._handler(ws))
        self.assertEqual(len(self.teardown_calls), 1)


class TestSessionLifecycleTrace(_PipeFixtureMixin, unittest.TestCase):
    """Session START / DISCONNECT / TEARDOWN START / TEARDOWN END trace
    events fire in that order, only when tracing is enabled, and carry
    the documented lifecycle_state (see transport_gateway.py's
    _SESSION_STATE_* constants) -- so a stuck session is identifiable
    from the trace log alone."""

    def test_lifecycle_stages_traced_in_order_when_tracing_enabled(self):
        from runtime import transport_gateway as tg_module

        stages = []

        def _record(stage, session_id=None, event_id="", **fields):
            stages.append((stage, fields.get("lifecycle_state")))

        gateway = TransportGateway(
            host="127.0.0.1", port=0, is_ready=lambda: True,
            session_factory=lambda provider: self.session,
            session_teardown=lambda session: None,
        )
        ws = _FakeWebSocketConnection("/ws?provider=openai", [])

        with mock.patch.object(tg_module.runtime_trace, "enabled", return_value=True), \
             mock.patch.object(tg_module.runtime_trace, "emit", side_effect=_record):
            asyncio.run(gateway._handler(ws))

        lifecycle_stages = [(stage, state) for stage, state in stages if state is not None]
        self.assertEqual(
            lifecycle_stages,
            [
                ("Session START", "CONNECTED"),
                ("Session DISCONNECT", "DISCONNECTING"),
                ("Session TEARDOWN START", "TEARDOWN"),
                ("Session TEARDOWN END", "CLOSED"),
            ],
        )

    def test_no_lifecycle_trace_emitted_when_tracing_disabled(self):
        from runtime import transport_gateway as tg_module

        gateway = TransportGateway(
            host="127.0.0.1", port=0, is_ready=lambda: True,
            session_factory=lambda provider: self.session,
            session_teardown=lambda session: None,
        )
        ws = _FakeWebSocketConnection("/ws?provider=openai", [])

        with mock.patch.object(tg_module.runtime_trace, "enabled", return_value=False), \
             mock.patch.object(tg_module.runtime_trace, "emit") as mock_emit:
            asyncio.run(gateway._handler(ws))

        mock_emit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
