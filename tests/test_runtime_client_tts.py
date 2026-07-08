"""
tests/test_runtime_client_tts.py
====================================
Unit tests for src/runtime_client/tts.py: the Phase 3 client-side TTS
provider abstraction (NullTTSProvider / SayTTSProvider / Pyttsx3Provider
/ build_tts_provider). See tts.py's module docstring for why playback
routes through a shared WAV-render-then-sounddevice-play design rather
than mirroring the SSoT's direct-Popen-to-default-output approach.

`say`/`afconvert`/`sounddevice`'s actual subprocess and audio-hardware
calls are mocked throughout so these tests run deterministically in any
environment (see docs/RUNBOOK.md-style validation notes in
MIGRATION_MATRIX.md for the separate *live* smoke check that exercises
the real `say` binary and real sounddevice playback on macOS).

Uses unittest (stdlib), consistent with the rest of this project's test
suite: pytest is not a dependency.
"""

import io
import os
import struct
import sys
import tempfile
import threading
import unittest
import wave
from unittest.mock import MagicMock, patch

_SRC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.tts import (
    NullTTSProvider,
    Pyttsx3Provider,
    SayTTSProvider,
    _read_wav_pcm16,
    _scale_volume,
    build_tts_provider,
)


def _write_silence_wav(path: str, samplerate: int = 8000, n_samples: int = 400) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(samplerate)
        wf.writeframes(struct.pack(f"<{n_samples}h", *([1000] * n_samples)))


class TestNullTTSProvider(unittest.TestCase):
    def test_all_methods_are_no_ops(self):
        provider = NullTTSProvider()
        provider.speak("hello")  # must not raise
        self.assertFalse(provider.is_speaking())
        provider.stop()  # must not raise


class TestScaleVolumeAndReadWav(unittest.TestCase):
    def test_scale_volume_unity_is_identity(self):
        import numpy as np

        samples = np.array([100, -100, 32000], dtype=np.int16)
        self.assertTrue((_scale_volume(samples, 1.0) == samples).all())

    def test_scale_volume_halves_amplitude(self):
        import numpy as np

        samples = np.array([1000, -1000], dtype=np.int16)
        scaled = _scale_volume(samples, 0.5)
        self.assertEqual(list(scaled), [500, -500])

    def test_scale_volume_zero_silences(self):
        import numpy as np

        samples = np.array([1000, -1000, 32000], dtype=np.int16)
        scaled = _scale_volume(samples, 0.0)
        self.assertEqual(list(scaled), [0, 0, 0])

    def test_scale_volume_clips_at_int16_bounds(self):
        import numpy as np

        samples = np.array([30000, -30000], dtype=np.int16)
        scaled = _scale_volume(samples, 2.0)
        self.assertEqual(list(scaled), [32767, -32768])

    def test_read_wav_pcm16_roundtrip(self):
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        try:
            _write_silence_wav(path, samplerate=8000, n_samples=400)
            samples, samplerate = _read_wav_pcm16(path)
            self.assertEqual(samplerate, 8000)
            self.assertEqual(len(samples), 400)
            self.assertEqual(samples[0], 1000)
        finally:
            os.remove(path)


class TestBuildTTSProvider(unittest.TestCase):
    def test_none_builds_null_provider(self):
        self.assertIsInstance(build_tts_provider("none"), NullTTSProvider)

    def test_unrecognized_name_falls_back_to_null(self):
        self.assertIsInstance(build_tts_provider("elevenlabs"), NullTTSProvider)

    def test_say_builds_say_provider(self):
        provider = build_tts_provider("say", voice="Daniel", rate=210, volume=0.8, device_id=3)
        self.assertIsInstance(provider, SayTTSProvider)
        self.assertEqual(provider._voice, "Daniel")
        self.assertEqual(provider._rate, 210)

    def test_say_default_rate_is_200_when_unset(self):
        provider = build_tts_provider("say", rate=None)
        self.assertEqual(provider._rate, 200)

    def test_pyttsx3_default_rate_is_175_when_unset(self):
        with patch.dict(sys.modules, {"pyttsx3": None}):
            provider = build_tts_provider("pyttsx3", rate=None)
        self.assertEqual(provider._rate, 175)


