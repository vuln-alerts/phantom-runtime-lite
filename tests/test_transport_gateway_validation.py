"""
tests/test_transport_gateway_validation.py
=================================================
H5-1 unit tests for TransportGateway's pre-handshake validation:
provider validation (400), the shutdown-readiness reject (503), and the
best-effort already-connected fast path (409). These exercise
_process_request in isolation via lightweight stand-ins for the
websockets Request/connection objects -- no real socket is opened and
no Runtime child is ever spawned (session_factory asserts if called).

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import asyncio
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


class _FakeConnection:
    def __init__(self):
        self.responses = []

    def respond(self, status: int, body: str):
        self.responses.append((status, body))
        return ("RESPONSE", status, body)


def _must_not_spawn(provider):
    raise AssertionError("session_factory must not be called from _process_request")


def _make_gateway(is_ready=lambda: True):
    return TransportGateway(
        host="127.0.0.1",
        port=0,
        is_ready=is_ready,
        session_factory=_must_not_spawn,
        session_teardown=lambda session: None,
    )


class TestTransportGatewayValidation(unittest.TestCase):
    def test_healthz_ok_when_ready(self):
        gateway = _make_gateway(is_ready=lambda: True)
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/healthz"))
        )
        self.assertEqual(result, ("RESPONSE", 200, "ok"))

    def test_healthz_503_when_not_ready(self):
        gateway = _make_gateway(is_ready=lambda: False)
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/healthz"))
        )
        self.assertEqual(result, ("RESPONSE", 503, "not ready"))

    def test_ws_missing_provider_rejected_400(self):
        gateway = _make_gateway()
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/ws"))
        )
        self.assertEqual(result[1], 400)

    def test_ws_unknown_provider_rejected_400(self):
        gateway = _make_gateway()
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/ws?provider=claude"))
        )
        self.assertEqual(result[1], 400)

    def test_ws_valid_provider_openai_allows_handshake(self):
        gateway = _make_gateway()
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/ws?provider=openai"))
        )
        self.assertIsNone(result)  # None means "proceed with the WebSocket handshake"

    def test_ws_valid_provider_gemini_allows_handshake(self):
        gateway = _make_gateway()
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/ws?provider=gemini"))
        )
        self.assertIsNone(result)

    def test_ws_rejected_503_when_shutting_down(self):
        gateway = _make_gateway(is_ready=lambda: False)
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/ws?provider=openai"))
        )
        self.assertEqual(result, ("RESPONSE", 503, "transport: shutting down"))

    def test_ws_409_when_already_connected(self):
        gateway = _make_gateway()
        gateway._active_connection = object()  # simulate an existing live connection
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/ws?provider=openai"))
        )
        self.assertEqual(result[1], 409)

    def test_unknown_path_404(self):
        gateway = _make_gateway()
        result = asyncio.run(
            gateway._process_request(_FakeConnection(), _FakeRequest(path="/nope"))
        )
        self.assertEqual(result[1], 404)


if __name__ == "__main__":
    unittest.main()
