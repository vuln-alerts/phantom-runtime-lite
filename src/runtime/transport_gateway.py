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
descriptors per session. Swapping the wire protocol later means touching
this file only.

Responsibilities (transport only — no Runtime/AI logic, per the
Compatibility Shell's existing boundary contract):
  - Serve GET /healthz with the same 200/503 readiness contract the Shell
    has always exposed (see runtime.health_server).
  - Accept a single live client connection at /ws?provider=openai|gemini.
    The `provider` query parameter is mandatory request metadata (H5-1):
    validated via runtime.provider_router (the Provider Router), which
    performs no provider-specific business logic of its own -- this
    module only extracts the client-supplied value and asks the Router
    to validate it, rejecting missing/unknown providers with 400 Bad
    Request before the WebSocket handshake completes.
  - Reject new /ws connections with 503 once the Shell is no longer
    ready (e.g. mid-shutdown), using the same is_ready callback already
    used for /healthz. This is what makes "stop accepting new work" an
    immediate consequence of the Shell's own readiness state, without
    touching whatever connection is already active -- Server.close()'s
    default close_connections=True behavior is deliberately NOT used
    here, since that would also sever the in-flight session instead of
    letting its shutdown grace period run its course.
  - For each accepted connection, atomically claim the single-session
    slot and only then ask the Shell (via session_factory) to spawn a
    fresh Runtime child scoped to that connection's validated provider
    -- routing is session-scoped, not deployment-wide. No child is ever
    spawned unless the claim succeeded first, so a losing connection in
    a race never produces an untracked/stray child.
  - Relay inbound binary frames to that session's audio-in pipe,
    verbatim and unexamined — this module has no knowledge of audio
    formats, sample rates, or block sizes; that is Runtime execution
    concern (phantom_runtime.py's --audio-source fd).
  - Relay outbound lines from that session's event-out pipe to the client
    as WebSocket text frames, verbatim and unexamined. The child owns the
    event schema (see phantom_runtime.py's _emit_event(): every line is
    already a versioned JSON envelope
    {"version": 1, "type": ..., "timestamp": ..., "payload": {...}}).
    This module does not construct, parse, or validate event content —
    only relays complete lines.
  - Tear the session down (via session_teardown) when the connection
    closes, session spawn fails, or any later step in bringing the
    session up fails -- so no Runtime child outlives its client and no
    reservation is ever left stuck claimed.

EXPORTED API:
  TransportGateway(host, port, is_ready, session_factory, session_teardown)
  gateway.start()               — begin serving on a dedicated background thread
  gateway.stop()                — stop serving and join that thread
  gateway.get_active_session()  — the currently active session object, if any (or None)
"""

import asyncio
import os
import threading
from typing import Any, Callable, Optional

from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request

from runtime.provider_router import ProviderRejected, select_provider_from_query

HEALTH_PATHS = ("/healthz", "/")
WS_PATH      = "/ws"

_PIPE_READ_CHUNK_SIZE = 65536
_EVENT_QUEUE_MAXSIZE  = 1000


def _log(message: str) -> None:
    print(f"[transport_gateway] {message}", flush=True)


class TransportGateway:
    """
    Serves /healthz and /ws on one port. Each /ws connection is its own
    session: a fresh Runtime child (spawned via session_factory, scoped to
    the connection's validated `provider` query parameter) and a pair of
    pipe fds pumped for the lifetime of that one connection only.
    """

    def __init__(
        self,
        host: str,
        port: int,
        is_ready: Callable[[], bool],
        session_factory: Callable[[str], Any],
        session_teardown: Callable[[Any], None],
    ) -> None:
        self._host             = host
        self._port             = port
        self._is_ready         = is_ready
        self._session_factory  = session_factory
        self._session_teardown = session_teardown

        self._loop:   Optional[asyncio.AbstractEventLoop] = None
        self._server: Optional[Server]                    = None
        self._thread: Optional[threading.Thread]          = None

        # Both fields are set/cleared together, always under _active_lock:
        # _active_connection guards the single-session slot (claimed before
        # any child is spawned); _active_session is attached only after
        # session_factory succeeds. A thread other than the gateway's own
        # (e.g. cloud_run_shell's SIGTERM handler, via get_active_session())
        # reads _active_session concurrently, hence the lock.
        self._active_connection: Optional[ServerConnection] = None
        self._active_session: Optional[Any] = None
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

    def get_active_session(self) -> Optional[Any]:
        """The session object for the current live connection, or None."""
        with self._active_lock:
            return self._active_session

    # ── Internal: dedicated event loop thread ───────────────────────────────

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._serve())
        finally:
            self._loop.close()

    async def _serve(self) -> None:
        async with serve(
            self._handler,
            self._host,
            self._port,
            process_request=self._process_request,
        ) as server:
            self._server = server
            _log(f"listening on :{self._port}  (GET {HEALTH_PATHS[0]} | WS {WS_PATH})")
            await asyncio.get_running_loop().run_in_executor(
                None, self._stop_event.wait
            )
        _log("stopped")

    # ── HTTP path: /healthz (plain HTTP, no upgrade) + /ws fast-path reject ─

    async def _process_request(self, connection: ServerConnection, request: Request):
        """
        Pre-handshake validation only. The provider check here is
        authoritative (missing/unknown -> 400). The "already connected"
        check here is a best-effort fast path only, NOT the atomic claim
        -- a losing connection that races past this check is still
        rejected in _handler, strictly before any Runtime child is
        spawned (see _handler). This avoids a window where a reservation
        could be made here but never released if the handshake itself
        subsequently fails before _handler runs.

        The not-ready check makes "stop accepting new work" an immediate
        consequence of the Shell's readiness state (see module docstring)
        -- it rejects new sessions the instant cloud_run_shell marks
        itself as shutting down, without touching whatever connection is
        already active.
        """
        path, _, query = request.path.partition("?")

        if path in HEALTH_PATHS:
            healthy = self._is_ready()
            body = "ok" if healthy else "not ready"
            return connection.respond(200 if healthy else 503, body)

        if path == WS_PATH:
            if not self._is_ready():
                return connection.respond(503, "transport: shutting down")

            try:
                select_provider_from_query(query)
            except ProviderRejected as exc:
                return connection.respond(400, str(exc))

            with self._active_lock:
                if self._active_connection is not None:
                    return connection.respond(
                        409, "transport: a client is already connected"
                    )
            return None  # let the WebSocket handshake proceed

        return connection.respond(404, "not found")

    # ── WS path: /ws — one session per connection ───────────────────────────

    async def _handler(self, websocket: ServerConnection) -> None:
        _, _, query = websocket.request.path.partition("?")
        try:
            provider = select_provider_from_query(query)
        except ProviderRejected:
            # Already validated in _process_request; this only defends
            # against a request object that somehow changed in between.
            await websocket.close(code=1008, reason="invalid provider")
            return

        # Atomic claim: check-and-set under one lock acquisition, with no
        # `await` between them, and strictly before session_factory is
        # ever called. A losing connection is rejected here -- before any
        # Runtime child is spawned -- even if it raced past the
        # best-effort 409 check in _process_request.
        with self._active_lock:
            if self._active_connection is not None:
                claimed = False
            else:
                self._active_connection = websocket
                claimed = True
        if not claimed:
            await websocket.close(code=1013, reason="already connected")
            return

        try:
            session = self._session_factory(provider)
        except Exception as exc:
            with self._active_lock:
                self._active_connection = None  # release the claim; nothing was spawned
            _log(f"session spawn failed (provider={provider}): {exc}")
            await websocket.close(code=1011, reason="runtime session failed to start")
            return

        # Everything from here on operates on a real, spawned session, so
        # the try/finally starts now: whatever fails below -- attaching the
        # session, starting the reader thread, scheduling the drain task,
        # or the relay loop itself -- session_teardown() and the active
        # slot release are guaranteed to run exactly once.
        reader_thread: Optional[threading.Thread] = None
        reader_stop = threading.Event()
        drain_task: "Optional[asyncio.Task]" = None
        try:
            with self._active_lock:
                self._active_session = session
            _log(f"client connected (provider={provider})")

            loop = asyncio.get_running_loop()
            event_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=_EVENT_QUEUE_MAXSIZE)
            reader_thread = threading.Thread(
                target=self._pump_events_from_pipe,
                args=(session.event_fd_r, loop, event_queue, reader_stop),
                name="transport-event-reader",
                daemon=True,
            )
            reader_thread.start()
            drain_task = asyncio.ensure_future(self._drain_event_queue(event_queue, websocket))

            async for message in websocket:
                if isinstance(message, str):
                    continue  # audio-in is binary only; ignore stray text frames
                try:
                    await loop.run_in_executor(
                        None, os.write, session.audio_fd_w, message
                    )
                except OSError:
                    break  # session's audio pipe gone (e.g. child already exited)
        except ConnectionClosed:
            pass
        finally:
            if drain_task is not None:
                drain_task.cancel()
            if reader_thread is not None:
                reader_stop.set()
                reader_thread.join(timeout=2.0)
            with self._active_lock:
                if self._active_connection is websocket:
                    self._active_connection = None
                self._active_session = None
            # Idempotent: safe even if a concurrent SIGTERM-driven shutdown
            # (see runtime.cloud_run_shell) already tore this session down.
            self._session_teardown(session)
            _log("client disconnected")

    # ── Event pipe → WS relay (outbound events, one per session) ────────────

    def _pump_events_from_pipe(
        self,
        event_fd_r: int,
        loop: asyncio.AbstractEventLoop,
        event_queue: "asyncio.Queue[str]",
        stop_event: threading.Event,
    ) -> None:
        """
        Runs on its own thread for the lifetime of one session. Blocking
        reads on the session's event pipe fd, split on newlines, handed to
        the gateway's event loop via a thread-safe queue put.
        """
        buf = b""
        while not stop_event.is_set():
            try:
                chunk = os.read(event_fd_r, _PIPE_READ_CHUNK_SIZE)
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
                loop.call_soon_threadsafe(self._enqueue_event, event_queue, text)

    def _enqueue_event(self, event_queue: "asyncio.Queue[str]", text: str) -> None:
        try:
            event_queue.put_nowait(text)
        except asyncio.QueueFull:
            pass  # live stream, not a durable log — drop under sustained backpressure

    async def _drain_event_queue(
        self, event_queue: "asyncio.Queue[str]", websocket: ServerConnection
    ) -> None:
        while True:
            text = await event_queue.get()
            try:
                await websocket.send(text)
            except ConnectionClosed:
                pass
