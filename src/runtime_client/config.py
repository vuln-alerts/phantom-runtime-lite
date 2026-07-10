"""
runtime_client/config.py
=========================
CLI argument parsing and connection URL construction for the Runtime
Client.

EXPORTED API:
  ClientConfig       -- resolved configuration for one client run
  parse_args(argv)   -- parse sys.argv (or argv) into a ClientConfig
  build_ws_url(url, provider) -- turn a Cloud Run http(s) URL into
                                  wss://.../ws?provider=<provider>
"""

import argparse
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit, urlunsplit

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_BLOCK_SIZE = 1600  # 100ms of audio at 16kHz mono
DEFAULT_MAX_RECONNECT_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_SECONDS = 1.0
# P5-4-1: NOT the server's RMS_THRESHOLD (120) -- that value was proven
# ineffective on real hardware (see investigation notes/report): ambient
# room noise measured 138-530 RMS on one real external mic, i.e. already
# above 120, so a threshold matched to the Server's default silently
# forwarded 100% of true silence. 700 sits with margin above the
# measured silence ceiling (max 530.2 across 7 trials / ~600+ blocks)
# and was confirmed live against Cloud Run to suppress the repeated-
# hallucination symptom. See runtime_client/audio_bridge.py's silence
# gate.
DEFAULT_SILENCE_RMS_THRESHOLD = 700


@dataclass(frozen=True)
class ClientConfig:
    url: str
    provider: str
    input_device: Optional[str]
    output_device: Optional[str]
    sample_rate: int
    channels: int
    block_size: int
    max_reconnect_attempts: int
    backoff_base_seconds: float
    manual_flush: bool
    silence_rms_threshold: int
    tts: str
    voice: str
    rate: Optional[int]
    volume: float
    list_devices: bool = False
    list_output_devices: bool = False
    # Production Verification investigation support (TEMPORARY -- see
    # runtime_client/debug_sink.py's module docstring). Does not alter
    # calibration behavior; only auto-enables the existing
    # PHANTOM_CALIBRATION_DEBUG=1 debug instrumentation and tees it to a
    # session log file, plus a post-calibration summary file. Defaulted
    # to False, at the end of the dataclass, so it is purely additive --
    # every existing ClientConfig(...) construction keeps working.
    production_verification: bool = False


def build_ws_url(url: str, provider: str) -> str:
    """
    Convert a Cloud Run base URL (http/https, with or without a path) into
    the Runtime's WebSocket endpoint: wss://<host>/ws?provider=<provider>.
    http:// -> ws://, https:// -> wss:// (Cloud Run only serves https, so
    this normally yields wss://). Any existing path/query on the input is
    discarded -- only the scheme+netloc are kept.
    """
    parts = urlsplit(url)
    scheme = {"http": "ws", "https": "wss", "ws": "ws", "wss": "wss"}.get(parts.scheme)
    if scheme is None or not parts.netloc:
        raise ValueError(f"invalid --url: {url!r} (expected http(s):// or ws(s)://)")
    return urlunsplit((scheme, parts.netloc, "/ws", f"provider={provider}", ""))


def parse_args(argv: Optional[list] = None) -> ClientConfig:
    parser = argparse.ArgumentParser(
        prog="runtime_client",
        description=(
            "Phantom Runtime Client -- streams Mac audio input (built-in "
            "mic, USB mic, or a virtual device such as BlackHole/Loopback "
            "fed by Zoom/Meet/Teams/Discord) to the Cloud Run Runtime over "
            "WebSocket, and renders the Typed Events it sends back."
        ),
    )
    parser.add_argument("--url", default=None, help="Cloud Run base URL, e.g. https://xxxxx.run.app")
    parser.add_argument("--provider", choices=("openai", "gemini"), default=None, help="Runtime provider for this session")
    parser.add_argument("--input-device", default=None, help="Input device name (substring match) or index; default: system default input")
    parser.add_argument("--output-device", default=None, help="Output device name (substring match) or index, for TTS playback (Phase 3); default: system default output")
    parser.add_argument("--list-devices", action="store_true", help="List available input devices and exit")
    parser.add_argument("--list-output-devices", action="store_true", help="List available output devices and exit")
    parser.add_argument(
        "--production-verification",
        action="store_true",
        help=(
            "Production Verification investigation support (temporary): "
            "auto-enables PHANTOM_CALIBRATION_DEBUG=1-equivalent Calibration "
            "Debug output, tees it to logs/calibration_<timestamp>.log, and "
            "writes logs/root_cause_summary.txt after Startup Calibration "
            "finishes. Normal startup/behavior is otherwise unchanged."
        ),
    )
    parser.add_argument("--sample-rate", type=int, default=DEFAULT_SAMPLE_RATE)
    parser.add_argument("--block-size", type=int, default=DEFAULT_BLOCK_SIZE, help="Frames per audio block sent per WebSocket message")
    parser.add_argument("--max-reconnect-attempts", type=int, default=DEFAULT_MAX_RECONNECT_ATTEMPTS)
    parser.add_argument("--backoff-base-seconds", type=float, default=DEFAULT_BACKOFF_BASE_SECONDS)
    parser.add_argument("--manual-flush", action="store_true", help="Match server's --manual-flush help text for the 'g' key (informational only; server decides the actual behavior)")
    parser.add_argument("--tts", default="none", choices=("none", "say", "pyttsx3"), help="Client-side TTS engine for spoken replies (Phase 3)")
    parser.add_argument("--voice", default="Samantha", help="TTS voice name, passed to the 'say' provider (Phase 3)")
    parser.add_argument("--rate", type=int, default=None, help="TTS speech rate (words/min); default: provider's own default (say=200, pyttsx3=175)")
    parser.add_argument("--volume", type=float, default=1.0, help="TTS playback volume, 0.0-1.0 (Phase 3)")
    args = parser.parse_args(argv)

    if not (args.list_devices or args.list_output_devices):
        if not args.url:
            parser.error("--url is required (unless --list-devices/--list-output-devices)")
        if not args.provider:
            parser.error("--provider is required (unless --list-devices/--list-output-devices)")

    if not (0.0 <= args.volume <= 1.0):
        parser.error(f"--volume must be between 0.0 and 1.0 (got {args.volume})")

    return ClientConfig(
        url=args.url or "",
        provider=args.provider or "",
        input_device=args.input_device,
        output_device=args.output_device,
        sample_rate=args.sample_rate,
        channels=DEFAULT_CHANNELS,
        block_size=args.block_size,
        max_reconnect_attempts=args.max_reconnect_attempts,
        backoff_base_seconds=args.backoff_base_seconds,
        manual_flush=args.manual_flush,
        silence_rms_threshold=DEFAULT_SILENCE_RMS_THRESHOLD,
        tts=args.tts,
        voice=args.voice,
        rate=args.rate,
        volume=args.volume,
        list_devices=args.list_devices,
        list_output_devices=args.list_output_devices,
        production_verification=args.production_verification,
    )
