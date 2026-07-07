"""
runtime/transport_gateway.py
=============================
Client-facing transport layer for the Cloud Run Compatibility Shell.

Owns the network boundary between a remote Phantom Client and the Runtime
child process. This module is named for the *role* it plays — transport —
not for the wire protocol it currently happens to use. The implementation
below uses a WebSocket, but nothing about the Shell's process-supervision
code (runtime.cloud_run_shell) or the Runtime child (phantom_runtime.py)
depends on that choice: both sides only know about two plain pipe file
descriptors. Swapping the wire protocol later means touching this file
only.

Responsibilities (transport only — no Runtime/AI logic, per the
Compatibility Shell's existing boundary contract):
  - Serve GET /healthz with the same 200/503 readiness contract the Shell
    has always exposed (see runtime.health_server).
  - Accept a single live client connection at /ws.
  - Relay inbound binary frames to the Runtime child's audio-in pipe,
    verbatim and unexamined — this module has no knowledge of audio
    formats, sample rates, or block sizes; that is Runtime execution
    concern (phantom_runtime.py's --audio-source fd).
  - Relay outbound lines from the Runtime child's event-out pipe to the
    client as WebSocket text frames, verbatim and unexamined. The child
    owns the event schema (see phantom_runtime.py's _emit_event(): every
    line is already a versioned JSON envelope
    {"version": 1, "type": ..., "timestamp": ..., "payload": {...}}).
    This module does not construct, parse, or validate event content —
    only relays complete lines.

EXPORTED API:
  TransportGateway(host, port, is_ready, audio_fd_w, event_fd_r)
  gateway.start()  — begin serving on a dedicated background thread
  gateway.stop()   — stop serving and join that thread
"""

import asyncio
import os
import threading
from typing import Callable, Optional

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request

HEALTH_PATHS = ("/healthz", "/")
WS_PATH      = "/ws"

_PIPE_READ_CHUNK_SIZE = 65536
_EVENT_QUEUE_MAXSIZE  = 1000


def _log(message: str) -> None:
    print(f"[transport_gateway] {message}", flush=True)


class TransportGateway:
    """
    Serves /healthz and /ws on one port. Pumps bytes between a single
    connected client and the two pipe fds handed to it by the Shell.
    """

    def __init__(
        self,
        host: str,
        port: int,
        is_ready: Callable[[], bool],
        audio_fd_w: int,
        event_fd_r: int,
    ) -> None:
        self._host       = host
        self._port       = port
        self._is_ready   = is_ready
        self._audio_fd_w = audio_fd_w
        self._event_fd_r = event_fd_r

        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[Server]                    = None
        self._thread: Optional[threading.Thread]          = None
        self._event_queue: Optional["asyncio.Queue[str]"] = None

        self._active_connection: Optional[ServerConnection] = None
        self._active_lock = threading.Lock()

        self._stop_event = threading.Event()

    # ── Public lifecycle ────────────────────────────────────────────────────

    def start(self) -> None:
        self._thread = threading.Thread(
            target=self._run, name="transport-gateway", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    # ── Internal: dedicated event loop thread ───────────────────────────────

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        self._event_queue = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)

        event_reader_thread = threading.Thread(
            target=self._pump_events_from_pipe,
            name="transport-event-reader",
            daemon=True,
        )
        event_reader_thread.start()

        async with serve(
            self._handler,
            self._host,
            self._port,
            process_request=self._process_request,
        ) as server:
            self._server = server
            _log(f"listening on :{self._port}  (GET {HEALTH_PATHS[0]} | WS {WS_PATH})")
            drain_task = asyncio.ensure_future(self._drain_event_queue())
            try:
                await asyncio.get_running_loop().run_in_executor(
                    None, self._stop_event.wait
                )
            finally:
                drain_task.cancel()
        _log("stopped")

    # ── HTTP path: /healthz (plain HTTP, no upgrade) ────────────────────────

    async def _process_request(self, connection: ServerConnection, request: Request):
        if request.path in HEALTH_PATHS:
            healthy = self._is_ready()
            body = "ok" if healthy else "not ready"
            return connection.respond(200 if healthy else 503, body)

        if request.path == WS_PATH:
            with self._active_lock:
                if self._active_connection is not None:
                    return connection.respond(
                        409, "transport: a client is already connected"
                    )
            return None  # let the WebSocket handshake proceed

        return connection.respond(404, "not found")

    # ── WS path: /ws — inbound audio ────────────────────────────────────────

    async def _handler(self, websocket: ServerConnection) -> None:
        with self._active_lock:
            self._active_connection = websocket
        _log("client connected")
        try:
            async for message in websocket:
                if isinstance(message, str):
                    continue  # audio-in is binary only; ignore stray text frames
                await asyncio.get_running_loop().run_in_executor(
                    None, os.write, self._audio_fd_w, message
                )
        except ConnectionClosed:
            pass
        finally:
            with self._active_lock:
                if self._active_connection is websocket:
                    self._active_connection = None
            _log("client disconnected")

    # ── Event pipe → WS relay (outbound events) ─────────────────────────────

    def _pump_events_from_pipe(self) -> None:
        """
        Runs on its own thread for the lifetime of the gateway — independent
        of individual client connections coming and going. Blocking reads on
        the event pipe fd, split on newlines, handed to the gateway's event
        loop via a thread-safe queue put.
        """
        buf = b""
        while not self._stop_event.is_set():
            try:
                chunk = os.read(self._event_fd_r, _PIPE_READ_CHUNK_SIZE)
            except OSError:
                break
            if not chunk:
                break  # Runtime child closed its event-out pipe
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if not line:
                    continue
                text = line.decode("utf-8", "replace")
                if self._loop is not None:
                    self._loop.call_soon_threadsafe(self._enqueue_event, text)

    def _enqueue_event(self, text: str) -> None:
        if self._event_queue is None:
            return
        try:
            self._event_queue.put_nowait(text)
        except asyncio.QueueFull:
            pass  # live stream, not a durable log — drop under sustained backpressure

    async def _drain_event_queue(self) -> None:
        while True:
            text = await self._event_queue.get()
            with self._active_lock:
                ws = self._active_connection
            if ws is None:
                continue  # nobody connected right now — drop
            try:
                await ws.send(text)
            except ConnectionClosed:
                pass
