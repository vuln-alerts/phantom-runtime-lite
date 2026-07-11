"""
runtime/transport_gateway.py
=============================
Client-facing transport layer for the Cloud Run Compatibility Shell.

Owns the network boundary between a remote Phantom Client and the Runtime
child process. This module is named for the *role* it plays — transport —
not for the wire protocol it currently happens to use. The implementation
below uses a WebSocket, but nothing about the Shell's process-supervision
code (runtime.cloud_run_shell) or the Runtime child (phantom_runtime.py)
depends on that choice: both sides only know about three plain pipe file
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
  - Relay inbound text frames (H6: Control Events, e.g.
    {"command": "generate_summary"}) to that session's control-in pipe,
    verbatim and unexamined, one frame per newline-terminated line --
    this module does not construct, parse, or validate command content;
    that is Runtime execution concern (phantom_runtime.py's
    PHANTOM_CONTROL_FD reader, which dispatches to the same functions
    the local keyboard loop already calls).
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
import runtime_trace

HEALTH_PATHS = ("/healthz", "/")
WS_PATH      = "/ws"

_PIPE_READ_CHUNK_SIZE = 65536
_EVENT_QUEUE_MAXSIZE  = 1000

# Per-connection Session Lifecycle, traced via runtime_trace (debug-only)
# so a future stall is visible at a glance in the trace log: which
# session got stuck, and at which stage. CONNECTED -> DISCONNECTING is
# entered the instant the relay loop exits (any reason); DISCONNECTING
# -> TEARDOWN is entered only after this loop-owned thread has already
# released _active_connection/_active_session (see _handler's finally
# block) -- so by the time TEARDOWN starts, the single-session slot is
# already free for a new connection, regardless of how long TEARDOWN
# itself (child process shutdown) takes. TEARDOWN -> CLOSED is entered
# once RuntimeSession.teardown() returns, on the background thread (see
# _finish_disconnect). Exactly one _handler() coroutine ever exists per
# connection and its try/finally runs exactly once, so this sequence
# cannot double-fire for the same connection.
_SESSION_STATE_CONNECTED     = "CONNECTED"
_SESSION_STATE_DISCONNECTING = "DISCONNECTING"
_SESSION_STATE_TEARDOWN      = "TEARDOWN"
_SESSION_STATE_CLOSED        = "CLOSED"


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
        # Fetched unconditionally, before any code that could raise, so
        # it is always available to the finally block below regardless
        # of where in this coroutine things fail -- _handler only ever
        # runs as a task on the gateway's own loop, so this is always
        # the same loop _run()/_serve() created.
        loop = asyncio.get_running_loop()
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
        # Ties this connection's trace lines to the same session id the
        # spawned Runtime child tags its own trace lines with (its pid) --
        # see runtime_trace.py's module docstring. Debug-only. Falls back
        # to id(session) for session stand-ins without a real child
        # process (e.g. test doubles), which have no pid of their own.
        _child = getattr(session, "child", None)
        _trace_session_id = f"srv-{getattr(_child, 'pid', id(session))}"
        try:
            with self._active_lock:
                self._active_session = session
            _log(f"client connected (provider={provider})")
            if runtime_trace.enabled():
                runtime_trace.emit(
                    "Session START", session_id=_trace_session_id,
                    event_id="session-start", provider=provider,
                    lifecycle_state=_SESSION_STATE_CONNECTED,
                )
                runtime_trace.emit(
                    "WebSocket RECEIVE", session_id=_trace_session_id,
                    event_id="connect", provider=provider,
                )

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
                    # Control Event (H6): one JSON command per text frame,
                    # e.g. {"command": "generate_summary"}. Relayed
                    # verbatim to the session's control pipe -- this
                    # module has no knowledge of what commands exist or
                    # what they do; that is Runtime execution concern
                    # (phantom_runtime.py's PHANTOM_CONTROL_FD reader).
                    try:
                        await loop.run_in_executor(
                            None, os.write, session.control_fd_w, message.encode("utf-8") + b"\n"
                        )
                    except OSError:
                        break  # session's control pipe gone (e.g. child already exited)
                    continue
                if runtime_trace.enabled():
                    runtime_trace.emit(
                        "WebSocket RECEIVE", session_id=_trace_session_id,
                        event_id=runtime_trace.next_event_id("ws-recv"),
                        nbytes=len(message),
                    )
                try:
                    await loop.run_in_executor(
                        None, os.write, session.audio_fd_w, message
                    )
                except OSError:
                    break  # session's audio pipe gone (e.g. child already exited)
        except ConnectionClosed as exc:
            if runtime_trace.enabled():
                runtime_trace.emit(
                    "WebSocket RECEIVE", session_id=_trace_session_id,
                    event_id="connection_closed", exception=str(exc),
                )
        finally:
            if runtime_trace.enabled():
                runtime_trace.emit(
                    "Session DISCONNECT", session_id=_trace_session_id,
                    event_id="session-disconnect",
                    lifecycle_state=_SESSION_STATE_DISCONNECTING,
                )
            if drain_task is not None:
                drain_task.cancel()

            # Shared TransportGateway state (_active_connection /
            # _active_session, guarded by _active_lock) is claimed and
            # released ONLY here, on the gateway's own event-loop thread
            # -- never from the background thread this finally block
            # hands off to below. That is what keeps the single-session
            # slot's state machine race-free: by the time a reconnect's
            # _process_request/_handler can observe this slot as free,
            # it really is free, regardless of how long this session's
            # own child-process shutdown (below) still takes.
            with self._active_lock:
                if self._active_connection is websocket:
                    self._active_connection = None
                self._active_session = None
            _log("client disconnected")

            # reader_thread.join() and session_teardown() (SIGINT ->
            # subprocess.wait(SHUTDOWN_GRACE_SECONDS) -> SIGKILL ->
            # subprocess.wait(_KILL_WAIT_SECONDS), see cloud_run_shell.
            # RuntimeSession.teardown()) are blocking, multi-second
            # operations. This Shell serves every connection -- /healthz
            # and every /ws session -- on the ONE asyncio event loop this
            # coroutine is itself running on (see _run()/_serve()); a
            # thread.join()/subprocess.wait() called directly here would
            # stall that loop, and therefore this process's Ping/Pong
            # keepalive handling and every other connection's handshake
            # processing, for the entire blocking duration. That stall is
            # exactly how one session's ordinary disconnect used to turn
            # into an unrelated reconnect being rejected with 409: while
            # the loop was frozen inside this call, a client's own
            # keepalive could time out (server not responding to Ping ->
            # close 1011) and, separately, a stale/already-abandoned
            # connection attempt could be the first thing the loop
            # processes once it unfreezes, claiming the just-freed slot
            # before the client's real reconnect arrived.
            #
            # Offloading to the default executor (a plain OS thread, not
            # this loop) fixes that: the loop is never blocked, so Ping/
            # Pong keepalive and every other connection's handshake are
            # always serviced promptly, and -- since _active_connection/
            # _active_session are already released above, before this is
            # scheduled -- a reconnect is free to succeed immediately.
            # The background thread never touches _active_connection/
            # _active_session/_active_lock; it owns nothing but this one
            # RuntimeSession's own shutdown.
            loop.run_in_executor(
                None, self._finish_disconnect,
                reader_thread, reader_stop, session, _trace_session_id,
            )

    def _finish_disconnect(
        self,
        reader_thread: Optional[threading.Thread],
        reader_stop: threading.Event,
        session: Any,
        trace_session_id: str,
    ) -> None:
        """
        Runs on the default executor -- a plain OS thread, never the
        gateway's asyncio event loop -- for exactly one connection's
        post-disconnect cleanup (see _handler's finally block, the only
        caller). Touches only this one RuntimeSession and its own
        reader_thread/reader_stop; TransportGateway's shared
        _active_connection/_active_session/_active_lock state is never
        read or written here (already fully released, on the loop
        thread, before this was scheduled).

        RuntimeSession.teardown() (runtime.cloud_run_shell) is itself
        idempotent (lock + one-shot flag), so this is also safe to run
        concurrently with a SIGTERM-driven shutdown teardown of the same
        session -- exactly the pre-existing guarantee this function
        preserves, just off the event loop thread now.

        Never raises: this runs detached (loop.run_in_executor's returned
        Future is intentionally not awaited/stored by the caller), so an
        uncaught exception here would only surface as an "exception was
        never retrieved" warning instead of anywhere actionable. Every
        step is therefore its own best-effort try/except, logged and
        swallowed, the same fail-open shape the rest of this file already
        uses for pipe-reader/relay errors.
        """
        try:
            if reader_thread is not None:
                reader_stop.set()
                reader_thread.join(timeout=2.0)
        except Exception as exc:
            _log(f"reader thread join raised (session_id={trace_session_id}): {exc}")

        if runtime_trace.enabled():
            try:
                runtime_trace.emit(
                    "Session TEARDOWN START", session_id=trace_session_id,
                    event_id="teardown-start",
                    lifecycle_state=_SESSION_STATE_TEARDOWN,
                )
            except Exception:
                pass

        try:
            self._session_teardown(session)
        except Exception as exc:
            _log(f"session teardown raised (session_id={trace_session_id}): {exc}")

        if runtime_trace.enabled():
            try:
                runtime_trace.emit(
                    "Session TEARDOWN END", session_id=trace_session_id,
                    event_id="teardown-end",
                    lifecycle_state=_SESSION_STATE_CLOSED,
                )
            except Exception:
                pass

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
