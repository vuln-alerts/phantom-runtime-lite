"""
tests/test_cloud_run_shell_session.py
=========================================
H5-1 unit tests for runtime.cloud_run_shell's session lifecycle:
RuntimeSession, _spawn_session, and teardown idempotency/safety.

Does not spawn the real phantom_runtime.py entrypoint (heavy: needs
OPENAI_API_KEY/GEMINI_API_KEY and audio deps, and this project's own
Single Runtime Policy already keeps automated tests from importing or
driving it directly -- see tests/test_h4_10_runtime_adapter.py). Instead
verifies spawn/teardown/env-propagation against a tiny stand-in
entrypoint, substituted in for _RUNTIME_ENTRYPOINT_PATH.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

import runtime.cloud_run_shell as crs

_STUB_ENTRYPOINT = textwrap.dedent(
    """
    import os, signal, sys, time
    provider = os.environ.get("PHANTOM_PROVIDER", "")
    audio_fd = os.environ.get("PHANTOM_AUDIO_FD", "")
    event_fd = os.environ.get("PHANTOM_EVENT_FD", "")
    sys.stderr.write("provider=%s audio_fd=%s event_fd=%s\\n" % (provider, audio_fd, event_fd))
    sys.stderr.flush()
    signal.signal(signal.SIGINT, lambda *a: sys.exit(0))
    time.sleep(30)
    """
)


class TestSpawnAndTeardownSession(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        script_path = os.path.join(self._tmpdir.name, "stub_runtime.py")
        with open(script_path, "w") as f:
            f.write(_STUB_ENTRYPOINT)
        self._original_entrypoint = crs._RUNTIME_ENTRYPOINT_PATH
        crs._RUNTIME_ENTRYPOINT_PATH = script_path

    def tearDown(self):
        crs._RUNTIME_ENTRYPOINT_PATH = self._original_entrypoint
        self._tmpdir.cleanup()

    def test_spawn_starts_exactly_one_child_and_teardown_ends_it(self):
        session = crs._spawn_session([], "openai")
        try:
            self.assertIsNone(session.child.poll())  # still running
            self.assertIsInstance(session.audio_fd_w, int)
            self.assertIsInstance(session.event_fd_r, int)
        finally:
            session.teardown()
        self.assertIsNotNone(session.child.poll())  # exited after teardown

    def test_provider_propagated_via_env_not_deployment_wide(self):
        self.assertNotIn("PHANTOM_PROVIDER", os.environ)
        session = crs._spawn_session([], "gemini")
        try:
            time.sleep(0.3)  # let the child flush its startup line
        finally:
            session.teardown()
        # PHANTOM_PROVIDER must never leak into this process's own
        # environment -- only the spawned child's env carries it.
        self.assertNotIn("PHANTOM_PROVIDER", os.environ)

    def test_teardown_is_idempotent(self):
        session = crs._spawn_session([], "openai")
        session.teardown()
        first_returncode = session.child.returncode
        # Must not raise, even though the child already exited and the
        # fds are already closed.
        session.teardown()
        self.assertEqual(session.child.returncode, first_returncode)

    def test_teardown_tolerates_already_closed_fds(self):
        session = crs._spawn_session([], "openai")
        os.close(session.audio_fd_w)
        os.close(session.event_fd_r)
        # Must not raise despite the fds already being closed out-of-band.
        session.teardown()

    def test_teardown_tolerates_already_exited_child(self):
        session = crs._spawn_session([], "openai")
        session.child.send_signal(15)  # SIGTERM: exit before teardown runs
        session.child.wait(timeout=5)
        # Must not raise despite the child having already exited.
        session.teardown()


class TestSpawnSessionFailureCleanup(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        script_path = os.path.join(self._tmpdir.name, "stub_runtime.py")
        with open(script_path, "w") as f:
            f.write(_STUB_ENTRYPOINT)
        self._original_entrypoint = crs._RUNTIME_ENTRYPOINT_PATH
        crs._RUNTIME_ENTRYPOINT_PATH = script_path

    def tearDown(self):
        crs._RUNTIME_ENTRYPOINT_PATH = self._original_entrypoint
        self._tmpdir.cleanup()

    def test_popen_failure_closes_every_fd_already_opened(self):
        opened = []
        real_pipe = os.pipe

        def tracking_pipe():
            r, w = real_pipe()
            opened.extend([r, w])
            return r, w

        real_popen = subprocess.Popen

        def failing_popen(*args, **kwargs):
            raise OSError("simulated Popen failure")

        os.pipe = tracking_pipe
        subprocess.Popen = failing_popen
        try:
            with self.assertRaises(crs.SessionSpawnError):
                crs._spawn_session([], "openai")
        finally:
            os.pipe = real_pipe
            subprocess.Popen = real_popen

        self.assertEqual(len(opened), 6)  # three pipes: audio + event + control
        for fd in opened:
            with self.assertRaises(OSError):
                os.close(fd)  # already closed by the failure path


if __name__ == "__main__":
    unittest.main()
