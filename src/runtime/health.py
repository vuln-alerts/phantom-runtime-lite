"""
runtime/health.py
=================
Runtime health snapshot and formatting for the Phantom Conversational Runtime.

Relocated from phantom_conversational_runtime_v22.py (M5 Runtime Core Separation).
Original location: health_monitor() delegation path and inline fallback
annotated [MODULE: runtime.health] in v22.

EXPORTED API:
  get_runtime_health(...)      — build a health snapshot dict
  format_health_line(snap)     — format the main health status line
  format_manual_buf_line(snap) — format the manual buffer status line
"""

from typing import Any, Dict, Optional


def get_runtime_health(
    session_id:          str,
    conv_state_value:    str,
    mode_label:          str,
    manual_buf_sec:      float,
    manual_buf_segments: int,
    audio_q_depth:       int,
    transcript_q_depth:  int,
    overflow_rate:       float,
    log_entries:         int,
    tts_active:          bool,
    last_flush_ts:       str,
    latency_summary:     Optional[Dict[str, Any]],
    audio_q_maxsize:     int,
) -> Dict[str, Any]:
    if audio_q_depth > 50 or transcript_q_depth > 2:
        pressure = "HIGH"
    elif audio_q_depth > 20 or transcript_q_depth > 0:
        pressure = "MED"
    else:
        pressure = "low"
    return {
        "session_id":          session_id,
        "conv_state_value":    conv_state_value,
        "mode_label":          mode_label,
        "manual_buf_sec":      manual_buf_sec,
        "manual_buf_segments": manual_buf_segments,
        "audio_q_depth":       audio_q_depth,
        "transcript_q_depth":  transcript_q_depth,
        "overflow_rate":       overflow_rate,
        "log_entries":         log_entries,
        "tts_active":          tts_active,
        "last_flush_ts":       last_flush_ts,
        "latency_summary":     latency_summary,
        "audio_q_maxsize":     audio_q_maxsize,
        "pressure":            pressure,
    }


def format_health_line(snap: Dict[str, Any]) -> str:
    return (
        f"[health]  mode={snap['mode_label']}  "
        f"audio_q={snap['audio_q_depth']}  "
        f"transcript_q={snap['transcript_q_depth']}  "
        f"overflow≈{snap['overflow_rate']:.1f}/min  "
        f"log={snap['log_entries']}  "
        f"pressure={snap['pressure']}"
    )


def format_manual_buf_line(snap: Dict[str, Any]) -> str:
    return (
        f"[health]  manual_buf={snap['manual_buf_sec']:.1f}s  "
        f"segments={snap['manual_buf_segments']}  "
        f"last_flush={snap['last_flush_ts']}"
    )