class TestSayTTSProvider(unittest.TestCase):
    def _fake_popen(self, wav_path_holder, returncode=0, wav_samplerate=22050):
        """Builds a subprocess.Popen replacement that writes a real WAV
        file to the `-o` path argument (mirroring what `say -o ...`
        actually does) and reports the given returncode."""

        def _popen(args, **kwargs):
            out_path = args[args.index("-o") + 1]
            wav_path_holder.append(out_path)
            if returncode == 0:
                _write_silence_wav(out_path, samplerate=wav_samplerate, n_samples=200)
            proc = MagicMock()
            proc.wait.return_value = None
            proc.returncode = returncode
            proc.poll.return_value = 0  # already finished by the time wait() returns
            return proc

        return _popen

    def test_speak_renders_via_say_and_plays_through_sounddevice(self):
        wav_paths = []
        with patch("runtime_client.tts.subprocess.Popen", side_effect=self._fake_popen(wav_paths)), \
             patch("runtime_client.tts.sd.play") as mock_play, \
             patch("runtime_client.tts.sd.wait") as mock_wait:
            provider = SayTTSProvider(voice="Samantha", rate=200, volume=1.0, device_id=5)
            provider.speak("hello there")
            provider._thread.join(timeout=2)

        mock_play.assert_called_once()
        samples, samplerate = mock_play.call_args[0]
        self.assertEqual(samplerate, 22050)
        self.assertEqual(mock_play.call_args[1]["device"], 5)
        mock_wait.assert_called_once()
        self.assertFalse(provider.is_speaking())
        # temp file cleaned up
        self.assertFalse(os.path.exists(wav_paths[0]))

    def test_speak_passes_voice_and_rate_to_say(self):
        wav_paths = []
        captured_args = []

        def _popen(args, **kwargs):
            captured_args.append(args)
            return self._fake_popen(wav_paths)(args, **kwargs)

        with patch("runtime_client.tts.subprocess.Popen", side_effect=_popen), \
             patch("runtime_client.tts.sd.play"), patch("runtime_client.tts.sd.wait"):
            provider = SayTTSProvider(voice="Daniel", rate=230, volume=1.0, device_id=None)
            provider.speak("test text")
            provider._thread.join(timeout=2)

        args = captured_args[0]
        self.assertIn("-v", args)
        self.assertEqual(args[args.index("-v") + 1], "Daniel")
        self.assertIn("-r", args)
        self.assertEqual(args[args.index("-r") + 1], "230")
        self.assertIn("test text", args)

    def test_volume_scaling_applied_before_playback(self):
        wav_paths = []
        with patch("runtime_client.tts.subprocess.Popen", side_effect=self._fake_popen(wav_paths)), \
             patch("runtime_client.tts.sd.play") as mock_play, \
             patch("runtime_client.tts.sd.wait"):
            provider = SayTTSProvider(volume=0.5, device_id=None)
            provider.speak("quiet please")
            provider._thread.join(timeout=2)

        samples, _samplerate = mock_play.call_args[0]
        self.assertEqual(samples[0], 500)  # 1000 * 0.5

    def test_render_failure_skips_playback(self):
        wav_paths = []
        with patch(
            "runtime_client.tts.subprocess.Popen",
            side_effect=self._fake_popen(wav_paths, returncode=1),
        ), patch("runtime_client.tts.sd.play") as mock_play, patch("runtime_client.tts.sd.wait"):
            provider = SayTTSProvider()
            provider.speak("this will fail to render")
            provider._thread.join(timeout=2)

        mock_play.assert_not_called()
        self.assertFalse(provider.is_speaking())

    def test_stop_terminates_in_flight_render_and_prevents_playback(self):
        release = threading.Event()
        started = threading.Event()
        wav_paths = []

        def _slow_popen(args, **kwargs):
            out_path = args[args.index("-o") + 1]
            wav_paths.append(out_path)
            proc = MagicMock()

            def _wait():
                started.set()
                release.wait(timeout=2)
                return None

            proc.wait.side_effect = _wait
            proc.returncode = -15  # SIGTERM, as if terminate() was honored
            proc.poll.return_value = None
            return proc

        with patch("runtime_client.tts.subprocess.Popen", side_effect=_slow_popen), \
             patch("runtime_client.tts.sd.play") as mock_play, patch("runtime_client.tts.sd.wait"), \
             patch("runtime_client.tts.sd.stop") as mock_sd_stop:
            provider = SayTTSProvider()
            provider.speak("a long utterance")
            self.assertTrue(started.wait(timeout=2), "render did not start in time")
            self.assertTrue(provider.is_speaking())
            provider.stop()
            release.set()
            provider._thread.join(timeout=2)

        mock_play.assert_not_called()
        # speak() itself calls stop() once up front (interrupt any prior
        # utterance) plus the explicit provider.stop() call above -- both
        # route to sd.stop(), so at least one (here: exactly two) calls.
        self.assertGreaterEqual(mock_sd_stop.call_count, 1)
        self.assertFalse(provider.is_speaking())


