"""
Phantom Runtime Lite
=====================
Human-controlled realtime conversational AI agent runtime.

Provides real-time conversational understanding and decision support:
transcription, speaker inference, hallucination filtering, profile
grounding, and structured analysis of an ongoing conversation.

RuntimeMode enum — INTERVIEW / MEETING / SUMMARY
  Selects how captured speech is processed: live conversational assistance,
  structured analysis of the discussion so far, or end-of-session summary
  generation.

Structured analysis output (Japanese):
  # サマリー
  # リスク・懸念事項
  # 検出された質問  (with 回答候補 per question)
  # 推奨アクション

Core capabilities:
  manual flush, state machine, transcript persistence, health monitoring,
  VAD extraction, speaker inference, profile grounding, hallucination guard,
  keyboard controls, queue management, degradation handling, cognition pipeline

EXAMPLE COMMAND:
  PYTHONUNBUFFERED=1 ENABLE_TRANSCRIPT_PERSIST=1 ENABLE_HALLUCINATION_GUARD=1 \\
  ENABLE_PROFILE_GROUNDING=1 ENABLE_SPEAKER_TRACE=1 SESSION_OUTPUT_DIR=./sessions \\
  INPUT_DEVICE="外部マイク" \\
  python3 phantom_runtime.py \\
  --profile enterprise --agent --tts none --manual-flush --cognition \\
  --threshold 120 --min-sec 0.4 --silence-sec 0.25 --max-sec 8 \\
  --history-turns 6 --english-level natural --candidates 3
"""

# ─────────────────────────────────────────────────────────────────────────────
# Standard library
# ─────────────────────────────────────────────────────────────────────────────
import argparse
import io
import json as _json
import os
import queue
import random
import signal
import sys
import threading
import datetime as _datetime
import time
import wave
import re
from collections import deque
from typing import NamedTuple, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Third-party
# ─────────────────────────────────────────────────────────────────────────────
import numpy as np
import sounddevice as sd
from dotenv import load_dotenv
from openai import OpenAI, APITimeoutError, APIConnectionError, RateLimitError

# H2A-5: Provider Interface for LLM chat completions. `openai` above remains
# in use only for Whisper audio transcription (client.audio.transcriptions),
# which is outside the Provider Interface's scope (chat-completion-shaped
# models only) — see the static provider instances below for detail.
from provider.openai_provider import OpenAIProvider
from provider.gemini_provider import GeminiProvider
from provider.models import (
    Message,
    ProviderRequest,
    StreamCancellationReason,
    StreamingCancellation,
    StreamingCompletion,
    StreamingError,
    StreamingTextDelta,
)
from provider.errors import (
    RuntimeRateLimitError,
    RuntimeTimeoutError,
)

# ─────────────────────────────────────────────────────────────────────────────
# Module imports (v15 real extractions)
# ─────────────────────────────────────────────────────────────────────────────
# These are actual module imports, not comments.
# Modules live in subdirectories next to this file.
# Each module is independently importable and testable.
import sys as _sys
import os as _os
_SCRIPT_DIR_EARLY = _os.path.dirname(_os.path.abspath(__file__))
if _SCRIPT_DIR_EARLY not in _sys.path:
    _sys.path.insert(0, _SCRIPT_DIR_EARLY)

try:
    from conversation.speaker_inference import infer_speaker as _ext_infer_speaker
    from conversation.speaker_inference import reset_speaker_state as _ext_reset_speaker
    _SPEAKER_MODULE_LOADED = True
except ImportError as _e:
    _SPEAKER_MODULE_LOADED = False
    print(f"[warn] conversation.speaker_inference not loaded: {_e}", file=_sys.stderr)

try:
    from transcript.persistence import (
        init_session  as _ext_init_session,
        persist_entry as _ext_persist_entry,
        get_session_id as _ext_get_session_id,
        close_session  as _ext_close_session,
    )
    _PERSIST_MODULE_LOADED = True
except ImportError as _e:
    _PERSIST_MODULE_LOADED = False
    print(f"[warn] transcript.persistence not loaded: {_e}", file=_sys.stderr)

try:
    from runtime.metrics import tracker as latency_tracker
    _METRICS_MODULE_LOADED = True
except ImportError as _e:
    latency_tracker = None
    _METRICS_MODULE_LOADED = False

try:
    from runtime.health import get_runtime_health, format_health_line, format_manual_buf_line
    _HEALTH_MODULE_LOADED = True
except ImportError as _e:
    _HEALTH_MODULE_LOADED = False

try:
    from audio.devices import resolve_device_id, print_input_devices
    _DEVICES_MODULE_LOADED = True
except ImportError as _e:
    _DEVICES_MODULE_LOADED = False
    def resolve_device_id(name): return None  # fallback

try:
    from audio.routing import playback_suppressor
    _ROUTING_MODULE_LOADED = True
except ImportError as _e:
    _ROUTING_MODULE_LOADED = False

try:
    from ui.keyboard import KeyboardController, RuntimeContext
    _KEYBOARD_MODULE_LOADED = True
except ImportError as _e:
    _KEYBOARD_MODULE_LOADED = False

try:
    from conversation.hallucination_guard import is_meaningful as _ext_is_meaningful
    _HALLUCINATION_MODULE_LOADED = True
except ImportError as _e:
    _HALLUCINATION_MODULE_LOADED = False
    print(f"[warn] conversation.hallucination_guard not loaded: {_e}", file=_sys.stderr)

# ── v16 additional module imports ─────────────────────────────────────────────
try:
    from config import RuntimeConfig as _RuntimeConfig
    _CONFIG_MODULE_LOADED = True
except ImportError:
    _CONFIG_MODULE_LOADED = False

try:
    from runtime.state_machine import (
        ConversationState,
        RuntimeMode,
    )
    _STATE_MACHINE_MODULE_LOADED = True
except ImportError:
    _STATE_MACHINE_MODULE_LOADED = False

try:
    from runtime.runtime_logger import RuntimeLogger as _RuntimeLogger
    _LOGGING_MODULE_LOADED = True
except ImportError:
    _LOGGING_MODULE_LOADED = False

try:
    from audio.vad import VADOrchestrator as _VADOrchestrator
    _VAD_MODULE_LOADED = True
except ImportError:
    _VAD_MODULE_LOADED = False

try:
    from audio.capture import AudioCapture as _AudioCapture
    _CAPTURE_MODULE_LOADED = True
except ImportError:
    _CAPTURE_MODULE_LOADED = False

try:
    from conversation.orchestration import ConversationOrchestrator as _ConvOrch
    _ORCH_MODULE_LOADED = True
except ImportError:
    _ORCH_MODULE_LOADED = False

try:
    from transcript.replay import load_session as _load_session, session_stats as _session_stats
    _REPLAY_MODULE_LOADED = True
except ImportError:
    _REPLAY_MODULE_LOADED = False

from audio.vad_buffering import VADBuffer as _VADBuffer

# ─────────────────────────────────────────────────────────────────────────────
# Path resolution — standalone-safe
# ─────────────────────────────────────────────────────────────────────────────
# Fix 2: Use __file__ directory only, not dirname(dirname(...)).
# This makes the runtime work whether run from its own directory or from anywhere.
_SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
PROFILES_DIR = os.path.join(_SCRIPT_DIR, "profiles")
BASE_DIR     = _SCRIPT_DIR   # prompts/ subfolder is relative to script, not parent

# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Real-time recruiter interview assistant",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--profile",       default="default",
                   help="Runtime profile name (without .md). Examples: verizon, workport, upwork, phantom_runtime")
    p.add_argument("--mode",          default="light", choices=["light", "full"],
                   help="Prompt mode: 'light'=built-in, 'full'=load prompts/phantom_core.txt")
    p.add_argument("--threshold",     default=120,  type=int,
                   help="RMS silence threshold")
    p.add_argument("--min-sec",       default=0.4,  type=float,
                   help="Minimum speech before VAD flush (seconds). 0.4s = fast segmentation")
    p.add_argument("--max-sec",       default=8.0,  type=float,
                   help="Force-flush ceiling (seconds). 8s ensures max 8s segments in manual mode")
    p.add_argument("--silence-sec",   default=0.25, type=float,
                   help="Silence duration that triggers VAD flush. 0.25s = clean boundaries")
    p.add_argument("--whisper-model", default="whisper-1",
                   help="Whisper model ID")
    p.add_argument("--gpt-model",     default="gpt-4o-mini",
                   help="GPT model for reply generation")
    p.add_argument("--no-color",      action="store_true",
                   help="Disable ANSI color output")
    p.add_argument("--interview-lang", default="mixed", choices=["en", "ja", "mixed"],
                   help="en=English call; ja=Japanese call; mixed=auto-detect (default)")
    p.add_argument("--pronunciation", action="store_true",
                   help="Add katakana reading guide [READ] after each [EN] reply")
    p.add_argument("--english-level", default="natural",
                   choices=["beginner", "simple", "natural", "fluent"],
                   help="beginner/simple/natural/fluent")
    p.add_argument("--health-interval", default=60, type=int,
                   help="Seconds between runtime health snapshots (0 = disable)")
    p.add_argument("--input-device", default="",
                   help="Input device name substring (e.g. '外部マイク', 'BlackHole'). "
                        "Also reads INPUT_DEVICE env var. Empty = system default.")
    p.add_argument("--audio-source", default="mic", choices=["mic", "fd"],
                   help="Audio input source: 'mic'=local sounddevice capture (default); "
                        "'fd'=read raw PCM16LE mono audio from the PHANTOM_AUDIO_FD pipe "
                        "handed to this process by runtime.cloud_run_shell.")

    # ── Manual flush mode (human-in-the-loop execution control) ──────────
    p.add_argument("--manual-flush", action="store_true",
                   help=(
                       "Human-controlled execution mode. "
                       "Audio segments accumulate silently after VAD flush. "
                       "Press 'g' to merge + send buffered audio to Whisper/GPT. "
                       "Press 'G' to generate summary. "
                       "Prevents accidental outputs and gives operator full timing control. "
                       "Recommended for Verizon and high-stakes calls."
                   ))

    # ── Agent mode ───────────────────────────────────────────────────────
    p.add_argument("--agent", action="store_true",
                   help=(
                       "Agent mode: autonomous response generation. "
                       "When recruiter finishes a question, GPT reply is generated "
                       "automatically and optionally spoken via TTS. "
                       "Default: off (observer/suggestion mode only)."
                   ))
    p.add_argument("--tts", default="none",
                   choices=["none", "say", "pyttsx3"],
                   help=(
                       "TTS provider for agent mode. "
                       "'none'=display only; 'say'=macOS built-in (zero deps); "
                       "'pyttsx3'=cross-platform (pip install pyttsx3)"
                   ))
    p.add_argument("--classify", action="store_true",
                   help=(
                       "Use GPT to classify ambiguous utterances as question/statement. "
                       "Adds ~200ms per utterance. Off by default (heuristic only)."
                   ))
    p.add_argument("--history-turns", default=4, type=int,
                   help="Number of recent conversation turns to include in agent GPT context (0=none)")

    # ── v17 Cognition pipeline ────────────────────────────────────────────
    p.add_argument("--cognition", action="store_true",
                   help=(
                       "Enable v17 cognition pipeline: "
                       "pre-flush conversation compression + multi-candidate generation. "
                       "Off by default — v16 behavior is preserved without this flag. "
                       "Adds ~0.5-2s latency after transcription. "
                       "Recommended: use with --manual-flush for operator control."
                   ))
    p.add_argument("--candidates", default=3, type=int,
                   choices=[1, 2, 3],
                   help="Number of response candidates to generate per question (1-3, default 3)")

    # ── Debug / stabilization flags ──────────────────────────────────────
    p.add_argument("--debug-short-mode", action="store_true",
                   help=(
                       "Debug mode: limits audio to 5s chunks, bypasses cognition, "
                       "enables maximal trace logging. Use only for pipeline diagnosis."
                   ))
    p.add_argument("--trace", action="store_true",
                   help="Enable per-stage pipeline trace logging (timestamps on every API stage).")
    return p


args = _build_parser().parse_args()

# ─────────────────────────────────────────────────────────────────────────────
# Configuration precedence (deterministic — Issue 8)
# ─────────────────────────────────────────────────────────────────────────────
# Priority (highest first):
#   1. CLI args  (args.*)                  — always win
#   2. Environment variables (RUNTIME_*)   — override config-file defaults
#   3. RuntimeConfig.from_env()            — env-derived config object
#   4. _build_parser() defaults            — lowest priority
#
# Rule: args.* is always used for runtime constants below.
# ENV vars that DON'T have CLI equivalents are read from os.getenv() directly.
# RuntimeConfig is available for modules that prefer a typed config object.

if _CONFIG_MODULE_LOADED:
    _cfg = _RuntimeConfig.from_env()   # env overrides config defaults
else:
    _cfg = None   # fallback: use argparse values directly throughout

# ─────────────────────────────────────────────────────────────────────────────
# Startup validation — API key
# ─────────────────────────────────────────────────────────────────────────────
load_dotenv()

_api_key = os.getenv("OPENAI_API_KEY", "").strip()
if not _api_key or not _api_key.startswith("sk-"):
    print("ERROR: OPENAI_API_KEY not set or invalid.", file=sys.stderr)
    print("  Set it in a .env file or as an environment variable.", file=sys.stderr)
    sys.exit(1)

client = OpenAI(api_key=_api_key, timeout=20.0)

# H2A-5: static Provider Interface instances for LLM chat completions.
# `client` above remains in use ONLY for Whisper audio transcription
# (client.audio.transcriptions.create) — out of scope for the Provider
# Interface, whose models are chat-completion-shaped only. Whisper
# therefore always uses OpenAI regardless of the selected provider below
# (intentional scope boundary — see ROADMAP_V10.md H2A notes).
#
# Three statically-configured instances preserve the exact timeout value
# already used by each existing call site (Behavioral Compatibility):
#   - _provider_default:    matches this client's own default (20.0s) —
#                           used by every buffered call site that never
#                           overrode the timeout.
#   - _provider_candidates: generate_candidates() explicitly used 30.0s.
#   - _provider_streaming:  both streaming call sites explicitly used
#                           45.0s for stream establishment.
# No routing, no factory — plain static construction, per H2A-5 scope.
#
# H5-1: the concrete class constructed here is now selected per Runtime
# session via PHANTOM_PROVIDER -- an env var set only on this specific
# subprocess by runtime.cloud_run_shell's session_factory (mirrors the
# existing PHANTOM_AUDIO_FD / PHANTOM_EVENT_FD pattern), never a
# deployment-wide PROVIDER setting. This remains a construction-site
# branch only — no dispatch layer, no factory, no DI. When _cfg is
# unavailable (config module failed to import), the provider defaults to
# "openai", preserving pre-H5-1 standalone behavior exactly.
_requested_provider = os.getenv("PHANTOM_PROVIDER", "openai").strip().lower() or "openai"
_selected_provider = _requested_provider if _cfg is not None else "openai"

if _selected_provider == "gemini":
    _provider_default    = GeminiProvider(api_key=_cfg.gemini_api_key, model=_cfg.gemini_model, timeout=20.0)
    _provider_candidates = GeminiProvider(api_key=_cfg.gemini_api_key, model=_cfg.gemini_model, timeout=30.0)
    _provider_streaming  = GeminiProvider(api_key=_cfg.gemini_api_key, model=_cfg.gemini_model, timeout=45.0)
else:
    _provider_default    = OpenAIProvider(api_key=_api_key, model=args.gpt_model, timeout=20.0)
    _provider_candidates = OpenAIProvider(api_key=_api_key, model=args.gpt_model, timeout=30.0)
    _provider_streaming  = OpenAIProvider(api_key=_api_key, model=args.gpt_model, timeout=45.0)

# ─────────────────────────────────────────────────────────────────────────────
# Debug Flags
# ─────────────────────────────────────────────────────────────────────────────

_DEBUG = False

_DEBUG_MEMORY     = _DEBUG
_DEBUG_MEETING    = _DEBUG
_DEBUG_SUMMARY    = _DEBUG
_DEBUG_VAD        = _DEBUG
_DEBUG_AUDIO      = _DEBUG
_DEBUG_TRANSCRIPT = _DEBUG
_DEBUG_DECISION   = _DEBUG
_DEBUG_RUNTIME    = _DEBUG

# ─────────────────────────────────────────────────────────────────────────────
# Audio constants
# ─────────────────────────────────────────────────────────────────────────────
SAMPLE_RATE    = 16000
CHANNELS       = 1
DTYPE          = "int16"
BLOCK_SIZE     = 1600   # 100ms per block

MIN_SAMPLES    = int(SAMPLE_RATE * args.min_sec)
MAX_SAMPLES    = int(SAMPLE_RATE * args.max_sec)
SILENCE_BLOCKS = max(2, int((args.silence_sec * SAMPLE_RATE) / BLOCK_SIZE))
RMS_THRESHOLD  = args.threshold
AUDIO_QUEUE_MAXSIZE = 200

# Pre-buffer: ring buffer of recent audio blocks prepended to VAD chunks.
# Recovers the first ~500ms of speech onset that the RMS threshold would otherwise miss.
# 5 blocks × 100ms/block = 500ms lookback. Cost: trivial (5 int16 arrays in memory).
PRE_BUFFER_BLOCKS = 5

# Manual buffer hard limit — prevents unbounded accumulation
# If buffer exceeds this, oldest segments are truncated with a warning.
MAX_MANUAL_BUFFER_SEC = 30.0   # hard ceiling; operator warned at 20s


# ── Resolve input device (env var overrides CLI arg) ─────────────────────────
# Priority order:
#   1. INPUT_DEVICE_ID=<int>  — direct index, bypasses all name matching
#   2. audio.devices.resolve_device_id  — sole name resolver
_INPUT_DEVICE_NAME = os.getenv("INPUT_DEVICE", args.input_device).strip()
_INPUT_DEVICE_ID: Optional[int] = None

_raw_device_id = os.getenv("INPUT_DEVICE_ID", "").strip()
if _raw_device_id.isdigit():
    _INPUT_DEVICE_ID = int(_raw_device_id)
elif _INPUT_DEVICE_NAME:
    if _DEVICES_MODULE_LOADED:
        _INPUT_DEVICE_ID = resolve_device_id(_INPUT_DEVICE_NAME)

# Session ID (set by persistence module or locally here)
_SESSION_ID: str = ""

# Fix 6 — audio overflow counter and clean reset helper
_audio_overflow_count  = 0
_audio_overflow_lock   = threading.Lock()
# Feature 7: rolling overflow rate — ring buffer of overflow timestamps (last 100 events)
_overflow_window: deque = deque(maxlen=100)


def _reset_overflow_counter() -> int:
    """Atomically read and reset the overflow counter. Returns the value before reset."""
    global _audio_overflow_count
    with _audio_overflow_lock:
        count = _audio_overflow_count
        _audio_overflow_count = 0
    return count


def _overflow_rate_per_min() -> float:
    """Events per minute in the last 60 seconds. Thread-safe read of the ring buffer."""
    now    = time.monotonic()
    cutoff = now - 60.0
    with _audio_overflow_lock:
        recent = sum(1 for t in _overflow_window if t > cutoff)
    return recent   # count per last 60s ≈ per-minute rate


# ─────────────────────────────────────────────────────────────────────────────
# Feature 5 — Memory provider abstraction
# ─────────────────────────────────────────────────────────────────────────────
# Current: _StaticMemoryProvider reads profile sections at startup.
# Future:  _VectorMemoryProvider does similarity search at startup (still no runtime cost).
#          Replace _ACTIVE_MEMORY_PROVIDER with a vector implementation to activate.
#
# Interface contract:
#   retrieve(context: str) -> str
#     context  — recruiting context hint (e.g., "GRC interview at Verizon")
#     returns  — compact memory string to inject into system prompt
#     called   — ONCE at startup, result cached in SYSTEM_PROMPT
#     latency  — must complete in < 500ms (startup only, not realtime path)

class _StaticMemoryProvider:
    """
    Current memory provider — returns pre-loaded profile sections verbatim.
    Zero runtime latency (all data already in memory at construction time).

    To swap for a retrieval-based provider:
      1. Implement a class with retrieve(context: str) -> str
      2. Replace _ACTIVE_MEMORY_PROVIDER assignment below
      3. No other code changes needed
    """
    def __init__(self, career: str, topics: str, examples: str) -> None:
        parts = []
        if career:   parts.append(f"CAREER: {career}")
        if topics:   parts.append(f"TOPICS: {topics}")
        if examples: parts.append(f"EXAMPLES:\n{examples}")
        self._block = "\n".join(parts)

    def retrieve(self, context: str = "") -> str:  # noqa: ARG002
        return self._block

# ─────────────────────────────────────────────────────────────────────────────
# Model capability registry
# ─────────────────────────────────────────────────────────────────────────────
# Fix 8: Structured capability map replaces the flat frozenset.
# Adding a new model = adding one entry here. All consumers use MODEL_CAPS lookup.
MODEL_CAPS: dict[str, dict[str, bool]] = {
    "whisper-1": {
        "verbose_json":    True,   # returns result.language field
        "supports_prompt": True,   # accepts prompt= parameter for context seeding
    },
    "gpt-4o-transcribe": {
        "verbose_json":    False,
        "supports_prompt": False,  # may 400 on some API versions
    },
    "gpt-4o-mini-transcribe": {
        "verbose_json":    False,
        "supports_prompt": False,
    },
}

def _model_cap(model: str, cap: str, default: bool = False) -> bool:
    """Lookup a capability for a model. Returns default if model is unknown."""
    return MODEL_CAPS.get(model.lower(), {}).get(cap, default)

# ─────────────────────────────────────────────────────────────────────────────
# Queues
# ─────────────────────────────────────────────────────────────────────────────
audio_queue:      queue.Queue = queue.Queue(maxsize=AUDIO_QUEUE_MAXSIZE)
# maxsize=4: tolerates brief GPT latency spikes (extra 1-2 slots) while preserving
# latest-wins behaviour — _enqueue_latest still drains stale items before inserting.
# Rationale: at maxsize=2, a single 2s GPT delay caused the next 2 chunks to be
# dropped entirely. At maxsize=4 the worker can absorb a ~6s spike before dropping.
transcript_queue: queue.Queue = queue.Queue(maxsize=4)


def _enqueue_latest(audio: np.ndarray) -> None:
    """Latest-wins: drain stale pending chunks, then insert newest."""
    drained = 0
    while True:
        try:
            transcript_queue.get_nowait()
            transcript_queue.task_done()
            drained += 1
        except queue.Empty:
            break
    if drained:
        show_warn(f"Queue: dropped {drained} stale chunk(s) — keeping latest")
    try:
        transcript_queue.put_nowait(audio)
    except queue.Full:
        show_warn("Worker busy — chunk skipped (will resume next utterance)")


# ─────────────────────────────────────────────────────────────────────────────
# Shared state
# ─────────────────────────────────────────────────────────────────────────────
_log_lock   = threading.Lock()
_print_lock = threading.Lock()


class LogEntry(NamedTuple):
    text:    str
    lang:    str     # "english", "japanese", etc. — from Whisper/detector
    ts:      str     # HH:MM:SS
    speaker: str     # "recruiter" | "user" | "unknown"
    # Speaker is inferred at log-append time via _infer_speaker().
    # This is more accurate than lang-only splitting because in "en" mode
    # the user may speak English too — lang alone can't distinguish speakers.


transcript_log: deque = deque(maxlen=200)   # raised from 80: supports 30-60 min enterprise calls
_shutdown = threading.Event()

# ─────────────────────────────────────────────────────────────────────────────
# Typed event transport (H3 client-cloud integration)
# ─────────────────────────────────────────────────────────────────────────────
# When this process is spawned by runtime.cloud_run_shell, PHANTOM_EVENT_FD
# names a pipe fd the Shell relays verbatim to a connected client (see
# runtime.transport_gateway). Every outbound event is a single JSON line
# wrapped in a common versioned envelope so the transport layer never needs
# to understand event-specific fields — only the payload varies by type.
# Local/dev runs without PHANTOM_EVENT_FD set are unaffected: _emit_event
# becomes a no-op and console output (show_*) is unchanged either way.
_EVENT_SCHEMA_VERSION = 1
_event_lock = threading.Lock()
_event_file = None


def _init_event_transport() -> None:
    global _event_file
    fd_str = os.getenv("PHANTOM_EVENT_FD", "").strip()
    if not fd_str:
        return
    try:
        _event_file = os.fdopen(int(fd_str), "w", buffering=1, encoding="utf-8")
    except (ValueError, OSError) as e:
        print(f"[warn] PHANTOM_EVENT_FD invalid ({e}) — typed events disabled",
              file=sys.stderr)


def _emit_event(event_type: str, **payload) -> None:
    """Write one versioned JSON event line: {version, type, timestamp, payload}."""
    if _event_file is None:
        return
    envelope = {
        "version":   _EVENT_SCHEMA_VERSION,
        "type":      event_type,
        "timestamp": _datetime.datetime.now(_datetime.timezone.utc).isoformat(),
        "payload":   payload,
    }
    line = _json.dumps(envelope, ensure_ascii=False)
    try:
        with _event_lock:
            _event_file.write(line + "\n")
    except (BrokenPipeError, OSError):
        pass  # transport/client gone — console output keeps working regardless


_init_event_transport()

# ── Meeting Analysis: incremental cursor (Task 5) ─────────────────────────────
# Tracks how many transcript_log entries have been processed by generate_meeting_analysis().
# Updated after each analysis — next call covers only new entries since last run.
_meeting_cursor: int = 0
_meeting_cursor_lock: threading.Lock = threading.Lock()

# ── Meeting Analysis: token growth protection (Task 6) ───────────────────────
MAX_MEETING_ANALYSIS_CHARS: int = 12000


# ─────────────────────────────────────────────────────────────────────────────
# Pipeline trace logging (debug stabilization)
# ─────────────────────────────────────────────────────────────────────────────
# Active when: args.trace  OR  args.debug_short_mode  OR  RUNTIME_LOG_LEVEL=DEBUG
# Zero cost when disabled (single boolean check).

def _trace(stage: str, detail: str = "") -> None:
    """Emit a timestamped pipeline trace line to stdout (bypasses _print_lock)."""
    # args may not exist yet at import time — guard safely
    try:
        enabled = getattr(args, "trace", False) or getattr(args, "debug_short_mode", False)
    except Exception:
        return
    if not enabled:
        return
    ts = time.strftime("%H:%M:%S")
    suffix = f"  {detail}" if detail else ""
    print(f"[trace] {ts}  {stage}{suffix}", flush=True)

# ─────────────────────────────────────────────────────────────────────────────
# Runtime environment feature flags
# ─────────────────────────────────────────────────────────────────────────────
# Read once at startup. Controlled via environment variables in production.
# CLI flags take precedence over env vars for overlapping settings.
# [MODULE: runtime.observability]
def _env(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes")

_ENV = {
    "TRANSCRIPT_PERSIST":   _env("ENABLE_TRANSCRIPT_PERSIST"),
    "SUMMARY_DEBUG":        _env("ENABLE_SUMMARY_DEBUG"),
    "SPEAKER_TRACE":        _env("ENABLE_SPEAKER_TRACE"),
    "QUEUE_METRICS":        _env("ENABLE_QUEUE_METRICS"),
    "AGENT_MODE":           _env("ENABLE_AGENT_MODE"),       # alt to --agent flag
    "HALLUCINATION_GUARD":  _env("ENABLE_HALLUCINATION_GUARD", "1"),
    "PROFILE_GROUNDING":    _env("ENABLE_PROFILE_GROUNDING", "1"),
    "COGNITION":            _env("ENABLE_COGNITION"),         # v17: compression pipeline
    "DEBUG_AUDIO_SAVE":     _env("DEBUG_AUDIO_SAVE"),         # v22.2: save pre-Whisper WAV
    "MEMORY_V13":           _env("ENABLE_MEMORY_V13"),        # v1.3: structured memory layer
}

# Session output directory for transcript persistence
_SESSION_DIR = os.getenv("SESSION_OUTPUT_DIR", "").strip()
_SESSION_TRANSCRIPT_PATH: Optional[str] = None

def _init_session_dir() -> None:
    """
    [MODULE: transcript.persistence]
    v15: delegates to extracted module when available.
    """
    global _SESSION_TRANSCRIPT_PATH, _SESSION_ID
    if _PERSIST_MODULE_LOADED and _ENV["TRANSCRIPT_PERSIST"] and _SESSION_DIR:
        success = _ext_init_session(
            session_dir = _SESSION_DIR,
            info_fn     = show_info,
            warn_fn     = show_warn,
        )
        if success:
            _SESSION_ID = _ext_get_session_id()
        return
    # Legacy inline fallback
    if not (_ENV["TRANSCRIPT_PERSIST"] and _SESSION_DIR):
        return
    try:
        os.makedirs(_SESSION_DIR, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        _SESSION_ID = f"session_{stamp}"
        _SESSION_TRANSCRIPT_PATH = os.path.join(_SESSION_DIR, f"transcript_{stamp}.jsonl")
        show_info(f"[persist] transcript → {_SESSION_TRANSCRIPT_PATH}")
    except Exception as e:
        show_warn(f"[persist] Could not create session dir: {e}")


def _persist_entry(entry, state: str = "unknown", latency_ms: float = 0.0) -> None:
    """
    [MODULE: transcript.persistence]
    v15: replay-safe JSONL with state, latency_ms, session_id, type fields.
    Delegates to extracted module when available.
    """
    if _PERSIST_MODULE_LOADED:
        _ext_persist_entry(entry=entry, state=state, latency_ms=latency_ms, warn_fn=show_warn)
        return
    # Legacy inline fallback — now includes replay metadata
    if not _SESSION_TRANSCRIPT_PATH:
        return
    try:
        row = {
            "type":       "utterance",
            "session_id": _SESSION_ID,
            "ts":         entry.ts,
            "speaker":    entry.speaker,
            "lang":       entry.lang,
            "text":       entry.text,
            "state":      state,
            "latency_ms": round(latency_ms, 1),
        }
        with open(_SESSION_TRANSCRIPT_PATH, "a", encoding="utf-8") as f:
            f.write(_json.dumps(row, ensure_ascii=False) + "\n")
    except Exception as e:
        show_warn(f"[persist] write error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# VAD buffering — state ownership delegated to audio.vad_buffering
# ─────────────────────────────────────────────────────────────────────────────
# [MODULE: audio.vad_buffering]
# _vad_buf is instantiated below, after show_info / show_warn / _print and
# ANSI codes are defined.  All mutable buffering state lives inside _vad_buf.


def _save_debug_audio(audio: np.ndarray) -> None:
    """Save pre-Whisper audio to ./debug_audio/ as WAV when DEBUG_AUDIO_SAVE=1.

    Never raises — failures are reported via show_warn() only so the runtime
    continues unaffected. Called only when _ENV["DEBUG_AUDIO_SAVE"] is True.
    """
    try:
        debug_audio(
            f"dtype={audio.dtype}"
            f" min={audio.min():.4f} max={audio.max():.4f}"
        )
        if audio.dtype in (np.float32, np.float64):
            pcm = np.clip(audio, -1.0, 1.0)
            pcm = (pcm * 32767).astype(np.int16)
        else:
            pcm = audio.astype(np.int16)
        debug_dir = os.path.join(os.getcwd(), "debug_audio")
        os.makedirs(debug_dir, exist_ok=True)
        filename = time.strftime("debug_audio_%Y%m%d_%H%M%S.wav")
        filepath = os.path.join(debug_dir, filename)
        with wave.open(filepath, "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
        debug_audio(f"saved: ./debug_audio/{filename}")
    except Exception as e:
        show_warn(f"[audio-debug] save failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# TTS interrupt event
# ─────────────────────────────────────────────────────────────────────────────
# Set when operator speech starts during TTS playback.
# reply_worker polls this during the TTS wait loop to interrupt immediately.
_tts_interrupt: threading.Event = threading.Event()


# ─────────────────────────────────────────────────────────────────────────────
# Speaker inference state (anti-oscillation)
# ─────────────────────────────────────────────────────────────────────────────
# [MODULE: conversation.speaker_inference]
_speaker_lock:           threading.Lock = threading.Lock()
_last_inferred_speaker:  str            = "recruiter"   # start assuming recruiter
_speaker_consecutive_lang: str          = ""            # last N utterances' lang
_speaker_flip_threshold: int            = 2             # consecutive same-lang to flip

# ─────────────────────────────────────────────────────────────────────────────
# Conversation state machine
# ─────────────────────────────────────────────────────────────────────────────
# States track the current phase of the interview conversation.
# Used by agent mode to decide when to generate autonomous responses.
# Observer mode ignores state (backward-compatible — all existing paths unaffected).
#
# State transitions:
#   IDLE            → RECRUITER_SPEAKING   (speech detected, lang=recruiter)
#   IDLE            → USER_SPEAKING        (speech detected, lang=user)
#   RECRUITER_SPEAKING → WAITING_FOR_REPLY (silence after recruiter speech + question detected)
#   RECRUITER_SPEAKING → IDLE              (silence after recruiter speech, no question)
#   WAITING_FOR_REPLY  → GENERATING        (GPT call started)
#   GENERATING         → SPEAKING          (TTS started, if TTS enabled)
#   GENERATING         → IDLE              (response displayed, no TTS)
#   SPEAKING           → IDLE              (TTS finished)
#   USER_SPEAKING      → IDLE              (user finished speaking)

import enum

if not _STATE_MACHINE_MODULE_LOADED:
    class ConversationState(enum.Enum):
        IDLE               = "idle"
        RECRUITER_SPEAKING = "recruiter_speaking"
        USER_SPEAKING      = "user_speaking"
        WAITING_FOR_REPLY  = "waiting_for_reply"
        GENERATING         = "generating"
        SPEAKING           = "speaking"


_state_lock  = threading.Lock()
_conv_state  = ConversationState.IDLE


def _set_state(new_state: ConversationState) -> None:
    """Thread-safe state transition with debug visibility."""
    global _conv_state
    with _state_lock:
        old = _conv_state
        _conv_state = new_state
    if old != new_state:
        show_info(f"[state] {old.value} → {new_state.value}")
        _emit_event("status", state=new_state.value, previous=old.value)


def _get_state() -> ConversationState:
    with _state_lock:
        return _conv_state


# ─────────────────────────────────────────────────────────────────────────────
# Runtime mode  (v22)
# ─────────────────────────────────────────────────────────────────────────────

if not _STATE_MACHINE_MODULE_LOADED:
    class RuntimeMode(enum.Enum):
        INTERVIEW = "interview"   # default: real-time interview assistance
        MEETING   = "meeting"     # meeting analysis: summary + risks + Q&A + actions
        SUMMARY   = "summary"     # reserved: transcript-only summary mode


_runtime_mode_lock: threading.Lock = threading.Lock()
_runtime_mode:      RuntimeMode    = RuntimeMode.INTERVIEW


def _get_runtime_mode() -> RuntimeMode:
    with _runtime_mode_lock:
        return _runtime_mode


def _set_runtime_mode(mode: RuntimeMode) -> None:
    global _runtime_mode
    with _runtime_mode_lock:
        _runtime_mode = mode


# ─────────────────────────────────────────────────────────────────────────────
# ANSI colors
# ─────────────────────────────────────────────────────────────────────────────
if args.no_color or not sys.stdout.isatty():
    RESET = BOLD = DIM = CYAN = YELLOW = GREEN = GRAY = RED = MAGENTA = WHITE = ""
else:
    RESET   = "\033[0m"
    BOLD    = "\033[1m"
    DIM     = "\033[2m"
    CYAN    = "\033[96m"
    YELLOW  = "\033[93m"
    GREEN   = "\033[92m"
    GRAY    = "\033[90m"
    RED     = "\033[91m"
    MAGENTA = "\033[95m"
    WHITE   = "\033[97m"

SEP = GRAY + "─" * 62 + RESET

# ─────────────────────────────────────────────────────────────────────────────
# Thread-safe print helpers
# ─────────────────────────────────────────────────────────────────────────────
def _print(*parts: str, end: str = "\n") -> None:
    with _print_lock:
        print("".join(parts), end=end, flush=True)

def show_sep()                        : _print(SEP)
def show_heard(text: str, lang: str, ts: str):
    _print(f"\n{CYAN}◎ {text}{RESET}  {GRAY}[{lang}] {ts}{RESET}")
def show_jp(text: str)                : _print(f"{YELLOW}[JP]{RESET} {text}")
def show_en(text: str)                : _print(f"\n{GREEN}{BOLD}[EN]{RESET} {BOLD}{text}{RESET}\n")
def show_read(text: str)              : _print(f"{MAGENTA}[読]{RESET} {text}")
def show_info(text: str)              : _print(f"{GRAY}· {text}{RESET}")
def show_warn(text: str)              : _print(f"{YELLOW}⚠  {text}{RESET}")
def show_err(label: str, err)         :
    _print(f"{RED}[{label}]{RESET} {err}")
    _emit_event("error", label=label, message=str(err))
def show_delay_en(phrase: str)        : _print(f"\n{WHITE}{BOLD}[DLY]{RESET} {WHITE}{phrase}{RESET}\n")
def show_delay_jp(phrase: str)        : _print(f"\n{YELLOW}{BOLD}[考]{RESET}  {YELLOW}{phrase}{RESET}\n")
def show_agent_reply(text: str)       : _print(f"\n{CYAN}{BOLD}[→]{RESET} {BOLD}{text}{RESET}\n")
def show_hold(phrase: str)            : _print(f"\n{WHITE}{BOLD}[HOLD]{RESET} {WHITE}{phrase}{RESET}\n")
def show_latency(stt_ms: float, gpt_ms: float):
    total = stt_ms + gpt_ms
    _print(f"{GRAY}· STT={stt_ms:.0f}ms  GPT={gpt_ms:.0f}ms  TOTAL={total:.0f}ms{RESET}")
    _emit_event("latency", stt_ms=stt_ms, gpt_ms=gpt_ms, total_ms=total)

# ─────────────────────────────────────────────────────────────────────────────
# Debug helpers — controlled by _DEBUG_* flags above
# ─────────────────────────────────────────────────────────────────────────────
def debug_memory(msg: str) -> None:
    if _DEBUG_MEMORY:
        show_info(f"[memory-debug] {msg}")

def debug_meeting(msg: str) -> None:
    if _DEBUG_MEETING:
        show_info(f"[meeting-debug] {msg}")

def debug_summary(msg: str) -> None:
    if _DEBUG_SUMMARY:
        show_info(f"[summary-debug] {msg}")

def debug_vad(msg: str) -> None:
    if _DEBUG_VAD:
        show_info(f"[vad-debug] {msg}")

def debug_audio(msg: str) -> None:
    if _DEBUG_AUDIO:
        show_info(f"[audio-debug] {msg}")

def debug_transcript(msg: str) -> None:
    if _DEBUG_TRANSCRIPT:
        show_info(f"[transcript-debug] {msg}")

def debug_decision(msg: str) -> None:
    if _DEBUG_DECISION:
        show_info(f"[decision-debug] {msg}")

def debug_runtime(msg: str) -> None:
    if _DEBUG_RUNTIME:
        show_info(f"[runtime-debug] {msg}")

# ─────────────────────────────────────────────────────────────────────────────
# VAD Buffer instance
# ─────────────────────────────────────────────────────────────────────────────
# [MODULE: audio.vad_buffering]
# Single owner of all VAD buffering state.  Instantiated here so that
# show_info, show_warn, _print, and ANSI codes are already defined.
_vad_buf: _VADBuffer = _VADBuffer(
    sample_rate           = SAMPLE_RATE,
    pre_buffer_blocks     = PRE_BUFFER_BLOCKS,
    min_samples           = MIN_SAMPLES,
    max_samples           = MAX_SAMPLES,
    silence_blocks        = SILENCE_BLOCKS,
    max_manual_buffer_sec = MAX_MANUAL_BUFFER_SEC,
    tail_padding_sec      = 0.80,
    info_fn               = show_info,
    warn_fn               = show_warn,
    print_fn              = _print,
    green                 = GREEN,
    bold                  = BOLD,
    gray                  = GRAY,
    reset                 = RESET,
)

# ─────────────────────────────────────────────────────────────────────────────
# TTS provider abstraction
# ─────────────────────────────────────────────────────────────────────────────
# Design:
#   _TTSProvider — interface (duck-typed, no ABC required for simplicity)
#     speak(text: str) → None   — start speaking asynchronously
#     stop()           → None   — interrupt current speech
#     is_speaking()    → bool   — True while TTS is active
#
#   _NullTTSProvider  — screen-only (default, zero deps)
#   _SayTTSProvider   — macOS `say` command (subprocess, zero install)
#   _Pyttsx3Provider  — cross-platform (pip install pyttsx3)
#
# Swap the active provider by setting _ACTIVE_TTS at startup.
# No other code changes needed — all consumers call _ACTIVE_TTS.speak(text).

import subprocess

class _NullTTSProvider:
    """Screen-only — no audio output. Default provider."""
    def speak(self, text: str) -> None:
        pass   # text already displayed on screen

    def stop(self) -> None:
        pass

    def is_speaking(self) -> bool:
        return False


class _SayTTSProvider:
    """
    macOS `say` command — zero dependencies, ships with every Mac.
    Runs in a background thread to avoid blocking reply_worker.
    """
    def __init__(self, voice: str = "Samantha", rate: int = 200) -> None:
        self._voice   = voice
        self._rate    = rate
        self._proc: Optional[subprocess.Popen] = None
        self._lock    = threading.Lock()

    def speak(self, text: str) -> None:
        self.stop()   # interrupt any ongoing speech
        with self._lock:
            try:
                self._proc = subprocess.Popen(
                    ["say", "-v", self._voice, "-r", str(self._rate), text],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                show_warn("TTS: `say` not found — install macOS or switch --tts provider")

    def stop(self) -> None:
        with self._lock:
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()
                self._proc = None

    def is_speaking(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None


class _Pyttsx3Provider:
    """
    Cross-platform TTS via pyttsx3 (pip install pyttsx3).
    Runs in a dedicated daemon thread to avoid blocking reply_worker.
    """
    def __init__(self, rate: int = 175) -> None:
        self._rate    = rate
        self._speaking = False
        self._lock    = threading.Lock()
        self._engine  = None
        self._thread: Optional[threading.Thread] = None
        try:
            import pyttsx3  # type: ignore[import]
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", rate)
        except Exception as e:
            show_warn(f"TTS: pyttsx3 init failed ({e}) — falling back to null provider")
            self._engine = None

    def speak(self, text: str) -> None:
        if self._engine is None:
            return
        self.stop()
        def _run() -> None:
            with self._lock:
                self._speaking = True
            try:
                self._engine.say(text)
                self._engine.runAndWait()
            except Exception:
                pass
            finally:
                with self._lock:
                    self._speaking = False
        self._thread = threading.Thread(target=_run, daemon=True, name="tts")
        self._thread.start()

    def stop(self) -> None:
        if self._engine is None:
            return
        try:
            self._engine.stop()
        except Exception:
            pass
        with self._lock:
            self._speaking = False

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

# ─────────────────────────────────────────────────────────────────────────────
# Profile system
# ─────────────────────────────────────────────────────────────────────────────
# Profiles are plain Markdown files with ## section headers.
# Parsed once at startup into dict[str, str] — zero runtime overhead.

_KNOWN_SECTIONS = frozenset({
    "identity", "positioning", "recruiter_context", "communication_style",
    "technical_focus", "forbidden_phrases", "language_behavior",
    "summary_tone", "delay_phrases_en", "delay_phrases_jp",
    # Fix 6: memory sections
    "career_summary", "topic_memory", "response_examples",
})

_VALID_INTERVIEW_LANGS = {"en", "ja", "mixed"}
_VALID_ENGLISH_LEVELS  = {"beginner", "simple", "natural", "fluent"}


def _parse_profile(text: str) -> dict[str, str]:
    """Parse ## section headers into a dict. Content stripped; internal newlines kept."""
    sections: dict[str, str] = {}
    current_key: Optional[str] = None
    current_lines: list[str]   = []

    for line in text.splitlines():
        if line.startswith("## "):
            if current_key is not None:
                sections[current_key] = "\n".join(current_lines).strip()
            current_key   = line[3:].strip().lower().replace(" ", "_")
            current_lines = []
        elif current_key is not None:
            current_lines.append(line)

    if current_key is not None:
        sections[current_key] = "\n".join(current_lines).strip()

    return sections


def _validate_profile(name: str, sections: dict[str, str]) -> list[str]:
    """
    Fix 9: Validate a loaded profile. Returns a list of warning strings.
    Does not abort — warnings are printed at startup, runtime continues.
    """
    warnings: list[str] = []

    # Check language_behavior values if present
    lb = sections.get("language_behavior", "")
    for line in lb.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower().replace("-", "_")
            val = val.strip().lower()
            if key == "interview_lang" and val not in _VALID_INTERVIEW_LANGS:
                warnings.append(
                    f"  profile '{name}': language_behavior.interview_lang='{val}' "
                    f"is invalid. Valid: {sorted(_VALID_INTERVIEW_LANGS)}"
                )
            if key == "english_level" and val not in _VALID_ENGLISH_LEVELS:
                warnings.append(
                    f"  profile '{name}': language_behavior.english_level='{val}' "
                    f"is invalid. Valid: {sorted(_VALID_ENGLISH_LEVELS)}"
                )

    # Warn on unrecognised sections (forward-compatibility notice)
    for key in sections:
        if key not in _KNOWN_SECTIONS:
            warnings.append(
                f"  profile '{name}': unknown section '## {key}' — ignored"
            )

    return warnings


def _load_profile(name: str) -> tuple[dict[str, str], str]:
    """
    Load and parse a profile by name (without .md extension).

    Resolution order:
      1. profiles/<name>.md  (next to main.py)
      2. profiles/default.md
      3. Hardcoded minimal fallback (never crashes startup)
    """
    safe_name = os.path.basename(name).replace("..", "").strip()
    path = os.path.join(PROFILES_DIR, f"{safe_name}.md")

    if os.path.isfile(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = f.read()
            sections = _parse_profile(raw)
            # Run validation, print any warnings
            for w in _validate_profile(safe_name, sections):
                print(f"[WARN] {w}", file=sys.stderr)
            return sections, safe_name
        except Exception as e:
            print(f"[WARN] Could not read profile '{safe_name}': {e}", file=sys.stderr)

    if safe_name != "default":
        print(
            f"[WARN] Profile '{safe_name}' not found in {PROFILES_DIR}/. "
            "Falling back to 'default'.",
            file=sys.stderr,
        )
        default_path = os.path.join(PROFILES_DIR, "default.md")
        if os.path.isfile(default_path):
            try:
                with open(default_path, "r", encoding="utf-8") as f:
                    raw = f.read()
                return _parse_profile(raw), "default"
            except Exception:
                pass

    # Hardcoded minimal fallback
    fallback: dict[str, str] = {
        "identity":           "You are a real-time interview assistant for a Japanese professional.",
        "positioning":        "Support bilingual communication during a live recruiter phone interview.",
        "recruiter_context":  "General recruiter phone screen.",
        "communication_style":"Keep replies short, natural, and spoken.",
        "technical_focus":    "No specific technical domain.",
        "forbidden_phrases":  "Certainly / Of course / Absolutely / Great question / As an AI",
        "summary_tone":       "Summarize in plain Japanese. Focus on what was asked.",
    }
    return fallback, "default(built-in)"


def _extract_profile_overrides(sections: dict[str, str]) -> dict[str, str]:
    """Parse language_behavior section for interview_lang / english_level overrides.

    language_behavior may be a dict (profiles.loader/schema normalisation) or a
    legacy str (raw ## section text) — both shapes are supported here.
    """
    lb = sections.get("language_behavior", "")
    if isinstance(lb, dict):
        return {k: v for k, v in lb.items() if k in ("interview_lang", "english_level")}

    overrides: dict[str, str] = {}
    for line in lb.splitlines():
        line = line.strip()
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip().lower().replace("-", "_")
            val = val.strip().lower()
            if key in ("interview_lang", "english_level"):
                overrides[key] = val
    return overrides


def _apply_profile_overrides(
    sections: dict[str, str],
    current_lang:  str,
    current_level: str,
) -> tuple[str, str]:
    """
    Profile language_behavior wins over argparse defaults but loses to explicit CLI flags.
    Priority: explicit CLI arg > profile > argparse default.
    """
    overrides = _extract_profile_overrides(sections)
    lang  = current_lang
    level = current_level
    if "interview_lang" in overrides and current_lang == "mixed":
        lang = overrides["interview_lang"]
    if "english_level" in overrides and current_level == "natural":
        level = overrides["english_level"]
    return lang, level


def _build_profile_banner(name: str, sections: dict[str, str]) -> str:
    """Compact startup banner. Printed once — no runtime cost."""
    identity  = sections.get("identity",          "").split("\n")[0][:80]
    ctx       = sections.get("recruiter_context",  "").split("\n")[0][:80]
    overrides = _extract_profile_overrides(sections)
    lines = [f"  identity: {identity}", f"  context:  {ctx}"]
    if "interview_lang" in overrides:
        lines.append(f"  lang_override: {overrides['interview_lang']}")
    if "english_level" in overrides:
        lines.append(f"  level_override: {overrides['english_level']}")
    extra = []
    if "delay_phrases_en"  in sections: extra.append("custom EN phrases")
    if "delay_phrases_jp"  in sections: extra.append("custom JP phrases")
    if "career_summary"    in sections: extra.append("career_summary")
    if "topic_memory"      in sections: extra.append("topic_memory")
    if "response_examples" in sections: extra.append("response_examples")
    if extra:
        lines.append(f"  extras: {', '.join(extra)}")
    return "\n".join(lines)


# ── Load profile FIRST — everything below may depend on it ───────────────────
# v17: prefer profiles.loader (supports .json and .md) when available
try:
    from profiles.loader import load_profile as _ext_load_profile
    _PROFILES_LOADER_LOADED = True
except ImportError:
    _PROFILES_LOADER_LOADED = False

if _PROFILES_LOADER_LOADED:
    _ACTIVE_PROFILE, _ACTIVE_PROFILE_NAME = _ext_load_profile(
        name         = args.profile,
        profiles_dir = PROFILES_DIR,
        warn_fn      = lambda s: print(f"[warn] {s}", file=sys.stderr),
        info_fn      = lambda s: print(f"[info] {s}", file=sys.stderr),
    )
else:
    _ACTIVE_PROFILE, _ACTIVE_PROFILE_NAME = _load_profile(args.profile)
_PROFILE_OVERRIDES = _extract_profile_overrides(_ACTIVE_PROFILE)

# ── Instantiate memory provider from profile sections ─────────────────────────
# Truncate response_examples to 400 chars to cap token overhead.
_mem_career   = _ACTIVE_PROFILE.get("career_summary",    "").strip()
_mem_topics   = _ACTIVE_PROFILE.get("topic_memory",      "").strip()
_mem_examples = _ACTIVE_PROFILE.get("response_examples", "").strip()[:400]

_ACTIVE_MEMORY_PROVIDER = _StaticMemoryProvider(
    career=_mem_career, topics=_mem_topics, examples=_mem_examples
)
# To use retrieval-based memory in the future:
#   _ACTIVE_MEMORY_PROVIDER = _VectorMemoryProvider.from_profile(_ACTIVE_PROFILE)


# ─────────────────────────────────────────────────────────────────────────────
# Feature 6 — Structured IntentResult
# ─────────────────────────────────────────────────────────────────────────────

class IntentResult(NamedTuple):
    """
    Structured fast-path response object.

    Fields:
      intent_key  — matched intent category ("self_intro", "motivation", etc.)
      response    — [JP] suggestion string to display
      confidence  — match confidence: 1.0 = keyword match, <1.0 = future fuzzy
      source      — "cache" | "profile_override" | "gpt_refined" (last = future)

    reply_worker uses .response for display and .source for future routing.
    When source == "gpt_refined", a second GPT call can be made to improve
    the cached response with live context — not yet implemented.
    """
    intent_key: str
    response:   str
    confidence: float
    source:     str


def _seed_intent_cache_from_profile(profile: dict) -> None:
    """
    Optional: parse profile's response_examples section for intent overrides.
    Format (in the profile's ## response_examples section):
      intent:self_intro: セキュリティとGRCの経験を3年、具体的に話そう
      intent:motivation: Verizonの規模と規制環境に関心があると伝えよう

    Lines NOT starting with "intent:" are treated as free-form examples (ignored here).
    This lets the profile author pre-seed the fast-path cache without editing main.py.
    """
    raw = profile.get("response_examples", "")
    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("intent:"):
            rest = line[7:]   # strip "intent:" prefix
            if ":" in rest:
                key, _, val = rest.partition(":")
                key = key.strip()
                val = val.strip()
                if key and val and key in _INTENT_PATTERNS:
                    _INTENT_CACHE[key] = val


# Seed the intent cache from the active profile
_seed_intent_cache_from_profile(_ACTIVE_PROFILE)

# ── Instantiate TTS provider ──────────────────────────────────────────────────
def _build_tts_provider():
    """Build the TTS provider selected by --tts CLI arg."""
    if args.tts == "say":
        return _SayTTSProvider()
    elif args.tts == "pyttsx3":
        return _Pyttsx3Provider()
    else:
        return _NullTTSProvider()

_ACTIVE_TTS = _build_tts_provider()
debug_runtime(f"ACTIVE_TTS={type(_ACTIVE_TTS).__name__}")

# ─────────────────────────────────────────────────────────────────────────────
# Delay phrases — defined AFTER profile load (Fix 1)
# ─────────────────────────────────────────────────────────────────────────────
_DELAY_EN_DEFAULT: list[str] = [
    "Let me think about that for a second",
    "That's a good point — give me just a moment",
    "One moment",
    "Let me just gather my thoughts on that",
    "Could you say a bit more about what you mean",
    "Sure, just let me think through that",
    "Right, let me make sure I answer that properly",
    "Yeah, give me just a second on that one",
    "I want to make sure I answer that well",
    "Let me just think on that briefly",
]

_DELAY_JP_DEFAULT: list[str] = [
    "少し考えさせてください",
    "少々お待ちください",
    "今整理しています",
    "ちょっと考えます",
    "えーと、少し考えます",
    "今まとめています",
    "確認させてください",
]

# ── Clarification / repeat-request phrases (v17.2) ───────────────────────────
_CLARIFY_DEFAULT: list[str] = [
    "Sorry, could you repeat the question?",
    "I want to make sure I understood correctly — could you say that once more?",
    "Sorry, the audio broke up a little bit. Could you repeat that?",
    "Could you clarify which part you'd like me to focus on?",
    "Just to make sure I heard that right — could you say it again?",
]


def show_clarify(phrase: str) -> None:
    _print(f"\n{WHITE}{BOLD}[REPEAT]{RESET} {WHITE}{phrase}{RESET}\n")


def show_random_clarify() -> None:
    show_clarify(random.choice(_CLARIFY_DEFAULT))


def _parse_phrase_list(raw) -> list[str]:
    """Accepts either a legacy str (raw ## section text) or a normalized list
    (profiles.loader/schema normalisation) — both shapes are supported here."""
    if isinstance(raw, list):
        return [str(item).strip() for item in raw
                if str(item).strip() and not str(item).strip().startswith("#")]
    return [ln.strip() for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]


# Apply profile phrase overrides if present
_profile_delay_en = _parse_phrase_list(_ACTIVE_PROFILE.get("delay_phrases_en", ""))
_profile_delay_jp = _parse_phrase_list(_ACTIVE_PROFILE.get("delay_phrases_jp", ""))

_DELAY_EN:       list[str] = _profile_delay_en or _DELAY_EN_DEFAULT
_DELAY_JP:       list[str] = _profile_delay_jp or _DELAY_JP_DEFAULT
_DELAY_EN_SLOTS: list[str] = _DELAY_EN[:5]


def show_random_delay_en() -> None:
    show_delay_en(random.choice(_DELAY_EN))


def show_random_delay_jp() -> None:
    show_delay_jp(random.choice(_DELAY_JP))


def show_delay_slot(n: int) -> None:
    idx = max(0, min(n - 1, len(_DELAY_EN_SLOTS) - 1))
    show_delay_en(_DELAY_EN_SLOTS[idx])


# ─────────────────────────────────────────────────────────────────────────────
# Language detection helpers
# ─────────────────────────────────────────────────────────────────────────────
_JP_LANGS = {"japanese", "ja", "jpn"}


def is_japanese_lang(lang: str) -> bool:
    lc = lang.lower()
    return lc in _JP_LANGS or lc.startswith("ja")


def _infer_speaker(lang: str, text: str = "") -> str:
    """
    [MODULE: conversation.speaker_inference]
    v15: delegates to extracted conversation.speaker_inference module when available.
    Falls back to legacy inline logic if module import failed.
    """
    debug_runtime(f"entering _infer_speaker  lang={lang!r}  SPEAKER_MODULE_LOADED={_SPEAKER_MODULE_LOADED}")
    if _SPEAKER_MODULE_LOADED:
        debug_runtime("_infer_speaker: taking external module branch")
        trace_fn = (lambda msg: show_info(msg)) if _ENV.get("SPEAKER_TRACE") else None
        debug_runtime("_infer_speaker: before _get_state for ext module")
        _conv_state_val = _get_state().value
        debug_runtime(f"_infer_speaker: after _get_state  conv_state={_conv_state_val!r}")
        debug_runtime("_infer_speaker: before _ext_infer_speaker call")
        _result = _ext_infer_speaker(
            lang           = lang,
            text           = text,
            conv_state     = _conv_state_val,
            effective_lang = _effective_lang,
            trace_fn       = trace_fn,
        )
        debug_runtime(f"_infer_speaker: after _ext_infer_speaker  result={_result!r}")
        debug_runtime(f"leaving _infer_speaker  result={_result!r}")
        return _result
    # ── Legacy fallback (preserved from v14) ─────────────────────────────────
    debug_runtime("_infer_speaker: taking legacy fallback branch")
    global _last_inferred_speaker, _speaker_consecutive_lang
    is_jp = is_japanese_lang(lang)
    words = text.split()
    debug_runtime("_infer_speaker: before _get_state (legacy)")
    current_state = _get_state()
    debug_runtime(f"_infer_speaker: after _get_state  current_state={current_state!r}")
    if current_state in (ConversationState.GENERATING, ConversationState.SPEAKING):
        debug_runtime(f"_infer_speaker: early return 'user' (state={current_state!r})")
        debug_runtime("leaving _infer_speaker  result='user'")
        return "user"
    if text.strip().lower().rstrip(".,!?") in _FILLER_PATTERNS:
        debug_runtime(f"_infer_speaker: early return filler  last={_last_inferred_speaker!r}")
        debug_runtime(f"leaving _infer_speaker  result={_last_inferred_speaker!r}")
        return _last_inferred_speaker
    if len(words) <= 2 and len(text.strip()) < 8:
        debug_runtime(f"_infer_speaker: early return short-text  last={_last_inferred_speaker!r}")
        debug_runtime(f"leaving _infer_speaker  result={_last_inferred_speaker!r}")
        return _last_inferred_speaker
    candidate = "user" if is_jp else "recruiter"
    debug_runtime(f"_infer_speaker: before _speaker_lock acquire  candidate={candidate!r}")
    with _speaker_lock:
        debug_runtime("_infer_speaker: inside _speaker_lock")
        if candidate == _last_inferred_speaker:
            _speaker_consecutive_lang = lang
            result = candidate
        else:
            if lang == _speaker_consecutive_lang:
                result = _last_inferred_speaker
            else:
                _speaker_consecutive_lang = lang
                result = candidate
        _last_inferred_speaker = result
    debug_runtime(f"_infer_speaker: after _speaker_lock released  result={result!r}")
    if _ENV["SPEAKER_TRACE"]:
        debug_runtime("_infer_speaker: before show_info (SPEAKER_TRACE)")
        show_info(f"[speaker] {lang[:2]} → {result}  (text={text[:30]!r})")
        debug_runtime("_infer_speaker: after show_info (SPEAKER_TRACE)")
    debug_runtime(f"leaving _infer_speaker  result={result!r}")
    return result


def detect_language_from_text(text: str) -> str:
    """
    Fix 5: Improved mixed JP/EN language detection.

    Strategy (no deps, <0.1ms):
      1. Any hiragana, katakana, or JP punctuation → "japanese" immediately.
         These codepoints are exclusive to Japanese text.
      2. Kanji present with adaptive threshold:
           len ≤ 25 chars: any kanji → "japanese"  (short fragments are almost always JP)
           len > 25 chars: kanji/total ≥ 5%  → "japanese"
         Old threshold was 10%, which missed "compliance経験 risk management" (7.1%).
         5% catches sparse kanji in mixed EN/JP sentences without false-positives
         on pure English text (which has 0% kanji).
      3. Otherwise → "english"

    Tested on 19 interview cases including mixed JP/EN with romaji-heavy text.
    """
    if not text:
        return "unknown"

    # Step 1: unambiguous JP codepoints (single-pass, early exit)
    for ch in text:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x309F: return "japanese"   # hiragana
        if 0x30A0 <= cp <= 0x30FF: return "japanese"   # katakana
        if 0xFF65 <= cp <= 0xFF9F: return "japanese"   # halfwidth kana
        if 0x3000 <= cp <= 0x303F: return "japanese"   # JP punctuation 。、「」…

    # Step 2: kanji with adaptive threshold
    kanji = sum(1 for ch in text
                if 0x3400 <= ord(ch) <= 0x4DBF   # CJK Extension A
                or 0x4E00 <= ord(ch) <= 0x9FFF)  # CJK Unified (main block)

    if kanji == 0:
        return "english"

    if len(text) <= 25:
        return "japanese"   # any kanji in a short string → JP

    if kanji / len(text) >= 0.05:
        return "japanese"

    return "english"


# ─────────────────────────────────────────────────────────────────────────────
# System prompt builder
# ─────────────────────────────────────────────────────────────────────────────

def _load_file(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


_ENGLISH_LEVEL_INSTRUCTIONS: dict[str, str] = {
    "beginner": "EN LEVEL: max 8 words. Basic words only. No contractions or idioms.",
    "simple":   "EN LEVEL: max 10 words. Simple casual spoken English. Contractions OK.",
    "natural":  "EN LEVEL: max 12 words. Conversational, relaxed job-interview English.",
    "fluent":   "EN LEVEL: max 15 words. Professional but natural. Industry terms OK.",
}


def _build_system_prompt(
    interview_lang: str,
    english_level:  str,
    pronunciation:  bool,
    mode:           str,
    profile:        dict,
) -> str:
    """
    Build SYSTEM_PROMPT from interview_lang + english_level + pronunciation + profile.
    Called once at startup — zero runtime cost.

    Fix 7: Prompt compressed. Removed duplicate rule statements, verbose examples,
    and the standalone _PRONUNCIATION_INSTRUCTION block. All profile memory sections
    (career_summary, topic_memory, response_examples) are injected here.

    Output logic (CONDITIONAL on detected input language):
      JP input → 1 line: [JP] only
      EN input → 2 lines: [JP] + [EN]  (+[READ] if pronunciation)
    """
    # Legacy full mode
    if mode == "full":
        custom = _load_file(os.path.join(BASE_DIR, "prompts", "phantom_core.txt"))
        if custom:
            return custom

    level_note = _ENGLISH_LEVEL_INSTRUCTIONS[english_level]
    read_rule  = "\n      [READ] Katakana reading of [EN]" if pronunciation else ""

    # ── Profile preamble (identity + positioning + context) ──────────────
    parts = []
    for key, prefix in (("identity", ""), ("positioning", "Role: "), ("recruiter_context", "Context: ")):
        val = profile.get(key, "").strip()
        if val:
            parts.append(f"{prefix}{val}")
    preamble = "\n".join(parts) or "You are a real-time interview assistant."

    # ── Profile context rules (communication_style + technical_focus) ────
    ctx_parts = []
    for key, label in (("communication_style", "STYLE"), ("technical_focus", "FOCUS")):
        val = profile.get(key, "").strip()
        if val:
            ctx_parts.append(f"{label}: {val}")
    context_block = "\n".join(ctx_parts)

    # ── Profile memory injection via memory provider (Feature 5) ─────────
    # Called at startup only — zero runtime latency.
    # Swap _ACTIVE_MEMORY_PROVIDER to change retrieval strategy.
    memory_block = _ACTIVE_MEMORY_PROVIDER.retrieve(
        context=profile.get("recruiter_context", "")
    )

    # ── Banned phrases (base + profile) ──────────────────────────────────
    base_banned = "Certainly/Of course/Absolutely/Great question/As an AI/I'd be happy to"
    extra_banned = profile.get("forbidden_phrases", "").strip()
    banned = f"{base_banned} / {extra_banned}" if extra_banned else base_banned

    # ── Assemble prompt (compressed, no duplicate sections) ──────────────
    mode_rules = {
        "mixed": "Input is almost always English from recruiter. Route by [Input language] tag.",
        "en":    "Input is almost always English from recruiter. Route by [Input language] tag.",
        "ja":    "Primary language is Japanese. EN input = interviewer switched languages.",
    }
    mode_note = mode_rules.get(interview_lang, mode_rules["mixed"])

    sections = [preamble]

    sections.append(f"""
OUTPUT — strictly conditional on [Input language] tag:

[Input language: Japanese] → output exactly 1 line:
  [JP] <short natural Japanese suggestion>

[Input language: English] → output exactly 2 lines:{read_rule}
  [JP] <accurate Japanese translation>
  [EN] <spoken English reply>

RULES: No preamble. No blank lines. Never [EN] on Japanese input.
{mode_note}
{level_note}""")

    if context_block:
        sections.append(context_block)

    if memory_block:
        sections.append(memory_block)

    sections.append(f"""BANNED in [EN]: {banned}
Noise/unclear — JP: [JP]（もう一度お願いします） / EN: [JP]（聞き取り中）[EN] Sorry, say that again""")

    return "\n\n".join(s.strip() for s in sections if s.strip())


# ─────────────────────────────────────────────────────────────────────────────
# Apply profile overrides, build prompts
# ─────────────────────────────────────────────────────────────────────────────
_effective_lang, _effective_level = _apply_profile_overrides(
    _ACTIVE_PROFILE, args.interview_lang, args.english_level,
)

SYSTEM_PROMPT = _build_system_prompt(
    interview_lang = _effective_lang,
    english_level  = _effective_level,
    pronunciation  = args.pronunciation,
    mode           = args.mode,
    profile        = _ACTIVE_PROFILE,
)

# Feature 4 — prompt size observability
# Shows at startup so the user can immediately detect profile bloat.
# chars/4 is a reliable token estimate for mixed EN/JP text (Japanese chars ~1.5-2 tokens,
# averaged against English to ~4 chars/token overall).
_PROMPT_CHARS  = len(SYSTEM_PROMPT)
_PROMPT_TOKENS = _PROMPT_CHARS // 4
_PROMPT_SIZE_NOTE = f"prompt_chars={_PROMPT_CHARS}  est_tokens≈{_PROMPT_TOKENS}"

_SUMMARY_TONE = _ACTIVE_PROFILE.get("summary_tone", "").strip()
SUMMARY_PROMPT = (
    "You are reviewing a verbatim transcript of a recruiter phone interview.\n"
    "Summarize ONLY what is explicitly present in the transcript below.\n"
    "DO NOT infer, guess, or add topics not mentioned in the transcript.\n"
    "DO NOT suggest preparation topics unless the recruiter explicitly asked about them.\n"
    "If the transcript is short or unclear, say so — do not pad the summary.\n\n"
    "Summarize in Japanese:\n"
    "1. Topics the recruiter explicitly asked about (quote briefly if helpful)\n"
    "2. Any questions or topics the user seemed uncertain about\n"
    "3. Next steps mentioned, if any\n\n"
    "STRICT RULE: if a topic was not in the transcript, do not mention it.\n"
    "Under 250 words. Bullet points."
    + (f"\nTone: {_SUMMARY_TONE}" if _SUMMARY_TONE else "")
)

# ─────────────────────────────────────────────────────────────────────────────
# Meeting analysis prompt  (v22 — RuntimeMode.MEETING)
# ─────────────────────────────────────────────────────────────────────────────

_MEETING_ANALYSIS_PROMPT = """\
あなたはリアルタイム会議支援AIです。
提供された会議トランスクリプトを分析し、以下の構造で日本語のみで出力してください。

# サマリー

（会議の内容を100字以内で簡潔にまとめる）

# リスク・懸念事項

（潜在的なリスク、未解決の問題、責任の曖昧さ、矛盾した発言、時間的プレッシャーをリストアップする。なければ「なし」と記載）

# 検出された質問

質問が1件以上存在する場合:
  質問 1:
  【Subject名】（明示的な質問、または文脈から推測される疑問点を原文に近い形で記載）

  回答候補:
  （トランスクリプトの内容に基づいた具体的・実用的な回答例）

  質問 2:
  【Subject名】（追加の質問があれば記載）

  回答候補:
  （具体的な回答例）

質問が0件の場合のみ:
  質問は検出されませんでした。

【重要ルール】質問が1件以上存在する場合は絶対に「質問は検出されませんでした。」を出力しない。
質問の有無はいずれか一方のみを出力すること。両方を同時に出力しない。

# 推奨アクション

（優先順位付きで、即実行可能な具体的アクションを以下の形式でリストアップする）
- 【Subject名】アクション内容

# 確認された事実

（トランスクリプト内で明示的に確認・断言された事実のみを記載する。推測や回答候補は含めない）
- 【Subject名】fact_type: value

例:
- 【VPN導入】status: 導入済み
- 【採用面談】schedule: 来月上旬
- 【システム点検】result: 問題なし

事実が確認できない場合:
なし

分析ルール:
- 明示的な質問（？や疑問形で終わるもの）と暗示的な質問（決定依頼・確認要求・意見求め）の両方を検出する。
- 回答候補はトランスクリプトの内容に根拠を持たせ、推測で補完する場合はその旨を明記する。
- リスクは具体的に記載し、重大度順に並べる。
- 推奨アクションは「誰が・何を・いつまでに」の形式で記載できる場合はそうする。
- 質問および推奨アクションの先頭には対象トピックを【Subject名】形式で付与する。Subjectが特定できない場合のみ省略可。
- 出力は日本語のみ。英語は使用しない。
"""

# ─────────────────────────────────────────────────────────────────────────────
# Hallucination / noise filter
# ─────────────────────────────────────────────────────────────────────────────
_HALLUCINATIONS = frozenset({
    "ご視聴ありがとうございました", "字幕は自動生成されています", "この動画は", "翻訳",
    "thank you for watching", "please subscribe", "like and subscribe",
    "thanks for watching", "[music]", "(music)", "♪", "…", ".", "。",
})

_FILLER_PATTERNS = (
    "um", "uh", "ah", "oh", "hmm", "mm", "mhm",
    "うん", "えー", "あー", "はい", "ええ",
)

_MIN_CHARS = 6


# ─────────────────────────────────────────────────────────────────────────────
# Modified 2026/5/28 15:29
# v1.10 M2: delegates to extracted conversation.hallucination_guard when available.
# ─────────────────────────────────────────────────────────────────────────────
def is_meaningful(text: str) -> bool:
    """
    [MODULE: conversation.hallucination_guard]
    v1.10 M2: delegates to extracted conversation.hallucination_guard module when available.
    Falls back to inline logic if module import failed.

    Improved noise filter for interview runtime.

    Added guards:
      - Single-word captures ("OK", "yeah", "right") below 10 chars → False
      - Repeated-token guard: ≤ 2 unique words in 4+ token utterance → False
      - All-punctuation / all-space → False
    """
    if _HALLUCINATION_MODULE_LOADED:
        _trace_enabled = (
            getattr(args, "trace", False) or
            getattr(args, "debug_short_mode", False)
        )
        _tfn = _trace if _trace_enabled else None
        return _ext_is_meaningful(text, trace_fn=_tfn)

    # ── Inline fallback (preserved for safety) ────────────────────────────────

    if not (_ENV["HALLUCINATION_GUARD"]):
        return True   # guard disabled — pass everything through

    stripped = (text or "").strip()

    if len(stripped) < _MIN_CHARS:
        return False

    # Punctuation-only
    if all(not c.isalpha() and not '\u3040' <= c <= '\u9FFF' for c in stripped):
        return False

    tl = stripped.lower()

    # ------------------------------------------------------------
    # Interview / recruiter prompts should pass
    # ------------------------------------------------------------

    try:
        lang_guess = detect_language_from_text(stripped)
    except Exception:
        import re
        lang_guess = "english" if re.search(r"[a-zA-Z]", stripped) else "japanese"

    if lang_guess == "english":

        # Explicit question
        if "?" in stripped:
            _trace(
                "meaningful/check",
                f"decision=True text={stripped[:80]!r}"
            )
            return True

        # Recruiter-style prompts
        if len(stripped.split()) >= 4:
            _trace(
                "meaningful/check",
                f"decision=True text={stripped[:80]!r}"
            )
            return True

    # ------------------------------------------------------------
    # Hallucination patterns
    # ------------------------------------------------------------

    if tl in _HALLUCINATIONS:
        _trace(
            "meaningful/check",
            f"decision=False text={stripped[:80]!r}"
        )
        return False

    # ------------------------------------------------------------
    # Filler patterns
    # ------------------------------------------------------------

    if tl.rstrip(".,!?") in _FILLER_PATTERNS:
        _trace(
            "meaningful/check",
            f"decision=False text={stripped[:80]!r}"
        )
        return False

    # ------------------------------------------------------------
    # Single-word short captures
    # ------------------------------------------------------------

    words = stripped.split()

    if len(words) == 1 and len(stripped) < 10:
        _trace(
            "meaningful/check",
            f"decision=False text={stripped[:80]!r}"
        )
        return False

    # ------------------------------------------------------------
    # Repetition guard
    # ------------------------------------------------------------

    if len(words) >= 4 and len(set(w.lower().rstrip(".,!?") for w in words)) <= 2:
        _trace(
            "meaningful/check",
            f"decision=False text={stripped[:80]!r}"
        )
        return False

    _trace(
        "meaningful/check",
        f"decision=True text={stripped[:80]!r}"
    )

    return True


# ─────────────────────────────────────────────────────────────────────────────
# In-memory WAV builder
# ─────────────────────────────────────────────────────────────────────────────
def make_wav_buffer(audio: np.ndarray) -> io.BytesIO:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(audio.tobytes())
    buf.seek(0)
    buf.name = "audio.wav"
    return buf


# ─────────────────────────────────────────────────────────────────────────────
# RMS silence detection
# ─────────────────────────────────────────────────────────────────────────────
def rms(block: np.ndarray) -> float:
    return float(np.sqrt(np.mean(block.astype(np.float32) ** 2)))


def is_silent(block: np.ndarray) -> bool:
    return rms(block) < RMS_THRESHOLD


# ─────────────────────────────────────────────────────────────────────────────
# Whisper transcription
# ─────────────────────────────────────────────────────────────────────────────
_RETRYABLE       = (APIConnectionError, APITimeoutError)
_WHISPER_PROMPT_SEED = "はい、えーと、そうですね。OK, so, right, yeah."


def _build_whisper_prompt() -> str:
    with _log_lock:
        recent = list(transcript_log)[-2:]
    if recent:
        return " ".join(e.text for e in recent)[-300:]
    return _WHISPER_PROMPT_SEED


def transcribe(audio: np.ndarray) -> tuple[str, str]:
    """
    Returns (text, detected_language).
    Uses MODEL_CAPS registry to select response_format and prompt usage.
    v17.2: per-call timeout + trace logging + full exception visibility.
    """
    # In debug-short-mode, truncate audio to 5 seconds
    if getattr(args, "debug_short_mode", False):
        max_samples = SAMPLE_RATE * 5
        if len(audio) > max_samples:
            audio = audio[:max_samples]
            _trace("transcribe/truncated", f"audio trimmed to 5s for debug-short-mode")

    wav_buf      = make_wav_buffer(audio)
    use_verbose  = _model_cap(args.whisper_model, "verbose_json")
    use_prompt   = _model_cap(args.whisper_model, "supports_prompt")
    fmt          = "verbose_json" if use_verbose else "json"
    extra: dict  = {}
    if use_prompt:
        extra["prompt"] = _build_whisper_prompt()

    _trace("transcribe/start", f"model={args.whisper_model} fmt={fmt} audio={len(audio)/SAMPLE_RATE:.1f}s")

    for attempt in range(2):
        try:
            # Per-call timeout: 30s hard limit on Whisper regardless of client default
            result = client.audio.transcriptions.create(
                file=wav_buf,
                model=args.whisper_model,
                response_format=fmt,
                temperature=0.0,
                timeout=30.0,
                **extra,
            )

            text = (result.text or "").strip()
            lang = (
                (getattr(result, "language", None) or "unknown")
                if use_verbose
                else detect_language_from_text(text)
            )
            _trace("transcribe/done", f"lang={lang} text_len={len(text)} preview={text[:80]!r}")
            return text, lang

        except _RETRYABLE as e:
            if attempt == 0:
                _trace("transcribe/retry", str(e))
                show_warn(f"Whisper retrying… ({e})")
                wav_buf = make_wav_buffer(audio)
                time.sleep(0.3)
            else:
                import traceback as _tb
                show_err("Whisper", e)
                _trace("transcribe/FAILED", str(e))
                print("[ERROR] Whisper RETRYABLE failure:", e, flush=True)
                _tb.print_exc()
                return "", "unknown"
        except RateLimitError as e:
            import traceback as _tb
            show_err("Whisper rate limit", f"API quota exceeded. ({e})")
            _trace("transcribe/RATELIMIT", str(e))
            _tb.print_exc()
            return "", "unknown"
        except Exception as e:
            import traceback as _tb
            show_err("Whisper", e)
            _trace("transcribe/EXCEPTION", str(e))
            print("[ERROR] Whisper unexpected exception:", e, flush=True)
            _tb.print_exc()
            return "", "unknown"

    return "", "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Fast-path intent classification  (extension point — not yet active)
# ─────────────────────────────────────────────────────────────────────────────
#
# ARCHITECTURE DESIGN (ready to activate — replace stub body):
#
#  The fast path runs BEFORE the Whisper→GPT pipeline in reply_worker.
#  It short-circuits to a cached or pre-composed response for high-frequency
#  recruiter openers, bypassing GPT streaming entirely.
#
#  LATENCY IMPACT:
#    Normal GPT path:   Whisper (~700ms) + GPT (~1200ms) = ~2s total
#    Fast path hit:     0ms (pure dict lookup + local string display)
#    Perceived speedup: ~2s → <50ms for cached intents
#
#  ACTIVATION:
#    1. Populate _INTENT_CACHE below with profile-specific answers
#    2. Replace the stub `return None` with the intent matching logic
#    3. Optionally load cache entries from the profile's `response_examples` section
#
#  INTENT MATCHING STRATEGY (two-tier):
#    Tier 1 — keyword match (zero latency):
#      Strip text to lowercase, check against _INTENT_PATTERNS.
#      If a pattern set matches → return the cached response string.
#    Tier 2 — GPT fallback (current behavior):
#      No match → return None → normal GPT streaming pipeline proceeds.
#
#  ROUTING TABLE (keyword → intent_key → cached_response):
#
_INTENT_PATTERNS: dict[str, frozenset[str]] = {
    # intent_key          trigger keywords (any one match → cache hit)
    "self_intro":      frozenset({"tell me about yourself", "introduce yourself",
                                  "walk me through your background", "about yourself"}),
    "motivation":      frozenset({"why are you interested", "why this role",
                                  "why do you want to", "what draws you"}),
    "strengths":       frozenset({"what are your strengths", "what do you consider",
                                  "what would you say your strength", "good at"}),
    "current_role":    frozenset({"current role", "what are you currently doing",
                                  "what do you do now", "current work", "current job",
                                  "current position", "currently working"}),
    "availability":    frozenset({"when can you start", "start date", "available from",
                                  "notice period", "how soon"}),
    "salary":          frozenset({"salary expectation", "compensation", "pay range",
                                  "what are you looking for in terms of", "what are you expecting"}),
}

# Cached responses — populated from profile or hardcoded here.
# Values are [JP] suggestion strings (shown via show_jp), not full GPT output.
# Profile's response_examples section can seed these at startup (not yet wired).
_INTENT_CACHE: dict[str, str] = {
    # These are EMPTY by default — fill them in the profile or here before a call.
    # Example:
    #   "self_intro":   "セキュリティとGRCの経験を3年、具体的に話そう",
    #   "motivation":   "Verizonのエンタープライズ規模と規制環境に関心があると伝えよう",
    #   "current_role": "現在の役割と責任範囲を簡潔に伝えよう",
}


def _match_intent(text: str) -> Optional[str]:
    """
    Tier 1 keyword match. Returns intent_key if found, else None.
    Case-insensitive substring search — no regex, zero latency.
    """
    tl = text.lower()
    for intent_key, patterns in _INTENT_PATTERNS.items():
        if any(p in tl for p in patterns):
            return intent_key
    return None


def _fast_path_check(text: str, lang: str) -> Optional[IntentResult]:
    """
    Pre-GPT intent hook. Returns an IntentResult to bypass GPT, or None to proceed.

    Returns IntentResult when:
      - Input is English (recruiter question)
      - Intent keyword matches an _INTENT_PATTERNS entry
      - That intent has a cached response in _INTENT_CACHE

    Returns None (passthrough to GPT) otherwise.
    JP self-talk always bypasses fast path — needs contextual GPT coaching.

    FUTURE EXTENSION — GPT refinement:
      When IntentResult.source == "gpt_refined" is implemented:
        result = _fast_path_check(text, lang)
        if result and result.source != "gpt_refined":
            return result   # instant cache hit
        elif result and result.source == "gpt_refined":
            # Run GPT with result.response as a seed for refinement
            pass
    """
    if is_japanese_lang(lang):
        return None   # JP self-talk → always use contextual GPT

    intent = _match_intent(text)
    if not intent:
        return None   # no intent matched → normal GPT pipeline

    if intent in _INTENT_CACHE:
        return IntentResult(
            intent_key = intent,
            response   = _INTENT_CACHE[intent],
            confidence = 1.0,
            source     = "cache",
        )

    return None   # intent matched but no cached response → GPT fallback


# ─────────────────────────────────────────────────────────────────────────────
# GPT streaming reply
# ─────────────────────────────────────────────────────────────────────────────
def generate_reply(text: str, lang: str) -> None:
    """
    Stream a GPT reply.
    v17.2: trace logging, per-call timeout, stream deadline guard.
    The stream deadline prevents a stalled streaming iterator from hanging forever.
    """
    is_jp = is_japanese_lang(lang)

    if _effective_lang == "en" and not is_jp:
        lang_label = "English"
    else:
        lang_label = "Japanese" if is_jp else "English"

    user_content = f"[Input language: {lang_label}]\n[Recruiter/speaker said]: {text}"

    if is_jp:
        max_tok = 60
    elif args.pronunciation:
        max_tok = 150
    else:
        max_tok = 120

    _trace("generate_reply/start", f"lang={lang_label} max_tok={max_tok}")

    # H2A-5: migrated to ProviderInterface.generate_stream(). Establishment
    # failures now surface as the first StreamingError event rather than a
    # raised exception; mid-stream failures surface as a later StreamingError.
    # This distinction (is_first) reproduces the original two separate
    # try/except blocks' control flow (establishment -> early return with no
    # trailing trace/show_sep; mid-stream -> falls through to trace/show_sep).
    provider_request = ProviderRequest(
        messages=[
            Message(role="system", content=SYSTEM_PROMPT),
            Message(role="user", content=user_content),
        ],
        temperature=0.15,
        max_tokens=max_tok,
    )
    stream_events = _provider_streaming.generate_stream(provider_request)

    line_buf   = ""
    jp_printed = False
    en_printed = False
    stream_deadline     = time.monotonic() + 30.0   # 30s max for full stream
    deadline_triggered  = False
    first_event         = True

    for event in stream_events:
        is_first    = first_event
        first_event = False

        if isinstance(event, StreamingError):
            if is_first:
                # Establishment failure — mirrors the original 3-way
                # categorized handling (connection / rate-limit / generic).
                if isinstance(event.cause, RuntimeTimeoutError):
                    _trace("generate_reply/CONNECT_FAIL", event.cause.message)
                    show_warn(f"GPT connection error: {event.cause.message}")
                elif isinstance(event.cause, RuntimeRateLimitError):
                    _trace("generate_reply/RATELIMIT", event.cause.message)
                    show_warn(f"GPT rate limit: {event.cause.message}")
                else:
                    _trace("generate_reply/EXCEPTION", event.cause.message)
                    show_err("GPT", event.cause)
                    print("[ERROR] generate_reply unexpected:", event.cause, flush=True)
                return
            else:
                # Mid-stream failure — mirrors the original generic handler.
                _trace("generate_reply/STREAM_EXCEPTION", event.cause.message)
                show_err("GPT stream", event.cause)
                print("[ERROR] GPT stream exception:", event.cause, flush=True)
            break

        if not deadline_triggered and time.monotonic() > stream_deadline:
            deadline_triggered = True
            _trace("generate_reply/STREAM_TIMEOUT", "30s stream deadline exceeded")
            show_warn("GPT stream timeout — partial response displayed")
            stream_events.cancel(StreamCancellationReason.DEADLINE_EXCEEDED)
            continue

        if isinstance(event, StreamingTextDelta):
            for ch in event.text:
                line_buf += ch
                if ch == "\n":
                    stripped = line_buf.rstrip("\n").lstrip()
                    if stripped.startswith("[JP]"):   jp_printed = True
                    if stripped.startswith("[EN]"):   en_printed = True
                    _emit_line(stripped, jp_printed, en_printed)
                    line_buf = ""
        elif isinstance(event, (StreamingCompletion, StreamingCancellation)):
            break

    if line_buf.strip():
        stripped = line_buf.strip()
        if stripped.startswith("[JP]"): jp_printed = True
        if stripped.startswith("[EN]"): en_printed = True
        _emit_line(stripped, jp_printed, en_printed)

    _trace("generate_reply/done")
    show_sep()


def _emit_line(line: str, jp_already: bool, en_already: bool) -> None:
    """Route one complete streamed line to the correct display function.
    Input has already been stripped by the caller; lstrip() here is belt-and-suspenders
    against any leading whitespace that slips through — prevents routing failures.
    """
    line = line.lstrip()   # normalise: e.g. "  [JP] ..." → "[JP] ..."
    if not line:
        return
    if line.startswith("[JP]"):
        text = line[4:].strip()
        show_jp(text)
        _emit_event("reply", lang="ja", text=text, speaker="agent", provider=_selected_provider)
    elif line.startswith("[EN]"):
        text = line[4:].strip()
        show_en(text)
        _emit_event("reply", lang="en", text=text, speaker="agent", provider=_selected_provider)
    elif line.startswith("[READ]"):
        text = line[6:].strip()
        show_read(text)
        _emit_event("reply", lang="pronunciation", text=text, speaker="agent", provider=_selected_provider)
    elif not jp_already and not en_already:
        _print(GRAY + line + RESET)


# ─────────────────────────────────────────────────────────────────────────────
# Question detection
# ─────────────────────────────────────────────────────────────────────────────
# Two-tier: heuristic (0ms) → optional GPT classification (~200ms).
# Heuristic covers >90% of recruiter questions in English and Japanese.
# GPT classification is only invoked when --classify is set AND the heuristic
# returns inconclusive (not clearly a question or statement).

_Q_PATTERNS_EN: frozenset[str] = frozenset({
    "?",
    "can you", "could you", "would you", "will you",
    "do you", "did you", "have you", "are you", "were you",
    "tell me about", "tell me more", "walk me through", "describe your",
    "what is", "what are", "what do", "what did", "what has", "what have",
    "how do", "how did", "how has", "how would", "how have",
    "why do", "why did", "why would",
    "when did", "when do", "when would",
    "where did", "where do",
    "which", "who did", "who are",
    "give me an example", "talk me through", "share with me",
    "explain your", "explain how", "explain what",
})

_Q_PATTERNS_JP: frozenset[str] = frozenset({
    "？", "ですか", "でしょうか", "ますか", "ませんか",
    "いただけますか", "教えてください", "おしえてください",
    "どのように", "なぜ", "何ですか", "なんですか",
    "ありますか", "できますか", "でしたか", "ましたか",
})


def _is_question_heuristic(text: str, lang: str) -> bool:
    """
    Tier 1: zero-latency substring check.
    Returns True if text looks like a question, False otherwise.
    """
    tl = text.lower()
    if is_japanese_lang(lang):
        return any(p in tl or p in text for p in _Q_PATTERNS_JP)
    return any(p in tl for p in _Q_PATTERNS_EN)


def _is_question_gpt(text: str) -> bool:
    """
    Tier 2: GPT classification (~200ms). Called only when --classify is set
    and the heuristic returns False but the utterance is long enough to be
    a question in disguise (e.g. indirect questions without '?').
    """
    try:
        resp = _provider_default.generate(ProviderRequest(
            messages=[Message(role="user", content=(
                f"Is this a recruiter question that expects a spoken answer? "
                f"Answer YES or NO only.\n\n\"{text}\""
            ))],
            temperature=0.0,
            max_tokens=3,
        ))
        answer = (resp.text or "").strip().upper()
        return answer.startswith("Y")
    except Exception:
        return False   # on failure, assume not a question (conservative)


def is_question(text: str, lang: str) -> bool:
    """
    Combined question detection. Used by agent mode to decide whether to respond.
    Observer mode never calls this.
    """
    if _is_question_heuristic(text, lang):
        return True
    # Only call GPT when --classify is on and text is substantial
    if args.classify and len(text) > 20:
        return _is_question_gpt(text)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Conversation history builder (agent mode)
# ─────────────────────────────────────────────────────────────────────────────

def _build_conversation_history() -> list[dict]:
    """
    Build a rolling N-turn conversation history from transcript_log.
    Used as additional messages in agent GPT calls for contextual awareness.

    Returns a list of {"role": ..., "content": ...} dicts ready for the API.
    The most recent --history-turns entries are included.
    Recruiter entries → "user" role; user/response entries → "assistant" role.
    """
    n = args.history_turns
    if n <= 0:
        return []

    with _log_lock:
        recent = list(transcript_log)[-(n * 2):]   # rough window, filtered below

    history = []
    for entry in recent:
        if entry.speaker == "recruiter":
            history.append({"role": "user", "content": f"[Recruiter]: {entry.text}"})
        elif entry.speaker in ("user", "agent"):
            history.append({"role": "assistant", "content": f"[Response]: {entry.text}"})

    return history[-n:]   # keep at most N turns


# ─────────────────────────────────────────────────────────────────────────────
# Agent system prompt (separate from observer SYSTEM_PROMPT)
# ─────────────────────────────────────────────────────────────────────────────
# The observer prompt outputs [JP]/[EN] tags for on-screen display.
# The agent prompt generates a direct spoken response — no tags, no formatting.
# The agent response is what will actually be spoken by TTS or read aloud.

def _build_agent_system_prompt(profile: dict) -> str:
    """
    Concise system prompt for autonomous response generation.
    Output: a single natural spoken English reply (no tags, no formatting).
    Must sound like a real person, not an AI assistant.
    """
    # Profile preamble
    identity    = profile.get("identity",          "").strip()
    positioning = profile.get("positioning",       "").strip()
    rec_context = profile.get("recruiter_context", "").strip()
    comm_style  = profile.get("communication_style","").strip()
    tech_focus  = profile.get("technical_focus",   "").strip()
    career      = profile.get("career_summary",    "").strip()
    topics      = profile.get("topic_memory",      "").strip()

    level_note = _ENGLISH_LEVEL_INSTRUCTIONS.get(_effective_level, "")
    base_banned = "Certainly/Of course/Absolutely/Great question/As an AI/I'd be happy to"
    extra_banned = profile.get("forbidden_phrases", "").strip()
    banned = f"{base_banned} / {extra_banned}" if extra_banned else base_banned

    parts = []
    if identity:     parts.append(identity)
    if positioning:  parts.append(f"Role: {positioning}")
    if rec_context:  parts.append(f"Context: {rec_context}")
    preamble = "\n".join(parts) or "You are a Japanese professional in a live recruiter phone interview."

    memory = []
    if career:  memory.append(f"CAREER: {career}")
    if topics:  memory.append(f"TOPICS: {topics}")
    memory_block = "\n".join(memory)

    style_block = ""
    if comm_style: style_block += f"STYLE: {comm_style}\n"
    if tech_focus: style_block += f"FOCUS: {tech_focus}"

    return f"""{preamble}

You are generating a spoken response to say out loud in a live recruiter phone interview.

OUTPUT RULES:
- Output ONE response only. No tags. No labels. No formatting.
- Write exactly what should be spoken — nothing else.
- Sound like a real person in a phone interview, not an AI.
- Keep it concise: 2-3 sentences maximum.
- Natural spoken English — contractions OK, professional but relaxed.
- If the question is in Japanese, respond in natural Japanese.

{level_note}

{memory_block}

{style_block}

BANNED: {banned}

Noise/unclear input: respond with "Sorry, could you say that again?" """


AGENT_SYSTEM_PROMPT = _build_agent_system_prompt(_ACTIVE_PROFILE)


# ─────────────────────────────────────────────────────────────────────────────
# Autonomous response generation (agent mode)
# ─────────────────────────────────────────────────────────────────────────────

def generate_agent_reply(text: str, lang: str) -> Optional[str]:
    """
    [MODULE: agent.response_generation]
    Generate a direct spoken response for agent mode.
    Returns the response string (for TTS and logging), or None on failure.

    v14 additions:
      - Hallucination guard: if ENABLE_HALLUCINATION_GUARD=1, validates response
        does not contain fabricated experience claims not present in profile memory.
      - Profile grounding: if ENABLE_PROFILE_GROUNDING=1, warns when response
        deviates from profile career_summary or topic_memory.
    """
    # ── Hallucination guard: pre-flight check ────────────────────────────────
    # If the guard is enabled, we use a slightly lower temperature and
    # inject an explicit anti-fabrication instruction into the user message.
    hallucination_guard = _ENV.get("HALLUCINATION_GUARD", True)
    profile_grounding   = _ENV.get("PROFILE_GROUNDING", True)

    is_jp = is_japanese_lang(lang)

    if _effective_lang == "en" and not is_jp:
        lang_label = "English"
    else:
        lang_label = "Japanese" if is_jp else "English"

    # Anti-hallucination instruction appended to user message when guard is active
    guard_note = ""
    if hallucination_guard:
        guard_note = (
            "\n[GUARD] Only refer to experience, skills, and facts explicitly provided "
            "in your profile context above. Do NOT invent or embellish experience."
        )

    messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]

    history = _build_conversation_history()
    if history:
        messages.extend(history)

    messages.append({
        "role": "user",
        "content": (
            f"[Input language: {lang_label}]\n"
            f"[Recruiter said]: {text}"
            f"{guard_note}"
        )
    })

    if is_jp:
        max_tok = 80
    elif args.pronunciation:
        max_tok = 120
    else:
        max_tok = 100

    # Slightly lower temperature when hallucination guard is active
    temperature = 0.1 if hallucination_guard else 0.2

    debug_runtime(f"entering GPT agent  model={args.gpt_model}  msgs={len(messages)}")

    # H2A-5: migrated to ProviderInterface.generate_stream(). See
    # generate_reply() for the is_first / mid-stream distinction rationale.
    provider_request = ProviderRequest(
        messages=[Message(role=m["role"], content=m["content"]) for m in messages],
        temperature=temperature,
        max_tokens=max_tok,
    )
    stream_events = _provider_streaming.generate_stream(provider_request)

    debug_runtime("GPT agent stream established — iterating chunks")
    response_parts: list[str] = []
    agent_stream_deadline = time.monotonic() + 45.0   # BUG FIX: was missing — stream can hang indefinitely without this
    deadline_triggered = False
    first_event        = True
    mid_stream_error    = False

    for event in stream_events:
        is_first    = first_event
        first_event = False

        if isinstance(event, StreamingError):
            if is_first:
                if isinstance(event.cause, RuntimeTimeoutError):
                    debug_runtime(f"GPT agent connect error: {event.cause.message}")
                    show_warn(f"Agent GPT connection error: {event.cause.message}")
                elif isinstance(event.cause, RuntimeRateLimitError):
                    debug_runtime(f"GPT agent rate limit: {event.cause.message}")
                    show_warn(f"Agent GPT rate limit: {event.cause.message}")
                else:
                    debug_runtime(f"GPT agent create exception: {event.cause.message}")
                    show_err("Agent GPT", event.cause)
                return None
            else:
                debug_runtime(f"GPT agent stream exception: {event.cause.message}")
                show_err("Agent GPT stream", event.cause)
                mid_stream_error = True
            break

        if not deadline_triggered and time.monotonic() > agent_stream_deadline:
            deadline_triggered = True
            debug_runtime("GPT agent stream deadline exceeded — breaking")
            show_warn("[agent] GPT stream timeout — partial response used")
            stream_events.cancel(StreamCancellationReason.DEADLINE_EXCEEDED)
            continue

        if isinstance(event, StreamingTextDelta):
            response_parts.append(event.text)
        elif isinstance(event, (StreamingCompletion, StreamingCancellation)):
            break

    if mid_stream_error and not response_parts:
        return None

    debug_runtime(f"GPT agent completed  parts={len(response_parts)}  chars={sum(len(p) for p in response_parts)}")

    response = "".join(response_parts).strip()
    if not response:
        return None

    # ── Profile grounding trace ───────────────────────────────────────────────
    # When ENABLE_PROFILE_GROUNDING=1, log a warning if the response seems to
    # reference content not present in profile memory (rough heuristic: checks
    # for numeric year claims that don't appear in career_summary).
    if profile_grounding and _ENV.get("SUMMARY_DEBUG"):
        import re as _re
        years_in_response = set(_re.findall(r'\b\d+\s*(?:years?|年)\b', response.lower()))
        career = _ACTIVE_PROFILE.get("career_summary", "").lower()
        topics = _ACTIVE_PROFILE.get("topic_memory", "").lower()
        for claim in years_in_response:
            digits = _re.search(r'\d+', claim)
            if digits and digits.group() not in career and digits.group() not in topics:
                show_warn(
                    f"[grounding] Response mentions '{claim}' not found in profile — "
                    "verify this is accurate before speaking"
                )

    return response
# ─────────────────────────────────────────────────────────────────────────────
# v17 COGNITION LAYER
# ─────────────────────────────────────────────────────────────────────────────
# Activated when: args.cognition is True  OR  ENABLE_COGNITION=1
#
# PHASE 1 (unchanged): Whisper STT → text, lang
# PHASE 2 (NEW): compress_conversation() → CompressionResult
# PHASE 3 (NEW): generate_candidates()   → list[ResponseCandidate]
# PHASE 4 (NEW): show_candidates()       → terminal display (operator chooses)
#
# Every phase is failure-isolated. Any exception returns None/empty and
# reply_worker falls back to the standard v16 generate_reply() path.
# The runtime NEVER crashes because of cognition pipeline failure.
#
# DESIGN:
#   CompressionResult  — structured output from Phase 2
#   ResponseCandidate  — one response option with EN + JP + KATAKANA
#   All phases in one thread (reply_worker) — no new threads, no new queues.
# ─────────────────────────────────────────────────────────────────────────────

from dataclasses import dataclass, field as dc_field
from typing import List


@dataclass
class CompressionResult:
    """
    Output of compress_conversation() — Phase 2 of the cognition pipeline.

    Fields:
      summary           — 1-3 sentence summary of the conversation/buffer
      detected_question — the most likely recruiter question (empty if unclear)
      intent            — inferred recruiter intent (e.g. "experience validation")
      topics            — list of detected topic keywords
      raw_text          — original transcription (preserved for fallback)
      phase_ms          — compression latency in milliseconds
    """
    summary:            str
    detected_question:  str
    intent:             str
    topics:             List[str]
    raw_text:           str
    phase_ms:           float = 0.0


@dataclass
class ResponseCandidate:
    """
    One response option — output of generate_candidates() — Phase 3.

    Fields:
      style     — "concise" | "technical" | "conservative"
      english   — natural spoken English response
      japanese  — Japanese explanation of the English response
      katakana  — katakana pronunciation guide for the English response
      grounded  — True if response was grounded to profile context
    """
    style:    str
    english:  str
    japanese: str
    katakana: str
    grounded: bool = True


# ── Display helpers (Phase 4) ─────────────────────────────────────────────────

def show_compression_result(result: CompressionResult) -> None:
    """Render compression result — v17.2: minimal two-line display only."""
    _print(f"\n{BOLD}{CYAN}[COGNITION]{RESET}  {GRAY}{result.phase_ms:.0f}ms{RESET}")
    if result.detected_question:
        _print(f"{BOLD}{YELLOW}[QUESTION]{RESET} {result.detected_question}")
    if result.intent:
        _print(f"{GRAY}[INTENT]{RESET}   {result.intent}")


def show_candidate(n: int, candidate: ResponseCandidate) -> None:
    """Render one response candidate.
    v17.2 katakana fix: [KA] is pronunciation of [EN] only — never transliterate JP.
    Order: EN → KA (reading guide for EN) → JP.
    """
    style_colors = {"concise": CYAN, "safer": YELLOW}
    color = style_colors.get(candidate.style, GRAY)
    _print(f"\n{color}{BOLD}[OPTION {n} — {candidate.style.upper()}]{RESET}")
    if candidate.english:
        _print(f"  {GREEN}{BOLD}[EN]{RESET}  {candidate.english}")
    if candidate.katakana:
        _print(f"  {MAGENTA}[KA]{RESET}  {candidate.katakana}")   # katakana reading of EN only
    if candidate.japanese:
        _print(f"  {YELLOW}[JP]{RESET}  {candidate.japanese}")


def show_candidates(candidates: List[ResponseCandidate]) -> None:
    """Render all candidates with a separator."""
    if not candidates:
        return
    show_sep()
    for i, c in enumerate(candidates, 1):
        show_candidate(i, c)
    show_sep()
    _print(f"{GRAY}↑ Choose a response above. Speak manually. {BOLD}No auto-speech.{RESET}")


# ── Phase 2: Conversation compression ────────────────────────────────────────

_COMPRESSION_PROMPT = """You are a conversational cognition assistant.

Analyze the following conversation/transcript and extract:
1. SUMMARY: A 1-2 sentence summary of what was discussed.
2. QUESTION: The most likely recruiter question being asked (or "unclear" if not identifiable).
3. INTENT: The recruiter's likely intent in 3-5 words (e.g. "experience validation", "technical depth check", "motivation assessment").
4. TOPICS: 3-5 keyword topics from the conversation, comma-separated.

Output EXACTLY in this format (no extra text, no explanation):
SUMMARY: <1-2 sentences>
QUESTION: <extracted question or "unclear">
INTENT: <3-5 words>
TOPICS: <keyword1, keyword2, keyword3>

Transcript:
"""


def compress_conversation(
    text:    str,
    history: Optional[List[dict]] = None,
) -> Optional[CompressionResult]:
    """
    Phase 2: Summarize buffered conversation and extract recruiter question.

    Runs ONCE per operator flush. Single non-streaming GPT call.
    Failure-isolated: returns None on any error, caller falls back to raw text.

    Args:
      text    — transcribed text from Whisper (may be multi-sentence)
      history — recent conversation history (list of {role, content} dicts)

    Returns CompressionResult or None on failure.
    Latency: ~300-700ms (non-streaming, short response).
    """
    t0 = time.monotonic()

    # Build context: include recent history if available
    context_parts = []
    if history:
        for msg in history[-4:]:  # last 4 turns max to control tokens
            role   = "Recruiter" if msg["role"] == "user" else "Candidate"
            content = msg["content"].replace("[Recruiter]: ", "").replace("[Response]: ", "")
            context_parts.append(f"{role}: {content}")
    context_parts.append(f"Latest: {text}")
    context = "\n".join(context_parts)

    _trace("compress/start", f"text_len={len(text)}")

    try:
        resp = _provider_default.generate(ProviderRequest(
            messages=[
                Message(role="system", content=_COMPRESSION_PROMPT),
                Message(role="user", content=context),
            ],
            temperature=0.0,
            max_tokens=180,
        ))
        raw = (resp.text or "").strip()
        _trace("compress/done", f"raw_len={len(raw)}")
    except Exception as e:
        import traceback as _tb
        _trace("compress/EXCEPTION", str(e))
        show_warn(f"[cognition] compress failed: {e}")
        print("[ERROR] compress_conversation:", e, flush=True)
        _tb.print_exc()
        return None

    phase_ms = (time.monotonic() - t0) * 1000

    # Parse structured output
    summary   = ""
    question  = ""
    intent    = ""
    topics    = []

    for line in raw.splitlines():
        line = line.strip()
        if line.startswith("SUMMARY:"):
            summary  = line[8:].strip()
        elif line.startswith("QUESTION:"):
            question = line[9:].strip()
            if question.lower() in ("unclear", "n/a", "none", ""):
                question = ""
        elif line.startswith("INTENT:"):
            intent   = line[7:].strip()
        elif line.startswith("TOPICS:"):
            topics   = [t.strip() for t in line[7:].split(",") if t.strip()]

    if _ENV.get("COGNITION"):
        show_info(f"[cognition] phase=compress  ms={phase_ms:.0f}  q={bool(question)}")

    return CompressionResult(
        summary           = summary,
        detected_question = question,
        intent            = intent,
        topics            = topics,
        raw_text          = text,
        phase_ms          = phase_ms,
    )


# ── Phase 3: Multi-candidate generation ──────────────────────────────────────

def _build_candidates_prompt(n_candidates: int) -> str:
    """
    v17.2: 2 candidates only (concise + safer). Technical removed.
    Katakana fix: KA must be pronunciation of the ENGLISH sentence ONLY.
    Do NOT transliterate Japanese into katakana.
    """
    candidate_styles = ["concise", "safer"][:n_candidates]
    style_desc = {
        "concise": "Short, direct, 1-2 sentences. Easy to say out loud.",
        "safer":   "Safe, measured, avoids over-claiming. 1-2 sentences.",
    }

    option_blocks = "\n\n".join(
        f"[OPTION{i+1}]\nSTYLE: {s}\nSTYLE_DESC: {style_desc[s]}"
        for i, s in enumerate(candidate_styles)
    )

    return f"""You are a profile-grounded response generation assistant for a live interview.

Generate exactly {n_candidates} response candidate(s) for the question provided.

CRITICAL RULES:
- Only use experience and facts from the PROFILE provided below.
- Do NOT invent, exaggerate, or fabricate experience.
- Each response must be speakable out loud naturally.
- English: 2-3 sentences max. Professional but conversational.
- Japanese: natural Japanese translation of the English response.
- Katakana: phonetic reading of the ENGLISH sentence for a Japanese speaker reading aloud.
  IMPORTANT: KA is ALWAYS the katakana pronunciation of EN. NEVER transliterate JP into katakana.
  Example: EN="My experience is in security ops." → KA="マイ エクスペリエンス イズ イン セキュリティ オプス。"

For EACH option output EXACTLY this block (no extra text):
[OPTIONn]
EN: <spoken English response>
KA: <katakana reading of the EN sentence above — NOT a transliteration of JP>
JP: <Japanese translation>

{option_blocks}"""


def _parse_candidates(
    raw:       str,
    n_expect:  int,
    styles:    List[str],
) -> List[ResponseCandidate]:
    """Parse GPT output into ResponseCandidate list. v17.2: field order EN→KA→JP."""
    import re as _re
    candidates = []
    blocks = _re.split(r'\[OPTION\d+\]', raw)
    blocks = [b.strip() for b in blocks if b.strip()]

    for i, block in enumerate(blocks[:n_expect]):
        en = jp = ka = ""
        for line in block.splitlines():
            line = line.strip()
            if line.startswith("EN:"):   en = line[3:].strip()
            elif line.startswith("KA:"): ka = line[3:].strip()
            elif line.startswith("JP:"): jp = line[3:].strip()

        if en:
            style = styles[i] if i < len(styles) else f"option{i+1}"
            candidates.append(ResponseCandidate(
                style=style, english=en, japanese=jp, katakana=ka, grounded=True,
            ))
    return candidates


def generate_candidates(
    question:  str,
    lang:      str,
    profile:   dict,
) -> List[ResponseCandidate]:
    """
    Phase 3: Generate multiple grounded response candidates in one GPT call.

    Returns list of ResponseCandidate. Empty list on failure.
    Failure-isolated: caller falls back to standard generate_reply() on empty list.

    Args:
      question  — extracted recruiter question (from CompressionResult or raw text)
      lang      — detected language of the question
      profile   — active profile dict for grounding

    Latency: ~600-1200ms (single non-streaming call with structured output).
    """
    t0           = time.monotonic()
    n            = min(args.candidates, 2)   # v17.2: max 2 candidates
    styles       = ["concise", "safer"][:n]
    is_jp        = is_japanese_lang(lang)
    lang_label   = "Japanese" if is_jp else "English"

    # Build grounding context from profile
    career   = profile.get("career_summary", "").strip()
    topics   = profile.get("topic_memory",   "").strip()
    identity = profile.get("identity",       "").strip()
    hg_note  = (
        "\n[GUARD] Use ONLY facts from the PROFILE below. Do NOT invent experience."
        if _ENV.get("HALLUCINATION_GUARD", True) else ""
    )

    profile_block = "\n".join(filter(None, [
        f"IDENTITY: {identity}" if identity else "",
        f"CAREER: {career}"    if career else "",
        f"TOPICS: {topics}"    if topics else "",
    ]))

    system_prompt = _build_candidates_prompt(n)
    user_content  = (
        f"PROFILE:\n{profile_block}\n\n"
        f"[Input language: {lang_label}]\n"
        f"QUESTION: {question}"
        f"{hg_note}"
    )

    _trace("candidates/start", f"n={n} lang={lang_label} q_len={len(question)}")

    try:
        resp = _provider_candidates.generate(ProviderRequest(
            messages=[
                Message(role="system", content=system_prompt),
                Message(role="user", content=user_content),
            ],
            temperature=0.15,
            max_tokens=350 * n,
        ))
        raw = (resp.text or "").strip()
        _trace("candidates/done", f"raw_len={len(raw)}")
    except Exception as e:
        import traceback as _tb
        _trace("candidates/EXCEPTION", str(e))
        show_warn(f"[cognition] candidates failed: {e}")
        print("[ERROR] generate_candidates:", e, flush=True)
        _tb.print_exc()
        return []

    phase_ms   = (time.monotonic() - t0) * 1000
    candidates = _parse_candidates(raw, n, styles)

    if _ENV.get("COGNITION"):
        show_info(
            f"[cognition] phase=candidates  ms={phase_ms:.0f}  "
            f"n_requested={n}  n_parsed={len(candidates)}"
        )

    return candidates


# ── Cognition pipeline orchestrator ──────────────────────────────────────────

def run_cognition_pipeline(
    text:    str,
    lang:    str,
    history: Optional[List[dict]],
) -> bool:
    """
    Execute the full v17 cognition pipeline for one transcribed utterance.

    Returns True if pipeline produced output, False if it failed completely
    (caller should then use generate_reply() as fallback).

    Phase isolation:
      Phase 2 (compress) failure → use raw text for Phase 3
      Phase 3 (candidates) failure → return False → fallback to generate_reply
    """
    t_pipeline = time.monotonic()

    # ── Phase 2: Conversation compression ────────────────────────────────
    compression = compress_conversation(text, history)
    if compression:
        show_compression_result(compression)
        question = compression.detected_question or text
    else:
        # Compression failed — use raw text as the question
        show_warn("[cognition] compression skipped — using raw transcript")
        question = text

    # ── Phase 3: Multi-candidate generation ──────────────────────────────
    candidates = generate_candidates(question, lang, _ACTIVE_PROFILE)

    if not candidates:
        show_warn("[cognition] candidate generation failed — falling back to standard reply")
        return False

    # ── Phase 4: Display candidates ───────────────────────────────────────
    show_candidates(candidates)

    total_ms = (time.monotonic() - t_pipeline) * 1000
    show_info(f"[cognition] pipeline_total={total_ms:.0f}ms")

    # Log the top candidate as the agent response for transcript continuity
    top = candidates[0]
    if top.english:
        ts = time.strftime("%H:%M:%S")
        with _log_lock:
            agent_entry = LogEntry(
                text    = f"[CANDIDATE] {top.english}",
                lang    = "english",
                ts      = ts,
                speaker = "agent",
            )
            transcript_log.append(agent_entry)
            _persist_entry(
                agent_entry,
                state      = "generating",
                latency_ms = total_ms,
            )
        if latency_tracker:
            latency_tracker.record(0, total_ms)   # GPT time = whole pipeline

    return True


# ─────────────────────────────────────────────────────────────────────────────
# Reply worker thread
# ─────────────────────────────────────────────────────────────────────────────
def reply_worker() -> None:
    """
    [MODULE: conversation.orchestration]
    Long-running worker thread. MUST NEVER return during normal operation.

    v17.2 debug stabilization:
      - Full per-stage trace logging (--trace or --debug-short-mode)
      - _persist_entry moved OUTSIDE _log_lock (eliminates lock-hold-on-file-open deadlock)
      - debug-short-mode bypasses cognition and enables maximal logging
      - All exceptions fully printed with traceback
    """
    agent_mode      = args.agent or _ENV["AGENT_MODE"]
    cognition_mode  = (args.cognition or _ENV.get("COGNITION", False)) and not args.debug_short_mode
    debug_short     = getattr(args, "debug_short_mode", False)

    _trace("reply_worker/start", f"agent={agent_mode} cognition={cognition_mode} debug={debug_short}")

    while not _shutdown.is_set():
        try:
            audio = transcript_queue.get(timeout=0.2)
        except queue.Empty:
            continue

        try:
            _trace("reply_worker/got_audio", f"audio_samples={len(audio)}")

            t_start    = time.monotonic()
            debug_runtime(f"entering whisper  samples={len(audio)}  sec={len(audio)/SAMPLE_RATE:.1f}")
            text, lang = transcribe(audio)
            t_stt      = time.monotonic() - t_start
            debug_runtime(f"whisper completed  text={text[:60]!r}  lang={lang}  ms={t_stt*1000:.0f}")

            debug_runtime("before _trace after_transcribe")
            _trace("reply_worker/after_transcribe", f"text={text[:80]!r} lang={lang} stt_ms={t_stt*1000:.0f}")
            debug_runtime("after _trace after_transcribe")

            debug_runtime(f"before empty-text check  text_len={len(text)}")
            if not text:
                _trace("reply_worker/empty_text", "transcription returned empty — skipping")
                continue
            debug_runtime("after empty-text check — text is non-empty")

            debug_runtime("before is_meaningful")
            if not is_meaningful(text):
                _trace("reply_worker/not_meaningful", f"filtered: {text[:60]!r}")
                continue
            debug_runtime("after is_meaningful — text passed filter")

            debug_runtime("before time.strftime")
            ts      = time.strftime("%H:%M:%S")
            debug_runtime(f"after time.strftime  ts={ts}")

            debug_runtime(f"before _infer_speaker  SPEAKER_MODULE_LOADED={_SPEAKER_MODULE_LOADED}")
            speaker = _infer_speaker(lang, text)
            debug_runtime(f"after _infer_speaker  speaker={speaker!r}")

            debug_runtime("before _get_state")
            state_v = _get_state().value
            debug_runtime(f"after _get_state  state_v={state_v!r}")

            # ── Safe TTS interrupt ────────────────────────────────────────
            debug_runtime(f"before _ACTIVE_TTS.is_speaking  tts_type={type(_ACTIVE_TTS).__name__}")
            _tts_is_speaking = _ACTIVE_TTS.is_speaking()
            debug_runtime(f"after _ACTIVE_TTS.is_speaking  result={_tts_is_speaking}")
            if _tts_is_speaking:
                debug_runtime("before _tts_interrupt.set")
                _tts_interrupt.set()
                debug_runtime("after _tts_interrupt.set")

                debug_runtime("before _ACTIVE_TTS.stop")
                _ACTIVE_TTS.stop()
                debug_runtime("after _ACTIVE_TTS.stop")

                debug_runtime("before _set_state IDLE")
                _set_state(ConversationState.IDLE)
                debug_runtime("after _set_state IDLE")

                debug_runtime("before _tts_interrupt.clear")
                _tts_interrupt.clear()
                debug_runtime("after _tts_interrupt.clear")

                if _ROUTING_MODULE_LOADED:
                    debug_runtime("before playback_suppressor.stop")
                    playback_suppressor.stop()
                    debug_runtime("after playback_suppressor.stop")

            debug_runtime(f"show_heard  speaker={speaker}  lang={lang}  text={text[:60]!r}")
            show_heard(text, lang, ts)

            # ── FIX: build entry and persist OUTSIDE _log_lock ────────────
            # Previously: _persist_entry called while holding _log_lock.
            # If file I/O blocks (slow FS, NFS), the lock is held and
            # any thread trying to read transcript_log deadlocks.
            entry = LogEntry(text=text, lang=lang, ts=ts, speaker=speaker)
            with _log_lock:
                transcript_log.append(entry)
            _emit_event("transcript", text=text, lang=lang, ts=ts, speaker=speaker)
            # Persist outside lock — safe because _persist_entry has its own write lock
            _trace("reply_worker/persisting")
            _persist_entry(entry, state=state_v, latency_ms=t_stt * 1000)
            _trace("reply_worker/persisted")

            # ── Observer mode ─────────────────────────────────────────────
            if not agent_mode:
                fast: Optional[IntentResult] = _fast_path_check(text, lang)
                if fast is not None:
                    show_jp(fast.response)
                    show_sep()
                    continue

                if cognition_mode:
                    history = _build_conversation_history()
                    _trace("reply_worker/cognition_start")
                    ok = run_cognition_pipeline(text, lang, history)
                    _trace("reply_worker/cognition_done", f"ok={ok}")
                    if ok:
                        show_latency(t_stt * 1000, 0)
                        continue

                _trace("reply_worker/generate_reply")
                t_gpt_start = time.monotonic()
                if _get_runtime_mode() == RuntimeMode.MEETING:
                    _set_runtime_mode(RuntimeMode.INTERVIEW)
                    generate_meeting_analysis()
                else:
                    generate_reply(text, lang)
                t_gpt = time.monotonic() - t_gpt_start
                show_latency(t_stt * 1000, t_gpt * 1000)
                if latency_tracker:
                    latency_tracker.record(t_stt * 1000, t_gpt * 1000)
                _trace("reply_worker/observer_done")
                continue

            # ── Agent mode ────────────────────────────────────────────────
            if speaker == "user":
                _set_state(ConversationState.USER_SPEAKING)
                _trace("reply_worker/user_path")
                t_gpt_start = time.monotonic()
                if _get_runtime_mode() == RuntimeMode.MEETING:
                    _set_runtime_mode(RuntimeMode.INTERVIEW)
                    generate_meeting_analysis()
                else:
                    generate_reply(text, lang)
                t_gpt = time.monotonic() - t_gpt_start
                show_latency(t_stt * 1000, t_gpt * 1000)
                if latency_tracker:
                    latency_tracker.record(t_stt * 1000, t_gpt * 1000)
                _set_state(ConversationState.IDLE)
                continue

            _set_state(ConversationState.RECRUITER_SPEAKING)
            _trace("reply_worker/recruiter_path")

            fast = _fast_path_check(text, lang)
            if fast is not None:
                show_jp(fast.response)
                show_sep()
                _set_state(ConversationState.IDLE)
                continue

            if cognition_mode:
                _set_state(ConversationState.GENERATING)
                history = _build_conversation_history()
                _trace("reply_worker/cognition_agent_start")
                ok = run_cognition_pipeline(text, lang, history)
                _trace("reply_worker/cognition_agent_done", f"ok={ok}")
                if ok:
                    show_latency(t_stt * 1000, 0)
                    show_sep()
                    _set_state(ConversationState.IDLE)
                    continue
                show_warn("[cognition] pipeline failed — falling back to agent reply")
                _set_state(ConversationState.IDLE)

            if not is_question(text, lang):
                _set_state(ConversationState.IDLE)
                t_gpt_start = time.monotonic()
                if _get_runtime_mode() == RuntimeMode.MEETING:
                    _set_runtime_mode(RuntimeMode.INTERVIEW)
                    generate_meeting_analysis()
                else:
                    generate_reply(text, lang)
                t_gpt = time.monotonic() - t_gpt_start
                show_latency(t_stt * 1000, t_gpt * 1000)
                if latency_tracker:
                    latency_tracker.record(t_stt * 1000, t_gpt * 1000)
                continue

            _set_state(ConversationState.WAITING_FOR_REPLY)
            _set_state(ConversationState.GENERATING)

            if _get_runtime_mode() == RuntimeMode.MEETING:
                _set_runtime_mode(RuntimeMode.INTERVIEW)
                t_gpt_start = time.monotonic()
                generate_meeting_analysis()
                t_gpt = time.monotonic() - t_gpt_start
                show_latency(t_stt * 1000, t_gpt * 1000)
                if latency_tracker:
                    latency_tracker.record(t_stt * 1000, t_gpt * 1000)
                show_sep()
                _set_state(ConversationState.IDLE)
                continue

            _trace("reply_worker/agent_reply_start")
            debug_runtime(f"entering generate_agent_reply  speaker={speaker}  lang={lang}")
            t_gpt_start = time.monotonic()
            response    = generate_agent_reply(text, lang)
            t_gpt       = time.monotonic() - t_gpt_start
            debug_runtime(f"generate_agent_reply returned  ms={t_gpt*1000:.0f}  response={'None' if response is None else repr(response[:60])}")
            _trace("reply_worker/agent_reply_done", f"response_len={len(response or '')}")

            if response:
                show_agent_reply(response)
                show_latency(t_stt * 1000, t_gpt * 1000)
                if latency_tracker:
                    latency_tracker.record(t_stt * 1000, t_gpt * 1000)

                agent_ts    = time.strftime("%H:%M:%S")
                agent_entry = LogEntry(text=response, lang="english", ts=agent_ts, speaker="agent")
                with _log_lock:
                    transcript_log.append(agent_entry)
                _emit_event("reply", text=response, lang="en", speaker="agent", ts=agent_ts, provider=_selected_provider)
                _persist_entry(
                    agent_entry,
                    state      = ConversationState.GENERATING.value,
                    latency_ms = (t_stt + t_gpt) * 1000,
                )

                if not isinstance(_ACTIVE_TTS, _NullTTSProvider):
                    _set_state(ConversationState.SPEAKING)
                    _tts_interrupt.clear()
                    if _ROUTING_MODULE_LOADED:
                        playback_suppressor.start()
                    _ACTIVE_TTS.speak(response)
                    deadline = time.monotonic() + 10.0
                    while (_ACTIVE_TTS.is_speaking()
                           and time.monotonic() < deadline
                           and not _tts_interrupt.is_set()
                           and not _shutdown.is_set()):
                        time.sleep(0.05)
                    if _tts_interrupt.is_set():
                        _ACTIVE_TTS.stop()
                        show_info("[TTS] interrupted by operator speech")
                    _tts_interrupt.clear()
                    if _ROUTING_MODULE_LOADED:
                        playback_suppressor.stop()
            else:
                show_warn("Agent: no response generated — check GPT connectivity")

            show_sep()
            _set_state(ConversationState.IDLE)
            _trace("reply_worker/cycle_complete")

        except Exception as e:
            import traceback as _tb
            _trace("reply_worker/EXCEPTION", str(e))
            show_err("reply_worker", e)
            print("[ERROR] reply_worker unhandled exception:", e, flush=True)
            _tb.print_exc()
            _set_state(ConversationState.IDLE)
        finally:
            transcript_queue.task_done()


# ─────────────────────────────────────────────────────────────────────────────
# Audio capture
# ─────────────────────────────────────────────────────────────────────────────
def _audio_callback(indata, frames, time_info, status) -> None:
    global _audio_overflow_count
    if status:
        _audio_callback._last_status = str(status)
    try:
        audio_queue.put_nowait(indata.copy())
    except queue.Full:
        # Count and timestamp overflow — report safely in record_audio, not here
        with _audio_overflow_lock:
            _audio_overflow_count += 1
            _overflow_window.append(time.monotonic())

_audio_callback._last_status = None  # type: ignore[attr-defined]


def _record_audio_from_fd() -> None:
    """
    Audio-in path for Cloud Run deployments (--audio-source fd).

    Reads raw PCM16LE mono audio from the PHANTOM_AUDIO_FD pipe handed to
    this process by runtime.cloud_run_shell (relayed there from a remote
    client's WebSocket binary frames via runtime.transport_gateway). Blocks
    are rechunked to the exact same shape sounddevice's InputStream callback
    produces — (BLOCK_SIZE, CHANNELS) int16 — and pushed into the same
    audio_queue the VAD/reply_worker pipeline already consumes from. No
    other part of the pipeline changes.
    """
    global _audio_overflow_count

    fd_str = os.getenv("PHANTOM_AUDIO_FD", "").strip()
    if not fd_str:
        show_err("Audio", "PHANTOM_AUDIO_FD not set — cannot use --audio-source fd")
        _shutdown.set()
        return
    try:
        fd = int(fd_str)
    except ValueError:
        show_err("Audio", f"invalid PHANTOM_AUDIO_FD={fd_str!r}")
        _shutdown.set()
        return

    show_info(f"[audio] fd-source active (fd={fd}) — awaiting client audio stream")
    show_sep()

    block_bytes = BLOCK_SIZE * CHANNELS * 2   # int16 -> 2 bytes/sample
    buf = bytearray()

    while not _shutdown.is_set():
        try:
            chunk = os.read(fd, 65536)
        except OSError as e:
            show_err("Audio", f"fd read failed: {e}")
            break
        if not chunk:
            show_info("[audio] fd closed by transport — audio-in ended")
            break

        buf.extend(chunk)
        while len(buf) >= block_bytes:
            block = np.frombuffer(bytes(buf[:block_bytes]), dtype=np.int16).reshape(
                BLOCK_SIZE, CHANNELS
            )
            del buf[:block_bytes]
            try:
                audio_queue.put_nowait(block.copy())
            except queue.Full:
                with _audio_overflow_lock:
                    _audio_overflow_count += 1
                    _overflow_window.append(time.monotonic())


def record_audio() -> None:
    """
    [MODULE: audio.capture]
    v16: delegates to AudioCapture when available.
    Falls back to inline sounddevice InputStream if module not loaded.
    """
    if args.audio_source == "fd":
        _record_audio_from_fd()
        return

    if _CAPTURE_MODULE_LOADED:
        def _on_overflow(count: int, rate: float) -> None:
            show_warn(
                f"Audio overflow: {count} block(s) dropped  "
                f"rate≈{rate:.1f}/min — processing may be too slow"
            )
        def _on_open() -> None:
            show_info(
                f"Mic open  |  profile={_ACTIVE_PROFILE_NAME}  |  "
                f"lang={_effective_lang}  |  level={_effective_level}  |  "
                f"RMS={RMS_THRESHOLD}  |  whisper={args.whisper_model}  |  "
                f"gpt={args.gpt_model}"
                + ("  |  [READ] ON" if args.pronunciation else "")
            )
            show_sep()
        capture = _AudioCapture(
            sample_rate   = SAMPLE_RATE,
            channels      = CHANNELS,
            dtype         = DTYPE,
            block_size    = BLOCK_SIZE,
            rms_threshold = RMS_THRESHOLD,
            audio_queue   = audio_queue,
            device_id     = _INPUT_DEVICE_ID,
            device_name   = _INPUT_DEVICE_NAME,
            on_status     = show_warn,
            on_overflow   = _on_overflow,
            on_info       = show_info,
            on_open       = _on_open,
        )
        try:
            capture.run(_shutdown)
        except RuntimeError as e:
            show_err("Microphone", e)
            show_err("Microphone", "Check System Preferences → Privacy → Microphone.")
            _shutdown.set()
            sys.exit(1)
        return


# ─────────────────────────────────────────────────────────────────────────────
# VAD loop (main thread)
# ─────────────────────────────────────────────────────────────────────────────
def vad_loop() -> None:
    """
    [MODULE: audio.vad]
    v16: delegates to VADOrchestrator when available.
    The key design change: segment routing (manual vs autonomous) is now
    a callback parameter — VAD itself has no GPT dependency.
    Falls back to inline implementation if module not loaded.
    """
    # Routing callback: VAD calls this with each finalized audio segment
    def _route_segment(audio: np.ndarray) -> None:
        # debug-short-mode: hard-cap audio to 5s before routing
        if getattr(args, "debug_short_mode", False):
            max_s = SAMPLE_RATE * 5
            if len(audio) > max_s:
                audio = audio[:max_s]
        if args.manual_flush:
            if _vad_buf.recording_active.is_set():
                _vad_buf.append_segment(audio)
            # else: recording is paused — drop segment silently
        else:
            _enqueue_latest(audio)

    if _VAD_MODULE_LOADED:
        derived = {
            "min_samples":    MIN_SAMPLES,
            "max_samples":    MAX_SAMPLES,
            "silence_blocks": SILENCE_BLOCKS,
        }
        orchestrator = _VADOrchestrator(
            sample_rate       = SAMPLE_RATE,
            block_size        = BLOCK_SIZE,
            rms_threshold     = RMS_THRESHOLD,
            min_samples       = derived["min_samples"],
            max_samples       = derived["max_samples"],
            silence_blocks    = derived["silence_blocks"],
            pre_buffer_blocks = PRE_BUFFER_BLOCKS,
            audio_queue       = audio_queue,
            on_segment_ready  = _route_segment,
            on_info           = show_info,
            on_warn           = show_warn,
        )
        orchestrator.run(_shutdown)
        return


# ─────────────────────────────────────────────────────────────────────────────
# Memory Layer v1
# ─────────────────────────────────────────────────────────────────────────────
# Persistent JSON memory for Meeting Analysis quality improvement.
# Stores: Rolling Summary (max 20), Question Memory (max 100), Decision Memory (max 100)
# No external DB. Auto-created at startup. Backward-compatible.

MEMORY_DIR            = os.path.join(_SCRIPT_DIR, "memory")
_MEMORY_SUMMARY_PATH  = os.path.join(MEMORY_DIR, "rolling_summary.json")
_MEMORY_QUESTION_PATH = os.path.join(MEMORY_DIR, "question_memory.json")
_MEMORY_DECISION_PATH = os.path.join(MEMORY_DIR, "decision_memory.json")
_MEMORY_SUBJECT_PATH  = os.path.join(MEMORY_DIR, "subject_registry.json")  # v1.3
_MEMORY_FACT_PATH     = os.path.join(MEMORY_DIR, "fact_memory.json")        # v1.3
_MERGE_HISTORY_PATH   = os.path.join(MEMORY_DIR, "merge_history.json")
_MEMORY_APPROVAL_PATH = os.path.join(MEMORY_DIR, "merge_approval.json")
_MEMORY_OWNER_PATH            = os.path.join(MEMORY_DIR, "owner_registry.json")          # v2.0
_MEMORY_QUESTION_CLUSTER_PATH = os.path.join(MEMORY_DIR, "question_cluster_memory.json") # A23-3

_MEMORY_MAX_SUMMARIES  = 20
_MEMORY_MAX_QUESTIONS  = 100
_MEMORY_MAX_DECISIONS  = 100
_MAX_QUESTION_CLUSTERS = 1000

_QUESTION_VALID_STATUSES     = {"OPEN", "ANSWERED"}
_QUESTION_TERMINAL_STATUSES  = frozenset({"ANSWERED"})
_QUESTION_RESOLUTION_SOURCES = frozenset({"answer", "fact", "decision"})

AUTO_MERGE_ENABLED              = False
AUTO_MERGE_CONFIDENCE_THRESHOLD = 95
AUTO_MERGE_EXECUTE_APPROVED     = False

_memory_lock = threading.Lock()

# S2 persistence backend selector (Contract C-5).  Default 'json' preserves 100%
# of the current behavior; 'postgres' routes the four persistence primitives to
# the dedicated persistence_pg module (imported lazily — see dispatchers below).
_PERSISTENCE_BACKEND = os.getenv("PHANTOM_PERSISTENCE_BACKEND", "json").strip().lower()

# ── v1.3: Record ID Framework ─────────────────────────────────────────────────

import uuid     as _uuid
import hashlib  as _hashlib


def _make_record_id(prefix: str) -> str:
    """Generate a unique record ID: '<prefix>-<8 hex chars>'."""
    return f"{prefix}-{_uuid.uuid4().hex[:8]}"


def _make_legacy_id(prefix: str, raw: str) -> str:
    """Deterministic ID for migrating v1.2a records that lack an id.

    Same input always produces the same output, making migration idempotent.
    """
    return f"{prefix}-{_hashlib.md5(raw.encode()).hexdigest()[:8]}"


_DECISION_KEYWORDS_RE = re.compile(
    r"導入|実施|対応|採用|決定|行う|進める|確認|実行|"
    r"検討|提供|通知|手配|依頼|報告|"
    r"次回.{0,10}までに"
)

# A19: confidence-based save gate — minimum score required to persist a decision
_DECISION_MIN_CONFIDENCE: float = 0.5

# A19: owner extraction — first matched person wins.
# Multi-owner extraction is not supported. Planned for A20 Owner Intelligence.
_OWNER_RE = re.compile(
    r'([^\s　、。・！？\n]{1,8}?)さん(?:が|に|へ|は|も)?'
    r'|([^\s　、。・！？\n]{1,8}?)(?:が担当|が対応|が確認|が実施)'
)

# A21-1: Due Date Extraction Layer — category-separated patterns

_DUE_ABS_JP = re.compile(
    r'\d{4}[/\-]\d{1,2}[/\-]\d{1,2}'   # 2026/06/30, 2026-06-30
    r'|\d{1,2}月\d{1,2}日'              # 6月30日
)

_DUE_REL_JP = re.compile(
    r'来週(?:月曜日?|火曜日?|水曜日?|木曜日?|金曜日?|土曜日?|日曜日?|中|末)?'
    r'|今週(?:中|末)?'
    r'|今月(?:末|中)?'
    r'|来月(?:末|\d+日)?'
    r'|[月火水木金土日]曜日?まで(?:に)?'
    r'|明日|明後日|今日中|当日中'
)

_DUE_ABS_EN = re.compile(
    r'\b(?:January|February|March|April|May|June|July|August|September|October|November|December'
    r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)'
    r'\s+\d{1,2}(?:,\s*\d{4})?'
    r'|\b\d{4}-\d{2}-\d{2}\b',
    re.IGNORECASE,
)

_DUE_REL_EN = re.compile(
    r'\bby\s+(?:next\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b'
    r'|\bby\s+(?:the\s+)?(?:end\s+of\s+)?next\s+week\b'
    r'|\bby\s+end\s+of\s+(?:this\s+)?month\b'
    r'|\bbefore\s+(?:(?:January|February|March|April|May|June|July|August|September|October|November|December'
    r'|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}(?:,\s*\d{4})?|\d{4}-\d{2}-\d{2})'
    r'|\bdue\s+(?:on|by)\s+\S+',
    re.IGNORECASE,
)

_DUE_UNRESOLVED = re.compile(
    r'次回.{0,10}まで(?:に)?'
    r'|(?<![a-zA-Z])ASAP(?![a-zA-Z])'  # \b fails when adjacent to Japanese (hiragana is \w in Python3)
    r'|\bas\s+soon\s+as\s+possible\b'
    r'|できるだけ早く|なるべく早く',
    re.IGNORECASE,
)

# A21-2: lookup tables for _normalize_due_date()
_DUE_MONTH_MAP: dict = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
_DUE_WEEKDAY_JP: dict = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}
_DUE_WEEKDAY_EN: dict = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _memory_load(path: str, migrate_prefix: str = "") -> list:
    """Load a JSON array from file. Returns [] on missing or error.

    When migrate_prefix is set, any record missing an 'id' is migrated
    in-place via _memory_migrate_record() and the file is written back.
    Existing call sites pass no prefix and are unaffected.
    """
    if _PERSISTENCE_BACKEND == "postgres":
        import persistence_pg  # lazy: postgres backend only
        return persistence_pg.load_entries(persistence_pg.store_name(path))
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = _json.load(f)
        if not isinstance(data, list):
            return []
    except (FileNotFoundError, _json.JSONDecodeError):
        return []

    if not migrate_prefix:
        return data

    changed = False
    for entry in data:
        if isinstance(entry, dict) and "id" not in entry:
            _memory_migrate_record(entry, migrate_prefix)
            changed = True
    if changed:
        _memory_save_file(path, data)
    return data


def _memory_save_file(path: str, data: list) -> None:
    """Save a list to JSON file. Never raises."""
    if _PERSISTENCE_BACKEND == "postgres":
        import persistence_pg  # lazy: postgres backend only
        persistence_pg.save_entries(persistence_pg.store_name(path), data)
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        show_warn(f"[memory] write error {os.path.basename(path)}: {e}")


# ── v1.3: Migration Layer ─────────────────────────────────────────────────────

def _memory_migrate_record(entry: dict, prefix: str) -> dict:
    """Upgrade a v1.2a record to v1.3 schema in-place. Idempotent.

    Called lazily by _memory_load() when migrate_prefix is set.
    Only runs when 'id' is absent — records already migrated are skipped.
    """
    raw = (
        entry.get("timestamp", "")
        + entry.get("decision", "")
        + entry.get("question", "")
    )
    entry["id"]             = _make_legacy_id(prefix, raw)
    entry["schema_version"] = "1.2a"

    if prefix == "d":
        if "subject_id" not in entry:
            entry["subject_id"] = None
        if "resolved_by_fact" not in entry:
            entry["resolved_by_fact"] = None
        if "owner_id" not in entry:
            entry["owner_id"] = None
        if "due_type" not in entry:          # A21-3: backward-compat for pre-1.5 records
            entry["due_type"] = None
        if "due_confidence" not in entry:    # A21-3: backward-compat for pre-1.5 records
            entry["due_confidence"] = None
        if "history" not in entry:
            entry["history"] = [{
                "at":        entry.get("timestamp", ""),
                "field":     "status",
                "old_value": None,
                "new_value": entry.get("status", "OPEN"),
                "trigger":   "legacy_migration",
                "confidence": 1.0,
            }]

    if prefix == "q":
        if "subject_id" not in entry:
            entry["subject_id"] = None
        if "expected_fact_types" not in entry:
            entry["expected_fact_types"] = []
        if "resolved_by_fact"     not in entry:
            entry["resolved_by_fact"]     = None
        if "resolved_by_decision" not in entry:
            entry["resolved_by_decision"] = None

    return entry


def memory_init() -> None:
    """Create memory/ directory and initialize empty JSON files if missing."""
    try:
        os.makedirs(MEMORY_DIR, exist_ok=True)
        for path in (
            _MEMORY_SUMMARY_PATH, _MEMORY_QUESTION_PATH, _MEMORY_DECISION_PATH,
            _MEMORY_SUBJECT_PATH, _MEMORY_FACT_PATH,
            _MEMORY_QUESTION_CLUSTER_PATH,
        ):
            if not os.path.exists(path):
                _memory_save_file(path, [])
        if not os.path.exists(_MEMORY_OWNER_PATH):
            _owner_registry_save({})
        _owner_migrate_decisions()
        show_info("[memory] initialized")
    except Exception as e:
        show_warn(f"[memory] init error: {e}")


# ── v1.3: Fact Memory ────────────────────────────────────────────────────────

def _fact_load() -> list:
    """Raw load of fact_memory.json. Callers must hold _memory_lock."""
    return _memory_load(_MEMORY_FACT_PATH)


def _fact_save(facts: list) -> None:
    """Raw save of fact_memory.json. Callers must hold _memory_lock."""
    _memory_save_file(_MEMORY_FACT_PATH, facts)


def _fact_find(subject_id: str, fact_type: str) -> Optional[dict]:
    """Return the first Fact matching subject_id and fact_type, or None.
    Exact match only. Acquires _memory_lock.
    """
    if not subject_id or not fact_type:
        return None
    with _memory_lock:
        facts = _fact_load()
        for f in facts:
            if f.get("subject_id") == subject_id and f.get("fact_type") == fact_type:
                return f
    return None


def _fact_create(
    subject_id: str,
    fact_type:  str,
    value:      str,
    confidence: float = 1.0,
    source:     str   = "",
) -> str:
    """Create and persist a new Fact record. Returns the new fact id.
    Returns "" if subject_id or fact_type is empty.
    """
    if not subject_id:
        subject_name = _subject_extract(value)
        if subject_name:
            subject_id = _subject_get_or_create(subject_name)

    if not subject_id or not fact_type:
        return ""
    now    = time.strftime("%Y-%m-%dT%H:%M:%S")
    new_id = _make_record_id("f")
    record = {
        "id":             new_id,
        "subject_id":     subject_id,
        "fact_type":      fact_type,
        "value":          value,
        "confidence":     round(float(confidence), 4),
        "source":         source,
        "timestamp":      now,
        "schema_version": "1.3",
    }
    with _memory_lock:
        facts = _fact_load()
        facts.append(record)
        _fact_save(facts)
    _fact_resolve_links(subject_id, new_id, fact_type, value)
    _question_resolve_links(
        subject_id,
        new_id,
        fact_type,
        value,
    )
    return new_id


def _fact_migrate_subject(source_subject_id: str, target_subject_id: str) -> int:
    """Reassign all Facts belonging to source_subject_id to target_subject_id.

    Scans fact_memory.json and updates fact["subject_id"] for every matching entry.
    Saves only if at least one Fact was modified.
    No changes are made to question_memory.json or decision_memory.json.

    Returns the number of Facts migrated (0 if none matched).
    """
    if not source_subject_id or not target_subject_id:
        return 0
    with _memory_lock:
        facts = _fact_load()
        count = 0
        for fact in facts:
            if not isinstance(fact, dict):
                continue
            if fact.get("subject_id") == source_subject_id:
                fact["subject_id"] = target_subject_id
                count += 1
        if count > 0:
            _fact_save(facts)
    return count


# ── v1.3: Subject Registry ───────────────────────────────────────────────────

def _subject_load() -> list:
    """Raw load of subject_registry.json. Callers must hold _memory_lock."""
    return _memory_load(_MEMORY_SUBJECT_PATH)


def _subject_save(subjects: list) -> None:
    """Raw save of subject_registry.json. Callers must hold _memory_lock."""
    _memory_save_file(_MEMORY_SUBJECT_PATH, subjects)


_subject_registry_save = _subject_save


def _subject_find(name: str) -> Optional[dict]:
    """Return the Subject record with matching canonical_name, or None.
    Exact match only. Acquires _memory_lock.
    """
    if not name or not name.strip():
        return None
    with _memory_lock:
        subjects = _subject_load()
        for s in subjects:
            if s.get("canonical_name", "") == name.strip():
                return s
    return None


def _subject_create(name: str, confidence: float = 1.0) -> str:
    """Create and persist a new Subject record. Returns the new subject id.
    Returns "" if name is empty or blank.
    """
    if not name or not name.strip():
        return ""
    now    = time.strftime("%Y-%m-%dT%H:%M:%S")
    new_id = _make_record_id("s")
    record = {
        "id":             new_id,
        "canonical_name": name.strip(),
        "aliases":        [],
        "confidence":     round(float(confidence), 4),
        "first_seen_at":  now,
        "last_seen_at":   now,
        "mention_count":  1,
        "schema_version": "1.3",
    }
    with _memory_lock:
        subjects = _subject_load()
        subjects.append(record)
        _subject_save(subjects)
    return new_id


_SUBJECT_RE = re.compile(
    r'[【\[(（]([^】\]）\)]+)[】\]）\)]'
)


def _subject_extract(text: str) -> Optional[str]:
    """Return the first bracketed token from text as a Subject name.
    Brackets matched: 【】  []  ()  （）
    Returns stripped inner content, or None if no match.
    """
    if not text:
        return None
    m = _SUBJECT_RE.search(text)
    if not m:
        return None
    name = m.group(1).strip()
    return name if name else None


def _subject_get_or_create(name: str) -> str:
    """Return subject_id for name. Creates the Subject if not found. Returns '' if name empty."""
    if not name or not name.strip():
        return ""
    existing = _subject_find(name)
    if existing:
        return existing["id"]
    return _subject_create(name)


def _subject_merge_execute(source_subject_id: str, target_subject_id: str) -> bool:
    """Mark source subject as merged into target in subject_registry.json.

    Adds merged_into and merged_at to the source record and saves.
    No modifications to fact_memory.json, question_memory.json, or decision_memory.json.

    Returns True on success, False if source_subject_id not found.
    """
    if not source_subject_id or not target_subject_id:
        return False
    with _memory_lock:
        subjects = _subject_load()
        for entry in subjects:
            if not isinstance(entry, dict):
                continue
            if entry.get("id") == source_subject_id:
                entry["merged_into"] = target_subject_id
                entry["merged_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                _subject_save(subjects)
                return True
    return False


# ── v2.0: Owner Registry ──────────────────────────────────────────────────────

# Suffixes stripped when normalizing an owner name to its canonical base.
# Processed longest-first to prevent partial matches (e.g. "マネージャー" before "ー").
_OWNER_STRIP_SUFFIXES: list[str] = [
    "マネージャー", "リーダー",
    "ちゃん", "副部長", "副課長",
    "部長", "課長", "次長", "係長", "主任", "専務", "常務", "社長", "代表",
    "さん", "氏", "君",
]

# Honorific suffixes auto-appended as aliases when a new owner is created.
_OWNER_HONORIFIC_SUFFIXES: list[str] = ["さん", "氏", "君", "ちゃん"]


def _owner_strip_suffix(name: str) -> str:
    """Return the base name with the first matching honorific/title suffix removed."""
    for suffix in _OWNER_STRIP_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _owner_registry_load() -> dict:
    """Raw load of owner_registry.json. Returns {} on missing or error."""
    if _PERSISTENCE_BACKEND == "postgres":
        import persistence_pg  # lazy: postgres backend only
        return persistence_pg.load_document("owner_registry")
    try:
        with open(_MEMORY_OWNER_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


def _owner_registry_save(registry: dict) -> None:
    """Raw save of owner_registry.json. Never raises."""
    if _PERSISTENCE_BACKEND == "postgres":
        import persistence_pg  # lazy: postgres backend only
        persistence_pg.save_document("owner_registry", registry)
        return
    try:
        with open(_MEMORY_OWNER_PATH, "w", encoding="utf-8") as f:
            _json.dump(registry, f, ensure_ascii=False, indent=2)
    except Exception as e:
        show_warn(f"[memory] write error owner_registry.json: {e}")


def _owner_next_id(registry: dict) -> str:
    """Generate the next sequential owner_id.

    Scans existing keys for 'owner_NNN' patterns and returns
    max(N)+1 formatted as 'owner_NNN'.  Falls back to 'owner_001'
    when the registry is empty or contains no matching keys.
    """
    nums: list[int] = []
    for key in registry:
        m = re.match(r"owner_(\d+)$", key)
        if m:
            nums.append(int(m.group(1)))
    return f"owner_{(max(nums) + 1):03d}" if nums else "owner_001"


def _owner_alias_match(name: str, registry: dict) -> Optional[str]:
    """Return owner_id if name matches any owner's canonical name, alias, or
    suffix-stripped form.  Returns None when not found.

    Matching order:
      1. Exact match on owner['name']
      2. Exact match in owner['aliases']
      3. Suffix-stripped name matches owner['name']
    """
    if not name:
        return None
    name = name.strip()
    base = _owner_strip_suffix(name)
    for owner_id, owner in registry.items():
        canonical = owner.get("name", "")
        if canonical == name or canonical == base:
            return owner_id
        if name in owner.get("aliases", []):
            return owner_id
    return None


def _owner_get_or_create_nolock(name: str, registry: dict) -> str:
    """Return owner_id for name, mutating registry if a new owner must be created.

    Caller must hold _memory_lock and call _owner_registry_save() when done.
    Returns '' when name is empty.
    """
    if not name or not name.strip():
        return ""
    name = name.strip()
    owner_id = _owner_alias_match(name, registry)
    if owner_id:
        return owner_id
    base     = _owner_strip_suffix(name)
    now      = time.strftime("%Y-%m-%dT%H:%M:%S")
    owner_id = _owner_next_id(registry)
    aliases  = [base + s for s in _OWNER_HONORIFIC_SUFFIXES if base + s != name]
    registry[owner_id] = {
        "owner_id":   owner_id,
        "name":       base,
        "aliases":    aliases,
        "created_at": now,
        "updated_at": now,
    }
    return owner_id


def _owner_normalize(name: str) -> Optional[str]:
    """Return owner_id for name if it exists in the registry, else None.
    Acquires _memory_lock.
    """
    if not name or not name.strip():
        return None
    with _memory_lock:
        registry = _owner_registry_load()
    return _owner_alias_match(name.strip(), registry)


def _owner_get_or_create(name: str) -> str:
    """Return owner_id for name. Creates a new owner record if not found.
    Returns '' when name is empty.  Acquires _memory_lock.
    """
    if not name or not name.strip():
        return ""
    with _memory_lock:
        registry = _owner_registry_load()
        owner_id = _owner_get_or_create_nolock(name.strip(), registry)
        _owner_registry_save(registry)
    return owner_id


def _owner_migrate_decisions() -> int:
    """Backfill owner_id on decisions that have an owner but no owner_id.

    Idempotent: decisions already carrying owner_id are skipped.
    Saves owner_registry.json and decision_memory.json only when changes occur.
    Returns the number of decisions updated.
    """
    with _memory_lock:
        decisions = _decision_load()
        registry  = _owner_registry_load()
        count     = 0
        for dec in decisions:
            if not isinstance(dec, dict):
                continue
            owner = dec.get("owner")
            if not owner:
                continue
            if dec.get("owner_id"):
                continue
            owner_id = _owner_get_or_create_nolock(owner, registry)
            dec["owner_id"] = owner_id
            count += 1
        if count > 0:
            _owner_registry_save(registry)
            _decision_save(decisions)
    if count:
        show_info(f"[memory] owner migration: {count} decisions updated")
    return count


# ── Merge History ─────────────────────────────────────────────────────────────

def _merge_history_load() -> list:
    """Load merge_history.json. Returns [] on missing or error. Callers must hold _memory_lock."""
    return _memory_load(_MERGE_HISTORY_PATH)


def _merge_history_save(records: list) -> None:
    """Save records to merge_history.json. Callers must hold _memory_lock."""
    _memory_save_file(_MERGE_HISTORY_PATH, records)


def _merge_history_append(
    source_subject_id: str,
    source_name: str,
    target_subject_id: str,
    target_name: str,
    confidence: int,
    recommendation: str,
) -> str:
    """Append one merge audit record and return its history_id."""
    history_id = _make_record_id("mh")
    record = {
        "id":                history_id,
        "source_subject_id": source_subject_id,
        "source_name":       source_name,
        "target_subject_id": target_subject_id,
        "target_name":       target_name,
        "confidence":        confidence,
        "recommendation":    recommendation,
        "merged_at":         time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    with _memory_lock:
        records = _merge_history_load()
        records.append(record)
        _merge_history_save(records)
    return history_id


def _merge_history_recent(limit: int = 10) -> list:
    """Return the most recent merge history records sorted by merged_at descending."""
    with _memory_lock:
        records = _merge_history_load()
    records.sort(key=lambda r: r.get("merged_at", ""), reverse=True)
    return records[:limit]


def _merge_history_stats() -> dict:
    """Return aggregate statistics for merge_history.json."""
    with _memory_lock:
        records = _merge_history_load()
    total   = len(records)
    approve = sum(1 for r in records if r.get("recommendation") == "APPROVE")
    review  = sum(1 for r in records if r.get("recommendation") == "REVIEW")
    return {
        "total_merges":   total,
        "approve_merges": approve,
        "review_merges":  review,
    }


# ── Merge Approval Queue ──────────────────────────────────────────────────────

def _merge_approval_load() -> list:
    """Load merge_approval.json. Returns [] on missing or error. Callers must hold _memory_lock."""
    return _memory_load(_MEMORY_APPROVAL_PATH)


def _merge_approval_save(records: list) -> None:
    """Save records to merge_approval.json. Callers must hold _memory_lock."""
    _memory_save_file(_MEMORY_APPROVAL_PATH, records)


def _merge_approval_create(
    source_subject_id: str,
    source_name: str,
    target_subject_id: str,
    target_name: str,
    confidence: int,
    recommendation: str,
) -> str:
    """Create a PENDING approval record. Returns approval_id.

    If a PENDING record for the same (source_subject_id, target_subject_id)
    already exists, returns the existing approval_id without creating a duplicate.
    """
    with _memory_lock:
        records = _merge_approval_load()
        for r in records:
            if (
                r.get("source_subject_id") == source_subject_id
                and r.get("target_subject_id") == target_subject_id
                and r.get("status") == "PENDING"
            ):
                return r["id"]
        approval_id = _make_record_id("ma")
        records.append({
            "id":                approval_id,
            "source_subject_id": source_subject_id,
            "source_name":       source_name,
            "target_subject_id": target_subject_id,
            "target_name":       target_name,
            "confidence":        confidence,
            "recommendation":    recommendation,
            "status":            "PENDING",
            "created_at":        time.strftime("%Y-%m-%dT%H:%M:%S"),
            "approved_at":       "",
        })
        _merge_approval_save(records)
    return approval_id


def _merge_approval_pending() -> list:
    """Return all approval records with status == 'PENDING'."""
    with _memory_lock:
        records = _merge_approval_load()
    return [r for r in records if r.get("status") == "PENDING"]


def _merge_approval_execute(approval_id: str) -> dict:
    """Execute the merge for a PENDING approval record.

    Status transitions:
      PENDING -> RUNNING  (before transaction)
      RUNNING -> APPROVED (on success)
      RUNNING -> FAILED   (on failure or exception)

    Returns _subject_merge_transaction result on success, or {"success": False}
    if the approval is not found or is not PENDING.
    """
    with _memory_lock:
        records = _merge_approval_load()
        approval = next((r for r in records if r.get("id") == approval_id), None)
        if not approval or approval.get("status") != "PENDING":
            return {"success": False}
        approval["status"] = "RUNNING"
        _merge_approval_save(records)

    try:
        result = _subject_merge_transaction(
            approval["source_subject_id"],
            approval["target_subject_id"],
        )
        new_status = "APPROVED" if result.get("success") else "FAILED"
    except Exception as e:
        show_warn(f"[approval] transaction exception (approval_id={approval_id}): {e}")
        result = {"success": False}
        new_status = "FAILED"

    with _memory_lock:
        records = _merge_approval_load()
        for r in records:
            if r.get("id") == approval_id:
                r["status"] = new_status
                if new_status == "APPROVED":
                    r["approved_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
                break
        _merge_approval_save(records)

    return result


def _merge_approval_stats() -> dict:
    """Return pending/running/approved/failed counts for merge_approval.json."""
    with _memory_lock:
        records = _merge_approval_load()
    return {
        "pending":  sum(1 for r in records if r.get("status") == "PENDING"),
        "running":  sum(1 for r in records if r.get("status") == "RUNNING"),
        "approved": sum(1 for r in records if r.get("status") == "APPROVED"),
        "failed":   sum(1 for r in records if r.get("status") == "FAILED"),
    }


# ── Auto Merge Engine ─────────────────────────────────────────────────────────

def _subject_auto_merge_candidates() -> dict:
    """Detect APPROVE candidates and optionally enqueue / execute merges.

    AUTO_MERGE_ENABLED=False
        → statistics only; merge_approval.json unchanged.
    AUTO_MERGE_ENABLED=True
        → calls _merge_approval_create() for each eligible candidate.
    AUTO_MERGE_EXECUTE_APPROVED=True (requires AUTO_MERGE_ENABLED=True)
        → additionally calls _merge_approval_execute() for each created approval.

    _subject_merge_transaction() is never called directly from here.
    memory_build_context() does not call this function.

    Returns:
        {
            "enabled":  bool,
            "detected": int,   # total candidates from _subject_merge_candidates()
            "eligible": int,   # candidates passing confidence threshold
            "executed": int,   # successful _merge_approval_execute() calls
            "failed":   int,   # failed  _merge_approval_execute() calls
        }
    """
    candidates = _subject_merge_candidates()
    detected   = len(candidates)

    plan           = _subject_merge_plan()
    eligible_plans = [p for p in plan if p["confidence"] >= AUTO_MERGE_CONFIDENCE_THRESHOLD]
    eligible       = len(eligible_plans)

    if not AUTO_MERGE_ENABLED:
        return {
            "enabled":  False,
            "detected": detected,
            "eligible": eligible,
            "executed": 0,
            "failed":   0,
        }

    with _memory_lock:
        subjects = _subject_load()
    name_to_id = {
        s.get("canonical_name"): s["id"]
        for s in subjects
        if isinstance(s, dict) and s.get("id") and s.get("canonical_name")
    }

    executed = 0
    failed   = 0
    for p in eligible_plans:
        source_id = name_to_id.get(p["source"])
        target_id = name_to_id.get(p["target"])
        if not source_id or not target_id:
            continue
        if _subject_resolve_merge(source_id) != source_id:
            continue
        approval_id = _merge_approval_create(
            source_id, p["source"],
            target_id, p["target"],
            p["confidence"], p["recommendation"],
        )
        if AUTO_MERGE_EXECUTE_APPROVED:
            result = _merge_approval_execute(approval_id)
            if result.get("success"):
                executed += 1
            else:
                failed += 1

    return {
        "enabled":  True,
        "detected": detected,
        "eligible": eligible,
        "executed": executed,
        "failed":   failed,
    }


# ── Rolling Summary Memory ────────────────────────────────────────────────────

def memory_add_summary(summary: str) -> None:
    """Append a summary. Keeps at most _MEMORY_MAX_SUMMARIES entries."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_SUMMARY_PATH)
        entries.append({
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "summary":   summary,
        })
        if len(entries) > _MEMORY_MAX_SUMMARIES:
            entries = entries[-_MEMORY_MAX_SUMMARIES:]
        _memory_save_file(_MEMORY_SUMMARY_PATH, entries)
    show_info("[memory] summary saved")


def memory_get_recent_summaries(limit: int = 10) -> list:
    """Return the most recent `limit` summary entries."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_SUMMARY_PATH)
    return entries[-limit:] if entries else []


# ── Question Memory ───────────────────────────────────────────────────────────

def _question_status(q: dict) -> str:
    """Return question status; defaults to 'OPEN' for pre-A24 records."""
    s = q.get("status", "OPEN")
    return s if s in _QUESTION_VALID_STATUSES else "OPEN"


def _question_can_transition(current: str, new: str) -> bool:
    """Return True if the status transition current → new is valid.

    Same-status transition (e.g. ANSWERED → ANSWERED) is always harmless.
    Terminal statuses block any cross-status transition.
    """
    if current == new:
        return True
    if current not in _QUESTION_VALID_STATUSES:
        return False
    if current in _QUESTION_TERMINAL_STATUSES:
        return False
    if new not in _QUESTION_VALID_STATUSES:
        return False
    return True


def _question_load() -> list:
    """Raw load of question_memory.json with v1.3/v1.4 migration. Callers must hold _memory_lock."""
    entries = _memory_load(_MEMORY_QUESTION_PATH, migrate_prefix="q")
    changed = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if "cluster_id" not in entry:
            entry["cluster_id"]          = None
            entry["canonical_key"]       = None
            entry["language"]            = "unknown"
            entry["normalized_question"] = ""
            entry["similarity_score"]    = None
            changed = True
        if "status" not in entry:
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            if entry.get("resolved_by_fact"):
                initial_status = "ANSWERED"
                res_src        = "fact"
            elif entry.get("resolved_by_decision"):
                initial_status = "ANSWERED"
                res_src        = "decision"
            elif entry.get("answer") and str(entry["answer"]).strip():
                initial_status = "ANSWERED"
                res_src        = "answer"
            else:
                initial_status = "OPEN"
                res_src        = None
            entry["status"]            = initial_status
            entry["status_history"]    = [{"from": None, "to": initial_status,
                                           "at": now, "trigger": "migration"}]
            entry["answered_at"]       = None
            entry["updated_at"]        = now
            entry["resolution_source"] = res_src
            entry["schema_version"]    = "1.4"
            changed = True
        if "answer_link" not in entry:
            entry["answer_link"]    = _question_migrate_answer_link(entry)
            entry["schema_version"] = "1.5"
            changed = True
    if changed:
        _memory_save_file(_MEMORY_QUESTION_PATH, entries)
    return entries


def _question_save(questions: list) -> None:
    """Raw save of question_memory.json. Callers must hold _memory_lock."""
    _memory_save_file(_MEMORY_QUESTION_PATH, questions)


def _question_find(question: str) -> Optional[dict]:
    """Return the most recent Question record matching question text exactly, or None.
    Exact match only. Acquires _memory_lock.
    """
    if not question:
        return None
    with _memory_lock:
        entries = _question_load()
    for e in reversed(entries):
        if e.get("question") == question:
            return e
    return None


def _question_mark_answered(question_id: str, fact_id: str, answer_text: str = "") -> bool:
    """Record the resolving fact id on a Question record.

    Finds by id (exact). Appends a history entry if the history field is present.
    Acquires _memory_lock.
    Returns True if the record was found and updated, False otherwise.
    """
    if not question_id or not fact_id:
        return False
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _memory_lock:
        entries = _question_load()
        for entry in entries:
            if entry.get("id") == question_id:
                entry["resolved_by_fact"] = fact_id
                if _question_can_transition(entry.get("status", "OPEN"), "ANSWERED"):
                    prev = entry.get("status", "OPEN")
                    entry["status"]            = "ANSWERED"
                    entry["answered_at"]       = now
                    entry["updated_at"]        = now
                    entry["resolution_source"] = "fact"
                    entry["answer_link"]       = _question_build_answer_link(
                        "fact", fact_id, answer_text or None,
                    )
                    entry.setdefault("status_history", []).append({
                        "from": prev, "to": "ANSWERED", "at": now,
                        "trigger": "fact_resolution",
                    })
                entry.setdefault("history", []).append({
                    "at":        now,
                    "field":     "resolved_by_fact",
                    "old_value": None,
                    "new_value": fact_id,
                    "trigger":   "fact_resolution",
                    "confidence": 1.0,
                })
                _question_save(entries)
                return True
    return False


def _question_mark_answered_by_decision(question_id: str, decision_id: str, answer_text: str = "") -> bool:
    """Mark a Question ANSWERED via a resolving Decision id.

    ANSWERED → ANSWERED is treated as a harmless no-op and returns True.
    Acquires _memory_lock. Returns True if the record is (or becomes) ANSWERED.
    """
    if not question_id or not decision_id:
        return False
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _memory_lock:
        entries = _question_load()
        for entry in entries:
            if entry.get("id") == question_id:
                current = entry.get("status", "OPEN")
                if current == "ANSWERED":
                    return True
                if _question_can_transition(current, "ANSWERED"):
                    entry["status"]                = "ANSWERED"
                    entry["resolved_by_decision"]  = decision_id
                    entry["answered_at"]           = now
                    entry["updated_at"]            = now
                    entry["resolution_source"]     = "decision"
                    entry["answer_link"]           = _question_build_answer_link(
                        "decision", decision_id, None,
                    )
                    entry.setdefault("status_history", []).append({
                        "from": current, "to": "ANSWERED", "at": now,
                        "trigger": "decision_resolution",
                    })
                    _question_save(entries)
                    return True
    return False


def _question_mark_answered_by_answer(question_id: str) -> bool:
    """Mark a Question ANSWERED when its answer field is non-empty.

    Returns False immediately if the answer field is absent or blank.
    ANSWERED → ANSWERED is treated as a harmless no-op and returns True.
    Acquires _memory_lock. Returns True if the record is (or becomes) ANSWERED.
    """
    if not question_id:
        return False
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _memory_lock:
        entries = _question_load()
        for entry in entries:
            if entry.get("id") == question_id:
                if not (entry.get("answer") and str(entry["answer"]).strip()):
                    return False
                current = entry.get("status", "OPEN")
                if current == "ANSWERED":
                    return True
                if _question_can_transition(current, "ANSWERED"):
                    entry["status"]            = "ANSWERED"
                    entry["answered_at"]       = now
                    entry["updated_at"]        = now
                    entry["resolution_source"] = "answer"
                    entry["answer_link"]       = _question_build_answer_link(
                        "direct", None, str(entry["answer"]).strip(),
                    )
                    entry.setdefault("status_history", []).append({
                        "from": current, "to": "ANSWERED", "at": now,
                        "trigger": "answer_resolution",
                    })
                    _question_save(entries)
                    return True
    return False


def _question_migrate_subject(source_subject_id: str, target_subject_id: str) -> int:
    """Reassign all Questions belonging to source_subject_id to target_subject_id.

    Scans question_memory.json and updates question["subject_id"] for every matching entry.
    Saves only if at least one Question was modified.
    No changes are made to fact_memory.json or decision_memory.json.

    Returns the number of Questions migrated (0 if none matched).
    """
    if not source_subject_id or not target_subject_id:
        return 0
    with _memory_lock:
        questions = _question_load()
        count = 0
        for question in questions:
            if not isinstance(question, dict):
                continue
            if question.get("subject_id") == source_subject_id:
                question["subject_id"] = target_subject_id
                count += 1
        if count > 0:
            _question_save(questions)
    return count


def memory_add_question(question: str, answer: str) -> None:
    """Append a question/answer. Keeps at most _MEMORY_MAX_QUESTIONS entries."""
    subject_name = _subject_extract(question)
    subject_id = (
        _subject_get_or_create(subject_name)
        if subject_name
        else None
    )
    question_id  = _make_record_id("q")
    now          = time.strftime("%Y-%m-%dT%H:%M:%S")
    has_answer   = bool(answer and str(answer).strip())
    init_status  = "ANSWERED" if has_answer else "OPEN"
    init_res_src = "answer"   if has_answer else None
    with _memory_lock:
        entries = _question_load()
        entries.append({
            "id":                   question_id,
            "timestamp":            now,
            "question":             question,
            "answer":               answer,
            "subject_id":           subject_id,
            "expected_fact_types":  [],
            "resolved_by_fact":     None,
            "resolved_by_decision": None,
            "cluster_id":           None,
            "canonical_key":        None,
            "language":             "unknown",
            "normalized_question":  "",
            "similarity_score":     None,
            "status":               init_status,
            "status_history":       [{"from": None, "to": init_status,
                                      "at": now, "trigger": "created"}],
            "answered_at":          now if has_answer else None,
            "updated_at":           now,
            "resolution_source":    init_res_src,
            "answer_link":          None,
            "schema_version":       "1.5",
        })
        if len(entries) > _MEMORY_MAX_QUESTIONS:
            entries = entries[-_MEMORY_MAX_QUESTIONS:]
        _question_save(entries)
    show_info("[memory] question saved")
    _question_cluster_assign(question_id, question)


def memory_get_questions(limit: int = 20) -> list:
    """Return the most recent `limit` question entries."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_QUESTION_PATH)
    return entries[-limit:] if entries else []


def memory_get_open_questions(limit: int = 20) -> list:
    """Return the most recent `limit` OPEN question entries, ordered by timestamp descending."""
    with _memory_lock:
        entries = _question_load()
    open_entries = [e for e in entries if _question_status(e) == "OPEN"]
    open_entries.sort(key=lambda e: e.get("timestamp", ""), reverse=True)
    return open_entries[:limit]


def memory_get_answered_questions(limit: int = 20) -> list:
    """Return the most recent `limit` ANSWERED question entries, ordered by answered_at descending."""
    with _memory_lock:
        entries = _question_load()
    answered = [e for e in entries if _question_status(e) == "ANSWERED"]
    answered.sort(key=lambda e: e.get("answered_at") or "", reverse=True)
    return answered[:limit]


def memory_get_answer_link(question_id: str) -> Optional[dict]:
    """Return the AnswerLink dict for a Question, or None if not set or question not found."""
    with _memory_lock:
        entries = _question_load()
    for entry in entries:
        if entry.get("id") == question_id:
            return entry.get("answer_link")
    return None


def memory_get_answer_links(limit: int = 20) -> list:
    """Return ANSWERED Questions that have a non-None answer_link, sorted by linked_at DESC."""
    with _memory_lock:
        entries = _question_load()
    links = [q for q in entries if q.get("answer_link")]
    links.sort(
        key=lambda q: q["answer_link"].get("linked_at") or "",
        reverse=True,
    )
    return links[:limit]


def memory_get_answer_links_by_subject(subject_id: str, limit: int = 20) -> list:
    """Return ANSWERED Questions for subject_id that have a non-None answer_link, sorted by linked_at DESC."""
    if not subject_id:
        return []
    with _memory_lock:
        entries = _question_load()
    links = [
        q for q in entries
        if q.get("subject_id") == subject_id
        and q.get("answer_link")
    ]
    links.sort(
        key=lambda q: q["answer_link"].get("linked_at") or "",
        reverse=True,
    )
    return links[:limit]


def memory_question_exists(question: str) -> bool:
    """Return True if an identical question already exists in memory."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_QUESTION_PATH)
    ql = question.lower().strip()
    return any(e.get("question", "").lower().strip() == ql for e in entries)


# ── Semantic Question Similarity ──────────────────────────────────────────────

import difflib
import unicodedata as _unicodedata

# ── A23-2: Language Detection ─────────────────────────────────────────────────

def _detect_language(text: str) -> str:
    """Detect question language from character composition. stdlib only.

    Returns 'ja' if CJK characters exceed 20% of non-space chars,
    'en' if ASCII alpha chars exceed 40%, else 'unknown'.
    """
    chars = [c for c in text if not c.isspace()]
    if not chars:
        return "unknown"
    total = len(chars)
    cjk = sum(
        1 for c in chars
        if "　" <= c <= "鿿" or "豈" <= c <= "﫿"
    )
    ascii_alpha = sum(1 for c in chars if c.isascii() and c.isalpha())
    if cjk / total > 0.2:
        return "ja"
    if ascii_alpha / total > 0.4:
        return "en"
    return "unknown"


# ── A23-2: Canonical Dictionary ───────────────────────────────────────────────

# Language-agnostic surface-form → canonical token mapping.
# Supersedes the former _SYNONYM_MAP (JA-only). _apply_canonical_dict() applies
# longest-match first so multi-word EN phrases are matched before sub-tokens.
_CANONICAL_DICT: dict[str, str] = {
    # MFA family — JA
    "mfa":        "mfa",
    "2fa":        "mfa",
    "二段階認証": "mfa",
    "二要素認証": "mfa",
    "多要素認証": "mfa",
    # MFA family — EN (multi-word entries listed before sub-tokens)
    "two factor authentication":   "mfa",
    "two-factor authentication":   "mfa",
    "multi factor authentication": "mfa",
    "multi-factor authentication": "mfa",
    "multifactor authentication":  "mfa",
    # Auth — JA
    "パスワード管理": "auth_mgmt",
    "認証情報":       "auth_mgmt",
    # Auth — EN
    "password management":   "auth_mgmt",
    "credential management": "auth_mgmt",
    # VPN family — JA/EN shared canonical token
    "vpn":                     "vpn",
    "virtual private network": "vpn",
}

# Backward compatibility for pre-A23 normalization code paths.
_SYNONYM_MAP = _CANONICAL_DICT

# Set of canonical token values — used by _derive_canonical_key().
_CANONICAL_KEY_SET = frozenset(_CANONICAL_DICT.values())


def _apply_canonical_dict(text: str) -> str:
    """Replace known surface forms with their canonical token (longest-match)."""
    for src in sorted(_CANONICAL_DICT, key=len, reverse=True):
        text = text.replace(src, _CANONICAL_DICT[src])
    return text


# ── A23-2: Normalization Engine ───────────────────────────────────────────────

_EN_STOP_WORDS = frozenset({
    "a", "an", "the", "is", "are", "do", "does", "have", "has",
    "you", "your", "we", "our", "they", "it", "its", "use", "using",
})

_QUESTION_SIMILARITY_THRESHOLD    = 0.85
_QUESTION_CLUSTER_JACCARD_THRESHOLD = 0.60

# Auxiliary verb endings and common particles stripped for content comparison
_Q_STRIP = re.compile(
    r"(していますか|しましたか|されていますか|済みですか|でしょうか|ますか|ですか)"
    r"|[はがをにでのもとやへ]"
)


def _normalize_question_ja(text: str) -> str:
    """JA normalization: NFKC, lowercase, punct strip, particle/aux removal, canonical dict."""
    t = _unicodedata.normalize("NFKC", text).lower().strip()
    t = re.sub(r"[?？。！!、,，]+$", "", t).strip()
    t = _Q_STRIP.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = _apply_canonical_dict(t)
    return t


def _normalize_question_en(text: str) -> str:
    """EN normalization: lowercase, punct strip, stop-word removal, canonical dict."""
    t = text.lower().strip()
    t = re.sub(r"[?!.,;:'\"()\[\]{}<>]+", " ", t)
    t = _apply_canonical_dict(t)
    tokens = [tok for tok in t.split() if tok and tok not in _EN_STOP_WORDS]
    return " ".join(tokens)


def _normalize_question_multilingual(text: str, language: str) -> str:
    """Dispatch to language-specific normalizer. 'unknown' falls back to JA."""
    if language == "en":
        return _normalize_question_en(text)
    return _normalize_question_ja(text)


def _normalize_question(text: str) -> str:
    """Backward-compatible wrapper. Delegates to the multilingual pipeline."""
    lang = _detect_language(text)
    return _normalize_question_multilingual(text, lang)


def _content_question(text: str) -> str:
    """Return content-only form: normalized then strip particles/auxiliaries."""
    return _Q_STRIP.sub("", _normalize_question(text))


def memory_question_similar_exists(question: str) -> bool:
    """Return True if a semantically similar question already exists in memory.

    Checks exact match first, then difflib ratio on full string, then on
    content-only form (particles/auxiliaries stripped). Either channel hitting
    >= threshold is treated as a duplicate.
    """
    with _memory_lock:
        entries = _memory_load(_MEMORY_QUESTION_PATH)

    if not entries:
        return False

    nq = _normalize_question(question)
    cq = _content_question(question)

    for e in entries:
        existing = e.get("question", "")
        ne = _normalize_question(existing)

        if nq == ne:
            return True

        ratio_full    = difflib.SequenceMatcher(None, nq, ne).ratio()
        ratio_content = difflib.SequenceMatcher(None, cq, _content_question(existing)).ratio()

        best = max(ratio_full, ratio_content)
        if best >= _QUESTION_SIMILARITY_THRESHOLD:
            show_info(
                f'[memory] similar question detected ratio={best:.2f}'
                f' existing="{existing}" new="{question}"'
            )
            return True

    return False


# ── A23-3: Question Cluster Memory ───────────────────────────────────────────

def _make_question_cluster_id() -> str:
    """Generate a unique cluster ID: 'qc-<8 hex chars>'."""
    return f"qc-{_uuid.uuid4().hex[:8]}"


def _load_question_cluster_memory() -> list:
    """Load question_cluster_memory.json. Callers must hold _memory_lock."""
    return _memory_load(_MEMORY_QUESTION_CLUSTER_PATH)


def _save_question_cluster_memory(clusters: list) -> None:
    """Save question_cluster_memory.json. Callers must hold _memory_lock."""
    _memory_save_file(_MEMORY_QUESTION_CLUSTER_PATH, clusters)


# ── A23-4: Similarity Scoring Engine ─────────────────────────────────────────

def _token_jaccard(tokens_a: frozenset, tokens_b: frozenset) -> float:
    """Jaccard similarity over token sets. Returns 0.0 if either set is empty."""
    if not tokens_a or not tokens_b:
        return 0.0
    return len(tokens_a & tokens_b) / len(tokens_a | tokens_b)


def _derive_canonical_key(normalized: str) -> Optional[str]:
    """Return the first canonical token found in normalized text, or None.

    Single canonical key only; multi-key support is deferred to A26.
    """
    for token in normalized.split():
        if token in _CANONICAL_KEY_SET:
            return token
    return None


def _question_similarity_score(question_a: str, question_b: str) -> float:
    """Return similarity score 0.0–1.0 with four-level priority.

    P1 Exact normalized match  → 1.0
    P2 Canonical-key match     → 0.95
    P3 SequenceMatcher ≥ 0.85  → ratio
    P4 Token Jaccard  ≥ 0.60   → jaccard (min 2-token guard on both sides)
    """
    lang_a = _detect_language(question_a)
    lang_b = _detect_language(question_b)
    norm_a = _normalize_question_multilingual(question_a, lang_a)
    norm_b = _normalize_question_multilingual(question_b, lang_b)

    # P1: exact normalized match
    if norm_a == norm_b:
        return 1.0

    # P2: canonical-key match (cross-lingual safe; single key per A23-4)
    key_a = _derive_canonical_key(norm_a)
    key_b = _derive_canonical_key(norm_b)
    if key_a and key_b and key_a == key_b:
        return 0.95

    # P3: SequenceMatcher
    score_l2 = difflib.SequenceMatcher(None, norm_a, norm_b).ratio()
    if score_l2 >= _QUESTION_SIMILARITY_THRESHOLD:
        return score_l2

    # P4: Token Jaccard (min 2-token guard on both sides)
    tokens_a = frozenset(t for t in norm_a.split() if t)
    tokens_b = frozenset(t for t in norm_b.split() if t)
    if len(tokens_a) >= 2 and len(tokens_b) >= 2 and (tokens_a & tokens_b):
        score_l3 = _token_jaccard(tokens_a, tokens_b)
        if score_l3 >= _QUESTION_CLUSTER_JACCARD_THRESHOLD:
            return score_l3

    return score_l2


def _question_find_cluster(
    normalized: str,
    canon_key: Optional[str],
    clusters: list,
) -> tuple[Optional[dict], float]:
    """Find best matching cluster from a pre-loaded clusters list.

    Priority mirrors _question_similarity_score:
      P1 Exact canonical_normalized match → 1.0
      P2 canonical_key match              → 0.95
      P3 SequenceMatcher ≥ 0.85          → ratio
      P4 Token Jaccard  ≥ 0.60           → jaccard
    Callers must hold _memory_lock and pass the already-loaded clusters list.
    """
    # P1: exact canonical_normalized match
    for c in clusters:
        if normalized == c.get("canonical_normalized", ""):
            return c, 1.0

    # P2: canonical_key match (single key; cross-lingual safe)
    if canon_key:
        for c in clusters:
            if c.get("canonical_key") == canon_key:
                return c, 0.95

    # P3/P4: similarity with cluster_tokens pre-filter
    norm_tokens = frozenset(t for t in normalized.split() if t)
    best_cluster: Optional[dict] = None
    best_score = 0.0

    for c in clusters:
        c_tokens = frozenset(c.get("cluster_tokens", []))
        if c_tokens and not (norm_tokens & c_tokens):
            continue  # no shared tokens — skip

        c_norm = c.get("canonical_normalized", "")

        # P3: SequenceMatcher
        s = difflib.SequenceMatcher(None, normalized, c_norm).ratio()
        if s >= _QUESTION_SIMILARITY_THRESHOLD and s > best_score:
            best_cluster, best_score = c, s
            continue

        # P4: Token Jaccard
        c_norm_tokens = frozenset(t for t in c_norm.split() if t)
        if (len(norm_tokens) >= 2 and len(c_norm_tokens) >= 2
                and (norm_tokens & c_norm_tokens)):
            j = _token_jaccard(norm_tokens, c_norm_tokens)
            if j >= _QUESTION_CLUSTER_JACCARD_THRESHOLD and j > best_score:
                best_cluster, best_score = c, j

    return best_cluster, best_score


def _question_cluster_assign(question_id: str, question: str) -> None:
    """Assign question_id to a cluster; create a new cluster if none matches.

    Acquires _memory_lock. Updates question_memory.json and
    question_cluster_memory.json. Single canonical key only;
    multi-key support is deferred to A26.
    """
    lang       = _detect_language(question)
    normalized = _normalize_question_multilingual(question, lang)
    canon_key  = _derive_canonical_key(normalized)

    with _memory_lock:
        clusters       = _load_question_cluster_memory()
        matched, score = _question_find_cluster(normalized, canon_key, clusters)

        if matched is None:
            cluster_id = _make_question_cluster_id()
            # cluster_tokens fixed at creation time; future expansion deferred.
            cluster_tokens = list(frozenset(t for t in normalized.split() if t))
            new_cluster = {
                "id":                   cluster_id,
                "canonical_key":        canon_key,
                "canonical_question":   question,
                "canonical_normalized": normalized,
                "cluster_tokens":       cluster_tokens,
                "member_ids":           [question_id],
                "member_count":         1,
                "created_at":           time.strftime("%Y-%m-%dT%H:%M:%S"),
                "schema_version":       "1.0",
            }
            if len(clusters) >= _MAX_QUESTION_CLUSTERS:
                clusters = clusters[-(_MAX_QUESTION_CLUSTERS - 1):]
            clusters.append(new_cluster)
            matched = new_cluster
            score   = 1.0
        else:
            # member_ids grows unbounded; future size optimization deferred.
            if question_id not in matched["member_ids"]:
                matched["member_ids"].append(question_id)
                matched["member_count"] = len(matched["member_ids"])
        _save_question_cluster_memory(clusters)

        questions = _question_load()
        for q in questions:
            if q.get("id") == question_id:
                q["cluster_id"]          = matched["id"]
                q["canonical_key"]       = matched.get("canonical_key")
                q["language"]            = lang
                q["normalized_question"] = normalized
                q["similarity_score"]    = round(score, 4)
                break
        _question_save(questions)


# ── Decision Memory ───────────────────────────────────────────────────────────

_DECISION_VALID_STATUSES = {"OPEN", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED"}

_TERMINAL_STATUSES = frozenset({"DONE", "CANCELLED"})


def _decision_can_transition(current_status: str, new_status: str) -> bool:
    """Decision lifecycle transition guard.

    Terminal states (DONE, CANCELLED) cannot transition to any other state.
    Transitions to unknown statuses are rejected.

    Examples:
        OPEN -> IN_PROGRESS   True
        OPEN -> BLOCKED       True
        OPEN -> DONE          True
        BLOCKED -> OPEN       True
        DONE -> OPEN          False
        CANCELLED -> OPEN     False
    """
    if current_status not in _DECISION_VALID_STATUSES:
        return False
    if current_status in _TERMINAL_STATUSES:
        return False
    if new_status not in _DECISION_VALID_STATUSES:
        return False
    return True


def _decision_load() -> list:
    """Raw load of decision_memory.json with v1.3 migration. Callers must hold _memory_lock."""
    return _memory_load(_MEMORY_DECISION_PATH, migrate_prefix="d")


def _decision_save(decisions: list) -> None:
    """Raw save of decision_memory.json. Callers must hold _memory_lock."""
    _memory_save_file(_MEMORY_DECISION_PATH, decisions)


def _decision_find(decision: str) -> Optional[dict]:
    """Return the most recent Decision record matching decision text exactly, or None.
    Exact match only. Acquires _memory_lock.
    """
    if not decision:
        return None
    with _memory_lock:
        entries = _decision_load()
    for e in reversed(entries):
        if e.get("decision") == decision:
            return e
    return None


def _decision_mark_done(decision_id: str, fact_id: str) -> bool:
    """Set a Decision's status to DONE and record the resolving fact id.

    Finds by id (exact). Appends a history entry. Acquires _memory_lock.
    Returns True if the record was found and updated, False otherwise.
    """
    if not decision_id or not fact_id:
        return False
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    with _memory_lock:
        entries = _decision_load()
        for entry in entries:
            if entry.get("id") == decision_id:
                old_status = entry.get("status", "OPEN")
                entry["status"]           = "DONE"
                entry["resolved_by_fact"] = fact_id
                entry.setdefault("history", []).append({
                    "at":        now,
                    "field":     "status",
                    "old_value": old_status,
                    "new_value": "DONE",
                    "trigger":   "fact_resolution",
                    "confidence": 1.0,
                })
                _decision_save(entries)
                return True
    return False


def _decision_migrate_subject(source_subject_id: str, target_subject_id: str) -> int:
    """Reassign all Decisions belonging to source_subject_id to target_subject_id.

    Scans decision_memory.json and updates decision["subject_id"] for every matching entry.
    Saves only if at least one Decision was modified.
    No changes are made to fact_memory.json or question_memory.json.

    Returns the number of Decisions migrated (0 if none matched).
    """
    if not source_subject_id or not target_subject_id:
        return 0
    with _memory_lock:
        decisions = _decision_load()
        count = 0
        for decision in decisions:
            if not isinstance(decision, dict):
                continue
            if decision.get("subject_id") == source_subject_id:
                decision["subject_id"] = target_subject_id
                count += 1
        if count > 0:
            _decision_save(decisions)
    return count


def _decision_status(entry: dict) -> str:
    """Return the status of a decision entry; defaults to OPEN for legacy records."""
    s = entry.get("status", "OPEN")
    return s if s in _DECISION_VALID_STATUSES else "OPEN"


def _decision_update_status(decision: dict, new_status: str) -> bool:
    """Update a Decision record's status in-place with status_history tracking.

    Does not acquire _memory_lock or save to disk — callers handle persistence.
    Returns True if the status was changed, False otherwise.
    """
    current = _decision_status(decision)
    if current == new_status:
        return False
    if not _decision_can_transition(current, new_status):
        return False
    decision["status"] = new_status
    decision.setdefault("status_history", []).append({
        "from": current,
        "to":   new_status,
        "at":   time.strftime("%Y-%m-%dT%H:%M:%S"),
    })
    return True


def memory_add_decision(
    decision: str,
    owner: Optional[str] = None,
    due: Optional[str] = None,
    *,
    action: Optional[str] = None,
    due_date: Optional[str] = None,
    due_type: Optional[str] = None,
    due_confidence: Optional[float] = None,
    confidence: float = 0.0,
    source: str = "meeting_analysis",
) -> None:
    """Append a decision with status=OPEN. Keeps at most _MEMORY_MAX_DECISIONS entries."""
    subject_name = _subject_extract(decision)
    subject_id = (
        _subject_get_or_create(subject_name)
        if subject_name
        else None
    )
    owner_id = _owner_get_or_create(owner) if owner else None
    with _memory_lock:
        entries = _decision_load()
        entries.append({
            "id":               _make_record_id("d"),
            "timestamp":        time.strftime("%Y-%m-%dT%H:%M:%S"),
            "decision":         decision,
            "action":           action if action is not None else decision,
            "subject_id":       subject_id,
            "owner":            owner,
            "owner_id":         owner_id,
            "due":              due,
            "due_date":         due_date,
            "due_type":         due_type,
            "due_confidence":   due_confidence,
            "confidence":       confidence,
            "source":           source,
            "status":           "OPEN",
            "status_history":   [{"from": None, "to": "OPEN", "at": time.strftime("%Y-%m-%dT%H:%M:%S")}],
            "resolved_by_fact": None,
            "history":          [],
            "schema_version":   "1.5",
        })
        if len(entries) > _MEMORY_MAX_DECISIONS:
            entries = entries[-_MEMORY_MAX_DECISIONS:]
        _decision_save(entries)
    show_info("[memory] decision saved")


def memory_get_decisions(limit: int = 20) -> list:
    """Return the most recent `limit` decision entries (all statuses)."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_DECISION_PATH)
    return entries[-limit:] if entries else []


def memory_get_open_decisions(limit: int = 20) -> list:
    """Return the most recent `limit` OPEN decision entries."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_DECISION_PATH)
    open_entries = [e for e in entries if _decision_status(e) == "OPEN"]
    return open_entries[-limit:]


def memory_get_done_decisions(limit: int = 20) -> list:
    """Return the most recent `limit` DONE decision entries."""
    with _memory_lock:
        entries = _memory_load(_MEMORY_DECISION_PATH)
    done_entries = [e for e in entries if _decision_status(e) == "DONE"]
    return done_entries[-limit:]


def memory_update_decision_status(decision: str, status: str) -> None:
    """Find the most recent matching decision entry and update its status."""
    if status not in _DECISION_VALID_STATUSES:
        show_warn(f"[memory] invalid status '{status}' — must be one of {_DECISION_VALID_STATUSES}")
        return
    with _memory_lock:
        entries = _memory_load(_MEMORY_DECISION_PATH)
        dl = decision.lower().strip()
        updated = False
        for e in reversed(entries):
            if e.get("decision", "").lower().strip() == dl:
                e["status"] = status
                updated = True
                break
        if updated:
            _memory_save_file(_MEMORY_DECISION_PATH, entries)
            show_info(
                f'[memory] decision status updated decision="{decision}" status={status}'
            )
        else:
            show_warn(f'[memory] decision not found: "{decision}"')


# ── v1.3: Resolution Engine ──────────────────────────────────────────────────

_DECISION_RESOLUTION_FACT_TYPES = frozenset({"status"})

_QUESTION_RESOLUTION_FACT_TYPES = frozenset({
    "status",
    "result",
    "schedule",
    "options",
})
# Conservative whitelist for Questions without
# expected_fact_types.
# Expand only after production evidence.

_COMPLETION_VALUES = frozenset({
    "完了", "done", "DONE", "completed",
    "実施済み", "対応済み", "finished", "resolved",
})


def _fact_resolve_links(
    subject_id: str,
    fact_id:    str,
    fact_type:  str,
    value:      str,
) -> None:
    """Link existing open Decisions to a newly created Fact.

    Matching rules (Decision):
      1. decision.subject_id == fact.subject_id
      2. fact.fact_type in _DECISION_RESOLUTION_FACT_TYPES
      3. fact.value in _COMPLETION_VALUES  (exact match)
      4. decision.status == "OPEN"
    """
    if not subject_id or not fact_id:
        return
    if fact_type not in _DECISION_RESOLUTION_FACT_TYPES:
        return
    if value not in _COMPLETION_VALUES:
        return

    with _memory_lock:
        decisions = _decision_load()

    for d in decisions:
        if d.get("subject_id") == subject_id and _decision_status(d) == "OPEN":
            _decision_mark_done(d["id"], fact_id)


def _question_resolve_links(
    subject_id: str,
    fact_id: str,
    fact_type:  str = "",
    fact_value: str = "",
) -> None:
    """Resolve pending Questions that share subject_id with a newly created Fact.

    Matching rules:
      1. question.subject_id == fact.subject_id
      2. not _question_is_resolved(question)
      3. _question_fact_type_matches(question, fact_type)
    Sets resolved_by_fact via _question_mark_answered; answer field is not modified.
    """
    if not subject_id or not fact_id:
        return
    with _memory_lock:
        questions = _question_load()
    for q in questions:
        if q.get("subject_id") == subject_id and not _question_is_resolved(q):
            if not _question_fact_type_matches(q, fact_type):
                continue
            _question_mark_answered(q["id"], fact_id, fact_value)


def _build_subject_context() -> dict:
    """Aggregate Questions, Decisions, and Facts keyed by subject_id.

    Merge-resolved: subject_id is followed through merged_into chains so that
    records belonging to a merged source subject are bucketed under the final
    canonical subject.  Uses the same hop/cycle rules as _subject_resolve_merge
    but without re-acquiring _memory_lock (subjects are already loaded here).

    Returns a dict shaped as:
        {
            "<subject_id>": {
                "subject":   {...},   # entry from subject_registry, or None
                "questions": [...],
                "decisions": [...],
                "facts":     [...],
            },
            "_unassigned": { "subject": None, "questions": [...], ... },
        }
    Callers must NOT hold _memory_lock; this function acquires it internally.
    """
    with _memory_lock:
        subjects   = _subject_load()
        questions  = _question_load()
        decisions  = _decision_load()
        facts      = _fact_load()

    subject_map = {s["id"]: s for s in subjects if s.get("id")}

    _MAX_HOPS = 10

    def _resolve(sid: str) -> str:
        """Return final canonical subject_id following merged_into chain."""
        if not sid:
            return sid
        visited: set = set()
        current = sid
        for _ in range(_MAX_HOPS):
            entry = subject_map.get(current)
            if not entry:
                break
            next_id = entry.get("merged_into")
            if not next_id:
                break
            if next_id in visited:
                return sid
            visited.add(current)
            current = next_id
        else:
            return sid
        return current

    result: dict = {}

    def _bucket(sid):
        key = sid if sid else "_unassigned"
        if key not in result:
            result[key] = {
                "subject":   subject_map.get(sid) if sid else None,
                "questions": [],
                "decisions": [],
                "facts":     [],
            }
        return result[key]

    for q in questions:
        _bucket(_resolve(q.get("subject_id") or ""))["questions"].append(q)
    for d in decisions:
        _bucket(_resolve(d.get("subject_id") or ""))["decisions"].append(d)
    for f in facts:
        _bucket(_resolve(f.get("subject_id") or ""))["facts"].append(f)

    return result


def _question_is_resolved(q: dict) -> bool:
    """Return True if the question has been resolved by any means.

    v1.4+: status field is authoritative.
    v1.3 fallback: answer / resolved_by_fact / resolved_by_decision.
    """
    if "status" in q:
        return q["status"] in _QUESTION_TERMINAL_STATUSES
    return bool(
        q.get("answer")
        or q.get("resolved_by_fact")
        or q.get("resolved_by_decision")
    )


def _question_fact_type_matches(question: dict, fact_type: str) -> bool:
    """Return True if the fact_type is allowed to resolve the Question."""
    eft = question.get("expected_fact_types") or []
    if not eft:
        return fact_type in _QUESTION_RESOLUTION_FACT_TYPES
    return fact_type in eft


def _question_build_answer_link(
    source_type: str,
    source_id:   Optional[str],
    answer_text: Optional[str],
    confidence:  float = 1.0,
    linked_at:   Optional[str] = None,
) -> dict:
    """Return an AnswerLink dict. Does not persist; callers embed it in the Question record."""
    return {
        "source_type": source_type,
        "source_id":   source_id,
        "answer_text": answer_text,
        "confidence":  round(float(confidence), 4),
        "linked_at":   linked_at or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _question_migrate_answer_link(entry: dict) -> Optional[dict]:
    """Derive an AnswerLink from a pre-v1.5 Question record, or None for OPEN records."""
    src = entry.get("resolution_source")
    ts  = entry.get("answered_at") or entry.get("updated_at")
    if src == "fact":
        return _question_build_answer_link(
            "fact", entry.get("resolved_by_fact"), None, 1.0, linked_at=ts,
        )
    if src == "decision":
        return _question_build_answer_link(
            "decision", entry.get("resolved_by_decision"), None, 1.0, linked_at=ts,
        )
    if src == "answer":
        return _question_build_answer_link(
            "direct", None, entry.get("answer") or "", 1.0, linked_at=ts,
        )
    return None


def _subject_status(bucket: dict) -> str:
    """Return the resolution status of a Subject bucket.

    RESOLVED : no open decisions and no pending questions
    OPEN     : at least one open decision or pending question
    """
    decisions = bucket.get("decisions", [])
    questions = bucket.get("questions", [])

    open_decisions    = sum(1 for d in decisions if _decision_status(d) == "OPEN")
    pending_questions = sum(1 for q in questions if not _question_is_resolved(q))

    if open_decisions == 0 and pending_questions == 0:
        return "RESOLVED"
    return "OPEN"


def _subject_lifecycle(bucket: dict) -> str:
    """Return the lifecycle stage of a Subject bucket, derived from current data.

    NEW      : questions exist but no facts and no decisions yet
    ACTIVE   : at least one open decision or pending question
    RESOLVED : _subject_status() == "RESOLVED"
    """
    facts     = bucket.get("facts", [])
    decisions = bucket.get("decisions", [])
    questions = bucket.get("questions", [])

    if (
        len(facts) == 0
        and len(decisions) == 0
        and len(questions) > 0
    ):
        return "NEW"

    open_decisions    = sum(1 for d in decisions if _decision_status(d) == "OPEN")
    pending_questions = sum(1 for q in questions if not _question_is_resolved(q))

    if open_decisions > 0 or pending_questions > 0:
        return "ACTIVE"

    if _subject_status(bucket) == "RESOLVED":
        return "RESOLVED"

    return "ACTIVE"


def _subject_is_merged(entry: dict) -> bool:
    """Return True when a subject registry entry has been merged into another subject."""
    return bool(entry.get("merged_into") or entry.get("merged_at"))


def _subject_is_compressible(bucket: dict) -> bool:
    """Return True when a Subject bucket is eligible for context compression.

    All four conditions must hold:
      Rule-1: _subject_status()   == "RESOLVED"
      Rule-2: _subject_lifecycle() == "RESOLVED"
      Rule-3: open Decision count  == 0
      Rule-4: pending Question count == 0
    Merged subjects are always compressible.
    """
    if _subject_is_merged(bucket.get("subject") or {}):
        return True
    if _subject_status(bucket) != "RESOLVED":
        return False
    if _subject_lifecycle(bucket) != "RESOLVED":
        return False
    decisions = bucket.get("decisions", [])
    questions = bucket.get("questions", [])
    if any(_decision_status(d) == "OPEN" for d in decisions):
        return False
    if any(not _question_is_resolved(q) for q in questions):
        return False
    return True


def _subject_priority_score(bucket: dict) -> int:
    """Return a priority score for a Subject bucket (higher = show first)."""
    decisions  = bucket.get("decisions", [])
    questions  = bucket.get("questions", [])
    subj       = bucket.get("subject") or {}

    open_count = sum(1 for d in decisions if _decision_status(d) == "OPEN")
    q_count    = sum(1 for q in questions if not _question_is_resolved(q))
    mention    = subj.get("mention_count", 0) or 0
    return open_count * 100 + q_count * 10 + mention


# ── A26: Subject-Aware Answer Context Builder ─────────────────────────────────

def _a26_resolve_source_value(
    source_type: Optional[str],
    source_id:   Optional[str],
    facts:       list,
    decisions:   list,
) -> Optional[str]:
    """Return source value for an AnswerLink by JOIN.

    "fact"     → Fact.value  (lookup by source_id in facts list)
    "decision" → Decision.action  (lookup by source_id in decisions list)
    "direct"   → None  (answer_text is the direct answer; no JOIN needed)
    JOIN failure → None  (silent; caller falls back to answer_text)
    """
    if source_type == "fact" and source_id:
        for f in facts:
            if f.get("id") == source_id:
                return f.get("value")
    elif source_type == "decision" and source_id:
        for d in decisions:
            if d.get("id") == source_id:
                return d.get("action")
    return None


def _a26_make_answer_entry(
    question:  dict,
    facts:     list,
    decisions: list,
) -> dict:
    """Build an AnswerEntry from a Question record plus its AnswerLink JOIN.

    OPEN questions get all answer/source fields set to None.
    ANSWERED questions have AnswerLink fields expanded and source_value
    resolved via _a26_resolve_source_value().
    """
    link = question.get("answer_link")
    if link is None or _question_status(question) == "OPEN":
        return {
            "question_id":   question.get("id", ""),
            "question_text": question.get("question", ""),
            "status":        "OPEN",
            "source_type":   None,
            "source_id":     None,
            "answer_text":   None,
            "confidence":    None,
            "linked_at":     None,
            "source_value":  None,
            "asked_at":      question.get("timestamp", ""),
            "answered_at":   None,
        }
    source_type = link.get("source_type")
    source_id   = link.get("source_id")
    return {
        "question_id":   question.get("id", ""),
        "question_text": question.get("question", ""),
        "status":        "ANSWERED",
        "source_type":   source_type,
        "source_id":     source_id,
        "answer_text":   link.get("answer_text"),
        "confidence":    link.get("confidence"),
        "linked_at":     link.get("linked_at"),
        "source_value":  _a26_resolve_source_value(
            source_type,
            source_id,
            facts,
            decisions,
        ),
        "asked_at":      question.get("timestamp", ""),
        "answered_at":   question.get("answered_at"),
    }


def _a26_sort_answer_entries(entries: list) -> list:
    """Sort AnswerEntry list: OPEN first (asked_at DESC), then ANSWERED (linked_at DESC)."""
    open_entries     = [e for e in entries if e.get("status") == "OPEN"]
    answered_entries = [e for e in entries if e.get("status") == "ANSWERED"]
    open_entries.sort(key=lambda e: e.get("asked_at") or "", reverse=True)
    answered_entries.sort(key=lambda e: e.get("linked_at") or "", reverse=True)
    return open_entries + answered_entries


_A26_LIFECYCLE_ORDER = {"ACTIVE": 0, "NEW": 1, "RESOLVED": 2}


def _a26_sort_contexts(contexts: list, sort_by: str) -> list:
    """Sort SubjectAnswerContext list by the given strategy.

    "priority"  — priority_score DESC, subject_name ASC
    "lifecycle" — ACTIVE > NEW > RESOLVED, then priority_score DESC
    "name"      — subject_name ASC
    """
    if sort_by == "lifecycle":
        return sorted(
            contexts,
            key=lambda c: (
                _A26_LIFECYCLE_ORDER.get(c.get("lifecycle", "RESOLVED"), 2),
                -c.get("priority_score", 0),
            ),
        )
    if sort_by == "name":
        return sorted(contexts, key=lambda c: c.get("subject_name", ""))
    return sorted(
        contexts,
        key=lambda c: (-c.get("priority_score", 0), c.get("subject_name", "")),
    )


def _a26_build_subject_answer_context_from_bucket(
    subject_id: str,
    bucket:     dict,
) -> dict:
    """Build a SubjectAnswerContext from a pre-loaded bucket dict.

    Internal helper shared by build_subject_answer_context() and
    build_all_subject_answer_contexts() so that _build_subject_context()
    is called only once per top-level invocation.
    """
    facts     = bucket.get("facts", [])
    decisions = bucket.get("decisions", [])
    questions = bucket.get("questions", [])
    subj      = bucket.get("subject") or {}

    answer_entries = _a26_sort_answer_entries([
        _a26_make_answer_entry(q, facts, decisions)
        for q in questions
    ])

    open_q     = sum(1 for e in answer_entries if e["status"] == "OPEN")
    answered_q = sum(1 for e in answer_entries if e["status"] == "ANSWERED")
    open_d     = sum(1 for d in decisions if _decision_status(d) == "OPEN")
    done_d     = sum(1 for d in decisions if _decision_status(d) == "DONE")

    return {
        "subject_id":              subject_id,
        "subject_name":            subj.get("canonical_name", ""),
        "lifecycle":               _subject_lifecycle(bucket),
        "priority_score":          _subject_priority_score(bucket),
        "open_question_count":     open_q,
        "answered_question_count": answered_q,
        "open_decision_count":     open_d,
        "done_decision_count":     done_d,
        "fact_count":              len(facts),
        "answer_entries":          answer_entries,
        "facts":                   list(facts),
        "decisions":               list(decisions),
        "built_at":                time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def build_subject_answer_context(subject_id: str) -> Optional[dict]:
    """Build a SubjectAnswerContext for the given subject_id.

    Returns None if subject_id is not found in any bucket.
    Read-only — no memory files are written.
    No additional _memory_lock acquired (delegated to _build_subject_context).
    """
    if not subject_id:
        return None
    all_buckets = _build_subject_context()
    bucket = all_buckets.get(subject_id)
    if bucket is None:
        return None
    return _a26_build_subject_answer_context_from_bucket(subject_id, bucket)


def build_all_subject_answer_contexts(
    *,
    lifecycle_filter:   Optional[str] = None,
    sort_by:            str           = "priority",
    include_unassigned: bool          = False,
) -> list:
    """Build SubjectAnswerContext for all subjects.

    _build_subject_context() is called exactly once; per-subject work is
    delegated to _a26_build_subject_answer_context_from_bucket().

    lifecycle_filter : if set, only subjects matching that lifecycle are returned.
    sort_by          : "priority" (default) | "lifecycle" | "name"
    include_unassigned: if False (default), the "_unassigned" bucket is excluded.
    Read-only — no memory files are written.
    """
    all_buckets = _build_subject_context()
    contexts: list = []

    for sid, bucket in all_buckets.items():
        if sid == "_unassigned" and not include_unassigned:
            continue
        ctx = _a26_build_subject_answer_context_from_bucket(sid, bucket)
        if lifecycle_filter and ctx.get("lifecycle") != lifecycle_filter:
            continue
        contexts.append(ctx)

    return _a26_sort_contexts(contexts, sort_by)


def get_subject_answer_context_summary(subject_id: str) -> Optional[dict]:
    """Return a lightweight SubjectAnswerContext summary (no detail lists).

    Suitable for generate_meeting_analysis() and similar callers that need
    counts/lifecycle without full entry lists.
    Returns None if subject_id is not found.
    """
    ctx = build_subject_answer_context(subject_id)
    if ctx is None:
        return None
    return {
        "subject_id":              ctx["subject_id"],
        "subject_name":            ctx["subject_name"],
        "lifecycle":               ctx["lifecycle"],
        "priority_score":          ctx["priority_score"],
        "open_question_count":     ctx["open_question_count"],
        "answered_question_count": ctx["answered_question_count"],
        "open_decision_count":     ctx["open_decision_count"],
        "done_decision_count":     ctx["done_decision_count"],
        "fact_count":              ctx["fact_count"],
    }


# ── A26 end ───────────────────────────────────────────────────────────────────

# ── A27: Answer Timeline Engine ───────────────────────────────────────────────

def _a27_to_timeline_entry(entry: dict, ctx: dict) -> dict:
    """Convert an A26 AnswerEntry + SubjectAnswerContext to an AnswerTimelineEntry.

    Caller must ensure entry["status"] == "ANSWERED" before calling.
    `lifecycle` reflects the Subject state at query time, not at linked_at time.
    """
    return {
        "subject_id":    ctx["subject_id"],
        "subject_name":  ctx["subject_name"],
        "lifecycle":     ctx["lifecycle"],
        "question_id":   entry["question_id"],
        "question_text": entry["question_text"],
        "source_type":   entry["source_type"],
        "source_id":     entry["source_id"],
        "answer_text":   entry["answer_text"],
        "source_value":  entry["source_value"],
        "confidence":    entry["confidence"],
        "asked_at":      entry["asked_at"],
        "answered_at":   entry["answered_at"],
        "linked_at":     entry["linked_at"],
    }


def _a27_sort_timeline(entries: list) -> list:
    """Sort AnswerTimelineEntry list: linked_at DESC, answered_at DESC, asked_at DESC."""
    entries.sort(
        key=lambda e: (
            e.get("linked_at")   or "",
            e.get("answered_at") or "",
            e.get("asked_at")    or "",
        ),
        reverse=True,
    )
    return entries


def build_subject_answer_timeline(
    subject_id: str,
    limit: int = 50,
) -> list:
    """Return AnswerTimelineEntry list for a single subject.

    ANSWERED questions only (OPEN excluded).
    Sorted: linked_at DESC, answered_at DESC, asked_at DESC.
    Returns [] if subject_id is not found or has no ANSWERED questions.
    Read-only — no memory files written.
    No additional _memory_lock acquired (delegated to A26).
    """
    ctx = build_subject_answer_context(subject_id)
    if ctx is None:
        return []
    entries = [
        _a27_to_timeline_entry(e, ctx)
        for e in ctx["answer_entries"]
        if e.get("status") == "ANSWERED"
    ]
    return _a27_sort_timeline(entries)[:limit]


def build_global_answer_timeline(
    limit: int = 100,
) -> list:
    """Return AnswerTimelineEntry list across all subjects.

    _build_subject_context() called exactly once (via build_all_subject_answer_contexts).
    _unassigned bucket excluded (consistent with A26 default).
    ANSWERED questions only (OPEN excluded).
    Sorted: linked_at DESC, answered_at DESC, asked_at DESC.
    Read-only — no memory files written.
    """
    all_contexts = build_all_subject_answer_contexts()
    entries = [
        _a27_to_timeline_entry(e, ctx)
        for ctx in all_contexts
        for e in ctx["answer_entries"]
        if e.get("status") == "ANSWERED"
    ]
    return _a27_sort_timeline(entries)[:limit]


# ── A27 end ───────────────────────────────────────────────────────────────────

# ── A28: Context Prioritization Engine ────────────────────────────────────────

def _a28_latest_activity_at(ctx: dict) -> str:
    """Return the most recent timestamp across all AnswerEntry fields.

    Collects linked_at, answered_at, asked_at from every entry in
    ctx["answer_entries"] (OPEN and ANSWERED alike), excludes None values,
    and returns the ISO8601 lexicographic maximum.  Returns "" when no
    non-None candidates exist.
    """
    candidates: list = []
    for entry in ctx.get("answer_entries", []):
        for field in ("linked_at", "answered_at", "asked_at"):
            v = entry.get(field)
            if v is not None:
                candidates.append(v)
    return max(candidates) if candidates else ""


def _a28_build_priority_entry(ctx: dict) -> dict:
    """Convert a SubjectAnswerContext to a SubjectPriorityEntry.

    rank_score = priority_score
                 + open_question_count * 50
                 + open_decision_count * 100
    """
    priority_score      = ctx.get("priority_score",      0)
    open_question_count = ctx.get("open_question_count", 0)
    open_decision_count = ctx.get("open_decision_count", 0)
    return {
        "subject_id":              ctx.get("subject_id",              ""),
        "subject_name":            ctx.get("subject_name",            ""),
        "lifecycle":               ctx.get("lifecycle",               ""),
        "priority_score":          priority_score,
        "open_question_count":     open_question_count,
        "answered_question_count": ctx.get("answered_question_count", 0),
        "open_decision_count":     open_decision_count,
        "done_decision_count":     ctx.get("done_decision_count",     0),
        "latest_activity_at":      _a28_latest_activity_at(ctx),
        "rank_score":              (
            priority_score
            + open_question_count * 50
            + open_decision_count * 100
        ),
    }


def _a28_sort_priority(entries: list) -> list:
    """Sort SubjectPriorityEntry list in-place and return it.

    Sort key: rank_score DESC, latest_activity_at DESC, subject_name ASC.
    ISO8601 timestamps are stripped of separators and negated for DESC ordering.
    """
    entries.sort(
        key=lambda e: (
            -e.get("rank_score", 0),
            -(int(
                (e.get("latest_activity_at") or "")
                .replace("-", "").replace(":", "").replace("T", "")
            ) if e.get("latest_activity_at") else 0),
            e.get("subject_name", ""),
        )
    )
    return entries


def build_subject_priority_list(
    *,
    lifecycle_filter: Optional[str] = None,
    limit:            int            = 50,
) -> list:
    """Return a ranked list of SubjectPriorityEntry dicts.

    build_all_subject_answer_contexts() is called exactly once; lifecycle
    filtering is delegated to A26 (no additional filter applied here).
    Read-only — no memory files written.
    """
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=lifecycle_filter,
    )
    entries = [_a28_build_priority_entry(ctx) for ctx in contexts]
    _a28_sort_priority(entries)
    return entries[:limit]


def get_top_priority_subject() -> Optional[dict]:
    """Return the highest-ranked SubjectPriorityEntry, or None if no subjects exist.

    Delegates to build_subject_priority_list(limit=1).
    Read-only — no memory files written.
    """
    results = build_subject_priority_list(limit=1)
    return results[0] if results else None


# ── A28 end ───────────────────────────────────────────────────────────────────

# ── A29: Context Recommendation Engine ───────────────────────────────────────

_A29_COLLECT_INFORMATION = "COLLECT_INFORMATION"
_A29_FOLLOW_UP_DECISION  = "FOLLOW_UP_DECISION"
_A29_ANSWER_QUESTION     = "ANSWER_QUESTION"
_A29_MONITOR             = "MONITOR"


def _a29_recommendation_type(ctx: dict) -> str:
    """Return the recommendation type for a SubjectAnswerContext.

    Rule-3 (NEW) is evaluated first: in NEW lifecycle open_question_count > 0
    always holds, so Rule-3 must precede Rule-2 to avoid masking.
    Rule-1 (FOLLOW_UP_DECISION) precedes Rule-2 (ANSWER_QUESTION) because
    decisions carry owners and deadlines with higher business impact.
    """
    lifecycle           = ctx.get("lifecycle", "")
    open_decision_count = ctx.get("open_decision_count", 0)
    open_question_count = ctx.get("open_question_count", 0)

    if lifecycle == "NEW":
        return _A29_COLLECT_INFORMATION
    if open_decision_count > 0:
        return _A29_FOLLOW_UP_DECISION
    if open_question_count > 0:
        return _A29_ANSWER_QUESTION
    return _A29_MONITOR


def _a29_build_recommendation_entry(ctx: dict) -> dict:
    """Convert a SubjectAnswerContext to a RecommendationEntry.

    Input: SubjectAnswerContext (A26).
    Read-only — no memory files written.
    """
    priority_score = ctx.get("priority_score", 0)
    rec_type       = _a29_recommendation_type(ctx)

    if priority_score >= 100:
        priority = "HIGH"
    elif priority_score >= 10:
        priority = "MEDIUM"
    else:
        priority = "LOW"

    reasons = {
        _A29_COLLECT_INFORMATION: "Subject is new and requires information gathering.",
        _A29_FOLLOW_UP_DECISION:  "Open decisions require follow-up.",
        _A29_ANSWER_QUESTION:     "Open questions require answers.",
        _A29_MONITOR:             "No immediate action required.",
    }

    return {
        "subject_id":           ctx.get("subject_id",           ""),
        "subject_name":         ctx.get("subject_name",         ""),
        "lifecycle":            ctx.get("lifecycle",            ""),
        "priority_score":       priority_score,
        "open_question_count":  ctx.get("open_question_count",  0),
        "open_decision_count":  ctx.get("open_decision_count",  0),
        "recommendation_type":  rec_type,
        "recommendation_reason": reasons[rec_type],
        "priority":             priority,
    }


def _a29_sort_recommendations(entries: list) -> list:
    """Sort RecommendationEntry list in-place and return it.

    Sort key: priority_score DESC, subject_name ASC.
    """
    entries.sort(
        key=lambda e: (-e.get("priority_score", 0), e.get("subject_name", "")),
    )
    return entries


def build_recommendation_list(
    *,
    lifecycle_filter: Optional[str] = None,
    limit:            int            = 50,
) -> list:
    """Return a ranked list of RecommendationEntry dicts.

    Depends on A26 build_all_subject_answer_contexts() directly.
    A28 APIs are not called — Priority Engine and Recommendation Engine
    are independent siblings both rooted at A26.
    _build_subject_context() is called exactly once (delegated to A26).
    Read-only — no memory files written.
    """
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=lifecycle_filter,
    )
    entries = [_a29_build_recommendation_entry(ctx) for ctx in contexts]
    _a29_sort_recommendations(entries)
    return entries[:limit]


def get_top_recommendation() -> Optional[dict]:
    """Return the highest-priority RecommendationEntry, or None if no subjects exist.

    Delegates to build_recommendation_list(limit=1).
    Read-only — no memory files written.
    """
    results = build_recommendation_list(limit=1)
    return results[0] if results else None


# ── A29 end ───────────────────────────────────────────────────────────────────

# ── A30: Context Intelligence Dashboard ───────────────────────────────────────

def _a30_build_subject_dashboard_entry(ctx: dict) -> dict:
    """Convert a SubjectAnswerContext to a DashboardEntry.

    Integrates A28 priority and A29 recommendation into a single dict.
    A30-F1: rank_score and latest_activity_at are extracted from the
    _a28_build_priority_entry() result — not recomputed independently.
    latest_answer is the most recent ANSWERED AnswerEntry converted to an
    AnswerTimelineEntry via _a27_to_timeline_entry(), or None when absent.
    Read-only — no memory files written.
    """
    priority_entry = _a28_build_priority_entry(ctx)
    rec_entry      = _a29_build_recommendation_entry(ctx)

    rank_score         = priority_entry["rank_score"]          # A30-F1
    latest_activity_at = priority_entry["latest_activity_at"]  # A30-F1

    answered = sorted(
        (e for e in ctx.get("answer_entries", []) if e.get("status") == "ANSWERED"),
        key=lambda e: e.get("linked_at") or "",
        reverse=True,
    )
    latest_answer = _a27_to_timeline_entry(answered[0], ctx) if answered else None

    return {
        "subject_id":           ctx.get("subject_id",           ""),
        "subject_name":         ctx.get("subject_name",         ""),
        "lifecycle":            ctx.get("lifecycle",            ""),
        "priority_score":       ctx.get("priority_score",       0),
        "rank_score":           rank_score,
        "recommendation_type":  rec_entry["recommendation_type"],
        "priority":             rec_entry["priority"],
        "open_question_count":  ctx.get("open_question_count",  0),
        "open_decision_count":  ctx.get("open_decision_count",  0),
        "latest_answer":        latest_answer,
        "latest_activity_at":   latest_activity_at,
    }


def _a30_sort_dashboard(entries: list) -> list:
    """Sort DashboardEntry list in-place and return it.

    Sort key: rank_score DESC, subject_name ASC.
    """
    entries.sort(
        key=lambda e: (-e.get("rank_score", 0), e.get("subject_name", "")),
    )
    return entries


def build_context_dashboard(
    *,
    lifecycle_filter: Optional[str] = None,
    limit:            int            = 50,
) -> list:
    """Return a ranked list of DashboardEntry dicts (rank_score DESC).

    Integration Layer: aggregates A26 context, A27 timeline, A28 priority,
    and A29 recommendation without re-computing A28/A29 values.
    build_all_subject_answer_contexts() is called exactly once.
    A27/A28/A29 public APIs are not called — internal helpers are used directly.
    Read-only — no memory files written.
    """
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=lifecycle_filter,
    )
    entries = [_a30_build_subject_dashboard_entry(ctx) for ctx in contexts]
    _a30_sort_dashboard(entries)
    return entries[:limit]


def get_dashboard_summary() -> dict:
    """Return a lightweight summary of the current Dashboard state.

    build_all_subject_answer_contexts() is called exactly once; no limit is
    applied so all subjects contribute to lifecycle and count totals.
    top_subject is the highest-ranked DashboardEntry (rank_score DESC),
    equivalent to build_context_dashboard(limit=1)[0].
    Read-only — no memory files written.
    """
    contexts = build_all_subject_answer_contexts()
    entries  = [_a30_build_subject_dashboard_entry(ctx) for ctx in contexts]
    _a30_sort_dashboard(entries)

    return {
        "subject_count":       len(entries),
        "active_count":        sum(1 for e in entries if e["lifecycle"] == "ACTIVE"),
        "new_count":           sum(1 for e in entries if e["lifecycle"] == "NEW"),
        "resolved_count":      sum(1 for e in entries if e["lifecycle"] == "RESOLVED"),
        "open_question_count": sum(e["open_question_count"] for e in entries),
        "open_decision_count": sum(e["open_decision_count"] for e in entries),
        "top_subject":         entries[0] if entries else None,
    }


# ── A30 end ───────────────────────────────────────────────────────────────────


# ── B27: Status Extraction Engine ────────────────────────────────────────────

_B27_STATUS_PRIORITY: dict[str, int] = {
    "CANCELLED": 6, "DONE": 5, "BLOCKED": 4,
    "ACTIVE": 3, "PLANNED": 2, "UNKNOWN": 1,
}

_B27_KW_BLOCKED_JA   = frozenset({"ブロック", "保留", "停止"})
_B27_KW_BLOCKED_EN   = frozenset({"blocked", "waiting for approval", "on hold", "stalled"})
_B27_KW_PLANNED_JA   = frozenset({"予定", "計画", "導入予定"})
_B27_KW_PLANNED_EN   = frozenset({"planned", "planning", "scheduled", "intend to"})
_B27_KW_ACTIVE_JA    = frozenset({"進行中", "実施中", "対応中", "作業中", "検討中", "確認中", "調査中"})
_B27_KW_ACTIVE_EN    = frozenset({"in progress", "underway"})
_B27_KW_DONE_JA      = frozenset({"完了", "終了", "対応済", "解決", "クローズ", "実施済"})
_B27_KW_DONE_EN      = frozenset({"completed", "done"})
_B27_KW_CANCELLED_JA = frozenset({"キャンセル", "中止", "廃止"})
_B27_KW_CANCELLED_EN = frozenset({"cancelled", "canceled"})

_B27_RULE_BLOCKED_FACT_JA    = "BLOCKED_FACT_JA"
_B27_RULE_BLOCKED_FACT_EN    = "BLOCKED_FACT_EN"
_B27_RULE_BLOCKED_DECISION   = "BLOCKED_DECISION"
_B27_RULE_PLANNED_FACT_JA    = "PLANNED_FACT_JA"
_B27_RULE_PLANNED_FACT_EN    = "PLANNED_FACT_EN"
_B27_RULE_PLANNED_DECISION   = "PLANNED_DECISION"
_B27_RULE_ACTIVE_FACT_JA     = "ACTIVE_FACT_JA"
_B27_RULE_ACTIVE_FACT_EN     = "ACTIVE_FACT_EN"
_B27_RULE_ACTIVE_DECISION    = "ACTIVE_DECISION"
_B27_RULE_DONE_FACT_JA       = "DONE_FACT_JA"
_B27_RULE_DONE_FACT_EN       = "DONE_FACT_EN"
_B27_RULE_DONE_DECISION      = "DONE_DECISION"
_B27_RULE_CANCELLED_FACT_JA  = "CANCELLED_FACT_JA"
_B27_RULE_CANCELLED_FACT_EN  = "CANCELLED_FACT_EN"
_B27_RULE_CANCELLED_DECISION = "CANCELLED_DECISION"


def _b27_text_contains(text: str, keywords: frozenset) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in keywords)


def _b27_collect_status_signals(ctx: dict) -> list[tuple[str, str, str]]:
    signals: list[tuple[str, str, str]] = []
    for fact in ctx.get("facts", []):
        value = str(fact.get("value", ""))
        if _b27_text_contains(value, _B27_KW_BLOCKED_JA):
            signals.append(("BLOCKED",   _B27_RULE_BLOCKED_FACT_JA,   "fact"))
        if _b27_text_contains(value, _B27_KW_BLOCKED_EN):
            signals.append(("BLOCKED",   _B27_RULE_BLOCKED_FACT_EN,   "fact"))
        if _b27_text_contains(value, _B27_KW_PLANNED_JA):
            signals.append(("PLANNED",   _B27_RULE_PLANNED_FACT_JA,   "fact"))
        if _b27_text_contains(value, _B27_KW_PLANNED_EN):
            signals.append(("PLANNED",   _B27_RULE_PLANNED_FACT_EN,   "fact"))
        if _b27_text_contains(value, _B27_KW_ACTIVE_JA):
            signals.append(("ACTIVE",    _B27_RULE_ACTIVE_FACT_JA,    "fact"))
        if _b27_text_contains(value, _B27_KW_ACTIVE_EN):
            signals.append(("ACTIVE",    _B27_RULE_ACTIVE_FACT_EN,    "fact"))
        if _b27_text_contains(value, _B27_KW_DONE_JA):
            signals.append(("DONE",      _B27_RULE_DONE_FACT_JA,      "fact"))
        if _b27_text_contains(value, _B27_KW_DONE_EN):
            signals.append(("DONE",      _B27_RULE_DONE_FACT_EN,      "fact"))
        if _b27_text_contains(value, _B27_KW_CANCELLED_JA):
            signals.append(("CANCELLED", _B27_RULE_CANCELLED_FACT_JA, "fact"))
        if _b27_text_contains(value, _B27_KW_CANCELLED_EN):
            signals.append(("CANCELLED", _B27_RULE_CANCELLED_FACT_EN, "fact"))
    for dec in ctx.get("decisions", []):
        ds = dec.get("status", "")
        if ds == "BLOCKED":
            signals.append(("BLOCKED",   _B27_RULE_BLOCKED_DECISION,   "decision"))
        elif ds == "IN_PROGRESS":
            signals.append(("ACTIVE",    _B27_RULE_ACTIVE_DECISION,    "decision"))
        elif ds == "OPEN":
            signals.append(("PLANNED",   _B27_RULE_PLANNED_DECISION,   "decision"))
        elif ds == "DONE":
            signals.append(("DONE",      _B27_RULE_DONE_DECISION,       "decision"))
        elif ds == "CANCELLED":
            signals.append(("CANCELLED", _B27_RULE_CANCELLED_DECISION,  "decision"))
    return signals


def _b27_build_status_entry(ctx: dict) -> dict:
    signals = _b27_collect_status_signals(ctx)
    extracted_at = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not signals:
        status = "ACTIVE" if ctx.get("lifecycle") == "ACTIVE" else "UNKNOWN"
        return {
            "subject_id":     ctx.get("subject_id", ""),
            "subject_name":   ctx.get("subject_name", ""),
            "lifecycle":      ctx.get("lifecycle", ""),
            "status":         status,
            "confidence":     0.0,
            "evidence_count": 0,
            "matched_rules":  [],
            "source_types":   [],
            "extracted_at":   extracted_at,
        }
    best_status    = max(signals, key=lambda s: _B27_STATUS_PRIORITY.get(s[0], 0))[0]
    best_signals   = [s for s in signals if s[0] == best_status]
    matched_rules  = list(dict.fromkeys(s[1] for s in best_signals))
    source_types   = list(dict.fromkeys(s[2] for s in best_signals))
    evidence_count = len(matched_rules)
    confidence     = min(0.3 + 0.13 * evidence_count, 0.95)
    return {
        "subject_id":     ctx.get("subject_id", ""),
        "subject_name":   ctx.get("subject_name", ""),
        "lifecycle":      ctx.get("lifecycle", ""),
        "status":         best_status,
        "confidence":     confidence,
        "evidence_count": evidence_count,
        "matched_rules":  matched_rules,
        "source_types":   source_types,
        "extracted_at":   extracted_at,
    }


def build_subject_status(subject_id: str):
    ctx = build_subject_answer_context(subject_id)
    if ctx is None:
        return None
    return _b27_build_status_entry(ctx)


def build_status_list(*, lifecycle_filter=None, limit: int = 50) -> list:
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=lifecycle_filter,
        sort_by="priority_score",
        include_unassigned=False,
    )
    entries = []
    for ctx in contexts:
        if ctx.get("subject_id") == "_unassigned":
            continue
        entries.append(_b27_build_status_entry(ctx))
    entries.sort(key=lambda e: (
        -_B27_STATUS_PRIORITY.get(e["status"], 0),
        -e["confidence"],
        e.get("subject_name", ""),
    ))
    return entries[:limit]


def get_top_status_subject():
    entries = build_status_list()
    return entries[0] if entries else None


# ── B27 end ───────────────────────────────────────────────────────────────────


# ── B28: Timeline Engine ──────────────────────────────────────────────────────

_B28_KNOWN_DECISION_STATUSES = frozenset({
    "OPEN", "IN_PROGRESS", "BLOCKED", "DONE", "CANCELLED",
})


def _b28_fact_status_hint(value: str) -> str | None:
    if _b27_text_contains(value, _B27_KW_CANCELLED_JA) or _b27_text_contains(value, _B27_KW_CANCELLED_EN):
        return "CANCELLED"
    if _b27_text_contains(value, _B27_KW_DONE_JA) or _b27_text_contains(value, _B27_KW_DONE_EN):
        return "DONE"
    if _b27_text_contains(value, _B27_KW_BLOCKED_JA) or _b27_text_contains(value, _B27_KW_BLOCKED_EN):
        return "BLOCKED"
    if _b27_text_contains(value, _B27_KW_ACTIVE_JA) or _b27_text_contains(value, _B27_KW_ACTIVE_EN):
        return "ACTIVE"
    if _b27_text_contains(value, _B27_KW_PLANNED_JA) or _b27_text_contains(value, _B27_KW_PLANNED_EN):
        return "PLANNED"
    return None


def _b28_decision_status_hint(status: str) -> str | None:
    return status if status in _B28_KNOWN_DECISION_STATUSES else None


def _b28_build_event(event_type: str, record: dict, subject_id: str) -> dict:
    if event_type == "fact":
        title       = str(record.get("value", ""))
        status_hint = _b28_fact_status_hint(title)
    else:
        title       = str(record.get("action") or record.get("decision", ""))
        status_hint = _b28_decision_status_hint(record.get("status", ""))
    return {
        "event_type":  event_type,
        "event_id":    record.get("id", ""),
        "subject_id":  subject_id,
        "timestamp":   record.get("timestamp", ""),
        "status_hint": status_hint,
        "title":       title,
        "source":      record,
    }


def _build_subject_timeline(subject_id: str) -> list[dict]:
    ctx = build_subject_answer_context(subject_id)
    if ctx is None:
        return []
    events: list[dict] = []
    for fact in ctx.get("facts", []):
        events.append(_b28_build_event("fact", fact, subject_id))
    for dec in ctx.get("decisions", []):
        events.append(_b28_build_event("decision", dec, subject_id))
    events.sort(key=lambda e: (
        e["timestamp"],
        0 if e["event_type"] == "decision" else 1,
    ))
    return events


def build_subject_timeline(subject_id: str) -> list[dict]:
    return _build_subject_timeline(subject_id)


def build_timeline_list(*, limit: int = 50) -> list[dict]:
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=None,
        sort_by="priority_score",
        include_unassigned=False,
    )
    result = []
    for ctx in contexts:
        sid = ctx.get("subject_id", "")
        if sid == "_unassigned":
            continue
        result.append({
            "subject_id":   sid,
            "subject_name": ctx.get("subject_name", ""),
            "events":       _build_subject_timeline(sid),
        })
    return result[:limit]


# ── B28 end ───────────────────────────────────────────────────────────────────

# ── B29: Status Consistency Engine ───────────────────────────────────────────

_B29_NORMALIZE_MAP: dict[str, str] = {
    "OPEN":        "PLANNED",
    "IN_PROGRESS": "ACTIVE",
    "BLOCKED":     "BLOCKED",
    "DONE":        "DONE",
    "CANCELLED":   "CANCELLED",
}


def _b29_normalize_status(status: str) -> str:
    return _B29_NORMALIZE_MAP.get(status, status)


def _b29_check_consistency(events: list[dict]) -> dict:
    status_events = [e for e in events if e.get("status_hint") is not None]
    if len(status_events) <= 1:
        latest = _b29_normalize_status(status_events[0]["status_hint"]) if status_events else None
        return {
            "consistency":   "UNKNOWN",
            "latest_status": latest,
            "event_count":   len(status_events),
            "violations":    [],
        }

    violations: list[dict] = []
    prev_norm = _b29_normalize_status(status_events[0]["status_hint"])

    for ev in status_events[1:]:
        curr_norm = _b29_normalize_status(ev["status_hint"])
        ts        = ev.get("timestamp", "")

        if prev_norm == "DONE" and curr_norm != "DONE":
            violations.append({
                "rule":            "RULE4",
                "previous_status": prev_norm,
                "current_status":  curr_norm,
                "timestamp":       ts,
            })
        elif prev_norm == "CANCELLED" and curr_norm != "CANCELLED":
            violations.append({
                "rule":            "RULE5",
                "previous_status": prev_norm,
                "current_status":  curr_norm,
                "timestamp":       ts,
            })

        prev_norm = curr_norm

    latest_norm = _b29_normalize_status(status_events[-1]["status_hint"])
    consistency = "INCONSISTENT" if violations else "CONSISTENT"
    return {
        "consistency":   consistency,
        "latest_status": latest_norm,
        "event_count":   len(status_events),
        "violations":    violations,
    }


def build_subject_consistency(subject_id: str) -> dict | None:
    events = _build_subject_timeline(subject_id)
    if not events:
        return None
    result = _b29_check_consistency(events)
    result["subject_id"] = subject_id
    return result


def build_consistency_list(*, limit: int = 50) -> list[dict]:
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=None,
        sort_by="priority_score",
        include_unassigned=False,
    )
    result = []
    for ctx in contexts:
        sid = ctx.get("subject_id", "")
        if sid == "_unassigned":
            continue
        entry = build_subject_consistency(sid)
        if entry is not None:
            entry["subject_name"] = ctx.get("subject_name", "")
            result.append(entry)
    return result[:limit]


# ── B29 end ───────────────────────────────────────────────────────────────────

# ── B30: Status Intelligence Dashboard ───────────────────────────────────────

def build_subject_dashboard(subject_id: str) -> dict | None:
    status_entry = build_subject_status(subject_id)
    timeline     = build_subject_timeline(subject_id)
    consistency  = build_subject_consistency(subject_id)

    current_status = "UNKNOWN"
    if status_entry is not None:
        current_status = status_entry.get("status") or "UNKNOWN"

    subject_name = ""
    if status_entry is not None:
        subject_name = status_entry.get("subject_name", "")

    cons_value      = "UNKNOWN"
    violation_count = 0
    if consistency is not None:
        cons_value      = consistency.get("consistency", "UNKNOWN")
        violation_count = len(consistency.get("violations", []))

    timeline_length = len(timeline) if timeline else 0
    latest_event    = None
    latest_event_at = None
    if timeline:
        last            = timeline[-1]
        latest_event    = last.get("title")
        latest_event_at = last.get("timestamp")

    return {
        "subject_id":      subject_id,
        "subject_name":    subject_name,
        "current_status":  current_status,
        "consistency":     cons_value,
        "violation_count": violation_count,
        "latest_event":    latest_event,
        "latest_event_at": latest_event_at,
        "timeline_length": timeline_length,
    }


def build_dashboard_list(
    *,
    lifecycle_filter:   Optional[str] = None,
    status_filter:      Optional[str] = None,
    consistency_filter: Optional[str] = None,
    limit:              int           = 50,
) -> list[dict]:
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=lifecycle_filter,
        sort_by="priority_score",
        include_unassigned=False,
    )
    result: list[dict] = []
    for ctx in contexts:
        sid = ctx.get("subject_id", "")
        if sid == "_unassigned":
            continue
        if lifecycle_filter is not None and ctx.get("lifecycle") != lifecycle_filter:
            continue
        entry = build_subject_dashboard(sid)
        if entry is None:
            continue
        if status_filter is not None and entry.get("current_status") != status_filter:
            continue
        if consistency_filter is not None and entry.get("consistency") != consistency_filter:
            continue
        result.append(entry)

    # Stable multi-pass sort (lowest priority first):
    result.sort(key=lambda e: e.get("subject_id", ""))                              # 5. asc
    result.sort(key=lambda e: e.get("latest_event_at") or "", reverse=True)         # 4. desc
    result.sort(key=lambda e: -e.get("violation_count", 0))                         # 3. desc
    result.sort(key=lambda e: 0 if e.get("current_status") == "BLOCKED" else 1)     # 2.
    result.sort(key=lambda e: 0 if e.get("consistency") == "INCONSISTENT" else 1)   # 1.

    return result[:limit]


def get_top_dashboard_subject() -> dict | None:
    entries = build_dashboard_list()
    return entries[0] if entries else None


# ── B30 end ───────────────────────────────────────────────────────────────────

# ── A31: Fact Graph ───────────────────────────────────────────────────────────
#
# Implements A31 Frozen Specification v1.1 / Test Specification v1.1.  Structures
# the Facts of a Subject into a relationship graph (SUPPORTS / CONTRADICTS /
# DERIVED_FROM / RELATED_TO) so related / contradicting / derived Facts become
# traceable.  Construction + Traceability Query (A31-R4: both in scope).
#
# Frozen Rules:
#   * Fact Source = A26 (build_subject_answer_context — read-only)
#   * QUESTION FactType は不採用 (§6)
#   * B29 Integration = 疎結合: STATUS contradictions は呼び出し側が渡す
#     contradiction_pairs のみ採用。A31 は B29 を直接呼ばず、STATUS 矛盾を
#     独自再判定しない (§9)。
#   * Weight Strategy = Hybrid: SUPPLIED は供給値、INTERNAL は A31 算出 (§10)
#   * DERIVED_FROM depth default = 5、depth 超過 Edge は生成しない (§11 v1.1)
#   * Cycle Handling = has_cycle flag のみ。循環 Edge は保持 (§12 v1.1)
#   * Read-only: メモリへの書き込みを行わない (§14)
#
# Public API = §13 の 4 本のみ (build_fact_graph / build_fact_graph_list /
# get_related_facts / get_contradicting_facts)。get_top_fact_graph_subject() は
# OUT_OF_SCOPE (実装しない)。

_A31_DERIVED_DEPTH_DEFAULT = 5
_A31_WINDOW_DEFAULT        = 10
_A31_WEIGHT_THRESHOLD      = 0.3
_A31_SUPPORT_SIM_THRESHOLD = 0.5
_A31_RELATED_SIM_THRESHOLD = 0.2

# Negation / cancellation markers for non-STATUS contradiction detection (§9).
_A31_NEGATION_MARKERS = frozenset({
    "not", "no", "never", "cancel", "cancelled", "reject", "rejected",
    "stop", "stopped", "ない", "しない", "中止", "却下", "見送り", "取りやめ",
})


def _a31_clamp01(x) -> float:
    """Clamp into [0.0, 1.0] (confidence/weight value-domain guard, G2-06=clamp)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return round(v, 4)


def _a31_tokenize(value: str) -> set:
    return set(re.findall(r"[0-9A-Za-z぀-ヿ一-鿿]+", str(value).lower()))


def _a31_jaccard(ta: set, tb: set) -> float:
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


def _a31_similarity(a_value: str, b_value: str) -> float:
    return _a31_jaccard(_a31_tokenize(a_value), _a31_tokenize(b_value))


def _a31_is_internal_contradiction(a_value: str, b_value: str) -> bool:
    """Non-STATUS contradiction heuristic: similar content, opposite polarity.

    STATUS contradictions are never decided here (B29 is the source of truth).
    """
    ta = _a31_tokenize(a_value)
    tb = _a31_tokenize(b_value)
    if _a31_jaccard(ta, tb) < _A31_RELATED_SIM_THRESHOLD:
        return False
    return bool(ta & _A31_NEGATION_MARKERS) != bool(tb & _A31_NEGATION_MARKERS)


def _a31_make_node(record: dict, subject_id: str, fact_type: str, value: str) -> dict:
    """Build a FactNode (Frozen Spec §3) from an A26 fact/decision record."""
    raw_turn    = record.get("source_turn")
    source_turn = raw_turn if isinstance(raw_turn, int) else None
    return {
        "fact_id":     record.get("id", ""),
        "subject_id":  subject_id,
        "fact_type":   fact_type,
        "value":       str(value),
        "confidence":  _a31_clamp01(record.get("confidence", 1.0)),
        "source_turn": source_turn,
        "created_at":  record.get("timestamp", "") or time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def _a31_node_from_fact(record: dict, subject_id: str) -> dict:
    ftype = "STATUS" if str(record.get("fact_type", "")).lower() == "status" else "CONTEXT"
    return _a31_make_node(record, subject_id, ftype, record.get("value", ""))


def _a31_node_from_decision(record: dict, subject_id: str) -> dict:
    action = str(record.get("action") or "").strip()
    if action:
        return _a31_make_node(record, subject_id, "ACTION", action)
    return _a31_make_node(record, subject_id, "DECISION", record.get("decision", ""))


def _a31_build_fact_nodes(subject_id: str) -> list:
    """Build FactNodes for a Subject from its A26 context (read-only).

    QUESTION Facts are not adopted (§6).  Nodes are de-duplicated by fact_id.
    """
    ctx = build_subject_answer_context(subject_id)
    if ctx is None:
        return []
    nodes: list = []
    seen: set = set()
    for f in ctx.get("facts", []):
        node = _a31_node_from_fact(f, subject_id)
        fid = node["fact_id"]
        if fid and fid not in seen:
            seen.add(fid)
            nodes.append(node)
    for d in ctx.get("decisions", []):
        node = _a31_node_from_decision(d, subject_id)
        fid = node["fact_id"]
        if fid and fid not in seen:
            seen.add(fid)
            nodes.append(node)
    return nodes


def _a31_window_ok(na: dict, nb: dict, window: int) -> bool:
    """source_turn proximity filter (§8).  None turns → TYPE filter only."""
    ta = na["source_turn"]
    tb = nb["source_turn"]
    if ta is None or tb is None:
        return True
    return abs(ta - tb) <= window


def _a31_support_direction(na: dict, nb: dict) -> tuple:
    """Directed SUPPORTS: higher-confidence Fact is the evidence (source)."""
    ca, cb = na["confidence"], nb["confidence"]
    if ca > cb:
        return na["fact_id"], nb["fact_id"]
    if cb > ca:
        return nb["fact_id"], na["fact_id"]
    a_id, b_id = na["fact_id"], nb["fact_id"]
    return (a_id, b_id) if a_id <= b_id else (b_id, a_id)


def _a31_make_edge(src: str, tgt: str, rel: str, weight: float, origin: str, now: str) -> dict:
    return {
        "edge_id":        f"e_{rel.lower()}__{src}__{tgt}",
        "source_fact_id": src,
        "target_fact_id": tgt,
        "relation_type":  rel,
        "weight":         _a31_clamp01(weight),
        "origin":         origin,
        "created_at":     now,
    }


def _a31_apply_depth_limit(nodes: list, *, depth_limit: int, window: int) -> list:
    """Build depth-capped DERIVED_FROM links within each FactType (§11 v1.1).

    Same-type Facts are time-ordered; a later Fact derives from the immediately
    preceding similar Fact.  Chains deeper than depth_limit are NOT extended —
    the excess Edge is not generated (no diagnostic flag, per Erratum v1.1).
    Newer→older within a total order ⇒ internally built chains are acyclic.
    Returns a list of (source_fact_id, target_fact_id, weight) tuples.
    """
    by_type: dict = {}
    for n in nodes:
        by_type.setdefault(n["fact_type"], []).append(n)

    derived: list = []
    for group in by_type.values():
        ordered = sorted(group, key=lambda n: (n["created_at"], n["fact_id"]))
        chain_depth = 0
        for k in range(1, len(ordered)):
            prev, curr = ordered[k - 1], ordered[k]
            if not _a31_window_ok(prev, curr, window):
                chain_depth = 0
                continue
            sim = _a31_similarity(prev["value"], curr["value"])
            if sim >= _A31_SUPPORT_SIM_THRESHOLD:
                chain_depth += 1
                if chain_depth > depth_limit:
                    continue  # depth exceeded → Edge not generated (v1.1)
                derived.append((curr["fact_id"], prev["fact_id"], _a31_clamp01(sim)))
            else:
                chain_depth = 0
    return derived


def _a31_build_relationship_edges(
    nodes:            list,
    contradiction_pairs,
    *,
    depth_limit:      int,
    window:           int,
    weight_threshold: float,
    now:              str,
) -> list:
    """Derive RelationshipEdges for a node set (Frozen Spec §7-§11)."""
    edges: list = []
    seen: set = set()

    def _add(src, tgt, rel, weight, origin):
        if src == tgt:
            return
        key = (src, tgt, rel)
        if key in seen:
            return
        seen.add(key)
        edges.append(_a31_make_edge(src, tgt, rel, weight, origin, now))

    # 1. CONTRADICTS — SUPPLIED (STATUS only, from B29 via caller).  Symmetric.
    if contradiction_pairs:
        status_ids = {n["fact_id"] for n in nodes if n["fact_type"] == "STATUS"}
        for pair in contradiction_pairs:
            if not pair or len(pair) < 2:
                continue
            a, b = pair[0], pair[1]
            if a in status_ids and b in status_ids and a != b:
                _add(a, b, "CONTRADICTS", 1.0, "SUPPLIED")
                _add(b, a, "CONTRADICTS", 1.0, "SUPPLIED")

    # 2. Same-type pairwise derivation (TYPE filter + window filter, §8).
    n = len(nodes)
    for i in range(n):
        for j in range(i + 1, n):
            na, nb = nodes[i], nodes[j]
            if na["fact_type"] != nb["fact_type"]:
                continue                                   # TYPE filter (§8)
            if not _a31_window_ok(na, nb, window):
                continue                                   # window filter (§8)

            # CONTRADICTS — INTERNAL (non-STATUS only).
            if na["fact_type"] != "STATUS" and \
                    _a31_is_internal_contradiction(na["value"], nb["value"]):
                w = _a31_clamp01(max(_a31_similarity(na["value"], nb["value"]),
                                     weight_threshold))
                _add(na["fact_id"], nb["fact_id"], "CONTRADICTS", w, "INTERNAL")
                _add(nb["fact_id"], na["fact_id"], "CONTRADICTS", w, "INTERNAL")
                continue

            sim = _a31_similarity(na["value"], nb["value"])
            if sim >= _A31_SUPPORT_SIM_THRESHOLD:
                src, tgt = _a31_support_direction(na, nb)   # directed SUPPORTS
                _add(src, tgt, "SUPPORTS", sim, "INTERNAL")
            elif sim >= _A31_RELATED_SIM_THRESHOLD:
                if sim < weight_threshold:
                    continue                                # discard very weak (§10)
                _add(na["fact_id"], nb["fact_id"], "RELATED_TO", sim, "INTERNAL")
                _add(nb["fact_id"], na["fact_id"], "RELATED_TO", sim, "INTERNAL")

    # 3. DERIVED_FROM (depth-capped, §11 v1.1).
    for src, tgt, w in _a31_apply_depth_limit(nodes, depth_limit=depth_limit, window=window):
        _add(src, tgt, "DERIVED_FROM", w, "INTERNAL")

    return edges


def _a31_has_cycle(nodes: list, edges: list) -> bool:
    """Detect a cycle over DERIVED_FROM edges (DFS coloring, §12).

    Detection only — cycle-forming edges are retained by the caller.
    """
    adj: dict = {}
    color: dict = {n["fact_id"]: 0 for n in nodes}  # 0=white 1=gray 2=black
    for e in edges:
        if e["relation_type"] != "DERIVED_FROM":
            continue
        s, t = e["source_fact_id"], e["target_fact_id"]
        adj.setdefault(s, []).append(t)
        color.setdefault(s, 0)
        color.setdefault(t, 0)

    def _visit(u: str) -> bool:
        color[u] = 1
        for v in adj.get(u, []):
            c = color.get(v, 0)
            if c == 1:
                return True
            if c == 0 and _visit(v):
                return True
        color[u] = 2
        return False

    for nid in list(color.keys()):
        if color[nid] == 0 and _visit(nid):
            return True
    return False


def _a31_find_subject_for_fact(fact_id: str):
    """Resolve the owning subject_id of a Fact (read-only scan of A26 contexts).

    Returns the subject_id string, or None if the fact_id is not found.
    """
    if not fact_id:
        return None
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=None,
        sort_by="priority_score",
        include_unassigned=True,
    )
    for ctx in contexts:
        for f in ctx.get("facts", []):
            if f.get("id") == fact_id:
                return ctx.get("subject_id")
        for d in ctx.get("decisions", []):
            if d.get("id") == fact_id:
                return ctx.get("subject_id")
    return None


def build_fact_graph(
    subject_id,
    contradiction_pairs=None,
    *,
    depth_limit:      int   = _A31_DERIVED_DEPTH_DEFAULT,
    window:           int   = _A31_WINDOW_DEFAULT,
    weight_threshold: float = _A31_WEIGHT_THRESHOLD,
) -> dict:
    """Build the Fact Graph for a Subject (Frozen Spec §13.1).

    contradiction_pairs : optional list of (fact_id, fact_id) STATUS contradictions
                          supplied by the caller from B29.  None → no STATUS
                          CONTRADICTS edges (non-STATUS still derived internally).
    Returns a FactGraph (§5).  Unknown / empty subject → empty graph (no error).
    Read-only.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not subject_id:
        return {"subject_id": subject_id or "", "nodes": [], "edges": [],
                "has_cycle": False, "built_at": now}
    nodes = _a31_build_fact_nodes(subject_id)
    if not nodes:
        return {"subject_id": subject_id, "nodes": [], "edges": [],
                "has_cycle": False, "built_at": now}
    edges = _a31_build_relationship_edges(
        nodes, contradiction_pairs,
        depth_limit=depth_limit, window=window,
        weight_threshold=weight_threshold, now=now,
    )
    return {
        "subject_id": subject_id,
        "nodes":      nodes,
        "edges":      edges,
        "has_cycle":  _a31_has_cycle(nodes, edges),
        "built_at":   now,
    }


def build_fact_graph_list() -> list:
    """Build a FactGraph for every Subject (Frozen Spec §13.4).

    No subjects → [].  Read-only.
    """
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=None,
        sort_by="priority_score",
        include_unassigned=False,
    )
    result: list = []
    for ctx in contexts:
        sid = ctx.get("subject_id", "")
        if sid == "_unassigned":
            continue
        result.append(build_fact_graph(sid))
    return result


def get_related_facts(fact_id: str) -> list:
    """Return all edges incident to fact_id (Frozen Spec §13.2).

    Unknown / unconnected fact_id → [] (no error).  Read-only.
    """
    if not fact_id:
        return []
    sid = _a31_find_subject_for_fact(fact_id)
    if sid is None:
        return []
    graph = build_fact_graph(sid)
    return [
        e for e in graph["edges"]
        if e["source_fact_id"] == fact_id or e["target_fact_id"] == fact_id
    ]


def get_contradicting_facts(fact_id: str) -> list:
    """Return only CONTRADICTS edges incident to fact_id (Frozen Spec §13.3)."""
    return [e for e in get_related_facts(fact_id) if e["relation_type"] == "CONTRADICTS"]


# ── A31 end ───────────────────────────────────────────────────────────────────

def _subject_merge_candidates() -> list[tuple[str, str]]:
    """Detect Subject pairs that are merge candidates due to name similarity.

    Detection only — no registry modifications are made.

    Rules:
      Rule-1: case-insensitive exact match (e.g. "VPN" vs "vpn")
      Rule-2: one name is a prefix of the other (e.g. "VPN" vs "VPN導入")
      Rule-3: one name is a suffix of the other (e.g. "Google" vs "Google管理")

    Returns a list of (name_a, name_b) tuples where name_a < name_b lexicographically
    to avoid duplicates.  Subjects without a canonical_name are ignored.
    """
    with _memory_lock:
        subjects = _subject_load()

    names: list[str] = []
    for entry in subjects:
        if not isinstance(entry, dict):
            continue
        cname = entry.get("canonical_name", "")
        if cname and cname.strip():
            names.append(cname.strip())

    candidates: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            a_norm = a.lower()
            b_norm = b.lower()

            is_candidate = (
                a_norm == b_norm                        # Rule-1
                or a_norm.startswith(b_norm)            # Rule-2 (b is prefix of a)
                or b_norm.startswith(a_norm)            # Rule-2 (a is prefix of b)
                or a_norm.endswith(b_norm)              # Rule-3 (b is suffix of a)
                or b_norm.endswith(a_norm)              # Rule-3 (a is suffix of b)
            )

            if is_candidate:
                key = (a, b) if a <= b else (b, a)
                if key not in seen:
                    seen.add(key)
                    candidates.append((a, b) if a <= b else (b, a))

    return candidates


def _subject_merge_confidence(name_a: str, name_b: str) -> int:
    """Return merge confidence (0-100) between two subject names.

    Rule-1: case-insensitive exact match → 100
    Rule-2: one name is a prefix of the other (case-insensitive) → 70
    Rule-3: one name is a suffix of the other (case-insensitive) → 60
    Otherwise → 0
    """
    a = name_a.lower()
    b = name_b.lower()
    if a == b:
        return 100
    if a.startswith(b) or b.startswith(a):
        return 70
    if a.endswith(b) or b.endswith(a):
        return 60
    return 0


# ── A32: Subject Graph ────────────────────────────────────────────────────────
#
# Implements A32 Frozen Specification v1.0 / Test Specification v1.0.  Structures
# Subjects into a relationship graph (MERGE_CANDIDATE / RELATED_TO) so related and
# merge-candidate Subjects become traceable.  Subject-level analogue of A31.
#
# Frozen Rules:
#   * Data Source = A26 only (build_all_subject_answer_contexts — read-only).
#   * A31 NON-DEPENDENT: build_fact_graph 等を呼ばず、_a31_* も再利用しない (§4).
#   * RelationType = { MERGE_CANDIDATE, RELATED_TO } のみ (§6).
#       MERGE_CANDIDATE : _subject_merge_confidence > 0、weight = conf/100 (MERGE).
#       RELATED_TO      : Token Jaccard >= 0.3、weight = overlap (FACT).
#   * subject_id が正本。subject_name(canonical_name) は比較入力のみ (§8.1).
#   * 無向グラフ。has_cycle を持たない (§5/§6).
#   * Read-only: 保存系を呼ばず registry/fact/decision を変更しない (§10).
#
# Public API = §9 の 3 本のみ。get_top_* は OUT_OF_SCOPE (実装しない)。

_A32_RELATED_THRESHOLD = 0.3


def _a32_clamp01(x) -> float:
    """Clamp into [0.0, 1.0] (weight value-domain guard)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    if v < 0.0:
        return 0.0
    if v > 1.0:
        return 1.0
    return round(v, 4)


def _a32_tokenize(value: str) -> set:
    return set(re.findall(r"[0-9A-Za-z぀-ヿ一-鿿]+", str(value).lower()))


def _a32_jaccard(ta: set, tb: set) -> float:
    union = ta | tb
    if not union:
        return 0.0
    return len(ta & tb) / len(union)


def _a32_subject_tokens(ctx: dict) -> set:
    """Union of fact-value tokens for a Subject (A26 facts only, A31 not consumed)."""
    toks: set = set()
    for f in ctx.get("facts", []):
        toks |= _a32_tokenize(f.get("value", ""))
    return toks


def _a32_fact_overlap(tokens_a: set, tokens_b: set) -> float:
    """RELATED_TO overlap score = Token Jaccard of two Subjects' fact tokens (§7)."""
    return _a32_jaccard(tokens_a, tokens_b)


def _a32_make_node(ctx: dict, now: str) -> dict:
    """Build a SubjectNode (§5.1) from an A26 SubjectAnswerContext."""
    return {
        "subject_id":     ctx.get("subject_id", ""),
        "subject_name":   ctx.get("subject_name", ""),
        "lifecycle":      ctx.get("lifecycle", ""),
        "priority_score": ctx.get("priority_score", 0),
        "fact_count":     ctx.get("fact_count", len(ctx.get("facts", []))),
        "created_at":     now,
    }


def _a32_make_edge(src: str, tgt: str, rel: str, weight: float, origin: str, now: str) -> dict:
    return {
        "edge_id":           f"se_{rel.lower()}__{src}__{tgt}",
        "source_subject_id": src,
        "target_subject_id": tgt,
        "relation_type":     rel,
        "weight":            _a32_clamp01(weight),
        "origin":            origin,
        "created_at":        now,
    }


def _a32_build_edges(nodes: list, tokens_by_id: dict, now: str) -> list:
    """Derive SubjectEdges over subject_id pairs (Frozen Spec §6-§8).

    MERGE_CANDIDATE : _subject_merge_confidence > 0 (names non-empty).
    RELATED_TO      : Token Jaccard >= _A32_RELATED_THRESHOLD.
    Both relations are undirected → reverse edge auto-generated.  Edges keyed
    on (source, target, relation_type) for dedup.  subject_id is the identity;
    canonical_name is comparison input only (§8.1).
    """
    edges: list = []
    seen: set = set()

    def _add(src, tgt, rel, weight, origin):
        if src == tgt:
            return
        key = (src, tgt, rel)
        if key in seen:
            return
        seen.add(key)
        edges.append(_a32_make_edge(src, tgt, rel, weight, origin, now))

    n = len(nodes)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = nodes[i], nodes[j]
            sid_a, sid_b = a["subject_id"], b["subject_id"]
            if not sid_a or not sid_b or sid_a == sid_b:
                continue

            # MERGE_CANDIDATE — name similarity (empty names excluded).
            name_a = (a["subject_name"] or "").strip()
            name_b = (b["subject_name"] or "").strip()
            if name_a and name_b:
                conf = _subject_merge_confidence(name_a, name_b)
                if conf > 0:
                    w = _a32_clamp01(conf / 100)
                    _add(sid_a, sid_b, "MERGE_CANDIDATE", w, "MERGE")
                    _add(sid_b, sid_a, "MERGE_CANDIDATE", w, "MERGE")

            # RELATED_TO — shared-fact overlap (A26 facts only).
            ov = _a32_fact_overlap(tokens_by_id.get(sid_a, set()),
                                   tokens_by_id.get(sid_b, set()))
            if ov >= _A32_RELATED_THRESHOLD:
                w = _a32_clamp01(ov)
                _add(sid_a, sid_b, "RELATED_TO", w, "FACT")
                _add(sid_b, sid_a, "RELATED_TO", w, "FACT")

    return edges


def build_subject_graph() -> dict:
    """Build the Subject Graph over all Subjects (Frozen Spec §9.1).

    No subjects → empty graph (nodes=[], edges=[]).  Read-only.  A31 not consumed.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=None,
        sort_by="priority_score",
        include_unassigned=False,
    )
    nodes: list = []
    tokens_by_id: dict = {}
    for ctx in contexts:
        sid = ctx.get("subject_id", "")
        if sid == "_unassigned":
            continue
        nodes.append(_a32_make_node(ctx, now))
        tokens_by_id[sid] = _a32_subject_tokens(ctx)
    edges = _a32_build_edges(nodes, tokens_by_id, now)
    return {"nodes": nodes, "edges": edges, "built_at": now}


def get_related_subjects(subject_id: str) -> list:
    """Return all edges incident to subject_id (Frozen Spec §9.2).

    Unknown / isolated subject_id → [] (no error).  Read-only.
    """
    if not subject_id:
        return []
    graph = build_subject_graph()
    return [
        e for e in graph["edges"]
        if e["source_subject_id"] == subject_id or e["target_subject_id"] == subject_id
    ]


def get_merge_candidate_subjects(subject_id: str) -> list:
    """Return only MERGE_CANDIDATE edges incident to subject_id (Frozen Spec §9.3)."""
    return [e for e in get_related_subjects(subject_id)
            if e["relation_type"] == "MERGE_CANDIDATE"]


# ── A32 end ───────────────────────────────────────────────────────────────────

# ── A33: Consistency Engine ───────────────────────────────────────────────────
#
# Implements A33 Frozen Specification v1.0 / Test Specification v1.0.  Read-only
# aggregation layer: consumes B29 (status consistency) + A31 (fact CONTRADICTS)
# + A26 (context) and structures ConsistencyFinding / ConsistencyReport across
# FACT / SUBJECT / DECISION entities.  NOT a detector.
#
# Frozen Rules:
#   * Aggregation only — consume下位エンジン出力、独自判定を書かない (FD-2 / SSoT).
#       A31 : build_fact_graph() の CONTRADICTS を消費。再判定/再生成しない (§10).
#       B29 : build_subject_consistency() の verdict を消費。RULE4/5 再実装しない.
#       A32 : v1.0 では非呼出（補助・横断は A35 待ち、残-D）.
#       A35 : 横断推論しない。A33 は Subject 単位のみ (§10 / OQ-3).
#   * entity_type = { FACT, SUBJECT, DECISION }。QUESTION 不採用 (FD-1).
#   * Finding に weight/confidence なし。ConsistencyEdge 不発行 (FD-4/FD-5).
#   * evidence は A31 edge / B29 violation の参照のみ（複製しない）.
#   * verdict = CONSISTENT | INCONSISTENT | UNKNOWN (B29 整合, FD-7).
#   * Read-only: 保存系を呼ばず、下位エンジン/registry/memory を変更しない (§8).
#
# Public API = §7 の 3 本のみ。get_top_* / ranking は OUT_OF_SCOPE.

_A33_CONSISTENT   = "CONSISTENT"
_A33_INCONSISTENT = "INCONSISTENT"
_A33_UNKNOWN      = "UNKNOWN"


def _a33_make_finding(entity_type: str, entity_id: str, verdict: str,
                      source: str, evidence: list, now: str) -> dict:
    """Build a ConsistencyFinding (§5.1).  No weight/confidence (FD-5)."""
    return {
        "finding_id":  f"cf_{entity_type.lower()}__{entity_id}__{verdict.lower()}",
        "entity_type": entity_type,
        "entity_id":   entity_id,
        "verdict":     verdict,
        "source":      source,
        "evidence":    evidence,
        "detected_at": now,
    }


def _a33_violation_ref(v: dict) -> str:
    """Reference id for a B29 violation (no duplication of the violation body)."""
    return f"{v.get('rule', '')}@{v.get('timestamp', '')}"


def _a33_fact_findings(fact_graph: dict, now: str) -> list:
    """FACT findings from A31 CONTRADICTS edges (§6.1).  Consume only."""
    contradicts = [e for e in fact_graph.get("edges", [])
                   if e.get("relation_type") == "CONTRADICTS"]
    findings: list = []
    seen: set = set()
    for e in contradicts:
        fid = e.get("source_fact_id", "")
        if not fid or fid in seen:
            continue
        seen.add(fid)
        evidence = [{"type": "A31_CONTRADICTS", "ref_id": ce.get("edge_id", "")}
                    for ce in contradicts if ce.get("source_fact_id") == fid]
        findings.append(_a33_make_finding("FACT", fid, _A33_INCONSISTENT,
                                          "A31", evidence, now))
    return findings


def _a33_subject_finding(subject_id: str, fact_graph: dict, b29, now: str):
    """SUBJECT finding for an internally-contradictory Subject (§6.2).

    INCONSISTENT iff the Subject's A31 fact graph contains CONTRADICTS edges,
    or B29 verdict == INCONSISTENT.  Cross-subject is NOT evaluated (A35).
    """
    contradicts = [e for e in fact_graph.get("edges", [])
                   if e.get("relation_type") == "CONTRADICTS"]
    has_internal = len(contradicts) > 0
    b29_incon = bool(b29) and b29.get("consistency") == _A33_INCONSISTENT
    if not (has_internal or b29_incon):
        return None
    evidence: list = []
    if has_internal:
        evidence += [{"type": "A31_CONTRADICTS", "ref_id": e.get("edge_id", "")}
                     for e in contradicts]
    if b29_incon:
        evidence += [{"type": "B29_VIOLATION", "ref_id": _a33_violation_ref(v)}
                     for v in b29.get("violations", [])]
    return _a33_make_finding("SUBJECT", subject_id, _A33_INCONSISTENT,
                             "A33", evidence, now)


def _a33_decision_finding(subject_id: str, b29, now: str) -> dict:
    """DECISION finding mirroring the B29 verdict (§6.3).  No re-judgement."""
    if b29 is None:
        return _a33_make_finding("DECISION", subject_id, _A33_UNKNOWN,
                                 "B29", [], now)
    verdict = b29.get("consistency", _A33_UNKNOWN)
    evidence: list = []
    if verdict == _A33_INCONSISTENT:
        evidence = [{"type": "B29_VIOLATION", "ref_id": _a33_violation_ref(v)}
                    for v in b29.get("violations", [])]
    return _a33_make_finding("DECISION", subject_id, verdict, "B29", evidence, now)


def _a33_aggregate_verdict(findings: list) -> str:
    """Aggregate report verdict (§6.5)."""
    if any(f["verdict"] == _A33_INCONSISTENT for f in findings):
        return _A33_INCONSISTENT
    if any(f["verdict"] == _A33_UNKNOWN for f in findings):
        return _A33_UNKNOWN
    return _A33_CONSISTENT


def build_consistency_report(subject_id) -> dict:
    """Aggregate consistency for a Subject (Frozen Spec §7.1).

    Consumes A31 (fact CONTRADICTS) + B29 (status verdict) read-only.  Unknown
    subject → empty report (verdict=UNKNOWN).  Read-only.  No memory writes.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    ctx = build_subject_answer_context(subject_id) if subject_id else None
    if ctx is None:
        return {"subject_id": subject_id or "", "subject_name": "",
                "verdict": _A33_UNKNOWN, "inconsistency_count": 0,
                "findings": [], "built_at": now}

    fact_graph = build_fact_graph(subject_id)            # A31 consume
    b29        = build_subject_consistency(subject_id)   # B29 consume

    findings: list = []
    findings += _a33_fact_findings(fact_graph, now)
    subject_finding = _a33_subject_finding(subject_id, fact_graph, b29, now)
    if subject_finding is not None:
        findings.append(subject_finding)
    findings.append(_a33_decision_finding(subject_id, b29, now))

    inconsistency_count = sum(1 for f in findings if f["verdict"] == _A33_INCONSISTENT)
    return {
        "subject_id":          subject_id,
        "subject_name":        ctx.get("subject_name", ""),
        "verdict":             _a33_aggregate_verdict(findings),
        "inconsistency_count": inconsistency_count,
        "findings":            findings,
        "built_at":            now,
    }


def build_consistency_report_list() -> list:
    """Aggregate consistency for every Subject (Frozen Spec §7.2).

    No subjects → [].  Read-only.  O(N × per-subject consume) accepted (§9).
    """
    contexts = build_all_subject_answer_contexts(
        lifecycle_filter=None,
        sort_by="priority_score",
        include_unassigned=False,
    )
    result: list = []
    for ctx in contexts:
        sid = ctx.get("subject_id", "")
        if sid == "_unassigned":
            continue
        result.append(build_consistency_report(sid))
    return result


def get_contradictions(subject_id: str) -> list:
    """Return only INCONSISTENT findings for a Subject (Frozen Spec §7.3).

    Unknown subject → [].  Read-only.
    """
    if not subject_id:
        return []
    report = build_consistency_report(subject_id)
    return [f for f in report["findings"] if f["verdict"] == _A33_INCONSISTENT]


# ── A33 end ───────────────────────────────────────────────────────────────────

# ── A34: Context Expansion ────────────────────────────────────────────────────
#
# Implements A34 Frozen Specification v1.0 / Test Specification v1.0.  Read-only
# retrieval layer: traverses existing A32 (RELATED_TO) + A31 (SUPPORTS /
# DERIVED_FROM / RELATED_TO) graphs and A26 context to gather related Subjects /
# Facts / Questions / Decisions for answer generation.  NOT inference.
#
# Frozen Rules:
#   * Subject traversal = A32 RELATED_TO only (MERGE_CANDIDATE excluded, S2).
#   * Fact traversal = A31 SUPPORTS/DERIVED_FROM/RELATED_TO only (CONTRADICTS excluded).
#   * consistency_flag = source subject's A33 verdict (consume only, no re-judge).
#   * Ordering = existing edge weight + distance (no new score). weight NOT stored.
#   * depth default 1 / max 2 (>=3 forbidden); item cap default 50 / max 200.
#   * Read-only; ExpandedContext is a derived view (no persistence).
#   * No A35: no inference / hypothesis / search / relevance score / query seed.
# Public API = build_expanded_context / get_related_context (no list / no get_top_*).

_A34_DEFAULT_DEPTH = 1
_A34_MAX_DEPTH     = 2
_A34_DEFAULT_LIMIT = 50
_A34_MAX_LIMIT     = 200
_A34_SUBJECT_RELATIONS = frozenset({"RELATED_TO"})
_A34_FACT_RELATIONS    = frozenset({"SUPPORTS", "DERIVED_FROM", "RELATED_TO"})


def _a34_clamp_depth(depth) -> int:
    try:
        d = int(depth)
    except (TypeError, ValueError):
        d = _A34_DEFAULT_DEPTH
    return max(1, min(_A34_MAX_DEPTH, d))


def _a34_clamp_limit(limit) -> int:
    try:
        l = int(limit)
    except (TypeError, ValueError):
        l = _A34_DEFAULT_LIMIT
    return max(1, min(_A34_MAX_LIMIT, l))


def _a34_make_item(entity_type, entity_id, relation, source_subject_id,
                   distance, flag, provenance) -> dict:
    return {
        "entity_type":       entity_type,
        "entity_id":         entity_id,
        "relation":          relation,
        "source_subject_id": source_subject_id,
        "distance":          distance,
        "consistency_flag":  flag,
        "provenance":        list(provenance),
    }


def _a34_collect_related_subjects(seed_id: str, depth: int) -> dict:
    """BFS over A32 RELATED_TO (MERGE_CANDIDATE excluded).

    Returns {subject_id: (distance, relation, weight, provenance)}.
    """
    graph = build_subject_graph()                       # A32 consume (once)
    adj: dict = {}
    for e in graph.get("edges", []):
        if e.get("relation_type") not in _A34_SUBJECT_RELATIONS:
            continue                                    # RELATED_TO only
        adj.setdefault(e.get("source_subject_id"), []).append(e)

    found = {seed_id: (0, "SEED", 1.0, [])}
    frontier = [seed_id]
    for dist in range(1, depth + 1):
        nxt = []
        for sid in frontier:
            for e in adj.get(sid, []):
                tgt = e.get("target_subject_id")
                if tgt and tgt not in found:
                    found[tgt] = (dist, "A32_RELATED_TO", e.get("weight", 0.0),
                                  [{"type": "A32_RELATED_TO", "ref_id": e.get("edge_id", "")}])
                    nxt.append(tgt)
        frontier = nxt
    return found


def _a34_subject_flag(subject_id: str, cache: dict) -> str:
    if subject_id in cache:
        return cache[subject_id]
    rep = build_consistency_report(subject_id)          # A33 consume (flag only)
    verdict = rep.get("verdict", "UNKNOWN") if rep else "UNKNOWN"
    cache[subject_id] = verdict
    return verdict


def _a34_fact_records(subject_id, distance, is_seed, flag) -> list:
    """FACT records via A31 SUPPORTS/DERIVED_FROM/RELATED_TO (CONTRADICTS excluded).

    Returns list of (weight, item).  Seed facts not on an allowed edge → SEED.
    """
    fg = build_fact_graph(subject_id)                   # A31 consume
    edge_map: dict = {}                                 # fact_id -> (relation, weight, prov)
    for e in fg.get("edges", []):
        rt = e.get("relation_type")
        if rt not in _A34_FACT_RELATIONS:
            continue                                    # CONTRADICTS excluded
        label = f"A31_{rt}"
        for fid in (e.get("source_fact_id"), e.get("target_fact_id")):
            if fid and fid not in edge_map:
                edge_map[fid] = (label, e.get("weight", 0.0),
                                 [{"type": label, "ref_id": e.get("edge_id", "")}])

    records: list = []
    if is_seed:
        ctx = build_subject_answer_context(subject_id)
        for f in (ctx.get("facts", []) if ctx else []):
            fid = f.get("id", "")
            if not fid:
                continue
            if fid in edge_map:
                rel, w, prov = edge_map[fid]
                records.append((w, _a34_make_item("FACT", fid, rel, subject_id, distance, flag, prov)))
            else:
                records.append((1.0, _a34_make_item("FACT", fid, "SEED", subject_id, distance, flag, [])))
    else:
        for fid, (rel, w, prov) in edge_map.items():
            records.append((w, _a34_make_item("FACT", fid, rel, subject_id, distance, flag, prov)))
    return records


def _a34_member_records(ctx, subject_id, distance, weight, flag) -> list:
    """QUESTION / DECISION records (A26_MEMBER) from a subject's context."""
    records: list = []
    for q in (ctx.get("questions", []) if ctx else []):
        qid = q.get("id", "")
        if qid:
            records.append((weight, _a34_make_item("QUESTION", qid, "A26_MEMBER",
                                                   subject_id, distance, flag, [])))
    for d in (ctx.get("decisions", []) if ctx else []):
        did = d.get("id", "")
        if did:
            records.append((weight, _a34_make_item("DECISION", did, "A26_MEMBER",
                                                   subject_id, distance, flag, [])))
    return records


def _a34_dedup(records: list) -> list:
    """Merge same (entity_type, entity_id): keep min distance / max weight,
    union provenance, primary relation = kept record's relation (G8)."""
    by_key: dict = {}
    for weight, item in records:
        key = (item["entity_type"], item["entity_id"])
        if key not in by_key:
            by_key[key] = [weight, item]
            continue
        kw, kept = by_key[key]
        seen = {(p.get("type"), p.get("ref_id")) for p in kept["provenance"]}
        for p in item["provenance"]:
            if (p.get("type"), p.get("ref_id")) not in seen:
                kept["provenance"].append(p)
        if item["distance"] < kept["distance"] or \
           (item["distance"] == kept["distance"] and weight > kw):
            kept["distance"] = item["distance"]
            kept["relation"] = item["relation"]
            kept["source_subject_id"] = item["source_subject_id"]
        by_key[key][0] = max(kw, weight)
    return list(by_key.values())


def _a34_order(records: list) -> list:
    """Deterministic ordering: -weight, distance, entity_id (G3 tie-break)."""
    return sorted(records, key=lambda wi: (-wi[0], wi[1]["distance"], wi[1]["entity_id"]))


def build_expanded_context(subject_id, *, depth=1, limit=50) -> dict:
    """Expand context for a Subject (Frozen Spec §7).  Read-only.

    Unknown subject → empty context (items=[]).  depth in [1,2], limit <= 200.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    d = _a34_clamp_depth(depth)
    cap = _a34_clamp_limit(limit)
    if not subject_id or build_subject_answer_context(subject_id) is None:
        return {"seed_subject_id": subject_id or "", "depth": d,
                "items": [], "item_count": 0, "built_at": now}

    flag_cache: dict = {}
    related = _a34_collect_related_subjects(subject_id, d)
    records: list = []
    for sid, (dist, subj_rel, subj_w, subj_prov) in related.items():
        is_seed = (sid == subject_id)
        weight = 1.0 if is_seed else subj_w
        flag = _a34_subject_flag(sid, flag_cache)
        ctx = build_subject_answer_context(sid)
        records.append((weight, _a34_make_item("SUBJECT", sid, subj_rel, sid, dist, flag, subj_prov)))
        records += _a34_fact_records(sid, dist, is_seed, flag)
        records += _a34_member_records(ctx, sid, dist, weight, flag)

    ordered = _a34_order(_a34_dedup(records))[:cap]
    items = [item for _w, item in ordered]
    return {"seed_subject_id": subject_id, "depth": d,
            "items": items, "item_count": len(items), "built_at": now}


def get_related_context(subject_id: str) -> list:
    """Return expanded items NOT belonging to the seed Subject (Frozen Spec §7).

    Excludes all items sourced from the seed (its own SUBJECT / FACT / QUESTION /
    DECISION at distance 0); returns only related (distance >= 1) items.
    """
    if not subject_id:
        return []
    ctx = build_expanded_context(subject_id)
    return [i for i in ctx["items"] if i["source_subject_id"] != subject_id]


# ── A34 end ───────────────────────────────────────────────────────────────────

# ── A35: Memory Reasoning ─────────────────────────────────────────────────────
#
# Implements A35 Frozen Specification v1.0.  Read-only interpretive reasoning:
# consumes A34 ExpandedContext + A26 (status) + A33 (consistency) and produces
# ReasoningReport / ReasoningFinding (MISSING_INFO / FOLLOW_UP /
# CONTRADICTION_EXPLANATION).  NOT hypothesis / causal / search / retrieval.
#
# Binding (A35-G GI):
#   GI-1 MISSING_INFO ← ctx["answer_entries"] status==OPEN (NOT ctx["questions"];
#        do NOT depend on A34 QUESTION items).  ref_id = answer_entry.question_id.
#   GI-2 FOLLOW_UP ← Decision status ∉ {DONE, CANCELLED} (OPEN/IN_PROGRESS/BLOCKED).
#   GI-3 CONTRADICTION_EXPLANATION ← A33 INCONSISTENT finding (get_contradictions).
#   GI-4 scope = distinct source_subject_id of A34 ExpandedContext items + seed.
#   GI-5 content = deterministic template (no hypothesis/probability/truth/causal).
#   GI-6 Read-only (no save).  finding has weight/confidence/score なし.

_A35_DEFAULT_CAP = 50
_A35_MAX_CAP     = 200
_A35_MISSING_INFO            = "MISSING_INFO"
_A35_FOLLOW_UP               = "FOLLOW_UP"
_A35_CONTRADICTION_EXPLANATION = "CONTRADICTION_EXPLANATION"
_A35_RESOLVED_DECISION       = frozenset({"DONE", "CANCELLED"})
_A35_TYPE_ORDER = {_A35_MISSING_INFO: 0, _A35_FOLLOW_UP: 1, _A35_CONTRADICTION_EXPLANATION: 2}


def _a35_clamp_cap(limit) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        n = _A35_DEFAULT_CAP
    return max(1, min(_A35_MAX_CAP, n))


def _a35_make_finding(finding_type, subject_id, content, evidence, source_refs, now) -> dict:
    ref = evidence[0]["ref_id"] if evidence else ""
    return {
        "finding_id":   f"rf_{finding_type.lower()}__{subject_id}__{ref}",
        "finding_type": finding_type,
        "subject_id":   subject_id,
        "content":      content,
        "evidence":     list(evidence),       # >=1 (Frozen §S1-7)
        "source_refs":  list(source_refs),
        "detected_at":  now,
    }


def _reasoning_scope_subjects(subject_id: str) -> list:
    """Scope = seed + distinct source_subject_id of A34 ExpandedContext items (GI-4)."""
    scope: list = []
    seen: set = set()
    if subject_id:
        scope.append(subject_id)
        seen.add(subject_id)
    ctx = build_expanded_context(subject_id)             # A34 consume
    for it in ctx.get("items", []):
        ssid = it.get("source_subject_id", "")
        if ssid and ssid not in seen:
            seen.add(ssid)
            scope.append(ssid)
    return scope


def _reasoning_missing_info(sid: str, now: str) -> list:
    """MISSING_INFO ← OPEN answer_entries (GI-1).  NOT ctx['questions']."""
    ctx = build_subject_answer_context(sid)              # A26 consume (status source)
    if ctx is None:
        return []
    out: list = []
    for e in ctx.get("answer_entries", []):
        if e.get("status") != "OPEN":
            continue                                     # ANSWERED (and any non-OPEN) excluded
        qid = e.get("question_id", "")
        out.append(_a35_make_finding(
            _A35_MISSING_INFO, sid,
            f"未回答の質問: {e.get('question_text', '')}",
            [{"type": "A26_QUESTION", "ref_id": qid}],
            [{"engine": "A26", "ref_id": sid}, {"engine": "A34", "ref_id": "expanded_context"}],
            now))
    return out


def _reasoning_follow_up(sid: str, now: str) -> list:
    """FOLLOW_UP ← Decision status ∉ {DONE, CANCELLED} (GI-2)."""
    ctx = build_subject_answer_context(sid)
    if ctx is None:
        return []
    out: list = []
    for d in ctx.get("decisions", []):
        status = _decision_status(d)
        if status in _A35_RESOLVED_DECISION:
            continue                                     # DONE / CANCELLED excluded
        did = d.get("id", "")
        dtext = str(d.get("action") or d.get("decision") or "")
        out.append(_a35_make_finding(
            _A35_FOLLOW_UP, sid,
            f"未解決の決定を確認: {dtext}（status={status}）",
            [{"type": "A26_DECISION", "ref_id": did}],
            [{"engine": "A26", "ref_id": sid}],
            now))
    return out


def _reasoning_contradictions(sid: str, now: str) -> list:
    """CONTRADICTION_EXPLANATION ← A33 INCONSISTENT findings (GI-3)."""
    out: list = []
    for cf in get_contradictions(sid):                   # A33 consume (INCONSISTENT only)
        fid = cf.get("finding_id", "")
        entity = cf.get("entity_type", "")
        eid = cf.get("entity_id", "")
        out.append(_a35_make_finding(
            _A35_CONTRADICTION_EXPLANATION, sid,
            f"矛盾が検出されています（{entity}:{eid}）。関連する事実・決定を確認してください。",
            [{"type": "A33_FINDING", "ref_id": fid}],
            [{"engine": "A33", "ref_id": sid}],
            now))
    return out


def _reasoning_sort(findings: list) -> list:
    """Deterministic order: finding_type, subject_id, evidence ref_id (G3 tie-break)."""
    return sorted(findings, key=lambda f: (
        _A35_TYPE_ORDER.get(f["finding_type"], 9),
        f["subject_id"],
        f["evidence"][0]["ref_id"] if f["evidence"] else "",
    ))


def build_reasoning_report(subject_id, *, limit=50) -> dict:
    """Build a ReasoningReport for a Subject (Frozen Spec §7).  Read-only.

    Unknown subject → empty report (findings=[]).  No persistence.
    """
    now = time.strftime("%Y-%m-%dT%H:%M:%S")
    if not subject_id or build_subject_answer_context(subject_id) is None:
        return {"seed_subject_id": subject_id or "", "findings": [],
                "finding_count": 0, "built_at": now}
    cap = _a35_clamp_cap(limit)
    findings: list = []
    for sid in _reasoning_scope_subjects(subject_id):
        findings += _reasoning_missing_info(sid, now)
        findings += _reasoning_follow_up(sid, now)
        findings += _reasoning_contradictions(sid, now)
    findings = _reasoning_sort(findings)[:cap]
    return {"seed_subject_id": subject_id, "findings": findings,
            "finding_count": len(findings), "built_at": now}


def get_reasoning_findings(subject_id: str) -> list:
    """Return the ReasoningFinding list for a Subject (Frozen Spec §7)."""
    if not subject_id:
        return []
    return build_reasoning_report(subject_id)["findings"]


# ── A35 end ───────────────────────────────────────────────────────────────────


def _subject_merge_recommendation(name_a: str, name_b: str) -> str:
    """Return merge approval recommendation based on confidence score.

    confidence >= 90 -> APPROVE
    confidence >= 60 -> REVIEW
    otherwise       -> IGNORE
    """
    confidence = _subject_merge_confidence(name_a, name_b)
    if confidence >= 90:
        return "APPROVE"
    if confidence >= 60:
        return "REVIEW"
    return "IGNORE"


def _subject_merge_plan() -> list[dict]:
    """Return dry-run merge plan for APPROVE-rated subject pairs.

    Canonical target selection:
      Rule-1: longer name becomes target.
      Rule-2: same length -> lexicographically smaller becomes target.

    No registry modifications are made.
    """
    candidates = _subject_merge_candidates()
    plan: list[dict] = []
    for a, b in candidates:
        if _subject_merge_recommendation(a, b) != "APPROVE":
            continue
        confidence = _subject_merge_confidence(a, b)
        if len(a) != len(b):
            target = a if len(a) > len(b) else b
            source = b if len(a) > len(b) else a
        else:
            target = min(a, b)
            source = max(a, b)
        plan.append({
            "source": source,
            "target": target,
            "confidence": confidence,
            "recommendation": "APPROVE",
        })
    return plan


def _subject_resolve_merge(subject_id: str) -> str:
    """Follow merged_into chain and return the final canonical subject_id.

    Rule-1: empty subject_id  -> return as-is (subject_id)
    Rule-2: no merged_into    -> return as-is (subject_id)
    Rule-3: merged_into found -> follow chain to the end
    Rule-4: circular ref or >10 hops -> return original subject_id
    Rule-5: registry read-only
    Rule-6: acquires _memory_lock
    """
    if not subject_id:
        return subject_id

    _MAX_HOPS = 10
    with _memory_lock:
        subjects = _subject_load()
    id_map = {s.get("id"): s for s in subjects if isinstance(s, dict) and s.get("id")}

    visited: set = set()
    current = subject_id
    for _ in range(_MAX_HOPS):
        entry = id_map.get(current)
        if not entry:
            break
        next_id = entry.get("merged_into")
        if not next_id:
            break
        if next_id in visited:
            return subject_id
        visited.add(current)
        current = next_id
    else:
        return subject_id
    return current


def _subject_merge_validate() -> list[str]:
    """Validate subject_registry.json merge chain integrity.

    Rule-1: merged_into target id must exist in registry.
    Rule-2: Self-reference (merged_into == own id) is forbidden.
    Rule-3: Circular references are forbidden.
    Rule-4: Merge chain depth must not exceed 10.

    Returns a list of error strings. Returns [] if no errors.
    No registry modifications are made.
    """
    with _memory_lock:
        subjects = _subject_load()

    id_to_entry: dict[str, dict] = {}
    for entry in subjects:
        if isinstance(entry, dict) and entry.get("id"):
            id_to_entry[entry["id"]] = entry

    errors: list[str] = []

    for entry in subjects:
        if not isinstance(entry, dict):
            continue
        sid = entry.get("id", "")
        target_id = entry.get("merged_into", "")
        if not target_id:
            continue

        sname = entry.get("canonical_name", sid)

        # Rule-2: self-reference
        if sid == target_id:
            errors.append(f"Self-reference detected: {sname}")
            continue

        # Rule-1: target must exist
        if target_id not in id_to_entry:
            errors.append(f"Missing merge target: {sname} -> {target_id}")
            continue

        # Rule-3 / Rule-4: follow chain to detect cycle or excessive depth
        visited: list[str] = [sid]
        chain_names: list[str] = [sname]
        current_id = target_id
        depth = 0
        cycle_detected = False
        chain_too_deep = False

        while current_id:
            depth += 1
            if depth > 10:
                chain_too_deep = True
                break
            current_entry = id_to_entry.get(current_id)
            current_name = current_entry.get("canonical_name", current_id) if current_entry else current_id
            if current_id in visited:
                chain_names.append(current_name)
                cycle_detected = True
                break
            visited.append(current_id)
            chain_names.append(current_name)
            current_id = current_entry.get("merged_into", "") if current_entry else ""

        if cycle_detected:
            errors.append(f"Circular merge detected: {' -> '.join(chain_names)}")
        elif chain_too_deep:
            errors.append(f"Merge chain too deep: {sname}")

    return errors


def _subject_merge_transaction(source_subject_id: str, target_subject_id: str) -> dict:
    """Execute a full Subject Merge Transaction in five ordered steps.

    Step-1: Migrate Facts        (_fact_migrate_subject)
    Step-2: Migrate Questions    (_question_migrate_subject)
    Step-3: Migrate Decisions    (_decision_migrate_subject)
    Step-4: Registry merge       (_subject_merge_execute)
    Step-5: Post-merge validate  (_subject_merge_validate)

    On success, appends one audit record to merge_history.json (best-effort).
    No rollback is performed on validation errors — detection only.
    Not called automatically by memory_build_context or any add_* functions.

    Returns:
        {
            "success":            bool,
            "migrated_facts":     int,
            "migrated_questions": int,
            "migrated_decisions": int,
            "validation_errors":  list[str],
            "history_id":         str | None,
        }
    """
    with _memory_lock:
        _subjects_snap = _subject_load()
    _name_map   = {s.get("id"): s.get("canonical_name", s.get("id", "")) for s in _subjects_snap if isinstance(s, dict)}
    source_name = _name_map.get(source_subject_id, source_subject_id)
    target_name = _name_map.get(target_subject_id, target_subject_id)

    migrated_facts     = _fact_migrate_subject(source_subject_id, target_subject_id)
    migrated_questions = _question_migrate_subject(source_subject_id, target_subject_id)
    migrated_decisions = _decision_migrate_subject(source_subject_id, target_subject_id)
    merge_success      = _subject_merge_execute(source_subject_id, target_subject_id)
    validation_errors  = _subject_merge_validate()

    success    = merge_success and len(validation_errors) == 0
    history_id = None
    if success:
        try:
            confidence     = _subject_merge_confidence(source_name, target_name)
            recommendation = _subject_merge_recommendation(source_name, target_name)
            history_id = _merge_history_append(
                source_subject_id, source_name,
                target_subject_id, target_name,
                confidence, recommendation,
            )
        except Exception as e:
            show_warn(f"[merge_audit] history append failed (non-fatal): {e}")

    return {
        "success":            success,
        "migrated_facts":     migrated_facts,
        "migrated_questions": migrated_questions,
        "migrated_decisions": migrated_decisions,
        "validation_errors":  validation_errors,
        "history_id":         history_id,
    }


def _render_subject_block(name: str, lifecycle: str, status: str, facts: list, decisions: list, questions: list, compressed: bool = False) -> str:
    """Render one Subject block for the Subject Memory section."""
    lines: list[str] = [f"Subject: {name}", f"Lifecycle: {lifecycle}", f"Status: {status}"]

    if compressed:
        lines.append("[COMPRESSED]")
        return "\n".join(lines)

    if facts:
        lines.append("Facts:")
        for f in facts:
            lines.append(f"* {f.get('fact_type', '')}: {f.get('value', '')}")
    else:
        lines.append("Facts:\nなし")

    open_dec = [d for d in decisions if _decision_status(d) == "OPEN"]
    if open_dec:
        lines.append("Open Decisions:")
        for d in open_dec:
            lines.append(f"* {d['decision']}")
    else:
        lines.append("Open Decisions:\nなし")

    pending_q = [q for q in questions if not _question_is_resolved(q)]
    if pending_q:
        lines.append("Questions:")
        for q in pending_q:
            lines.append(f"* {q['question']}")
    else:
        lines.append("Questions:\nなし")

    return "\n".join(lines)


# ── R5 Runtime Section Plugins ────────────────────────────────────────────────
#
# Plugin Registry + Loader for the 12 R4-integrated engines (A27-A30 / B27-B30 /
# A31 / A32 / A33 / A35).  Each EngineSpec packages a zero-arg read-only builder
# + a data->str formatter (returns "" to skip).  Logic migrated verbatim from the
# former inline blocks of memory_build_context(); output is byte-equivalent.
# Runtime-layer owned; engine logic unchanged.  Base sections are out of R5 scope
# (not plugins).  No persistence/cache; loader does not call memory_build_context
# (no cycle).

def _runtime_section_plugins() -> list:
    """Ordered EngineSpec list {name, section_title, builder, formatter, order,
    enabled} for the 12 R4 engines.  Built at call time so engine public APIs
    resolve.  Read-only; no persistence/cache."""

    def _fmt_answer_timeline(timeline):
        if not timeline:
            return ""
        return "\n".join(
            f"- [{e.get('subject_name', '')}] Q: {e.get('question_text', '')}"
            f" / A: {e.get('answer_text', '')}"
            for e in timeline
        )

    def _fmt_subject_priority(priority_list):
        if not priority_list:
            return ""
        return "\n".join(
            f"- {e.get('subject_name', '')} "
            f"(rank={e.get('rank_score', 0)}, {e.get('lifecycle', '')})"
            for e in priority_list
        )

    def _fmt_recommendations(recommendations):
        if not recommendations:
            return ""
        return "\n".join(
            f"- {e.get('subject_name', '')}\n"
            f"  {e.get('recommendation_type', '')}\n"
            f"  {e.get('recommendation_reason', '')}"
            for e in recommendations
        )

    def _fmt_context_dashboard(dash):
        if dash.get("subject_count", 0) <= 0:
            return ""
        top = dash.get("top_subject") or {}
        lines = (
            f"Subjects: {dash.get('subject_count', 0)} "
            f"(ACTIVE {dash.get('active_count', 0)} / NEW {dash.get('new_count', 0)} / "
            f"RESOLVED {dash.get('resolved_count', 0)})\n"
            f"Open Questions: {dash.get('open_question_count', 0)}  "
            f"Open Decisions: {dash.get('open_decision_count', 0)}"
        )
        if top:
            lines += f"\nTop: {top.get('subject_name', '')} (rank={top.get('rank_score', 0)})"
        return lines

    def _fmt_status_overview(status_list):
        if not status_list:
            return ""
        return "\n".join(
            f"- {e.get('subject_name', '')}: {e.get('status', '')}"
            for e in status_list
        )

    def _fmt_status_timeline(timeline_list):
        if not timeline_list:
            return ""
        blocks: list = []
        for t in timeline_list:
            evs = [
                f"  {ev.get('timestamp', '')} {ev.get('status_hint', '')} {ev.get('title', '')}"
                for ev in t.get("events", [])
                if ev.get("status_hint")
            ]
            if evs:
                blocks.append(f"[{t.get('subject_name', '')}]\n" + "\n".join(evs))
        if not blocks:
            return ""
        return "\n".join(blocks)

    def _fmt_status_consistency(consistency_list):
        if not consistency_list:
            return ""
        return "\n".join(
            f"- {e.get('subject_name', '')}: {e.get('consistency', '')} "
            f"(latest={e.get('latest_status', '')}, "
            f"violations={len(e.get('violations', []))})"
            for e in consistency_list
        )

    def _fmt_status_dashboard(top_dash):
        if not top_dash:
            return ""
        return (
            f"Top: {top_dash.get('subject_name', '')} | "
            f"status={top_dash.get('current_status', '')} | "
            f"consistency={top_dash.get('consistency', '')} "
            f"(violations={top_dash.get('violation_count', 0)}) | "
            f"events={top_dash.get('timeline_length', 0)}"
        )

    def _fmt_fact_relations(fact_graphs):
        rel_lines: list = []
        for g in fact_graphs:
            for e in g.get("edges", []):
                rel_lines.append(
                    f"- [{g.get('subject_id', '')}] {e.get('relation_type', '')}: "
                    f"{e.get('source_fact_id', '')} -> {e.get('target_fact_id', '')}"
                )
        if not rel_lines:
            return ""
        return "\n".join(rel_lines[:30])

    def _fmt_subject_relations(subject_graph):
        subj_rel_lines: list = []
        for e in subject_graph.get("edges", []):
            if e.get("relation_type") != "RELATED_TO":
                continue
            subj_rel_lines.append(
                f"- {e.get('source_subject_id', '')} <-> {e.get('target_subject_id', '')}"
            )
        if not subj_rel_lines:
            return ""
        return "\n".join(subj_rel_lines[:30])

    def _fmt_consistency_overview(consistency_reports):
        cons_lines: list = []
        for r in consistency_reports:
            cons_lines.append(
                f"- {r.get('subject_name', '')}: {r.get('verdict', '')} "
                f"({r.get('inconsistency_count', 0)})"
            )
        if not cons_lines:
            return ""
        return "\n".join(cons_lines[:30])

    def _fmt_reasoning(findings):
        reasoning_lines: list = []
        for f in findings:
            reasoning_lines.append(
                f"- [{f.get('finding_type', '')}] {f.get('content', '')}"
            )
        if not reasoning_lines:
            return ""
        return "\n".join(reasoning_lines[:30])

    def _reasoning_builder():
        # Preserves the original single get_top_dashboard_subject() call for the
        # A35 seed (no extra call; TD-R4 behavior unchanged).
        seed = get_top_dashboard_subject()
        return get_reasoning_findings(seed.get("subject_id", "")) if seed else []

    return [
        {"name": "A27", "section_title": "Answer Timeline",
         "builder": lambda: build_global_answer_timeline(limit=20),
         "formatter": _fmt_answer_timeline, "order": 1, "enabled": True},
        {"name": "A28", "section_title": "Subject Priority",
         "builder": lambda: build_subject_priority_list(limit=20),
         "formatter": _fmt_subject_priority, "order": 2, "enabled": True},
        {"name": "A29", "section_title": "Context Recommendations",
         "builder": lambda: build_recommendation_list(limit=20),
         "formatter": _fmt_recommendations, "order": 3, "enabled": True},
        {"name": "A30", "section_title": "Context Dashboard",
         "builder": lambda: get_dashboard_summary(),
         "formatter": _fmt_context_dashboard, "order": 4, "enabled": True},
        {"name": "B27", "section_title": "Status Overview",
         "builder": lambda: build_status_list(limit=20),
         "formatter": _fmt_status_overview, "order": 5, "enabled": True},
        {"name": "B28", "section_title": "Status Timeline",
         "builder": lambda: build_timeline_list(limit=10),
         "formatter": _fmt_status_timeline, "order": 6, "enabled": True},
        {"name": "B29", "section_title": "Status Consistency",
         "builder": lambda: build_consistency_list(limit=20),
         "formatter": _fmt_status_consistency, "order": 7, "enabled": True},
        {"name": "B30", "section_title": "Status Dashboard",
         "builder": lambda: get_top_dashboard_subject(),
         "formatter": _fmt_status_dashboard, "order": 8, "enabled": True},
        {"name": "A31", "section_title": "Fact Relations",
         "builder": lambda: build_fact_graph_list(),
         "formatter": _fmt_fact_relations, "order": 9, "enabled": True},
        {"name": "A32", "section_title": "Subject Relations",
         "builder": lambda: build_subject_graph(),
         "formatter": _fmt_subject_relations, "order": 10, "enabled": True},
        {"name": "A33", "section_title": "Consistency Overview",
         "builder": lambda: build_consistency_report_list(),
         "formatter": _fmt_consistency_overview, "order": 11, "enabled": True},
        {"name": "A35", "section_title": "Reasoning",
         "builder": _reasoning_builder,
         "formatter": _fmt_reasoning, "order": 12, "enabled": True},
    ]


def _run_runtime_section_plugins(parts: list) -> None:
    """Plugin Loader: execute each enabled EngineSpec in order; append the
    non-empty formatted section to parts.  Runtime-layer; read-only; does not own
    persistence; does not call memory_build_context (no cycle)."""
    for spec in sorted(_runtime_section_plugins(), key=lambda s: s["order"]):
        if not spec.get("enabled", True):
            continue
        body = spec["formatter"](spec["builder"]())
        if body:
            parts.append(f"=== {spec['section_title']} ===\n{body}")


# ── Memory Context Builder ────────────────────────────────────────────────────

def memory_build_context() -> str:
    """Build a combined context string from all memory stores for Meeting Analysis injection."""
    summaries     = memory_get_recent_summaries(limit=5)
    questions     = memory_get_questions(limit=10)
    open_dec      = memory_get_open_decisions(limit=20)
    done_dec      = memory_get_done_decisions(limit=5)

    # A22-4: lifecycle state classification (new local vars; existing logic untouched)
    _all_dec        = memory_get_decisions(limit=200)
    in_progress_dec = [e for e in _all_dec if _decision_status(e) == "IN_PROGRESS"]
    blocked_dec     = [e for e in _all_dec if _decision_status(e) == "BLOCKED"]
    cancelled_dec   = [e for e in _all_dec if _decision_status(e) == "CANCELLED"][-5:]

    show_info(
        f"[memory] context loaded "
        f"summaries={len(summaries)} questions={len(questions)} "
        f"open_decisions={len(open_dec)} done_decisions={len(done_dec)}"
    )

    parts: list[str] = []

    # ── Subject Memory section ────────────────────────────────────────────────
    subject_context = _build_subject_context()
    if subject_context:
        subj_lines: list[str] = []
        unassigned_block: Optional[str] = None

        sorted_subjects = sorted(
            ((sid, bucket) for sid, bucket in subject_context.items() if sid != "_unassigned"),
            key=lambda pair: _subject_priority_score(pair[1]),
            reverse=True,
        )
        compressed_subjects: list[str] = []
        n_merged = 0
        for sid, bucket in sorted_subjects:
            subj = bucket.get("subject") or {}
            name = subj.get("canonical_name", sid)
            if _subject_is_compressible(bucket):
                compressed_subjects.append(name)
                if _subject_is_merged(subj):
                    n_merged += 1
                continue
            subj_lines.append(_render_subject_block(
                name,
                _subject_lifecycle(bucket),
                _subject_status(bucket),
                bucket.get("facts", []),
                bucket.get("decisions", []),
                bucket.get("questions", []),
                False,
            ))

        if compressed_subjects:
            subj_lines.insert(0, "=== Compressed Subjects ===\n" + "\n".join(compressed_subjects))

        n_compressed = len(compressed_subjects)
        n_active = len(sorted_subjects) - n_compressed
        n_total = len(sorted_subjects)
        compression_ratio = round(n_compressed * 100 / n_total) if n_total > 0 else 0
        merge_candidates = _subject_merge_candidates()
        merge_block = ""
        if merge_candidates:
            scored = sorted(
                ((a, b, _subject_merge_confidence(a, b)) for a, b in merge_candidates),
                key=lambda x: x[2],
                reverse=True,
            )
            merge_lines = "\n".join(
                f"{a} -> {b} ({c}%) [{_subject_merge_recommendation(a, b)}]"
                for a, b, c in scored
            )
            merge_block = f"\n\n=== Merge Candidates ===\n{merge_lines}"

        merge_plan = _subject_merge_plan()
        merge_plan_block = ""
        if merge_plan:
            plan_lines = "\n".join(f"{p['source']} -> {p['target']}" for p in merge_plan)
            merge_plan_block = f"\n\n=== Merge Plan ===\n{plan_lines}"

        validation_errors = _subject_merge_validate()
        validation_block = ""
        if validation_errors:
            formatted: list[str] = []
            for err in validation_errors:
                if ": " in err:
                    label, detail = err.split(": ", 1)
                    formatted.append(f"{label}:\n{detail}")
                else:
                    formatted.append(err)
            validation_block = "\n\n=== Merge Validation ===\n\n" + "\n\n".join(formatted)

        audit_stats  = _merge_history_stats()
        audit_recent = _merge_history_recent(limit=5)
        audit_block  = ""
        if audit_stats["total_merges"] > 0:
            latest_lines = "\n".join(
                f"{r.get('source_name', '')} -> {r.get('target_name', '')}"
                for r in audit_recent
            )
            audit_block = (
                f"\n\n=== Merge Audit ===\n"
                f"Total Merges: {audit_stats['total_merges']}\n"
                f"APPROVE: {audit_stats['approve_merges']}\n"
                f"REVIEW: {audit_stats['review_merges']}\n"
                f"Latest Merges:\n{latest_lines}"
            )

        approval_stats = _merge_approval_stats()
        approval_block = (
            f"\n\n=== Merge Approval ===\n"
            f"Pending: {approval_stats['pending']}\n"
            f"Running: {approval_stats['running']}\n"
            f"Approved: {approval_stats['approved']}\n"
            f"Failed: {approval_stats['failed']}"
        )

        stats_block = (
            f"=== Subject Statistics ===\n"
            f"Active Subjects: {n_active}\n"
            f"Compressed Subjects: {n_compressed}\n"
            f"Merged Subjects: {n_merged}\n"
            f"Compression Ratio: {compression_ratio}%"
            + merge_block
            + merge_plan_block
            + validation_block
            + audit_block
            + approval_block
        )
        subj_lines.insert(0, stats_block)

        if "_unassigned" in subject_context:
            bucket = subject_context["_unassigned"]
            compressible = _subject_is_compressible(bucket)
            unassigned_block = _render_subject_block(
                "Unassigned",
                _subject_lifecycle(bucket),
                _subject_status(bucket),
                bucket.get("facts", []),
                bucket.get("decisions", []),
                bucket.get("questions", []),
                compressible,
            )

        if unassigned_block:
            subj_lines.append(unassigned_block)

        if subj_lines:
            parts.append("=== Subject Memory ===\n" + "\n\n---\n\n".join(subj_lines))
    # ─────────────────────────────────────────────────────────────────────────

    # Open Decisions injected first (highest priority for next-meeting follow-up)
    if open_dec:
        lines = "\n".join(
            f"* {e['decision']}"
            + (f" [担当: {e['owner']}]" if e.get("owner") else "")
            + (f" [期日: {e['due']}]"   if e.get("due")   else "")
            for e in open_dec
        )
        parts.append(f"=== Open Decisions ===\n{lines}")

    # A22-4: In Progress Decisions (limit=20, due_date priority)
    if in_progress_dec:
        lines = "\n".join(
            f"* {e['decision']}"
            + (f" [担当: {e['owner']}]" if e.get("owner") else "")
            + (
                f" [期日: {e['due_date']}]"
                if e.get("due_date")
                else (f" [期日: {e['due']}]" if e.get("due") else "")
            )
            for e in in_progress_dec[-20:]
        )
        parts.append(f"=== In Progress Decisions ===\n{lines}")

    # A22-4: Blocked Decisions (limit=20, due_date priority)
    if blocked_dec:
        lines = "\n".join(
            f"* {e['decision']}"
            + (f" [担当: {e['owner']}]" if e.get("owner") else "")
            + (
                f" [期日: {e['due_date']}]"
                if e.get("due_date")
                else (f" [期日: {e['due']}]" if e.get("due") else "")
            )
            for e in blocked_dec[-20:]
        )
        parts.append(f"=== Blocked Decisions ===\n{lines}")

    # Open Decisions by Owner (A20)
    if open_dec:
        owner_buckets: dict[str, list] = {}
        unowned: list = []
        for dec in open_dec:
            owner_name = dec.get("owner") or ""
            if owner_name:
                owner_buckets.setdefault(owner_name, []).append(dec)
            else:
                unowned.append(dec)
        if owner_buckets or unowned:
            by_owner_parts: list[str] = []
            for owner_name in sorted(owner_buckets):
                decs = owner_buckets[owner_name]
                by_owner_parts.append(f"{owner_name} ({len(decs)})")
                by_owner_parts.append("")
                for dec in decs:
                    action = dec.get("action") or dec.get("decision", "")
                    by_owner_parts.append(f"- {action}")
                by_owner_parts.append("")
            if unowned:
                by_owner_parts.append(f"担当者未定 ({len(unowned)})")
                by_owner_parts.append("")
                for dec in unowned:
                    action = dec.get("action") or dec.get("decision", "")
                    by_owner_parts.append(f"- {action}")
            parts.append(
                "=== Open Decisions by Owner ===\n" + "\n".join(by_owner_parts).strip()
            )

    # A21-5: Upcoming Due Dates / Overdue Decisions
    today_str = _datetime.date.today().isoformat()
    upcoming: list = []
    overdue: list = []
    for dec in open_dec:
        dd = dec.get("due_date")
        if not dd:
            continue
        if dd >= today_str:
            upcoming.append(dec)
        else:
            overdue.append(dec)

    if upcoming:
        upcoming.sort(key=lambda d: d["due_date"])
        date_groups: dict[str, list] = {}
        for dec in upcoming:
            date_groups.setdefault(dec["due_date"], []).append(dec)
        group_parts: list[str] = []
        for date in sorted(date_groups):
            items = [f"- {dec['decision']} ({dec.get('owner') or '未設定'})"
                     for dec in date_groups[date]]
            group_parts.append(date + "\n" + "\n".join(items))
        parts.append("=== Upcoming Due Dates ===\n\n" + "\n\n".join(group_parts))

    if overdue:
        overdue.sort(key=lambda d: d["due_date"])
        date_groups2: dict[str, list] = {}
        for dec in overdue:
            date_groups2.setdefault(dec["due_date"], []).append(dec)
        group_parts2: list[str] = []
        for date in sorted(date_groups2):
            items = [f"- {dec['decision']} ({dec.get('owner') or '未設定'})"
                     for dec in date_groups2[date]]
            group_parts2.append(date + "\n" + "\n".join(items))
        parts.append("=== Overdue Decisions ===\n\n" + "\n\n".join(group_parts2))

    if done_dec:
        lines = "\n".join(f"* {e['decision']}" for e in done_dec)
        parts.append(f"=== Completed Decisions ===\n{lines}")

    # A22-4: Cancelled Decisions
    if cancelled_dec:
        lines = "\n".join(f"* {e['decision']}" for e in cancelled_dec)
        parts.append(f"=== Cancelled Decisions ===\n{lines}")

    if summaries:
        lines = "\n".join(f"- [{e['timestamp']}] {e['summary']}" for e in summaries)
        parts.append(f"=== Rolling Summary ===\n{lines}")

    if questions:
        lines = "\n".join(
            f"- Q: {e['question']}\n  A: {e.get('answer', '')}" for e in questions
        )
        parts.append(f"=== Previous Questions ===\n{lines}")

    # R5 Plugin Loader: the 12 R4-integrated engine sections (A27-A30 / B27-B30 /
    # A31 / A32 / A33 / A35) are produced by the runtime section plugin registry.
    # Migrated verbatim from the former inline blocks; output is byte-equivalent.
    _run_runtime_section_plugins(parts)

    return "\n\n".join(parts)


# ── Memory extraction helpers ─────────────────────────────────────────────────

def _memory_extract_section(text: str, section_name: str) -> str:
    """Extract content of a # SectionName block from the analysis output."""
    m = re.search(
        rf"#\s*{re.escape(section_name)}\s*\n(.*?)(?=\n#\s|\Z)",
        text, re.DOTALL
    )
    return m.group(1).strip() if m else ""


def _memory_save_questions_from_section(questions_text: str) -> None:
    """Parse 質問N: / 回答候補: pairs and save new entries to question memory."""
    lines        = questions_text.splitlines()
    current_q: Optional[str] = None
    answer_lines: list[str]  = []
    in_answer    = False

    def _flush() -> None:
        if current_q and answer_lines:
            answer = " ".join(answer_lines).strip()
            if answer and not memory_question_similar_exists(current_q):
                memory_add_question(current_q, answer)

    for line in lines:
        stripped = line.strip()
        if re.match(r"質問\s*\d+[:：]", stripped):
            _flush()
            # Use "" (not None) as sentinel for "header seen, text pending"
            current_q    = re.sub(r"^質問\s*\d+[:：]\s*", "", stripped).strip()
            answer_lines = []
            in_answer    = False
        elif current_q is None:
            pass   # not inside a question block — skip
        elif re.match(r"回答候補[:：]", stripped):
            in_answer = True
        elif stripped.startswith("（") and stripped.endswith("）"):
            inner = stripped[1:-1].strip()
            if in_answer:
                answer_lines.append(inner)
            elif not current_q:           # "" = awaiting question body
                current_q = inner
        elif in_answer and stripped:
            answer_lines.append(stripped)
        elif not in_answer and stripped:
            current_q = (current_q + " " + stripped).strip() if current_q else stripped

    _flush()


def _extract_action(text: str) -> str:
    """Strip Subject prefix to obtain the action verb phrase."""
    return re.sub(r'^[【\[(（][^】\]）\)]+[】\]）\)]\s*', '', text).strip() or text


def _extract_owner(text: str) -> Optional[str]:
    """Extract owner name from decision text.

    First matched person wins.
    Multi-owner extraction is not supported.
    Planned for A20 Owner Intelligence.
    """
    m = _OWNER_RE.search(text)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip() or None


def _extract_due_date(text: str) -> tuple:
    """Extract due date expression from decision text.

    Returns (raw_due, due_type) where due_type is one of:
    "absolute", "relative", "unresolved", or (None, None) when no match.

    Priority: EN-relative > JP-absolute > JP-relative > EN-absolute > unresolved.
    EN-relative must come first so that phrases like "due on 2026-06-30" and
    "before June 30" are classified as relative before the ISO date substring
    is claimed by JP-absolute.
    """
    for pattern, due_type in (
        (_DUE_REL_EN,     "relative"),
        (_DUE_ABS_JP,     "absolute"),
        (_DUE_REL_JP,     "relative"),
        (_DUE_ABS_EN,     "absolute"),
        (_DUE_UNRESOLVED, "unresolved"),
    ):
        m = pattern.search(text)
        if m:
            return (m.group(0).strip(), due_type)
    return (None, None)


def _last_day_of_month(year: int, month: int) -> _datetime.date:
    """Return the last calendar day of the given year/month."""
    if month == 12:
        return _datetime.date(year + 1, 1, 1) - _datetime.timedelta(days=1)
    return _datetime.date(year, month + 1, 1) - _datetime.timedelta(days=1)


def _normalize_due_date(
    raw_due: Optional[str],
    due_type: Optional[str],
    *,
    _today: Optional[_datetime.date] = None,
) -> tuple:
    """Normalize a raw due-date expression to (iso_str, confidence).

    iso_str is YYYY-MM-DD or None.  confidence is 0.0-1.0 or None.
    _today is injectable for deterministic testing; defaults to date.today().
    """
    if raw_due is None or due_type is None:
        return (None, None)

    today = _today or _datetime.date.today()

    if due_type == "unresolved":
        return (None, 0.4)

    # ── absolute ──────────────────────────────────────────────────────────────
    if due_type == "absolute":
        # YYYY/MM/DD or YYYY-MM-DD
        m = re.match(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', raw_due)
        if m:
            try:
                d = _datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
                return (d.isoformat(), 1.0)
            except ValueError:
                pass

        # X月Y日 — supplement current year; roll to next year if date has passed
        m = re.match(r'(\d{1,2})月(\d{1,2})日', raw_due)
        if m:
            mon, day = int(m.group(1)), int(m.group(2))
            year = today.year if (mon, day) >= (today.month, today.day) else today.year + 1
            try:
                return (_datetime.date(year, mon, day).isoformat(), 0.95)
            except ValueError:
                pass

        # English "June 30" or "June 30, 2026"
        m = re.match(
            r'(january|february|march|april|may|june|july|august|september|october|november|december'
            r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})(?:,\s*(\d{4}))?',
            raw_due, re.IGNORECASE,
        )
        if m:
            mon = _DUE_MONTH_MAP.get(m.group(1).lower())
            if mon:
                day = int(m.group(2))
                year = int(m.group(3)) if m.group(3) else (
                    today.year if (mon, day) >= (today.month, today.day) else today.year + 1
                )
                try:
                    return (_datetime.date(year, mon, day).isoformat(), 0.9)
                except ValueError:
                    pass

        return (None, 0.3)

    # ── relative ──────────────────────────────────────────────────────────────
    if due_type == "relative":
        r = raw_due

        if r == "明日":
            return ((today + _datetime.timedelta(days=1)).isoformat(), 0.9)

        if r == "明後日":
            return ((today + _datetime.timedelta(days=2)).isoformat(), 0.9)

        if r in ("今日中", "当日中"):
            return (today.isoformat(), 0.9)

        if r.startswith("今週"):
            suffix = r[2:]
            if "末" in suffix:
                days = (6 - today.weekday()) % 7 or 7  # Sunday; if today=Sun -> next Sun
                return ((today + _datetime.timedelta(days=days)).isoformat(), 0.6)
            else:
                days = (4 - today.weekday()) % 7       # Friday; if today=Fri -> 0 = today
                return ((today + _datetime.timedelta(days=days)).isoformat(), 0.8)

        if r.startswith("今月"):
            return (_last_day_of_month(today.year, today.month).isoformat(), 0.6)

        if r.startswith("来週"):
            suffix = r[2:]
            days_to_mon = (7 - today.weekday()) % 7 or 7
            next_mon = today + _datetime.timedelta(days=days_to_mon)
            if "末" in suffix:
                return ((next_mon + _datetime.timedelta(days=6)).isoformat(), 0.6)
            for jp, wd in _DUE_WEEKDAY_JP.items():
                if suffix.startswith(jp):
                    return ((next_mon + _datetime.timedelta(days=wd)).isoformat(), 0.8)
            return ((next_mon + _datetime.timedelta(days=4)).isoformat(), 0.7)  # Friday default

        if r.startswith("来月"):
            nm = today.month + 1
            ny = today.year
            if nm > 12:
                nm, ny = 1, ny + 1
            suffix = r[2:]
            m2 = re.search(r'(\d+)日', suffix)
            if m2:
                try:
                    return (_datetime.date(ny, nm, int(m2.group(1))).isoformat(), 0.7)
                except ValueError:
                    pass
            return (_last_day_of_month(ny, nm).isoformat(), 0.6)

        # [月火水木金土日]曜日まで
        m2 = re.match(r'([月火水木金土日])曜日?まで(?:に)?', r)
        if m2:
            wd = _DUE_WEEKDAY_JP.get(m2.group(1), 4)
            days = (wd - today.weekday()) % 7 or 7
            return ((today + _datetime.timedelta(days=days)).isoformat(), 0.8)

        # ── English relative ──────────────────────────────────────────────────
        rl = r.lower()

        # "due on/by YYYY-MM-DD"
        m2 = re.search(r'due\s+(?:on|by)\s+(\d{4}-\d{2}-\d{2})', rl)
        if m2:
            parts = m2.group(1).split("-")
            try:
                d = _datetime.date(int(parts[0]), int(parts[1]), int(parts[2]))
                return (d.isoformat(), 0.95)
            except (ValueError, IndexError):
                pass

        # "before Month Day [, Year]"
        m2 = re.search(
            r'before\s+(january|february|march|april|may|june|july|august|september|october|november|december'
            r'|jan|feb|mar|apr|jun|jul|aug|sep|oct|nov|dec)\s+(\d{1,2})(?:,\s*(\d{4}))?',
            r, re.IGNORECASE,
        )
        if m2:
            mon = _DUE_MONTH_MAP.get(m2.group(1).lower())
            if mon:
                day = int(m2.group(2))
                year = int(m2.group(3)) if m2.group(3) else (
                    today.year if (mon, day) >= (today.month, today.day) else today.year + 1
                )
                try:
                    return (_datetime.date(year, mon, day).isoformat(), 0.7)
                except ValueError:
                    pass

        # "by next week" / "by end of next week"
        if re.search(r'by\s+(?:the\s+)?(?:end\s+of\s+)?next\s+week', rl):
            days_to_mon = (7 - today.weekday()) % 7 or 7
            next_fri = today + _datetime.timedelta(days=days_to_mon + 4)
            return (next_fri.isoformat(), 0.7)

        # "by [next] Weekday"
        m2 = re.match(
            r'by\s+(next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)', rl,
        )
        if m2:
            wd = _DUE_WEEKDAY_EN[m2.group(2)]
            if m2.group(1):  # "by next Friday" -> next week's that day
                days_to_mon = (7 - today.weekday()) % 7 or 7
                next_mon = today + _datetime.timedelta(days=days_to_mon)
                d = next_mon + _datetime.timedelta(days=wd)
            else:            # "by Friday" -> next upcoming that day
                days = (wd - today.weekday()) % 7 or 7
                d = today + _datetime.timedelta(days=days)
            return (d.isoformat(), 0.8)

        # "by end of [this] month"
        if re.search(r'by\s+end\s+of\s+(?:this\s+)?month', rl):
            return (_last_day_of_month(today.year, today.month).isoformat(), 0.6)

    return (None, None)


def _decision_confidence(
    text: str,
    owner: Optional[str],
    due_date: Optional[str],
    keyword_match: bool,
) -> float:
    """Compute save-gate confidence for a Decision candidate.

    Hard prerequisite: keyword_match OR subject present.
    Without either, returns 0.0 (guaranteed below threshold).

    Scoring when prerequisite met:
      0.5  base
    + 0.3  keyword matched
    + 0.2  Subject present
    + 0.1  owner extracted
    + 0.1  due_date extracted
    """
    has_subject = bool(_subject_extract(text))
    if not keyword_match and not has_subject:
        return 0.0
    score = 0.5
    if keyword_match:
        score += 0.3
    if has_subject:
        score += 0.2
    if owner:
        score += 0.1
    if due_date:
        score += 0.1
    return min(score, 1.0)


def _memory_save_decisions_from_section(actions_text: str) -> None:
    """Save decision candidates that meet the confidence threshold."""
    debug_memory(f"actions_text={actions_text!r}")
    for line in actions_text.splitlines():
        debug_memory(f"raw_line={line!r}")
        stripped = line.strip().lstrip("-・•*# ").strip()
        if not stripped:
            continue
        debug_memory(f"decision_candidate={stripped!r}")
        keyword_match                    = bool(_DECISION_KEYWORDS_RE.search(stripped))
        action                           = _extract_action(stripped)
        owner                            = _extract_owner(action)
        raw_due, due_type                = _extract_due_date(stripped)
        normalized_due_date, due_confidence = _normalize_due_date(raw_due, due_type)
        conf = _decision_confidence(stripped, owner, raw_due, keyword_match)
        debug_memory(f"confidence={conf:.2f} keyword_match={keyword_match}")
        if conf >= _DECISION_MIN_CONFIDENCE:
            debug_memory(f"decision_saved={stripped!r}")
            memory_add_decision(
                stripped,
                owner=owner,
                action=action,
                due=raw_due,
                due_date=normalized_due_date,
                due_type=due_type,
                due_confidence=due_confidence,
                confidence=conf,
                source="meeting_analysis",
            )


def _memory_save_facts_from_section(facts_text: str) -> None:
    """Parse '- 【Subject名】fact_type: value' lines and save via _fact_create()."""
    for line in facts_text.splitlines():
        stripped = line.strip().lstrip("-・•*# ").strip()
        if not stripped or stripped == "なし":
            continue
        subject_name = _subject_extract(stripped)
        if not subject_name:
            continue
        rest = re.sub(r'^[【\[(（][^】\]）\)]+[】\]）\)]\s*', '', stripped)
        sep = re.search(r'[:：]', rest)
        if not sep:
            continue
        fact_type = rest[:sep.start()].strip()
        value     = rest[sep.end():].strip()
        if not fact_type or not value:
            continue
        subject_id = _subject_get_or_create(subject_name)
        _fact_create(subject_id, fact_type, value, source="meeting_analysis")


_COMPLETION_KEYWORDS_RE = re.compile(
    r"完了|実施済み|導入済み|対応済み|終了|クローズ|解決済み|[Dd]one"
)

_NEGATIVE_COMPLETION_RE = re.compile(
    r"未完了"
    r"|完了していない|完了していません"
    r"|完了できていない|完了できていません"
    r"|未対応|未実施|保留|対応予定|これから対応"
)

# Characters around a decision mention to scan for completion/negation signals
_COMPLETION_WINDOW = 60


def _memory_detect_completed_decisions(analysis_result: str) -> None:
    """Scan OPEN decisions; mark DONE only when completion is confirmed without negation."""
    open_decisions = memory_get_open_decisions(limit=100)
    if not open_decisions:
        return

    text_lower = analysis_result.lower()

    for entry in open_decisions:
        dec = entry.get("decision", "")
        if not dec:
            continue
        pos = text_lower.find(dec.lower())
        if pos == -1:
            continue
        window_start = max(0, pos - _COMPLETION_WINDOW)
        window_end   = min(len(analysis_result), pos + len(dec) + _COMPLETION_WINDOW)
        window       = analysis_result[window_start:window_end]

        # Negation takes priority — if the window contains a negative phrase, skip
        if _NEGATIVE_COMPLETION_RE.search(window):
            continue

        if _COMPLETION_KEYWORDS_RE.search(window):
            memory_update_decision_status(dec, "DONE")
            show_info(f'[memory] decision auto-completed decision="{dec}"')


_IN_PROGRESS_PATTERNS = re.compile(
    r"進めています|進めております|進めている"
    r"|対応中|作業中|実施中|調査中|開発中|検証中|実装中|取り組み中|準備中"
    r"|着手しました|着手した|着手しています"
    r"|working\s+on|in[\s\-]progress|currently\s+implementing"
    r"|under\s+investigation|underway|started|begun",
    re.IGNORECASE,
)

_BLOCKED_PATTERNS = re.compile(
    r"保留|停止|待機|ブロック"
    r"|承認待ち|レビュー待ち|依存待ち|確認待ち"
    r"|blocked|on\s+hold"
    r"|waiting\s+for\s+approval|waiting\s+for\s+review"
    r"|pending",
    re.IGNORECASE,
)

_CANCELLED_PATTERNS = re.compile(
    r"中止|取りやめ|キャンセル|不要"
    r"|実施しない|見送る|見送り|廃止|取り消し"
    r"|cancell?ed|abandoned|will\s+not\s+proceed|no\s+longer\s+needed",
    re.IGNORECASE,
)


def _memory_detect_status_transitions(text: str) -> Optional[str]:
    """Classify text into a lifecycle status based on keyword signals.

    Priority: CANCELLED > BLOCKED > IN_PROGRESS.
    Returns the detected status string, or None if no signal matches.
    """
    if _CANCELLED_PATTERNS.search(text):
        return "CANCELLED"
    if _BLOCKED_PATTERNS.search(text):
        return "BLOCKED"
    if _IN_PROGRESS_PATTERNS.search(text):
        return "IN_PROGRESS"
    return None


def _memory_apply_status_transition(text: str) -> int:
    """Detect a lifecycle signal in text and apply it to all OPEN decisions.

    Uses _memory_detect_status_transitions() for signal detection and
    _decision_update_status() for each per-record transition.
    Returns the number of decisions updated.
    """
    transition = _memory_detect_status_transitions(text)
    if transition is None:
        return 0
    updated = 0
    with _memory_lock:
        entries = _decision_load()
        for entry in entries:
            if _decision_status(entry) != "OPEN":
                continue
            if _decision_update_status(entry, transition):
                updated += 1
        if updated:
            _decision_save(entries)
    return updated


def _memory_extract_and_save(analysis_result: str) -> None:
    """Extract summary, questions, and decisions from analysis output and persist to memory."""
    try:
        summary_text = _memory_extract_section(analysis_result, "サマリー")
        if summary_text:
            memory_add_summary(summary_text)

        questions_text = _memory_extract_section(analysis_result, "検出された質問")
        if questions_text and "質問は検出されませんでした" not in questions_text:
            _memory_save_questions_from_section(questions_text)

        actions_text = _memory_extract_section(analysis_result, "推奨アクション")
        # Phase 4: detect completion for existing OPEN decisions before saving new ones
        _memory_detect_completed_decisions(analysis_result)
        if actions_text:
            _memory_save_decisions_from_section(actions_text)

        # Phase 4.5: detect IN_PROGRESS / BLOCKED / CANCELLED transitions
        _memory_apply_status_transition(analysis_result)

        # Phase 5: save confirmed facts from 確認された事実 section
        facts_text = _memory_extract_section(analysis_result, "確認された事実")
        if facts_text and facts_text.strip() not in ("なし", "特になし"):
            _memory_save_facts_from_section(facts_text)

    except Exception as e:
        show_warn(f"[memory] extraction error: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Summary
# ─────────────────────────────────────────────────────────────────────────────
def generate_summary() -> None:
    with _log_lock:
        log_copy = list(transcript_log)

    if not log_copy:
        show_info("ログが空です。")
        return

    # Grounding guard: refuse to summarize very short transcripts
    # (prevents hallucinated topics from a 2-utterance exchange)
    recruiter_count = sum(1 for e in log_copy if e.speaker == "recruiter")
    if recruiter_count < 2:
        show_info(
            f"トランスクリプトが短すぎます ({recruiter_count} 件のリクルーター発言) — "
            "まとめには最低2件以上必要です。"
        )
        return

    show_info(f"まとめ生成中 ({len(log_copy)}件の発話)…")

    recruiter_lines = [e for e in log_copy if e.speaker == "recruiter"]
    user_lines      = [e for e in log_copy if e.speaker in ("user", "agent")]
    other_lines     = [e for e in log_copy if e.speaker == "unknown"]

    def _fmt(entries: list) -> str:
        return "\n".join(f"  [{e.ts}] {e.text}" for e in entries) or "  (なし)"

    merged = (
        f"【リクルーターの発言】\n{_fmt(recruiter_lines)}\n\n"
        f"【ユーザーの発言・回答】\n{_fmt(user_lines)}"
        + (f"\n\n【その他】\n{_fmt(other_lines)}" if other_lines else "")
    )

    try:
        resp = _provider_default.generate(ProviderRequest(
            messages=[
                Message(role="system", content=SUMMARY_PROMPT),
                Message(role="user", content=merged),
            ],
            temperature=0.2,
            max_tokens=500,
        ))
        summary = resp.text.strip()
        show_sep()
        _print(f"{BOLD}【まとめ】{RESET}\n{summary}")
        show_sep()
    except Exception as e:
        show_err("Summary", e)


import re as _re_meeting

# ── Filler/noise patterns for meeting transcript cleanup (Task 2) ─────────────
_MEETING_FILLER_LINES = re.compile(
    r"^\s*(?:あ+[ーっ]*|あー+|ああ+|えー+|えっと|うーん|そのー|そのですね"
    r"|テスト+|[1-5])\s*$",
    re.MULTILINE,
)
_MEETING_FILLER_REPEAT = re.compile(
    r"^\s*(?:(?:あ|テスト|[1-5])[、,・\s]){2,}(?:あ|テスト|[1-5])?\s*$",
    re.MULTILINE,
)


def clean_meeting_transcript(text: str) -> str:
    """Remove filler/noise lines from a meeting transcript before GPT analysis.

    Targets single-word utterances and repetitive test sequences.
    Meaningful sentences are never removed.
    """
    lines_out = []
    for line in text.splitlines():
        text_part = line.split(": ", 1)[-1] if ": " in line else line
        stripped = text_part.strip()
        if _MEETING_FILLER_LINES.match(stripped):
            continue
        if _MEETING_FILLER_REPEAT.match(stripped):
            continue
        lines_out.append(line)
    return "\n".join(lines_out)


def generate_meeting_analysis() -> None:
    """
    v22 RuntimeMode.MEETING — structured meeting analysis.

    Called from two paths:
      1. reply_worker (after full pipeline: Whisper → is_meaningful → log append →
         persist → state transitions) — the fresh entry is already in transcript_log.
      2. Keyboard thread directly (empty buffer or non-manual-flush mode) — reads
         existing transcript_log with no new entry.

    Output: サマリー / リスク・懸念事項 / 検出された質問+回答候補 / 推奨アクション

    v22.1 additions:
      - Incremental analysis: only entries since last run (Task 5)
      - Token growth protection: truncate to MAX_MEETING_ANALYSIS_CHARS (Task 6)
      - Filler/noise cleanup via clean_meeting_transcript() (Task 2)
    """
    global _meeting_cursor

    with _log_lock:
        log_copy = list(transcript_log)

    if not log_copy:
        show_info("[meeting] トランスクリプトが空です。")
        return

    # Task 5: incremental — only new entries since last analysis.
    # min() guards against cursor drift when transcript_log is cleared/rotated.
    with _meeting_cursor_lock:
        cursor = min(_meeting_cursor, len(log_copy))

    new_entries = log_copy[cursor:]

    if not new_entries:
        show_info("[meeting] 新しい発話がありません。(前回分析以降の追加なし)")
        return

    show_info(
        f"[meeting] ミーティング分析中 "
        f"(entries {cursor}–{cursor + len(new_entries) - 1}, 計{len(new_entries)}件)…"
    )

    lines = "\n".join(f"[{e.ts}] {e.speaker}: {e.text}" for e in new_entries)

    # Task 2: filler/noise cleanup
    lines = clean_meeting_transcript(lines)

    # Task 6: token growth protection — tail-priority truncation
    if len(lines) > MAX_MEETING_ANALYSIS_CHARS:
        original_len = len(lines)
        lines = lines[-MAX_MEETING_ANALYSIS_CHARS:]
        show_info(
            f"[meeting] context truncated "
            f"chars={original_len} -> {MAX_MEETING_ANALYSIS_CHARS}"
        )

    # Memory Layer v1: inject memory context before transcript
    mem_ctx = memory_build_context()
    if mem_ctx:
        user_content = f"{mem_ctx}\n\n=== Recent Transcript ===\n{lines}"
    else:
        user_content = lines

    try:
        resp = _provider_default.generate(ProviderRequest(
            messages=[
                Message(role="system", content=_MEETING_ANALYSIS_PROMPT),
                Message(role="user", content=user_content),
            ],
            temperature=0.2,
            max_tokens=1500,
        ))
        result = resp.text.strip()
        _emit_event("analysis", text=result)
        show_sep()
        _print(f"{BOLD}【ミーティング分析】{RESET}\n{result}")
        show_sep()

        # Memory Layer v1: extract and save summary/questions/decisions
        _memory_extract_and_save(result)

        # Task 5: advance cursor only after successful analysis
        with _meeting_cursor_lock:
            _meeting_cursor = len(log_copy)

    except Exception as e:
        show_err("Meeting Analysis", e)


# ─────────────────────────────────────────────────────────────────────────────
# Keyboard help text
# ─────────────────────────────────────────────────────────────────────────────
_HELP_MANUAL = "\n".join([
    f"{GRAY}{'─'*40}{RESET}",
    f"{BOLD}キー操作 / Keyboard Commands  [MANUAL-FLUSH MODE]{RESET}",
    f"  {BOLD}r{RESET}      toggle recording ●ON / ○OFF      {GRAY}(no API — instant){RESET}",
    f"  {BOLD}g{RESET}      ★ FLUSH → Whisper → ミーティング分析 {GRAY}(main action){RESET}",
    f"  {BOLD}G{RESET}      インタビューまとめ生成",
    f"  {BOLD}h{RESET}      hold phrase    {GRAY}[HOLD]{RESET}  (no API)",
    f"  {BOLD}u{RESET}      clarify/repeat {GRAY}[REPEAT]{RESET} (no API — instant)",
    f"  {BOLD}d{RESET}      EN delay phrase {GRAY}[DLY]{RESET}",
    f"  {BOLD}t{RESET}      JP phrase       {GRAY}[考]{RESET}",
    f"  {BOLD}1-5{RESET}    EN phrase slots",
    f"  {BOLD}s{RESET}      show state / buffer status / stop TTS",
    f"  {BOLD}c{RESET}      clear log + buffer",
    f"  {BOLD}l{RESET}      display transcript log",
    f"  {BOLD}?{RESET}      show this help",
    f"  {BOLD}q{RESET}      quit",
    f"{GRAY}{'─'*40}{RESET}",
])

_HELP = "\n".join([
    f"{GRAY}{'─'*40}{RESET}",
    f"{BOLD}キー操作 / Keyboard Commands{RESET}",
    f"  {BOLD}r{RESET}      toggle recording ●ON / ○OFF  {GRAY}(instant){RESET}",
    f"  {BOLD}h{RESET}      hold phrase    {GRAY}[HOLD]{RESET}  (no API)",
    f"  {BOLD}u{RESET}      clarify/repeat {GRAY}[REPEAT]{RESET} (no API — instant)",
    f"  {BOLD}d{RESET}      EN delay phrase {GRAY}[DLY]{RESET}",
    f"  {BOLD}t{RESET}      JP phrase       {GRAY}[考]{RESET}",
    f"  {BOLD}1-5{RESET}    EN phrase slots",
    f"  {BOLD}s{RESET}      show conversation state / stop TTS",
    f"  {BOLD}g{RESET}      ミーティング分析",
    f"  {BOLD}G{RESET}      インタビューまとめ生成",
    f"  {BOLD}c{RESET}      clear transcript log",
    f"  {BOLD}l{RESET}      display transcript log",
    f"  {BOLD}?{RESET}      show this help",
    f"  {BOLD}q{RESET}      quit",
    f"{GRAY}{'─'*40}{RESET}",
])


# ─────────────────────────────────────────────────────────────────────────────
# Feature 8 — Runtime health monitor (daemon thread)
# ─────────────────────────────────────────────────────────────────────────────
def health_monitor() -> None:
    """
    [MODULE: runtime.health]
    Periodic runtime health snapshot. v15: uses runtime.health module when available,
    includes latency tracker p90, and structured get_runtime_health() output.
    """
    interval = args.health_interval
    if interval <= 0:
        return

    time.sleep(interval)

    while not _shutdown.is_set():
        aq    = audio_queue.qsize()
        tq    = transcript_queue.qsize()
        rate  = _overflow_rate_per_min()
        with _log_lock:
            log_n = len(transcript_log)

        agent_mode = args.agent or _ENV.get("AGENT_MODE", False)
        mode_label = (
            "MANUAL-FLUSH" if args.manual_flush else
            ("AGENT" if agent_mode else "observer")
        )

        buf_sec, buf_segs, _buf_flush_ts = _vad_buf.get_stats()

        lat_summary = latency_tracker.summary() if latency_tracker else None

        if _HEALTH_MODULE_LOADED:
            snap = get_runtime_health(
                session_id          = _SESSION_ID or "—",
                conv_state_value    = _get_state().value,
                mode_label          = mode_label,
                manual_buf_sec      = buf_sec,
                manual_buf_segments = buf_segs,
                audio_q_depth       = aq,
                transcript_q_depth  = tq,
                overflow_rate       = rate,
                log_entries         = log_n,
                tts_active          = _ACTIVE_TTS.is_speaking(),
                last_flush_ts       = _buf_flush_ts,
                latency_summary     = lat_summary,
                audio_q_maxsize     = AUDIO_QUEUE_MAXSIZE,
            )
            show_info(format_health_line(snap))
            if args.manual_flush:
                show_info(format_manual_buf_line(snap))

        if lat_summary and lat_summary.get("count", 0) > 0:
            show_info(
                f"[latency]  stt_avg={lat_summary['stt_avg_ms']:.0f}ms  "
                f"gpt_avg={lat_summary['gpt_avg_ms']:.0f}ms  "
                f"total_p90={lat_summary['total_p90_ms']:.0f}ms  "
                f"(n={lat_summary['window']})"
            )

        if _ENV.get("QUEUE_METRICS"):
            show_info(
                f"[metrics]  audio_q_util={aq/AUDIO_QUEUE_MAXSIZE*100:.1f}%  "
                f"transcript_q_maxsize=4"
            )

        # Thread health diagnostics (always — low cost, high value)
        threads_alive = {t.name: t.is_alive() for t in threading.enumerate()}
        dead = [n for n, alive in threads_alive.items() if not alive and n in ("reply-worker", "audio-capture")]
        if dead:
            show_warn(f"[health] DEAD THREADS: {dead} — runtime may be stalled!")
        _trace("health/threads", f"alive={list(threads_alive.keys())}")
        _trace("health/queues", f"audio_q={aq} transcript_q={tq} overflow_rate={rate:.1f}/min")

        for _ in range(interval * 10):
            if _shutdown.is_set():
                return
            time.sleep(0.1)


# ─────────────────────────────────────────────────────────────────────────────
# Keyboard loop  (Feature 1 — hardened against terminal / IME instability)
# ─────────────────────────────────────────────────────────────────────────────
def keyboard_loop() -> None:
    """
    [MODULE: ui.keyboard]
    v15: Uses extracted KeyboardController when ui.keyboard module is available.
    Falls back to hardened inline loop if module import failed.

    CRITICAL BUG FIX (v14): cmd_lower == 'g' fired before cmd == 'G' check.
    When user typed 'G', cmd_lower was 'g', so manual-flush triggered instead of summary.
    Fix: check raw cmd == 'G' BEFORE cmd_lower == 'g' in all dispatch paths.
    The KeyboardController module implements this correctly.
    """
    if _KEYBOARD_MODULE_LOADED:
        ctx = RuntimeContext(
            shutdown_event           = _shutdown,
            manual_flush_enabled     = args.manual_flush,
            sample_rate              = SAMPLE_RATE,
            enqueue_latest_fn        = _enqueue_latest,
            manual_buf_flush_fn      = _vad_buf.flush,
            manual_buf_status_fn     = _vad_buf.status,
            manual_buf_lock          = _vad_buf.lock,
            manual_audio_buffer      = _vad_buf.audio_buffer,
            recording_active         = _vad_buf.recording_active,
            show_recording_status_fn = _vad_buf.show_recording_status,
            tts                      = _ACTIVE_TTS,
            tts_interrupt_event      = _tts_interrupt,
            get_state_fn             = _get_state,
            set_state_fn             = _set_state,
            idle_state               = ConversationState.IDLE,
            transcript_log           = transcript_log,
            log_lock                 = _log_lock,
            show_info                = show_info,
            show_warn                = show_warn,
            show_sep                 = show_sep,
            show_hold                = show_hold,
            show_clarify_fn          = show_random_clarify,
            show_delay_en_fn         = show_random_delay_en,
            show_delay_jp_fn         = show_random_delay_jp,
            show_delay_slot_fn       = show_delay_slot,
            print_fn                 = _print,
            delay_en_list            = _DELAY_EN,
            delay_jp_list            = _DELAY_JP,
            delay_en_slots           = _DELAY_EN_SLOTS,
            generate_summary_fn      = generate_summary,
            generate_meeting_analysis_fn = generate_meeting_analysis,
            debug_audio_save         = _ENV["DEBUG_AUDIO_SAVE"],
            save_debug_audio_fn      = _save_debug_audio,
            set_runtime_mode_fn      = _set_runtime_mode,
            runtime_mode_meeting     = RuntimeMode.MEETING,
            agent_mode               = args.agent,
            tts_name                 = args.tts,
            CYAN=CYAN, YELLOW=YELLOW, GREEN=GREEN,
            GRAY=GRAY, RESET=RESET, BOLD=BOLD, WHITE=WHITE,
            agent_env_flag           = _ENV.get("AGENT_MODE", False),
        )
        controller = KeyboardController(ctx)
        controller.run()
        return

    # ── Inline fallback (preserved for safety, with G/g bug fixed) ────────
    _print(_HELP_MANUAL if args.manual_flush else _HELP)
    _eof_count = 0

    while not _shutdown.is_set():
        try:
            cmd = input().strip()   # NO .lower() here
        except EOFError:
            _eof_count += 1
            if _eof_count >= 3:
                show_info("stdin repeatedly closed — keyboard control disabled.")
                return
            show_info(f"stdin closed briefly ({_eof_count}/3) — retrying in 2s…")
            time.sleep(2.0)
            continue
        except UnicodeDecodeError as e:
            show_warn(f"Keyboard: encoding error ({e}) — ignored")
            continue
        except KeyboardInterrupt:
            break
        except Exception as e:
            show_warn(f"Keyboard: unexpected error ({e}) — continuing")
            continue

        _eof_count  = 0
        cmd_lower   = cmd.lower()

        try:
            # ── UPPERCASE G checked BEFORE cmd_lower == 'g' (BUG FIX) ───────
            if cmd == "G":
                threading.Thread(target=generate_summary, daemon=True, name="summary").start()

            elif cmd_lower == "r":
                # Toggle push-to-buffer recording (v17.2)
                if _vad_buf.recording_active.is_set():
                    _vad_buf.recording_active.clear()
                    show_info("○ RECORDING OFF — audio ignored until 'r' pressed again")
                else:
                    _vad_buf.recording_active.set()
                    show_info("● RECORDING ON")
                _vad_buf.show_recording_status()

            elif cmd_lower == "u":
                # Clarification / repeat-request phrase (v17.2)
                show_random_clarify()

            elif cmd_lower == "h":
                show_hold(random.choice(_DELAY_EN))
            elif cmd_lower == "d":
                show_random_delay_en()
            elif cmd_lower == "t":
                show_random_delay_jp()
            elif cmd_lower in ("1", "2", "3", "4", "5"):
                show_delay_slot(int(cmd_lower))

            elif cmd_lower == "g":
                if args.manual_flush:
                    merged = _vad_buf.flush()
                    if merged is not None:
                        if _ENV["DEBUG_AUDIO_SAVE"]:
                            _save_debug_audio(merged)
                        _set_runtime_mode(RuntimeMode.MEETING)
                        show_info(f"[meeting] {len(merged)/SAMPLE_RATE:.1f}s → Whisper → ミーティング分析…")
                        _enqueue_latest(merged)
                    else:
                        show_info("[meeting] バッファ空 — 現在のログで分析中…")
                        threading.Thread(
                            target=generate_meeting_analysis,
                            daemon=True,
                            name="meeting-analysis",
                        ).start()
                else:
                    threading.Thread(
                        target=generate_meeting_analysis,
                        daemon=True,
                        name="meeting-analysis",
                    ).start()

            elif cmd_lower == "s":
                state      = _get_state()
                agent_mode_s = args.agent or _ENV["AGENT_MODE"]
                mode_label = (
                    "MANUAL-FLUSH" if args.manual_flush else
                    ("AGENT" if agent_mode_s else "OBSERVER")
                )
                show_info(f"state={state.value}  mode={mode_label}  tts={args.tts}")
                _vad_buf.show_recording_status()   # v17.2: always show recording status on 's'
                if args.manual_flush:
                    show_info(f"buffer: {_vad_buf.status()}")
                if _ACTIVE_TTS.is_speaking():
                    _ACTIVE_TTS.stop()
                    _tts_interrupt.set()
                    show_info("TTS stopped.")
                    _set_state(ConversationState.IDLE)

            elif cmd_lower == "c":
                with _log_lock:
                    transcript_log.clear()
                with _vad_buf.lock:
                    _vad_buf.audio_buffer.clear()
                show_info("ログと音声バッファをクリアしました。")

            elif cmd_lower == "l":
                with _log_lock:
                    log_copy = list(transcript_log)
                if log_copy:
                    show_sep()
                    for i, e in enumerate(log_copy, 1):
                        if e.speaker == "recruiter":   tag = f"{CYAN}REC{RESET}"
                        elif e.speaker == "agent":     tag = f"{GREEN}AGT{RESET}"
                        elif e.speaker == "user":      tag = f"{YELLOW}YOU{RESET}"
                        else:                          tag = f"{GRAY}???{RESET}"
                        _print(f"  {GRAY}{i:02d} {e.ts}{RESET} [{tag}] {e.text}")
                    show_sep()
                else:
                    show_info("ログは空です。")

            elif cmd_lower in ("q", "quit", "exit"):
                show_info("終了します。")
                _shutdown.set()
                break

            elif cmd_lower == "?":
                _print(_HELP_MANUAL if args.manual_flush else _HELP)

        except Exception as e:
            show_warn(f"Keyboard command error ({e}) — continuing")


# ─────────────────────────────────────────────────────────────────────────────
# Control Event loop (H6: remote keyboard-equivalent commands over Cloud Run)
# ─────────────────────────────────────────────────────────────────────────────
def control_loop() -> None:
    """
    When this process is spawned by runtime.cloud_run_shell, PHANTOM_CONTROL_FD
    names a pipe fd that runtime.transport_gateway relays inbound WebSocket
    text frames (Control Events) into verbatim, one JSON command per line
    (see runtime/transport_gateway.py's inbound handler). This is a second
    trigger path into the same dispatch already reachable from the local
    keyboard loop's 'G'/'g'/'r' commands (see keyboard_loop() above) -- no
    new business logic, only a remote-command entry point onto it.

    Local/dev runs without PHANTOM_CONTROL_FD set are unaffected: this
    function returns immediately and control_loop's thread exits.

    Recognized commands (JSON object with a "command" key):
      {"command": "generate_summary"}           -- same as keyboard 'G'
      {"command": "generate_meeting_analysis"}   -- same as keyboard 'g'
      {"command": "toggle_recording"}            -- same as keyboard 'r'
    """
    fd_str = os.getenv("PHANTOM_CONTROL_FD", "").strip()
    if not fd_str:
        return
    try:
        control_file = os.fdopen(int(fd_str), "r", encoding="utf-8")
    except (ValueError, OSError) as e:
        print(f"[warn] PHANTOM_CONTROL_FD invalid ({e}) — control events disabled",
              file=sys.stderr)
        return

    while not _shutdown.is_set():
        try:
            line = control_file.readline()
        except (OSError, ValueError):
            break
        if not line:
            break  # transport gateway closed the control pipe (session ending)
        line = line.strip()
        if not line:
            continue

        try:
            command = _json.loads(line).get("command")
        except (ValueError, AttributeError) as e:
            show_warn(f"Control event: malformed command ({e}) — ignored")
            continue

        try:
            # Same dispatch bodies as keyboard_loop()'s 'G'/'g'/'r' handling
            # above -- this is the identical logic, triggered remotely.
            if command == "generate_summary":
                threading.Thread(target=generate_summary, daemon=True, name="control-summary").start()

            elif command == "toggle_recording":
                if _vad_buf.recording_active.is_set():
                    _vad_buf.recording_active.clear()
                    show_info("○ RECORDING OFF — audio ignored until toggled again")
                else:
                    _vad_buf.recording_active.set()
                    show_info("● RECORDING ON")
                _vad_buf.show_recording_status()

            elif command == "generate_meeting_analysis":
                if args.manual_flush:
                    merged = _vad_buf.flush()
                    if merged is not None:
                        if _ENV["DEBUG_AUDIO_SAVE"]:
                            _save_debug_audio(merged)
                        _set_runtime_mode(RuntimeMode.MEETING)
                        show_info(f"[meeting] {len(merged)/SAMPLE_RATE:.1f}s → Whisper → ミーティング分析…")
                        _enqueue_latest(merged)
                    else:
                        show_info("[meeting] バッファ空 — 現在のログで分析中…")
                        threading.Thread(
                            target=generate_meeting_analysis,
                            daemon=True,
                            name="control-meeting-analysis",
                        ).start()
                else:
                    threading.Thread(
                        target=generate_meeting_analysis,
                        daemon=True,
                        name="control-meeting-analysis",
                    ).start()

            else:
                show_warn(f"Control event: unknown command {command!r} — ignored")

        except Exception as e:
            show_warn(f"Control event error ({e}) — continuing")


# ─────────────────────────────────────────────────────────────────────────────
# Signal handler
# ─────────────────────────────────────────────────────────────────────────────
def _handle_signal(signum, frame):
    show_info("\nCtrl+C — シャットダウン中…")
    _shutdown.set()

signal.signal(signal.SIGINT,  _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    _init_session_dir()
    memory_init()

    loaded_v15 = [m for m, ok in [
        ("speaker_inference",   _SPEAKER_MODULE_LOADED),
        ("persistence",         _PERSIST_MODULE_LOADED),
        ("metrics",             _METRICS_MODULE_LOADED),
        ("health",              _HEALTH_MODULE_LOADED),
        ("devices",             _DEVICES_MODULE_LOADED),
        ("routing",             _ROUTING_MODULE_LOADED),
        ("keyboard",            _KEYBOARD_MODULE_LOADED),
        ("hallucination_guard", _HALLUCINATION_MODULE_LOADED),
    ] if ok]
    loaded_v16 = [m for m, ok in [
        ("config",            _CONFIG_MODULE_LOADED),
        ("state_machine",     _STATE_MACHINE_MODULE_LOADED),
        ("runtime_logger",    _LOGGING_MODULE_LOADED),
        ("audio.vad",         _VAD_MODULE_LOADED),
        ("audio.capture",     _CAPTURE_MODULE_LOADED),
        ("orchestration",     _ORCH_MODULE_LOADED),
        ("replay",            _REPLAY_MODULE_LOADED),
    ] if ok]

    module_lines = []
    if loaded_v15 or loaded_v16:
        module_lines.append(f"  modules: {', '.join(loaded_v15 + loaded_v16)}")
    else:
        module_lines.append("  all inline")
    module_note = "\n".join(module_lines) + "\n"

    profile_banner = _build_profile_banner(_ACTIVE_PROFILE_NAME, _ACTIVE_PROFILE)
    agent_mode     = args.agent or _ENV["AGENT_MODE"]
    cognition_mode = args.cognition or _ENV.get("COGNITION", False)

    agent_note = (
        f"  {BOLD}{CYAN}AGENT MODE{RESET} "
        f"{GRAY}tts={args.tts}  classify={'ON' if args.classify else 'off'}  "
        f"history={args.history_turns}{RESET}\n"
        if agent_mode else ""
    )
    cognition_note = (
        f"  {BOLD}{MAGENTA}COGNITION MODE{RESET} "
        f"{GRAY}compress+candidates  n={args.candidates}  "
        f"(fallback: standard reply on failure){RESET}\n"
        if cognition_mode else ""
    )
    manual_note = (
        f"  {BOLD}{WHITE}★ MANUAL-FLUSH  g=ミーティング分析  G=まとめ  MAX={MAX_MANUAL_BUFFER_SEC:.0f}s{RESET}\n"
        if args.manual_flush else ""
    )
    _print(
        f"\n{BOLD}{CYAN}Phantom Conversational Runtime  v22{RESET}  "
        f"{BOLD}[PROFILE: {_ACTIVE_PROFILE_NAME}]{RESET}\n"
        f"{GRAY}{profile_banner}{RESET}\n"
        f"{GRAY}  {_PROMPT_SIZE_NOTE}{RESET}\n"
        f"{GRAY}{module_note}{RESET}"
        f"{agent_note}"
        f"{cognition_note}"
        f"{manual_note}"
        f"\n{GRAY}lang={_effective_lang}  level={_effective_level}  "
        f"threshold={RMS_THRESHOLD}  min_sec={args.min_sec}  silence_sec={args.silence_sec}  "
        f"(Ctrl+C or 'q' to quit){RESET}\n"
    )

    threads = [
        threading.Thread(target=record_audio,   daemon=True, name="audio-capture"),
        threading.Thread(target=reply_worker,   daemon=True, name="reply-worker"),
        threading.Thread(target=keyboard_loop,  daemon=True, name="keyboard"),
        threading.Thread(target=control_loop,   daemon=True, name="control"),
        threading.Thread(target=health_monitor, daemon=True, name="health"),
    ]
    for t in threads:
        t.start()

    try:
        vad_loop()
    except KeyboardInterrupt:
        _shutdown.set()

    show_info("クリーンシャットダウン中…")
    # Close session transcript
    if _PERSIST_MODULE_LOADED and _ENV["TRANSCRIPT_PERSIST"]:
        _ext_close_session(info_fn=show_info)
    for t in threads:
        t.join(timeout=3.0)
    show_info("終了しました。")


if __name__ == "__main__":
    main()
