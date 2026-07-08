"""
tests/test_runtime_client_websocket_client.py
===================================================
Unit tests for src/runtime_client/websocket_client.py's
RuntimeWebSocketClient: the audio-out/control-out/event-in pump
coroutines in isolation, plus run()'s reconnect classification (fatal
handshake/close codes vs. retryable ones) and bounded exponential
backoff, against a lightweight fake `connect()` -- no real socket is
ever opened.

Uses unittest (stdlib) with unittest.mock for patching the module-level
`connect` symbol, consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import asyncio
import os
import sys
import unittest
from unittest.mock import AsyncMock, patch

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from websockets.exceptions import ConnectionClosed, InvalidStatus
from websockets.frames import Close

from runtime_client.websocket_client import RuntimeWebSocketClient


class _FakeWebSocket:
    """Async-iterable over a fixed inbound message list; records sends."""

    def __init__(self, inbound=None):
        self._inbound = list(inbound or [])
        self.sent = []
        self.closed_with = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._inbound:
            raise StopAsyncIteration
        return self._inbound.pop(0)

    async def send(self, data):
        self.sent.append(data)

    async def close(self, code=1000, reason=""):
        self.closed_with = (code, reason)


class TestSendAudio(unittest.IsolatedAsyncioTestCase):
    async def test_sends_bytes_from_queue_as_binary_frames(self):
        client = RuntimeWebSocketClient("ws://x", 3, 1.0)
        ws = _FakeWebSocket()
        queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        await queue.put(b"\x01\x02")
        await queue.put(b"\x03\x04")

        task = asyncio.ensure_future(client._send_audio(ws, queue))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(ws.sent, [b"\x01\x02", b"\x03\x04"])


class TestSendControl(unittest.IsolatedAsyncioTestCase):
    async def test_sends_strings_from_queue_as_text_frames(self):
        client = RuntimeWebSocketClient("ws://x", 3, 1.0)
        ws = _FakeWebSocket()
        queue: "asyncio.Queue[str]" = asyncio.Queue()
        await queue.put('{"command": "generate_summary"}')

        task = asyncio.ensure_future(client._send_control(ws, queue))
        await asyncio.sleep(0.05)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

        self.assertEqual(ws.sent, ['{"command": "generate_summary"}'])


class TestReceiveEvents(unittest.IsolatedAsyncioTestCase):
    async def test_text_messages_forwarded_to_on_event_in_order(self):
        client = RuntimeWebSocketClient("ws://x", 3, 1.0)
        ws = _FakeWebSocket(inbound=['{"type": "status"}', '{"type": "transcript"}'])
        received = []
        await client._receive_events(ws, received.append)
        self.assertEqual(received, ['{"type": "status"}', '{"type": "transcript"}'])

    async def test_binary_messages_ignored(self):
        client = RuntimeWebSocketClient("ws://x", 3, 1.0)
        ws = _FakeWebSocket(inbound=[b"\x01\x02", '{"type": "status"}', b"\x03"])
        received = []
        await client._receive_events(ws, received.append)
        self.assertEqual(received, ['{"type": "status"}'])


class _NeverEndingWebSocket(_FakeWebSocket):
    """__anext__ never resolves on its own -- only cancellation ends it,
    so the pump's stopper task is guaranteed to be the one that wins."""

    async def __anext__(self):
        await asyncio.Event().wait()
        raise AssertionError("unreachable")


class TestPumpStopEvent(unittest.IsolatedAsyncioTestCase):
    async def test_stop_event_closes_websocket_and_cancels_pending_tasks(self):
        client = RuntimeWebSocketClient("ws://x", 3, 1.0)
        ws = _NeverEndingWebSocket()
        audio_queue: "asyncio.Queue[bytes]" = asyncio.Queue()
        control_queue: "asyncio.Queue[str]" = asyncio.Queue()
        stop_event = asyncio.Event()

        async def _set_stop_soon():
            await asyncio.sleep(0.05)
            stop_event.set()

        asyncio.ensure_future(_set_stop_soon())
        await client._pump(ws, audio_queue, control_queue, stop_event, lambda line: None)

        self.assertEqual(ws.closed_with, (1000, "client shutdown"))


