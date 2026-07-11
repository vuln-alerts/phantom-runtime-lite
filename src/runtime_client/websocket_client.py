"""
runtime_client/websocket_client.py
====================================
WebSocket transport half of the Runtime Client.

Speaks exactly the wire contract implemented by runtime.transport_gateway
(server side, unmodified by this client): connect to
wss://<host>/ws?provider=openai|gemini, send audio-in as binary frames,
send Control Events (Phase 1-3) as text frames, receive Typed Events
(Phase 1-4) as text frames. Owns connection lifecycle (initial connect,
bounded reconnect with exponential backoff, graceful close) and nothing
else -- audio capture, control-event construction, and event rendering
are the caller's job.

EXPORTED API:
  RuntimeWebSocketClient(url, max_reconnect_attempts, backoff_base_seconds)
  client.run(audio_queue, control_queue, stop_event, on_event) -- run
      until stop_event is set or reconnect attempts are exhausted
"""

import asyncio
import time
from typing import Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

import runtime_trace


def _log(message: str) -> None:
    print(f"[websocket_client] {message}", flush=True)


class RuntimeWebSocketClient:
    """
    One logical session against the Runtime's /ws endpoint, transparently
    reconnecting (bounded, exponential backoff) across transient
    disconnects. A rejected handshake (400 invalid provider, 1008/1011/1013
    close codes) is treated as fatal -- retrying an unconditionally-
    rejected request would just waste every attempt on the same
    guaranteed rejection.
    """

    _FATAL_CLOSE_CODES = (1008, 1011, 1013)

    def __init__(
        self,
        url: str,
        max_reconnect_attempts: int,
        backoff_base_seconds: float,
    ) -> None:
        self._url = url
        self._max_reconnect_attempts = max_reconnect_attempts
        self._backoff_base_seconds = backoff_base_seconds
        # Debug-only state for the Runtime Pipeline stall investigation
        # (see runtime_trace.py). Read via get_state(); never branched on.
        self._state = {
            "connected": False,
            "reconnect_count": 0,
            "last_send_ts": None,
            "last_recv_ts": None,
            "close_reason": None,
            "last_exception": None,
        }

    def get_state(self) -> dict:
        return dict(self._state)

    async def run(
        self,
        audio_queue: "asyncio.Queue[bytes]",
        control_queue: "asyncio.Queue[str]",
        stop_event: asyncio.Event,
        on_event: Callable[[str], None],
    ) -> None:
        attempt = 0
        while not stop_event.is_set():
            try:
                async with connect(self._url) as websocket:
                    _log(f"connected: {self._url}")
                    self._state["connected"] = True
                    attempt = 0  # a successful handshake resets the backoff counter
                    await self._pump(websocket, audio_queue, control_queue, stop_event, on_event)
                self._state["connected"] = False
                if stop_event.is_set():
                    return
                self._state["close_reason"] = "disconnected_by_server"
                _log("disconnected by server; reconnecting")
            except InvalidStatus as exc:
                self._state["connected"] = False
                self._state["last_exception"] = str(exc)
                status = getattr(exc, "response", None)
                code = getattr(status, "status_code", None)
                if code in (400, 404, 409):
                    self._state["close_reason"] = f"handshake_rejected_{code}"
                    _log(f"fatal: handshake rejected ({code}); not retrying")
                    return
                self._state["close_reason"] = f"handshake_failed_{code}"
                _log(f"handshake failed ({code}); will retry")
            except ConnectionClosed as exc:
                self._state["connected"] = False
                self._state["last_exception"] = str(exc)
                close_code = exc.rcvd.code if exc.rcvd is not None else None
                if close_code in self._FATAL_CLOSE_CODES:
                    self._state["close_reason"] = f"fatal_close_{close_code}"
                    _log(f"fatal: server closed with code {close_code}; not retrying")
                    return
                self._state["close_reason"] = f"connection_closed_{close_code}"
                _log(f"connection closed ({exc}); reconnecting")
            except OSError as exc:
                self._state["connected"] = False
                self._state["last_exception"] = str(exc)
                self._state["close_reason"] = "os_error"
                _log(f"connection error ({exc}); reconnecting")

            if stop_event.is_set():
                return
            attempt += 1
            self._state["reconnect_count"] += 1
            if runtime_trace.enabled():
                runtime_trace.emit(
                    "WebSocket RECONNECT", event_id=f"reconnect-{attempt}",
                    ws_state=self.get_state(),
                )
            if attempt > self._max_reconnect_attempts:
                _log(f"giving up after {self._max_reconnect_attempts} reconnect attempts")
                return
            delay = self._backoff_base_seconds * (2 ** (attempt - 1))
            _log(f"reconnect attempt {attempt}/{self._max_reconnect_attempts} in {delay:.1f}s")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _pump(self, websocket, audio_queue, control_queue, stop_event, on_event) -> None:
        sender = asyncio.ensure_future(self._send_audio(websocket, audio_queue))
        control_sender = asyncio.ensure_future(self._send_control(websocket, control_queue))
        receiver = asyncio.ensure_future(self._receive_events(websocket, on_event))
        stopper = asyncio.ensure_future(stop_event.wait())
        try:
            done, pending = await asyncio.wait(
                {sender, control_sender, receiver, stopper},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (sender, control_sender, receiver, stopper):
                if not task.done():
                    task.cancel()
            await asyncio.gather(sender, control_sender, receiver, stopper, return_exceptions=True)

        if stopper in done:
            await websocket.close(code=1000, reason="client shutdown")
            return

        # sender/control_sender/receiver finished first -- surface its
        # exception (if any) so run() can classify it and decide whether
        # to reconnect.
        finished = next(iter(done))
        exc = finished.exception()
        if exc is not None:
            raise exc

    async def _send_audio(self, websocket, audio_queue: "asyncio.Queue[bytes]") -> None:
        while True:
            block = await audio_queue.get()
            await websocket.send(block)  # bytes -> binary frame (audio-in contract)
            self._state["last_send_ts"] = time.time()
            if runtime_trace.enabled():
                runtime_trace.emit(
                    "WebSocket SEND", event_id=runtime_trace.next_event_id("ws-send"),
                    nbytes=len(block),
                )

    async def _send_control(self, websocket, control_queue: "asyncio.Queue[str]") -> None:
        while True:
            line = await control_queue.get()
            await websocket.send(line)  # str -> text frame (Control Event, Phase 1-3)

    async def _receive_events(self, websocket, on_event: Callable[[str], None]) -> None:
        async for message in websocket:
            if isinstance(message, (bytes, bytearray)):
                continue  # events are text-only per contract; ignore stray binary
            self._state["last_recv_ts"] = time.time()
            if runtime_trace.enabled():
                runtime_trace.emit(
                    "WebSocket RECEIVE", event_id=runtime_trace.next_event_id("ws-recv-evt"),
                    nbytes=len(message),
                )
            on_event(message)
