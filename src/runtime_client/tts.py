"""
runtime_client/tts.py
========================
Client-side TTS provider abstraction (Phase 3).

Ported concept from phantom_conversational_runtime_v22.py's duck-typed
TTS backends (_NullTTSProvider / _SayTTSProvider / _Pyttsx3Provider /
_build_tts_provider()), but the playback mechanism itself is net-new:
the SSoT never played audio anywhere selectable (it shelled out to `say`
or ran pyttsx3.runAndWait() directly against whatever the process's
default audio output happened to be, with no output-device concept at
all). Here every provider renders one utterance to a WAV file and hands
it to a shared player that targets an explicit sounddevice output
device index -- this is what lets the Client route speech into a
virtual device (e.g. BlackHole) or a Multi-Output Device (speakers +
BlackHole simultaneously) without ever touching the macOS system
default output.

EXPORTED API:
  TTSProvider       -- duck-typed interface: speak(text)/stop()/is_speaking()
  NullTTSProvider    -- screen-only, zero audio (default)
  SayTTSProvider     -- macOS `say`, zero pip deps
  Pyttsx3Provider    -- cross-platform via pyttsx3 (optional dependency)
  build_tts_provider(name, *, voice, rate, volume, device_id) -- factory
"""

import os
import subprocess
import tempfile
import threading
import wave
from typing import Optional

import numpy as np
import sounddevice as sd

_DEFAULT_SAY_RATE = 200
_DEFAULT_PYTTSX3_RATE = 175


class TTSProvider:
    """
    Duck-typed interface every provider below implements. Matches the
    contract ui/keyboard.py's RuntimeContext.tts already requires
    (frozen -- keyboard.py is reused verbatim, not modified):
      speak(text: str) -> None
      stop() -> None
      is_speaking() -> bool
    """


class NullTTSProvider(TTSProvider):
    """Screen-only -- no audio output. Default provider (--tts none)."""

    def speak(self, text: str) -> None:
        pass

    def stop(self) -> None:
        pass

    def is_speaking(self) -> bool:
        return False


def _scale_volume(samples: "np.ndarray", volume: float) -> "np.ndarray":
    if volume == 1.0:
        return samples
    scaled = samples.astype(np.float32) * volume
    return np.clip(scaled, -32768, 32767).astype(np.int16)


def _read_wav_pcm16(path: str):
    """Returns (samples: np.ndarray[int16], samplerate: int)."""
    with wave.open(path, "rb") as wf:
        samplerate = wf.getframerate()
        channels = wf.getnchannels()
        frames = wf.readframes(wf.getnframes())
    samples = np.frombuffer(frames, dtype=np.int16)
    if channels > 1:
        samples = samples.reshape(-1, channels)
    return samples, samplerate


class _WavPlayer:
    """
    Shared playback engine used by every concrete TTS provider: decode a
    rendered WAV file via stdlib `wave`, apply volume by scaling PCM
    samples, and play it through sounddevice targeting an explicit
    output device (None = system default). A future TTS provider only
    needs to render its utterance to a WAV file and call `play()` --
    device routing, volume, and is_speaking()/stop() bookkeeping are
    handled once, here.
    """

    def __init__(self, volume: float, device_id: Optional[int]) -> None:
        self._volume = volume
        self._device_id = device_id

    def play(self, wav_path: str) -> None:
        samples, samplerate = _read_wav_pcm16(wav_path)
        samples = _scale_volume(samples, self._volume)
        sd.play(samples, samplerate, device=self._device_id)
        sd.wait()

    def stop(self) -> None:
        sd.stop()


