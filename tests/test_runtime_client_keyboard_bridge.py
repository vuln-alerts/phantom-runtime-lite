"""
tests/test_runtime_client_keyboard_bridge.py
=================================================
Unit tests for src/runtime_client/keyboard_bridge.py: NotifyingEvent's
callback firing, _send_control's queue-enqueue behavior, and
build_keyboard_thread's wiring of the real (unmodified)
ui.keyboard.KeyboardController/RuntimeContext into Control Events
(H6) -- verifying, end to end through a live KeyboardController.run()
loop fed synthetic keystrokes, that 'G'/'g'/'r' produce exactly the
Control Event JSON phantom_runtime.py's control_loop() is documented
to accept (see tests/test_h6_control_event_relay.py for the transport
side of that same contract). Also covers Phase 3: the 's' key's
existing (unmodified) TTS-stop branch in ui/keyboard.py, now wired to
the real store.tts / store.tts_interrupt_event instead of the old
Phase 1-4 _NullTTS stub.

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from unittest.mock import patch

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.config import parse_args
from runtime_client.keyboard_bridge import NotifyingEvent, _send_control, build_keyboard_thread
from runtime_client.typed_event import TypedEventStore


class _FakeTTS:
    """Not a NullTTSProvider -- lets tests observe ctx.tts.stop() calls
    made by ui/keyboard.py's real (unmodified) 's' handler."""

    def __init__(self, speaking=True):
        self._speaking = speaking
        self.stop_calls = 0

    def speak(self, text):
        self._speaking = True

    def stop(self):
        self.stop_calls += 1
        self._speaking = False

    def is_speaking(self):
        return self._speaking


class TestNotifyingEvent(unittest.TestCase):
    def test_set_fires_callback_with_true(self):
        seen = []
        ev = NotifyingEvent(on_change=seen.append)
        ev.set()
        self.assertEqual(seen, [True])
        self.assertTrue(ev.is_set())

    def test_clear_fires_callback_with_false(self):
        seen = []
        ev = NotifyingEvent(on_change=seen.append)
        ev.set()
        ev.clear()
        self.assertEqual(seen, [True, False])
        self.assertFalse(ev.is_set())


class TestSendControl(unittest.TestCase):
    def test_send_control_enqueues_json_command_line(self):
        loop = asyncio.new_event_loop()
        try:
            queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=10)
            _send_control(loop, queue, "generate_summary")
            loop.run_until_complete(asyncio.sleep(0))  # let the scheduled callback run
            self.assertEqual(queue.get_nowait(), json.dumps({"command": "generate_summary"}))
        finally:
            loop.close()

    def test_send_control_drops_when_queue_full(self):
        loop = asyncio.new_event_loop()
        try:
            queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=1)
            queue.put_nowait("already full")
            with patch("runtime_client.keyboard_bridge.show_warn") as warn:
                _send_control(loop, queue, "toggle_recording")
                loop.run_until_complete(asyncio.sleep(0))
            warn.assert_called_once()
            self.assertEqual(queue.get_nowait(), "already full")  # unchanged, no crash
        finally:
            loop.close()


def _run_keyboard_and_collect(commands, store=None):
    """
    Runs the real KeyboardController (via build_keyboard_thread) against
    a scripted stdin, on a live background asyncio loop (so
    call_soon_threadsafe callbacks actually execute), and returns
    whatever landed in the control queue plus the kb_shutdown event.
    """
    loop = asyncio.new_event_loop()
    loop_thread = threading.Thread(target=loop.run_forever, daemon=True)
    loop_thread.start()
    try:
        control_queue: "asyncio.Queue[str]" = asyncio.Queue(maxsize=100)
        config = parse_args(["--url", "https://xxxxx.run.app", "--provider", "openai"])
        if store is None:
            store = TypedEventStore()
        kb_shutdown = threading.Event()

        it = iter(commands)

        def fake_input():
            try:
                return next(it)
            except StopIteration:
                raise EOFError()

        with patch("builtins.input", side_effect=fake_input), patch("builtins.print"):
            kb_thread = build_keyboard_thread(config, store, loop, control_queue, kb_shutdown)
            kb_thread.start()
            kb_thread.join(timeout=5)
            # 'G'/'g' dispatch their control-send via a short-lived daemon
            # thread (matching ui/keyboard.py's own behavior for these
            # two keys); give it a moment to actually run before draining.
            time.sleep(0.3)

        async def _drain():
            items = []
            while not control_queue.empty():
                items.append(control_queue.get_nowait())
            return items

        items = asyncio.run_coroutine_threadsafe(_drain(), loop).result(timeout=2)
        return items, kb_shutdown
    finally:
        loop.call_soon_threadsafe(loop.stop)
        loop_thread.join(timeout=2)
        loop.close()


class TestBuildKeyboardThreadControlEvents(unittest.TestCase):
    def test_uppercase_G_sends_generate_summary(self):
        items, _ = _run_keyboard_and_collect(["G", "q"])
        self.assertIn(json.dumps({"command": "generate_summary"}), items)

    def test_lowercase_g_sends_generate_meeting_analysis(self):
        items, _ = _run_keyboard_and_collect(["g", "q"])
        self.assertIn(json.dumps({"command": "generate_meeting_analysis"}), items)

    def test_r_toggles_and_sends_toggle_recording(self):
        # recording_active starts set() (ON) inside build_keyboard_thread,
        # so the first 'r' clears it -- exactly one toggle_recording event.
        items, _ = _run_keyboard_and_collect(["r", "q"])
        self.assertEqual(items.count(json.dumps({"command": "toggle_recording"})), 1)

    def test_two_r_presses_send_two_toggle_events(self):
        items, _ = _run_keyboard_and_collect(["r", "r", "q"])
        self.assertEqual(items.count(json.dumps({"command": "toggle_recording"})), 2)

    def test_q_sets_kb_shutdown_event(self):
        _, kb_shutdown = _run_keyboard_and_collect(["q"])
        self.assertTrue(kb_shutdown.is_set())

    def test_non_control_keys_do_not_touch_control_queue(self):
        # 'h'/'u'/'d'/'t' are local, no-API console actions (see
        # keyboard_bridge.py's module docstring) and must never produce
        # a Control Event.
        items, _ = _run_keyboard_and_collect(["h", "u", "d", "t", "?", "q"])
        self.assertEqual(items, [])


class TestSKeyStopsRealStoreTTS(unittest.TestCase):
    def test_s_key_stops_speaking_tts_via_store(self):
        fake = _FakeTTS(speaking=True)
        store = TypedEventStore(tts=fake)
        _run_keyboard_and_collect(["s", "q"], store=store)
        self.assertEqual(fake.stop_calls, 1)
        self.assertTrue(store.tts_interrupt_event.is_set())  # keyboard.py sets it; not our job to clear

    def test_s_key_is_a_no_op_when_tts_not_speaking(self):
        fake = _FakeTTS(speaking=False)
        store = TypedEventStore(tts=fake)
        _run_keyboard_and_collect(["s", "q"], store=store)
        self.assertEqual(fake.stop_calls, 0)


if __name__ == "__main__":
    unittest.main()
