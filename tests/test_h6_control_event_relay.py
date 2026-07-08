"""
tests/test_h6_control_event_relay.py
=========================================
H6 unit tests for TransportGateway's per-session relay loop (`_handler`):
inbound WebSocket text frames (Control Events) are relayed verbatim to
the session's control pipe, inbound binary frames (audio) are relayed
verbatim to the session's audio pipe, and the active-connection/session
slot plus session_teardown are exercised exactly once per connection.

Exercises the real `_handler` coroutine against real os.pipe() fds and a
lightweight fake WebSocket connection (async-iterable over a fixed
message list) -- no real socket is opened and no Runtime child is ever
spawned (session_factory returns a plain stand-in object). This module
never imports phantom_runtime -- per the Single Runtime Policy (see
tests/test_h4_10_runtime_adapter.py), TransportGateway has no knowledge
of what a Control Event *means*, only that it is a text frame relayed
byte-for-byte onto the pipe fd phantom_runtime.py's PHANTOM_CONTROL_FD
reader is documented to consume; that consumption is out of scope here.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import asyncio
import dataclasses
import os
import sys
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.transport_gateway import TransportGateway


class _FakeRequest:
    def __init__(self, path: str):
        self.path = path


class _FakeWebSocketConnection:
    """
    Minimal stand-in for websockets' ServerConnection: async-iterable
    over a fixed list of inbound messages (str = text frame, bytes =
    binary frame), matching exactly what `_handler`'s
    `async for message in websocket` loop consumes. Exhausting the list
    ends the loop the same way a clean disconnect would.
    """

    def __init__(self, path: str, messages):
        self.request = _FakeRequest(path)
        self._messages = list(messages)
        self.sent = []
        self.closed_with = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return self._messages.pop(0)

    async def send(self, text):
        self.sent.append(text)

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


@dataclasses.dataclass
class _FakeSession:
    audio_fd_w: int
    event_fd_r: int
    control_fd_w: int


class TestControlEventRelay(unittest.TestCase):
    def setUp(self):
        self._teardown_calls = []

        # Session-owned ends the gateway writes to; test-owned ends read
        # back what the gateway wrote, to assert relay fidelity.
        self.audio_r, self.audio_w = os.pipe()
        self.control_r, self.control_w = os.pipe()

        # The event pipe is drained by a background reader thread inside
        # _handler; closing the write end immediately gives that thread
        # an instant EOF so the test doesn't pay its 2s join timeout.
        event_r, event_w = os.pipe()
        os.close(event_w)
        self.event_r = event_r

        self.session = _FakeSession(
            audio_fd_w=self.audio_w, event_fd_r=self.event_r, control_fd_w=self.control_w
        )

        self.gateway = TransportGateway(
            host="127.0.0.1",
            port=0,
            is_ready=lambda: True,
            session_factory=lambda provider: self.session,
            session_teardown=lambda session: self._teardown_calls.append(session),
        )

    def tearDown(self):
        for fd in (self.audio_r, self.audio_w, self.control_r, self.control_w, self.event_r):
            try:
                os.close(fd)
            except OSError:
                pass

    def _run(self, messages):
        ws = _FakeWebSocketConnection("/ws?provider=openai", messages)
        asyncio.run(self.gateway._handler(ws))
        return ws

    def test_text_frame_relayed_to_control_pipe_verbatim(self):
        self._run([b'{"already":"queued audio frame"}', '{"command": "generate_summary"}'])
        self.assertEqual(
            os.read(self.control_r, 4096), b'{"command": "generate_summary"}\n'
        )

    def test_multiple_control_events_relayed_in_order_one_per_line(self):
        self._run(
            [
                '{"command": "toggle_recording"}',
                '{"command": "generate_meeting_analysis"}',
            ]
        )
        self.assertEqual(
            os.read(self.control_r, 4096),
            b'{"command": "toggle_recording"}\n{"command": "generate_meeting_analysis"}\n',
        )

    def test_binary_frame_relayed_to_audio_pipe_not_control_pipe(self):
        self._run([b"\x01\x02\x03\x04"])
        self.assertEqual(os.read(self.audio_r, 4096), b"\x01\x02\x03\x04")
        # Nothing was written to the control pipe -- closing the write
        # end now (post-handler) turns the read into an immediate EOF
        # instead of a blocking wait, so absence of data is observable.
        os.close(self.control_w)
        self.assertEqual(os.read(self.control_r, 4096), b"")

    def test_mixed_audio_and_control_frames_each_go_to_their_own_pipe(self):
        self._run([b"\xaa\xbb", '{"command": "generate_summary"}', b"\xcc\xdd"])
        self.assertEqual(os.read(self.audio_r, 4096), b"\xaa\xbb\xcc\xdd")
        self.assertEqual(os.read(self.control_r, 4096), b'{"command": "generate_summary"}\n')

    def test_session_teardown_called_exactly_once_after_relay_completes(self):
        self._run(['{"command": "generate_summary"}'])
        self.assertEqual(self._teardown_calls, [self.session])

    def test_active_connection_and_session_slots_released_after_handler_returns(self):
        self._run(['{"command": "generate_summary"}'])
        self.assertIsNone(self.gateway._active_connection)
        self.assertIsNone(self.gateway._active_session)

    def test_control_pipe_gone_breaks_relay_loop_without_raising(self):
        os.close(self.control_r)
        os.close(self.control_w)
        # Any write to the now-fully-closed control pipe raises OSError;
        # _handler must catch it, break the relay loop, and still tear
        # down cleanly rather than propagate.
        self._run(['{"command": "generate_summary"}', b"\x01\x02"])
        self.assertEqual(self._teardown_calls, [self.session])


if __name__ == "__main__":
    unittest.main()
