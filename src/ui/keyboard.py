"""
ui.keyboard
============
Keyboard Controls — Phantom Conversational Runtime.

Extracted from phantom_conversational_runtime_v22.py (M2 High-Risk Extraction).
Original location: keyboard_loop() [MODULE: ui.keyboard] annotations in v22.

Public API
----------
RuntimeContext     -- context dataclass carrying all runtime state references
KeyboardController -- keyboard shortcut dispatch loop

This module is independently importable.
It carries no dependency on the main runtime file.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, List


@dataclass
class RuntimeContext:
    """
    All runtime state references required by KeyboardController.

    Every field is a reference to an already-existing Runtime object or
    callable.  No new Runtime state is introduced by this dataclass.
    """

    # Lifecycle
    shutdown_event: threading.Event

    # Operating mode
    manual_flush_enabled: bool

    # Audio
    sample_rate: int
    enqueue_latest_fn: Callable
    manual_buf_flush_fn: Callable
    manual_buf_status_fn: Callable
    manual_buf_lock: threading.Lock
    manual_audio_buffer: Any

    # Recording control (v17.2)
    recording_active: threading.Event
    show_recording_status_fn: Callable

    # TTS
    tts: Any
    tts_interrupt_event: threading.Event

    # Conversation state
    get_state_fn: Callable
    set_state_fn: Callable
    idle_state: Any

    # Transcript
    transcript_log: Any
    log_lock: threading.Lock

    # Output functions
    show_info: Callable
    show_warn: Callable
    show_sep: Callable
    show_hold: Callable
    show_clarify_fn: Callable
    show_delay_en_fn: Callable
    show_delay_jp_fn: Callable
    show_delay_slot_fn: Callable
    print_fn: Callable

    # Phrase lists
    delay_en_list: List[str]
    delay_jp_list: List[str]
    delay_en_slots: List[str]

    # Actions
    generate_summary_fn: Callable
    generate_meeting_analysis_fn: Callable

    # Debug audio
    debug_audio_save: bool
    save_debug_audio_fn: Callable

    # Runtime mode
    set_runtime_mode_fn: Callable
    runtime_mode_meeting: Any

    # Configuration
    agent_mode: Any
    tts_name: str
    agent_env_flag: bool

    # ANSI colors
    CYAN: str
    YELLOW: str
    GREEN: str
    GRAY: str
    RESET: str
    BOLD: str
    WHITE: str


class KeyboardController:
    """
    Keyboard shortcut dispatcher for Phantom Conversational Runtime.

    Implements identical behavior to the inline fallback in keyboard_loop(),
    reading all runtime references from the supplied RuntimeContext rather
    than from module-level globals.

    CRITICAL BUG FIX (v14, preserved): cmd == 'G' is checked BEFORE
    cmd_lower == 'g' so that uppercase G always triggers generate_summary,
    never the manual-flush path.
    """

    def __init__(self, ctx: RuntimeContext) -> None:
        self._ctx = ctx

    # ── Help strings ──────────────────────────────────────────────────────────

    def _help(self) -> str:
        c = self._ctx
        B, G, R = c.BOLD, c.GRAY, c.RESET
        return "\n".join([
            f"{G}{'─'*40}{R}",
            f"{B}キー操作 / Keyboard Commands{R}",
            f"  {B}r{R}      toggle recording ●ON / ○OFF  {G}(instant){R}",
            f"  {B}h{R}      hold phrase    {G}[HOLD]{R}  (no API)",
            f"  {B}u{R}      clarify/repeat {G}[REPEAT]{R} (no API — instant)",
            f"  {B}d{R}      EN delay phrase {G}[DLY]{R}",
            f"  {B}t{R}      JP phrase       {G}[考]{R}",
            f"  {B}1-5{R}    EN phrase slots",
            f"  {B}s{R}      show conversation state / stop TTS",
            f"  {B}g{R}      ミーティング分析",
            f"  {B}G{R}      インタビューまとめ生成",
            f"  {B}c{R}      clear transcript log",
            f"  {B}l{R}      display transcript log",
            f"  {B}?{R}      show this help",
            f"  {B}q{R}      quit",
            f"{G}{'─'*40}{R}",
        ])

    def _help_manual(self) -> str:
        c = self._ctx
        B, G, R = c.BOLD, c.GRAY, c.RESET
        return "\n".join([
            f"{G}{'─'*40}{R}",
            f"{B}キー操作 / Keyboard Commands  [MANUAL-FLUSH MODE]{R}",
            f"  {B}r{R}      toggle recording ●ON / ○OFF      {G}(no API — instant){R}",
            f"  {B}g{R}      ★ FLUSH → Whisper → ミーティング分析 {G}(main action){R}",
            f"  {B}G{R}      インタビューまとめ生成",
            f"  {B}h{R}      hold phrase    {G}[HOLD]{R}  (no API)",
            f"  {B}u{R}      clarify/repeat {G}[REPEAT]{R} (no API — instant)",
            f"  {B}d{R}      EN delay phrase {G}[DLY]{R}",
            f"  {B}t{R}      JP phrase       {G}[考]{R}",
            f"  {B}1-5{R}    EN phrase slots",
            f"  {B}s{R}      show state / buffer status / stop TTS",
            f"  {B}c{R}      clear log + buffer",
            f"  {B}l{R}      display transcript log",
            f"  {B}?{R}      show this help",
            f"  {B}q{R}      quit",
            f"{G}{'─'*40}{R}",
        ])

    # ── Main loop ─────────────────────────────────────────────────────────────

    def run(self) -> None:
        ctx = self._ctx

        ctx.print_fn(self._help_manual() if ctx.manual_flush_enabled else self._help())
        _eof_count = 0

        while not ctx.shutdown_event.is_set():
            try:
                cmd = input().strip()   # NO .lower() here
            except EOFError:
                _eof_count += 1
                if _eof_count >= 3:
                    ctx.show_info("stdin repeatedly closed — keyboard control disabled.")
                    return
                ctx.show_info(f"stdin closed briefly ({_eof_count}/3) — retrying in 2s…")
                time.sleep(2.0)
                continue
            except UnicodeDecodeError as e:
                ctx.show_warn(f"Keyboard: encoding error ({e}) — ignored")
                continue
            except KeyboardInterrupt:
                break
            except Exception as e:
                ctx.show_warn(f"Keyboard: unexpected error ({e}) — continuing")
                continue

            _eof_count = 0
            cmd_lower  = cmd.lower()

            try:
                # ── UPPERCASE G checked BEFORE cmd_lower == 'g' (BUG FIX) ───
                if cmd == "G":
                    threading.Thread(
                        target=ctx.generate_summary_fn, daemon=True, name="summary"
                    ).start()

                elif cmd_lower == "r":
                    # Toggle push-to-buffer recording (v17.2)
                    if ctx.recording_active.is_set():
                        ctx.recording_active.clear()
                        ctx.show_info("○ RECORDING OFF — audio ignored until 'r' pressed again")
                    else:
                        ctx.recording_active.set()
                        ctx.show_info("● RECORDING ON")
                    ctx.show_recording_status_fn()

                elif cmd_lower == "u":
                    # Clarification / repeat-request phrase (v17.2)
                    ctx.show_clarify_fn()

                elif cmd_lower == "h":
                    ctx.show_hold(random.choice(ctx.delay_en_list))
                elif cmd_lower == "d":
                    ctx.show_delay_en_fn()
                elif cmd_lower == "t":
                    ctx.show_delay_jp_fn()
                elif cmd_lower in ("1", "2", "3", "4", "5"):
                    ctx.show_delay_slot_fn(int(cmd_lower))

                elif cmd_lower == "g":
                    if ctx.manual_flush_enabled:
                        merged = ctx.manual_buf_flush_fn()
                        if merged is not None:
                            if ctx.debug_audio_save:
                                ctx.save_debug_audio_fn(merged)
                            ctx.set_runtime_mode_fn(ctx.runtime_mode_meeting)
                            ctx.show_info(
                                f"[meeting] {len(merged)/ctx.sample_rate:.1f}s"
                                f" → Whisper → ミーティング分析…"
                            )
                            ctx.enqueue_latest_fn(merged)
                        else:
                            ctx.show_info("[meeting] バッファ空 — 現在のログで分析中…")
                            threading.Thread(
                                target=ctx.generate_meeting_analysis_fn,
                                daemon=True,
                                name="meeting-analysis",
                            ).start()
                    else:
                        threading.Thread(
                            target=ctx.generate_meeting_analysis_fn,
                            daemon=True,
                            name="meeting-analysis",
                        ).start()

                elif cmd_lower == "s":
                    state        = ctx.get_state_fn()
                    agent_mode_s = ctx.agent_mode or ctx.agent_env_flag
                    mode_label   = (
                        "MANUAL-FLUSH" if ctx.manual_flush_enabled else
                        ("AGENT" if agent_mode_s else "OBSERVER")
                    )
                    ctx.show_info(
                        f"state={state.value}  mode={mode_label}  tts={ctx.tts_name}"
                    )
                    ctx.show_recording_status_fn()   # v17.2: always show recording status on 's'
                    if ctx.manual_flush_enabled:
                        ctx.show_info(f"buffer: {ctx.manual_buf_status_fn()}")
                    if ctx.tts.is_speaking():
                        ctx.tts.stop()
                        ctx.tts_interrupt_event.set()
                        ctx.show_info("TTS stopped.")
                        ctx.set_state_fn(ctx.idle_state)

                elif cmd_lower == "c":
                    with ctx.log_lock:
                        ctx.transcript_log.clear()
                    with ctx.manual_buf_lock:
                        ctx.manual_audio_buffer.clear()
                    ctx.show_info("ログと音声バッファをクリアしました。")

                elif cmd_lower == "l":
                    with ctx.log_lock:
                        log_copy = list(ctx.transcript_log)
                    if log_copy:
                        ctx.show_sep()
                        for i, e in enumerate(log_copy, 1):
                            if e.speaker == "recruiter":   tag = f"{ctx.CYAN}REC{ctx.RESET}"
                            elif e.speaker == "agent":     tag = f"{ctx.GREEN}AGT{ctx.RESET}"
                            elif e.speaker == "user":      tag = f"{ctx.YELLOW}YOU{ctx.RESET}"
                            else:                          tag = f"{ctx.GRAY}???{ctx.RESET}"
                            ctx.print_fn(
                                f"  {ctx.GRAY}{i:02d} {e.ts}{ctx.RESET} [{tag}] {e.text}"
                            )
                        ctx.show_sep()
                    else:
                        ctx.show_info("ログは空です。")

                elif cmd_lower in ("q", "quit", "exit"):
                    ctx.show_info("終了します。")
                    ctx.shutdown_event.set()
                    break

                elif cmd_lower == "?":
                    ctx.print_fn(
                        self._help_manual() if ctx.manual_flush_enabled else self._help()
                    )

            except Exception as e:
                ctx.show_warn(f"Keyboard command error ({e}) — continuing")