class SayTTSProvider(TTSProvider):
    """
    macOS `say` command -- zero dependencies, ships with every Mac.
    Renders to a temp WAV (`say -o ... --data-format=LEI16@<rate>`) and
    plays it through the shared _WavPlayer so it can target a specific
    output device.
    """

    def __init__(
        self,
        voice: str = "Samantha",
        rate: Optional[int] = None,
        volume: float = 1.0,
        device_id: Optional[int] = None,
        samplerate: int = 22050,
    ) -> None:
        self._voice = voice
        self._rate = rate if rate is not None else _DEFAULT_SAY_RATE
        self._samplerate = samplerate
        self._player = _WavPlayer(volume, device_id)
        self._render_proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._busy = threading.Event()
        self._lock = threading.Lock()

    def speak(self, text: str) -> None:
        self.stop()

        def _run() -> None:
            self._busy.set()
            fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="phantom_tts_")
            os.close(fd)
            try:
                with self._lock:
                    self._render_proc = subprocess.Popen(
                        [
                            "say", "-v", self._voice, "-r", str(self._rate),
                            "-o", wav_path,
                            f"--data-format=LEI16@{self._samplerate}",
                            text,
                        ],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    proc = self._render_proc
                if proc is not None:
                    proc.wait()
                    with self._lock:
                        self._render_proc = None
                    if proc.returncode == 0 and os.path.exists(wav_path):
                        self._player.play(wav_path)
            except FileNotFoundError:
                pass  # `say` not found -- not on macOS; nothing further to do
            finally:
                self._busy.clear()
                try:
                    os.remove(wav_path)
                except OSError:
                    pass

        self._thread = threading.Thread(target=_run, daemon=True, name="tts-say")
        self._thread.start()

    def stop(self) -> None:
        with self._lock:
            proc = self._render_proc
            if proc is not None and proc.poll() is None:
                proc.terminate()
            self._render_proc = None
        self._player.stop()
        self._busy.clear()

    def is_speaking(self) -> bool:
        return self._busy.is_set()


class Pyttsx3Provider(TTSProvider):
    """
    Cross-platform TTS via pyttsx3 (pip install pyttsx3) -- optional
    dependency, exactly like the SSoT: import failure is caught, warned
    once, and every method silently no-ops afterward. Renders to AIFF
    (pyttsx3's macOS `nsss` driver output), normalizes to WAV via the
    stock `afconvert` CLI, then plays through the shared _WavPlayer.
    """

    def __init__(
        self,
        rate: Optional[int] = None,
        volume: float = 1.0,
        device_id: Optional[int] = None,
        on_warn=None,
    ) -> None:
        self._rate = rate if rate is not None else _DEFAULT_PYTTSX3_RATE
        self._player = _WavPlayer(volume, device_id)
        self._engine = None
        self._thread: Optional[threading.Thread] = None
        self._busy = threading.Event()
        self._warn = on_warn or (lambda msg: None)
        try:
            import pyttsx3  # type: ignore[import]

            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", self._rate)
        except Exception as exc:
            self._warn(f"TTS: pyttsx3 init failed ({exc}) -- falling back to null provider")
            self._engine = None

    def speak(self, text: str) -> None:
        if self._engine is None:
            return
        self.stop()

        def _run() -> None:
            fd, aiff_path = tempfile.mkstemp(suffix=".aiff", prefix="phantom_tts_")
            os.close(fd)
            wav_path = aiff_path[:-5] + ".wav"
            self._busy.set()
            try:
                self._engine.save_to_file(text, aiff_path)
                self._engine.runAndWait()
                result = subprocess.run(
                    ["afconvert", "-f", "WAVE", "-d", "LEI16", aiff_path, wav_path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if result.returncode == 0 and os.path.exists(wav_path):
                    self._player.play(wav_path)
            except Exception as exc:
                self._warn(f"TTS: pyttsx3 render/playback failed ({exc})")
            finally:
                self._busy.clear()
                for p in (aiff_path, wav_path):
                    try:
                        os.remove(p)
                    except OSError:
                        pass

        self._thread = threading.Thread(target=_run, daemon=True, name="tts-pyttsx3")
        self._thread.start()

    def stop(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
        except Exception:
            pass
        self._player.stop()
        self._busy.clear()

    def is_speaking(self) -> bool:
        if self._engine is None:
            return False
        return self._busy.is_set()


def build_tts_provider(
    name: str,
    *,
    voice: str = "Samantha",
    rate: Optional[int] = None,
    volume: float = 1.0,
    device_id: Optional[int] = None,
    on_warn=None,
) -> TTSProvider:
    """Build the TTS provider selected by --tts. Unrecognized names fall
    back to NullTTSProvider, matching the SSoT's catch-all `else` branch."""
    if name == "say":
        return SayTTSProvider(voice=voice, rate=rate, volume=volume, device_id=device_id)
    elif name == "pyttsx3":
        return Pyttsx3Provider(rate=rate, volume=volume, device_id=device_id, on_warn=on_warn)
    else:
        return NullTTSProvider()
