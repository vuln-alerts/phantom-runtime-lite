"""
runtime/cloud_run_shell.py
============================
Cloud Run Compatibility Shell.

Process supervisor that runs OUTSIDE the Runtime Boundary. It spawns the
existing, unmodified Runtime entrypoint (phantom_runtime.py) as a child
process -- one fresh child per WebSocket session (H5-1: session-scoped
Provider Routing; see runtime.provider_router / runtime.transport_gateway)
-- and exposes a Cloud Run-compatible readiness endpoint, plus the
client-facing transport (health + WebSocket), via runtime.transport_gateway.

The Shell remains the sole owner of: the health endpoint, the WebSocket
endpoint, process supervision, and each Runtime child's lifecycle. For
every accepted connection it hands the Runtime child three anonymous
pipes -- one for inbound audio, one for outbound events, one for inbound
Control Events (H6: remote keyboard-equivalent commands, e.g.
generate_summary/generate_meeting_analysis/toggle_recording) -- and
never inspects the bytes flowing through any of them. The child decides
whether to use them (via its own --audio-source flag and its own
PHANTOM_CONTROL_FD reader); the Shell's job is only to provide the
plumbing and the network boundary.

CONSTRAINTS (do not violate):
  - Never import phantom_conversational_runtime_v22, config, or provider.*
    The Runtime child is spawned as a subprocess, never imported.
  - Never add a signal handler inside the Runtime child. SIGTERM received by
    this Shell is translated to SIGINT and delivered to the active child (if
    any), reusing the Runtime's existing KeyboardInterrupt-based graceful
    shutdown.
  - Runtime CLI arguments are forwarded verbatim, unmodified. The three pipe
    fds and the session's provider are communicated to the child via
    environment variables (PHANTOM_AUDIO_FD / PHANTOM_EVENT_FD /
    PHANTOM_CONTROL_FD / PHANTOM_PROVIDER), never by mutating argv, and
    never via a deployment-wide PROVIDER environment variable.

EXPORTED API:
  main() — process entrypoint
           (invoked as: python -m runtime.cloud_run_shell -- <runtime args>)
"""

import dataclasses
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Optional

from runtime.transport_gateway import TransportGateway

_RUNTIME_ENTRYPOINT_NAME = "phantom_runtime.py"

# Directory containing this file's parent package root (src/), i.e. the same
# directory phantom_conversational_runtime_v22.py is copied into by the
# Dockerfile. Resolved via __file__ so behavior does not depend on cwd.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME_ENTRYPOINT_PATH = os.path.join(_SRC_DIR, _RUNTIME_ENTRYPOINT_NAME)

STARTUP_GRACE_SECONDS = float(os.getenv("CLOUD_RUN_STARTUP_GRACE_SECONDS", "5"))
SHUTDOWN_GRACE_SECONDS = float(os.getenv("CLOUD_RUN_SHUTDOWN_GRACE_SECONDS", "10"))
_POLL_INTERVAL_SECONDS = 0.2
_KILL_WAIT_SECONDS = 2.0

_STATE_STARTING = "starting"
_STATE_HEALTHY = "healthy"
_STATE_SHUTTING_DOWN = "shutting_down"


def _log(message: str) -> None:
    print(f"[cloud_run_shell] {message}", flush=True)


