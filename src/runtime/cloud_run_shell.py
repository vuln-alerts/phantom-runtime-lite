"""
runtime/cloud_run_shell.py
============================
Cloud Run Compatibility Shell.

Process supervisor that runs OUTSIDE the Runtime Boundary. It spawns the
existing, unmodified Runtime entrypoint (phantom_conversational_runtime_v22.py)
as a child process and exposes a Cloud Run-compatible readiness endpoint via
runtime.health_server.

Design basis: docs/V1_11_H2_CLOUD_RUN_COMPATIBILITY_CONTRACT.md,
docs/V1_11_H2_CLOUD_RUN_IMPLEMENTATION_PLAN.md (Part I Sections 1, 4, 5;
Part II Phases 1, 3, 4).

CONSTRAINTS (do not violate):
  - Never import phantom_conversational_runtime_v22, config, or provider.*
    The Runtime child is spawned as a subprocess, never imported.
  - Never add a signal handler inside the Runtime child. SIGTERM received by
    this Shell is translated to SIGINT and delivered to the child process,
    reusing the Runtime's existing KeyboardInterrupt-based graceful shutdown.
  - Runtime CLI arguments are forwarded verbatim, unmodified.

EXPORTED API:
  main() — process entrypoint
           (invoked as: python -m runtime.cloud_run_shell -- <runtime args>)
"""

import os
import signal
import subprocess
import sys
import threading
import time

from runtime.health_server import HealthServer

_RUNTIME_ENTRYPOINT_NAME = "phantom_runtime.py"

# Directory containing this file's parent package root (src/), i.e. the same
# directory phantom_conversational_runtime_v22.py is copied into by the
# Dockerfile. Resolved via __file__ so behavior does not depend on cwd.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_RUNTIME_ENTRYPOINT_PATH = os.path.join(_SRC_DIR, _RUNTIME_ENTRYPOINT_NAME)

STARTUP_GRACE_SECONDS = float(os.getenv("CLOUD_RUN_STARTUP_GRACE_SECONDS", "5"))
SHUTDOWN_GRACE_SECONDS = float(os.getenv("CLOUD_RUN_SHUTDOWN_GRACE_SECONDS", "10"))
_POLL_INTERVAL_SECONDS = 0.2

_STATE_STARTING = "starting"
_STATE_HEALTHY = "healthy"
_STATE_SHUTTING_DOWN = "shutting_down"
_STATE_FAILED = "failed"


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


def _forward_args(argv: list) -> list:
    """Strip a leading '--' separator, if present; return the rest verbatim."""
    if argv and argv[0] == "--":
        return argv[1:]
    return argv


def _spawn_runtime(forwarded_args: list) -> subprocess.Popen:
    cmd = [sys.executable, _RUNTIME_ENTRYPOINT_PATH] + forwarded_args
    _log(f"spawning runtime child: {' '.join(cmd)}")
    return subprocess.Popen(cmd, cwd=_SRC_DIR)


def main() -> None:
    port = int(os.getenv("PORT", "8080"))
    forwarded_args = _forward_args(sys.argv[1:])

    readiness = _ReadinessState()
    health_server = HealthServer(port=port, is_ready=readiness.is_ready)
    health_server.start()
    _log(f"health server listening on :{port}")

    child = _spawn_runtime(forwarded_args)
    _log(f"runtime child started (pid={child.pid})")

    startup_deadline = time.monotonic() + STARTUP_GRACE_SECONDS
    marked_healthy = False
    shutdown_deadline = None

    def _handle_sigterm(signum, frame) -> None:
        nonlocal shutdown_deadline
        readiness.set(_STATE_SHUTTING_DOWN)
        if shutdown_deadline is None:
            shutdown_deadline = time.monotonic() + SHUTDOWN_GRACE_SECONDS
            _log("SIGTERM received — forwarding SIGINT to runtime child")
            try:
                child.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass

    signal.signal(signal.SIGTERM, _handle_sigterm)

    while True:
        exit_code = child.poll()
        if exit_code is not None:
            if not marked_healthy:
                readiness.set(_STATE_FAILED)
                _log(f"runtime child exited during startup (code={exit_code})")
                sys.exit(exit_code if exit_code != 0 else 1)
            _log(f"runtime child exited (code={exit_code})")
            sys.exit(exit_code)

        if not marked_healthy and time.monotonic() >= startup_deadline:
            readiness.set(_STATE_HEALTHY)
            marked_healthy = True
            _log("readiness = healthy")

        if shutdown_deadline is not None and time.monotonic() >= shutdown_deadline:
            _log("shutdown grace window elapsed — sending SIGKILL to runtime child")
            try:
                child.kill()
            except ProcessLookupError:
                pass
            child.wait()
            sys.exit(0)

        time.sleep(_POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
