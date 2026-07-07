"""
runtime/health_server.py
=========================
Cloud Run health/readiness HTTP endpoint for the Compatibility Shell.

Standard-library only (http.server) — no new dependency. This module never
imports the Runtime, config, or provider.* — it only knows how to serve a
readiness flag supplied by its caller (the Compatibility Shell).

EXPORTED API:
  HealthServer(port, is_ready) — construct; is_ready is a zero-arg callable
                                  returning bool
  server.start()                — start serving on a daemon thread
  server.stop()                 — stop serving and release the socket
"""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

HEALTH_PATHS = ("/healthz", "/")


class _HealthRequestHandler(BaseHTTPRequestHandler):
    # Overridden per-instance via a dynamically built subclass in HealthServer.
    is_ready: Callable[[], bool] = staticmethod(lambda: False)

    def do_GET(self) -> None:
        if self.path not in HEALTH_PATHS:
            self.send_response(404)
            self.end_headers()
            return
        healthy = self.is_ready()
        body = b"ok" if healthy else b"not ready"
        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        # Suppress default per-request access logging (Contract Section 11, L-4).
        pass


class HealthServer:
    """Minimal readiness endpoint. Owns no Runtime state — reads only `is_ready`."""

    def __init__(self, port: int, is_ready: Callable[[], bool]) -> None:
        handler_cls = type(
            "_BoundHealthRequestHandler",
            (_HealthRequestHandler,),
            {"is_ready": staticmethod(is_ready)},
        )
        self._httpd = ThreadingHTTPServer(("0.0.0.0", port), handler_cls)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="health-server",
            daemon=True,
        )

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