class _ReadinessState:
    """Thread-safe readiness flag owned by the Shell (Plan Part I Section 2)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state = _STATE_STARTING

    def set(self, state: str) -> None:
        with self._lock:
            self._state = state

    def is_ready(self) -> bool:
        with self._lock:
            return self._state == _STATE_HEALTHY


@dataclasses.dataclass
class RuntimeSession:
    """
    One Runtime child, spawned for exactly one WebSocket connection
    (H5-1: session-scoped Provider Routing). Owns the Shell-side halves
    of the three pipes handed to that child -- audio_fd_w (write audio
    in), event_fd_r (read events out), and control_fd_w (write Control
    Events in, H6) -- and nothing else.

    teardown() is idempotent: the lock+flag below ensure the actual
    SIGINT/wait/SIGKILL/fd-close sequence runs exactly once even if
    called concurrently from more than one path (e.g. the connection's
    own disconnect handling racing a Cloud Run SIGTERM).
    """

    child: subprocess.Popen
    audio_fd_w: int
    event_fd_r: int
    control_fd_w: int
    _lock: threading.Lock = dataclasses.field(default_factory=threading.Lock, repr=False)
    _torn_down: bool = dataclasses.field(default=False, repr=False)

    def teardown(self) -> None:
        with self._lock:
            if self._torn_down:
                return
            self._torn_down = True

        try:
            self.child.send_signal(signal.SIGINT)
        except ProcessLookupError:
            pass

        try:
            self.child.wait(timeout=SHUTDOWN_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                self.child.kill()
            except ProcessLookupError:
                pass
            try:
                self.child.wait(timeout=_KILL_WAIT_SECONDS)
            except subprocess.TimeoutExpired:
                pass  # reaped later by the OS; avoid blocking shutdown indefinitely

        for fd in (self.audio_fd_w, self.event_fd_r, self.control_fd_w):
            try:
                os.close(fd)
            except OSError:
                pass


class SessionSpawnError(Exception):
    """Raised by _spawn_session when a Runtime child could not be started.

    Guarantees every fd opened during the failed attempt has already
    been closed, and any partially-started child has already been
    killed, before this is raised -- callers never need their own
    cleanup on this path.
    """


def _forward_args(argv: list) -> list:
    """Strip a leading '--' separator, if present; return the rest verbatim."""
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _close_fd(fd: Optional[int]) -> None:
    if fd is None:
        return
    try:
        os.close(fd)
    except OSError:
        pass


def _spawn_session(forwarded_args: list, provider: str) -> RuntimeSession:
    """
    Spawn a fresh Runtime child scoped to one WebSocket connection's
    validated provider (see runtime.provider_router). On any failure,
    every fd opened so far is closed and any process that was already
    started is killed before SessionSpawnError is raised -- no fd or
    process is ever left behind on a failed attempt.
    """
    opened_fds: list = []
    child: Optional[subprocess.Popen] = None
    try:
        audio_read_fd, audio_write_fd = os.pipe()
        opened_fds += [audio_read_fd, audio_write_fd]
        event_read_fd, event_write_fd = os.pipe()
        opened_fds += [event_read_fd, event_write_fd]
        control_read_fd, control_write_fd = os.pipe()
        opened_fds += [control_read_fd, control_write_fd]

        cmd = [sys.executable, _RUNTIME_ENTRYPOINT_PATH] + forwarded_args
        env = dict(os.environ)
        env.update(
            {
                "PHANTOM_AUDIO_FD": str(audio_read_fd),
                "PHANTOM_EVENT_FD": str(event_write_fd),
                "PHANTOM_CONTROL_FD": str(control_read_fd),
                "PHANTOM_PROVIDER": provider,
            }
        )
        child = subprocess.Popen(
            cmd,
            cwd=_SRC_DIR,
            env=env,
            pass_fds=(audio_read_fd, event_write_fd, control_read_fd),
        )
    except Exception as exc:
        if child is not None:
            try:
                child.kill()
                child.wait(timeout=_KILL_WAIT_SECONDS)
            except Exception:
                pass
        for fd in opened_fds:
            _close_fd(fd)
        raise SessionSpawnError(f"failed to spawn runtime session: {exc}") from exc

    # Ownership of these three fds has passed to the child now; the Shell
    # only keeps its own ends (audio_write_fd, event_read_fd, control_write_fd).
    _close_fd(audio_read_fd)
    _close_fd(event_write_fd)
    _close_fd(control_read_fd)
    _log(f"runtime session started (pid={child.pid}, provider={provider})")
    return RuntimeSession(
        child=child,
        audio_fd_w=audio_write_fd,
        event_fd_r=event_read_fd,
        control_fd_w=control_write_fd,
    )


def _teardown_session(session: RuntimeSession) -> None:
    session.teardown()


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    forwarded_args = _forward_args(sys.argv[1:])

    readiness = _ReadinessState()

    gateway = TransportGateway(
        host="0.0.0.0",
        port=port,
        is_ready=readiness.is_ready,
        session_factory=lambda provider: _spawn_session(forwarded_args, provider),
        session_teardown=_teardown_session,
    )
    gateway.start()
    _log(f"transport gateway listening on :{port}")

    startup_deadline = time.monotonic() + STARTUP_GRACE_SECONDS
    marked_healthy = False
    shutdown_deadline = None

    def _handle_sigterm(signum, frame) -> None:
        nonlocal shutdown_deadline
        readiness.set(_STATE_SHUTTING_DOWN)  # rejects new /ws sessions immediately
        if shutdown_deadline is None:
            shutdown_deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
            _log("SIGTERM received — forwarding SIGINT to active runtime session (if any)")
            session = gateway.get_active_session()
            if session is not None:
                try:
                    session.child.send_signal(signal.SIGINT)
                except ProcessLookupError:
                    pass

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while True:
            if not marked_healthy and time.monotonic() >= startup_deadline:
                readiness.set(_STATE_HEALTHY)
                marked_healthy = True
                _log("readiness = healthy")

            if shutdown_deadline is not None:
                session = gateway.get_active_session()
                if session is None:
                    break
                if session.child.poll() is not None:
                    session.teardown()  # child already exited; still ensure fds are closed
                    break
                if time.monotonic() >= shutdown_deadline:
                    _log("shutdown grace window elapsed — force-terminating active runtime session")
                    session.teardown()
                    break

            time.sleep(_POLL_INTERVAL_SECONDS)
    finally:
        gateway.stop()
        sys.exit(0)


if __name__ == "__main__":
    main()