class TestPyttsx3ProviderUnavailable(unittest.TestCase):
    def test_missing_pyttsx3_warns_and_becomes_no_op(self):
        warnings = []
        with patch.dict(sys.modules, {"pyttsx3": None}):
            provider = Pyttsx3Provider(rate=180, on_warn=warnings.append)
        self.assertTrue(any("pyttsx3" in w for w in warnings))
        provider.speak("should be a no-op")  # must not raise
        self.assertFalse(provider.is_speaking())
        provider.stop()  # must not raise


class TestPyttsx3ProviderAvailable(unittest.TestCase):
    def _install_fake_pyttsx3(self):
        fake_module = MagicMock()
        fake_engine = MagicMock()
        fake_module.init.return_value = fake_engine
        return fake_module, fake_engine

    def test_speak_renders_via_afconvert_and_plays(self):
        fake_module, fake_engine = self._install_fake_pyttsx3()
        wav_paths = []

        def _fake_afconvert(args, **kwargs):
            aiff_path = args[-2]
            wav_path = args[-1]
            wav_paths.append(wav_path)
            _write_silence_wav(wav_path, samplerate=22050, n_samples=150)
            result = MagicMock()
            result.returncode = 0
            return result

        with patch.dict(sys.modules, {"pyttsx3": fake_module}), \
             patch("runtime_client.tts.subprocess.run", side_effect=_fake_afconvert), \
             patch("runtime_client.tts.sd.play") as mock_play, \
             patch("runtime_client.tts.sd.wait"):
            provider = Pyttsx3Provider(rate=175, volume=1.0, device_id=2)
            provider.speak("hello from pyttsx3")
            provider._thread.join(timeout=2)

        fake_engine.save_to_file.assert_called_once()
        fake_engine.runAndWait.assert_called_once()
        mock_play.assert_called_once()
        self.assertEqual(mock_play.call_args[1]["device"], 2)
        self.assertFalse(provider.is_speaking())
        self.assertFalse(os.path.exists(wav_paths[0]))

    def test_afconvert_failure_skips_playback(self):
        fake_module, fake_engine = self._install_fake_pyttsx3()

        def _failing_afconvert(args, **kwargs):
            result = MagicMock()
            result.returncode = 1
            return result

        with patch.dict(sys.modules, {"pyttsx3": fake_module}), \
             patch("runtime_client.tts.subprocess.run", side_effect=_failing_afconvert), \
             patch("runtime_client.tts.sd.play") as mock_play:
            provider = Pyttsx3Provider()
            provider.speak("will fail to convert")
            provider._thread.join(timeout=2)

        mock_play.assert_not_called()
        self.assertFalse(provider.is_speaking())

    def test_stop_calls_engine_stop(self):
        fake_module, fake_engine = self._install_fake_pyttsx3()
        with patch.dict(sys.modules, {"pyttsx3": fake_module}), \
             patch("runtime_client.tts.sd.stop"):
            provider = Pyttsx3Provider()
            provider.stop()
        fake_engine.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