class TestRunReconnectClassification(unittest.IsolatedAsyncioTestCase):
    async def test_fatal_invalid_status_400_stops_without_retry(self):
        client = RuntimeWebSocketClient("ws://x", max_reconnect_attempts=5, backoff_base_seconds=0.01)
        call_count = 0

        def fake_connect(url):
            nonlocal call_count
            call_count += 1
            raise InvalidStatus(_FakeResponse(400))

        with patch("runtime_client.websocket_client.connect", side_effect=fake_connect):
            await client.run(asyncio.Queue(), asyncio.Queue(), asyncio.Event(), lambda line: None)

        self.assertEqual(call_count, 1)

    async def test_fatal_connection_closed_1008_stops_without_retry(self):
        client = RuntimeWebSocketClient("ws://x", max_reconnect_attempts=5, backoff_base_seconds=0.01)
        attempts = []

        class _OneShotWS(_FakeWebSocket):
            async def __anext__(self):
                raise ConnectionClosed(rcvd=Close(1008, "already connected"), sent=None)

        def fake_connect(url):
            attempts.append(url)
            return _FakeConnectCM(_OneShotWS())

        with patch("runtime_client.websocket_client.connect", side_effect=fake_connect):
            await client.run(asyncio.Queue(), asyncio.Queue(), asyncio.Event(), lambda line: None)

        self.assertEqual(len(attempts), 1)

    async def test_retryable_error_retries_up_to_max_then_gives_up(self):
        client = RuntimeWebSocketClient("ws://x", max_reconnect_attempts=2, backoff_base_seconds=0.001)
        call_count = 0

        def fake_connect(url):
            nonlocal call_count
            call_count += 1
            raise OSError("connection refused")

        with patch("runtime_client.websocket_client.connect", side_effect=fake_connect):
            await client.run(asyncio.Queue(), asyncio.Queue(), asyncio.Event(), lambda line: None)

        # 1 initial attempt + 2 retries = 3 total connect() calls.
        self.assertEqual(call_count, 3)

    async def test_stop_event_already_set_returns_without_connecting(self):
        client = RuntimeWebSocketClient("ws://x", max_reconnect_attempts=5, backoff_base_seconds=0.01)
        stop_event = asyncio.Event()
        stop_event.set()
        call_count = 0

        def fake_connect(url):
            nonlocal call_count
            call_count += 1
            raise AssertionError("must not connect once stop_event is already set")

        with patch("runtime_client.websocket_client.connect", side_effect=fake_connect):
            await client.run(asyncio.Queue(), asyncio.Queue(), stop_event, lambda line: None)

        self.assertEqual(call_count, 0)

    async def test_successful_handshake_resets_backoff_counter(self):
        client = RuntimeWebSocketClient("ws://x", max_reconnect_attempts=2, backoff_base_seconds=0.001)
        call_count = 0

        class _EmptyWS(_FakeWebSocket):
            pass  # __anext__ raises StopAsyncIteration immediately -> "disconnected by server"

        def fake_connect(url):
            nonlocal call_count
            call_count += 1
            if call_count <= 3:
                return _FakeConnectCM(_EmptyWS())
            raise OSError("give up now")

        stop_event = asyncio.Event()

        async def _stop_after_a_few():
            await asyncio.sleep(0.2)
            stop_event.set()

        asyncio.ensure_future(_stop_after_a_few())
        with patch("runtime_client.websocket_client.connect", side_effect=fake_connect):
            await client.run(asyncio.Queue(), asyncio.Queue(), stop_event, lambda line: None)

        # Every clean "disconnected by server" resets attempt=0, so this
        # must run far more than max_reconnect_attempts+1 times before
        # the 0.2s stop_event fires -- proving the counter never climbs.
        self.assertGreater(call_count, 3)


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code


class _FakeConnectCM:
    """Fakes `async with connect(url) as websocket: ...`."""

    def __init__(self, ws):
        self._ws = ws

    async def __aenter__(self):
        return self._ws

    async def __aexit__(self, *exc_info):
        return False


if __name__ == "__main__":
    unittest.main()
