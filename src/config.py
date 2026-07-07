"""
config.py
==========
Centralized runtime configuration for the Phantom Conversational Runtime.

All thresholds, limits, and defaults live here.
Import this module anywhere — it has no dependencies on the runtime itself.

USAGE:
  from config import RuntimeConfig
  cfg = RuntimeConfig()          # all defaults
  cfg = RuntimeConfig.from_env() # overrides from environment variables
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class RuntimeConfig:
    """
    Centralized runtime configuration.
    All values have safe defaults. Override via environment or CLI.

    DESIGN:
      - Dataclass: type-safe, inspectable, serialisable
      - from_env() reads RUNTIME_* environment variables
      - CLI args take precedence over from_env() values
      - No mutable global state — each runtime instance has its own Config
    """

    # ── Audio capture ───────────────────────────────────────────────────────
    sample_rate:         int   = 16000     # Hz — Whisper native
    channels:            int   = 1
    dtype:               str   = "int16"
    block_size:          int   = 1600      # 100ms per block at 16kHz
    audio_queue_maxsize: int   = 200       # blocks before overflow drops

    # ── VAD / segmentation ──────────────────────────────────────────────────
    rms_threshold:       int   = 120       # RMS amplitude floor for speech detection
    min_sec:             float = 0.4       # minimum speech before VAD flush
    max_sec:             float = 12.0      # force-flush ceiling
    silence_sec:         float = 0.25      # silence duration that triggers flush
    pre_buffer_blocks:   int   = 5         # pre-buffer blocks (500ms onset recovery)

    # ── Manual buffer ────────────────────────────────────────────────────────
    max_manual_buffer_sec: float = 30.0    # hard ceiling before oldest segment drops
    manual_buffer_warn_sec: float = 20.0   # warning threshold before ceiling

    # ── Transcript retention ────────────────────────────────────────────────
    transcript_maxlen:   int   = 200       # max LogEntry objects in memory
    # (was 80 — raised for long enterprise calls; deque enforces this bound)

    # ── Queue settings ───────────────────────────────────────────────────────
    transcript_queue_maxsize: int = 4      # Whisper/GPT queue depth

    # ── GPT / Whisper ───────────────────────────────────────────────────────
    gpt_model:           str   = "gpt-4o-mini"
    whisper_model:       str   = "whisper-1"
    api_timeout:         float = 20.0      # OpenAI client-level timeout (seconds)
    gpt_temperature:     float = 0.15      # observer mode
    agent_temperature:   float = 0.1       # agent mode (lower = more deterministic)
    max_tokens_jp:       int   = 60        # JP-only response (1 line)
    max_tokens_en:       int   = 120       # EN response ([JP]+[EN])
    max_tokens_en_pron:  int   = 150       # EN + pronunciation line
    max_tokens_agent:    int   = 100       # autonomous agent response
    whisper_prompt_chars: int  = 300       # max chars from transcript for whisper context

    # ── Degradation / retry ──────────────────────────────────────────────────
    whisper_max_retries:   int   = 2
    whisper_retry_delay:   float = 0.3
    gpt_stream_timeout:    float = 30.0    # streaming session timeout
    degradation_cooldown:  float = 2.0     # seconds to pause after API error

    # ── Health monitoring ────────────────────────────────────────────────────
    health_interval:     int   = 60        # seconds between health snapshots
    overflow_window:     int   = 100       # events in rolling overflow deque

    # ── TTS ──────────────────────────────────────────────────────────────────
    tts_provider:        str   = "none"    # none | say | pyttsx3
    tts_max_wait:        float = 10.0      # max seconds to wait for TTS to finish
    say_voice:           str   = "Samantha"
    say_rate:            int   = 200
    pyttsx3_rate:        int   = 175

    # ── Logging ──────────────────────────────────────────────────────────────
    log_level:           str   = "INFO"    # INFO | WARN | ERROR | DEBUG
    log_json:            bool  = False     # emit structured JSON logs
    runtime_log_level:   str   = "INFO"    # from RUNTIME_LOG_LEVEL env

    # ── Session ──────────────────────────────────────────────────────────────
    session_dir:         str   = ""        # SESSION_OUTPUT_DIR
    profile_name:        str   = "default"
    input_device:        str   = ""        # INPUT_DEVICE env or --input-device

    # ── LLM Provider selection (H5-1: session-scoped, not deployment-wide) ──
    # No `provider` field here: PROVIDER no longer participates in routing.
    # Provider selection is session-scoped, read directly from
    # PHANTOM_PROVIDER by phantom_runtime.py -- set fresh per spawned
    # Runtime child by runtime.cloud_run_shell, based on the client's
    # validated `provider` request metadata (runtime.provider_router).
    gemini_api_key:      str   = ""        # GEMINI_API_KEY env
    gemini_model:        str   = "gemini-2.5-flash"  # RUNTIME_GEMINI_MODEL env

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        """
        Build a RuntimeConfig with environment variable overrides.
        Unset env vars fall back to dataclass defaults.
        """
        def _int(name: str, default: int) -> int:
            v = os.getenv(name, "")
            return int(v) if v.strip().isdigit() else default

        def _float(name: str, default: float) -> float:
            try:
                return float(os.getenv(name, ""))
            except (ValueError, TypeError):
                return default

        def _str(name: str, default: str) -> str:
            return os.getenv(name, default).strip() or default

        def _bool(name: str, default: bool = False) -> bool:
            return os.getenv(name, "").strip().lower() in ("1", "true", "yes")

        return cls(
            rms_threshold          = _int("RUNTIME_RMS_THRESHOLD", 120),
            max_manual_buffer_sec  = _float("RUNTIME_MAX_MANUAL_BUFFER_SEC", 30.0),
            transcript_maxlen      = _int("RUNTIME_TRANSCRIPT_MAXLEN", 200),
            gpt_model              = _str("RUNTIME_GPT_MODEL", "gpt-4o-mini"),
            whisper_model          = _str("RUNTIME_WHISPER_MODEL", "whisper-1"),
            api_timeout            = _float("RUNTIME_API_TIMEOUT", 20.0),
            health_interval        = _int("RUNTIME_HEALTH_INTERVAL", 60),
            session_dir            = _str("SESSION_OUTPUT_DIR", ""),
            input_device           = _str("INPUT_DEVICE", ""),
            log_level              = _str("RUNTIME_LOG_LEVEL", "INFO"),
            log_json               = _bool("RUNTIME_LOG_JSON"),
            runtime_log_level      = _str("RUNTIME_LOG_LEVEL", "INFO"),
            gemini_api_key         = _str("GEMINI_API_KEY", ""),
            gemini_model           = _str("RUNTIME_GEMINI_MODEL", "gemini-2.5-flash"),
        )

    def derived_samples(self) -> dict:
        """Pre-compute sample counts derived from time settings."""
        return {
            "min_samples":    int(self.sample_rate * self.min_sec),
            "max_samples":    int(self.sample_rate * self.max_sec),
            "silence_blocks": max(2, int((self.silence_sec * self.sample_rate) / self.block_size)),
        }
