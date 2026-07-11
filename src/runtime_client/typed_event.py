"""
runtime_client/typed_event.py
================================
Parsing, console rendering, and local mirroring of Runtime Typed Events
(H4 Runtime Event Contract). Read-only consumer: this module never
constructs or validates events on the Runtime's behalf, it only renders
whatever JSON line the Transport Gateway relays (see
docs/H4_RUNTIME_EVENT_CONTRACT.md, runtime/transport_gateway.py's
_drain_event_queue) and keeps a local `transcript_log` mirror so the
ported 'l'/'s'/'c' keyboard commands (see keyboard_bridge.py) have
something to operate on, matching the original app's console UX.

`reply` events additionally drive client-side TTS playback (Phase 3,
see tts.py) when a real TTS provider is wired in -- the speak/wait/
interrupt loop mirrors phantom_conversational_runtime_v22.py:3167-3184.

P5-4 Adaptive Runtime Calibration, Phase 3 (Runtime UI) addition: the
four calibration screens design doc section 8.1-8.4 specify (startup /
in-progress / complete / failed). These are pure rendering functions --
callers pass in numbers already computed elsewhere (calibration.py's
NoiseFloorSampler/EnvironmentObserver/CalibrationEngine, Phase 1-2);
this module does not sample audio, derive a Speech Gate, or hold any
calibration state of its own (Runtime Philosophy: UI is Read Only, see
design doc's "UIはRuntimeの状態を可視化するだけです"). Not wired into
main.py's startup sequence yet -- that wiring is Phase 5 (Integration).
Deliberately out of scope for Phase 3 (see design doc section 8.5 /
Implementation Plan's Phase 4 boundary): the "Environment Changed"
re-calibration screen, and any actual 'c'-key re-calibration handling.

P5-4 Adaptive Runtime Calibration, Phase 4 (Re-calibration) addition:
show_environment_changed(), design doc section 8.5's fifth calibration
screen. Same pure-renderer contract as Phase 3's four screens above --
every number (including the before/after reject-rate percentages the
design doc's own worked example shows as "3% -> 96%") is supplied by
the caller, never computed here. In particular this module does not
decide *when* to show this screen -- design doc section 6.4/7/10.6's
FR-6 automatic drift trigger has no concrete threshold specified
anywhere in either design doc, so deciding that is out of scope here
just as it is for calibration.py's RecalibrationController (see that
module's docstring). This function only renders the screen once some
other, not-yet-implemented caller decides to show it.

EXPORTED API:
  LogEntry        -- (text, lang, ts, speaker) NamedTuple, shape-compatible
                      with what ui/keyboard.py's 'l' handler expects
  ANSI colors: CYAN, YELLOW, GREEN, GRAY, RESET, BOLD, WHITE
  show_info/show_warn/show_sep/show_hold/show_clarify/show_delay_en/
  show_delay_jp -- console helpers matching phantom_runtime.py's originals
  show_calibration_start/show_calibration_progress/
  show_calibration_complete/show_calibration_failed -- design doc section
                     8.1-8.4's four calibration screens (Phase 3)
  show_environment_changed -- design doc section 8.5's re-calibration
                     screen (Phase 4)
  TypedEventStore -- local mirror + renderer for the inbound event stream,
                     now also driving TTS playback for 'reply' events
"""

import datetime
import json
import threading
import time
from collections import deque
from typing import NamedTuple, Optional

from runtime_client.tts import NullTTSProvider, TTSProvider
import runtime_trace

_TTS_SPEAK_DEADLINE_SECONDS = 10.0
_TTS_POLL_INTERVAL_SECONDS = 0.05


class LogEntry(NamedTuple):
    text: str
    lang: str
    ts: str
    speaker: str


RESET  = "\033[0m"
BOLD   = "\033[1m"
CYAN   = "\033[96m"
YELLOW = "\033[93m"
GREEN  = "\033[92m"
GRAY   = "\033[90m"
RED    = "\033[91m"
WHITE  = "\033[97m"

SEP = GRAY + "─" * 62 + RESET

_print_lock = threading.Lock()


def _print(*parts: str, end: str = "\n") -> None:
    with _print_lock:
        print("".join(parts), end=end, flush=True)


def show_sep() -> None:
    _print(SEP)


def show_info(text: str) -> None:
    _print(f"{GRAY}· {text}{RESET}")


def show_warn(text: str) -> None:
    _print(f"{YELLOW}⚠  {text}{RESET}")


def show_err(label: str, err) -> None:
    _print(f"{RED}[{label}]{RESET} {err}")


def show_hold(phrase: str) -> None:
    _print(f"\n{WHITE}{BOLD}[HOLD]{RESET} {WHITE}{phrase}{RESET}\n")


def show_clarify(phrase: str) -> None:
    _print(f"\n{WHITE}{BOLD}[REPEAT]{RESET} {WHITE}{phrase}{RESET}\n")


def show_delay_en(phrase: str) -> None:
    _print(f"\n{WHITE}{BOLD}[DLY]{RESET} {WHITE}{phrase}{RESET}\n")


def show_delay_jp(phrase: str) -> None:
    _print(f"\n{YELLOW}{BOLD}[考]{RESET}  {YELLOW}{phrase}{RESET}\n")


def show_agent_reply(text: str) -> None:
    _print(f"\n{CYAN}{BOLD}[→]{RESET} {BOLD}{text}{RESET}\n")


def show_transcript(text: str, lang: str, ts: str) -> None:
    _print(f"\n{CYAN}◎ {text}{RESET}  {GRAY}[{lang}] {ts}{RESET}")


# --- P5-4 Adaptive Runtime Calibration, Phase 3: Runtime UI (design doc
# section 8) -----------------------------------------------------------
# Pure renderers: every number below is supplied by the caller (Phase
# 1-2's calibration.py), never computed here. See module docstring.

_CALIBRATION_HEADER = f"{CYAN}🎤 Audio Calibration{RESET}\n{GRAY}環境ノイズを測定しています…静かにしてください{RESET}"


def show_calibration_start(window_blocks: int) -> None:
    """Design doc section 8.1: the very first frame shown on entry into
    CALIBRATING, before any block has been sampled."""
    _print(f"\n{_CALIBRATION_HEADER}\n\n{GRAY}サンプル取得中: 0/{window_blocks} blocks{RESET}")


def show_calibration_progress(
    sample_count: int,
    window_blocks: int,
    elapsed_seconds: float,
    window_seconds: float,
    noise_floor_estimate: Optional[float] = None,
    bar_width: int = 10,
) -> None:
    """Design doc section 8.2: repeated while CALIBRATING is sampling.
    noise_floor_estimate is the provisional (not-yet-final) p90 of
    whatever samples have been collected so far, if the caller has one
    to show; omitted entirely from the display when None."""
    fraction = 0.0 if window_seconds <= 0 else min(elapsed_seconds / window_seconds, 1.0)
    filled = max(0, min(bar_width, int(round(bar_width * fraction))))
    bar = "■" * filled + "□" * (bar_width - filled)
    lines = [
        f"\n{_CALIBRATION_HEADER}",
        "",
        f"{GRAY}{bar} {elapsed_seconds:.1f}s / {window_seconds:.1f}s{RESET}",
        f"{GRAY}サンプル取得中: {sample_count}/{window_blocks} blocks{RESET}",
    ]
    if noise_floor_estimate is not None:
        lines.append(f"{GRAY}現在の推定 Noise Floor: {noise_floor_estimate:.0f} RMS (暫定){RESET}")
    _print("\n".join(lines))


def show_calibration_complete(
    noise_floor: float,
    speech_gate: float,
    sample_count: int,
    percentile: int,
    multiplier: float,
    microphone_name: str = "",
) -> None:
    """Design doc section 8.3: shown once on CALIBRATING -> CALIBRATED,
    then normal operation ("RECORDING") begins."""
    mic = microphone_name or "(system default)"
    _print(
        f"\n{GREEN}{BOLD}✓ Calibration Complete{RESET}\n\n"
        f"Noise Floor  : {noise_floor:.0f} RMS  (p{percentile}, {sample_count} samples)\n"
        f"Speech Gate  : {speech_gate:.0f} RMS  (floor x {multiplier:g})\n"
        f"Microphone   : {mic}\n"
        f"Recalibrate  : press 'c' anytime\n\n"
        f"{GREEN}● RECORDING{RESET}  (gate: {speech_gate:.0f} RMS)"
    )


def show_calibration_failed(attempts: int, max_attempts: int, fallback_gate: float) -> None:
    """Design doc section 8.4: shown on CALIBRATION_FAILED -> FALLBACK.
    fallback_gate is the conservative estimate FALLBACK adopts -- always
    labeled as an estimate, never presented as a measured value (design
    doc section 9.1 / AC-10: no silent fallback)."""
    _print(
        f"\n{YELLOW}⚠ Calibration Incomplete{RESET}\n"
        f"{max_attempts}回中{attempts}回、静寂区間中に音声を検出しました\n\n"
        f"Fallback Gate : {fallback_gate:.0f} RMS  (保守的推定・未確定)\n"
        f"この値は実測ではなく安全側のフォールバックです\n"
        f"静かな環境で 'c' を押すと再測定できます"
    )


# --- P5-4 Adaptive Runtime Calibration, Phase 4: Re-calibration screen
# (design doc section 8.5) ----------------------------------------------
# Same pure-renderer contract as Phase 3's four screens above -- see
# module docstring's Phase 4 note. This function does not decide when
# a re-calibration is happening or why; the caller (not implemented as
# of Phase 4) supplies the before/after reject-rate figures and the
# elapsed/window timing.


def show_environment_changed(
    previous_reject_rate: float,
    current_reject_rate: float,
    elapsed_seconds: float,
    window_seconds: float,
    bar_width: int = 10,
) -> None:
    """Design doc section 8.5: shown while a re-calibration cycle is in
    progress (RecalibrationController.begin_recalibration(), see
    calibration.py). previous_reject_rate/current_reject_rate are
    percentages (e.g. 3.0, 96.0 for the design doc's own "3% -> 96%"
    worked example) supplied by the caller -- this function derives
    nothing and holds no state (Runtime Philosophy: UI is Read Only).
    Recording continues throughout a re-calibration (design doc section
    6.4's "録音を止めずに行う"), which this screen states explicitly."""
    fraction = 0.0 if window_seconds <= 0 else min(elapsed_seconds / window_seconds, 1.0)
    filled = max(0, min(bar_width, int(round(bar_width * fraction))))
    bar = "■" * filled + "□" * (bar_width - filled)
    _print(
        f"\n{YELLOW}{BOLD}⟳ Environment Changed{RESET}\n"
        f"{GRAY}直近10秒で棄却率が急上昇 ({previous_reject_rate:.0f}% -> {current_reject_rate:.0f}%){RESET}\n"
        f"{GRAY}マイクまたは環境が変化した可能性 — 裏で再測定します{RESET}\n\n"
        f"{GRAY}{bar} {elapsed_seconds:.1f}s / {window_seconds:.1f}s{RESET}\n"
        f"{GRAY}録音は継続中 (発話を止める必要はありません){RESET}"
    )


def _now_ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


class TypedEventStore:
    """
    Holds the Client's local mirror of the server's Typed Event stream:
    a bounded transcript_log (fed by `transcript`/`reply` events, same
    LogEntry shape ui/keyboard.py's 'l' handler already knows how to
    render) plus the most recently seen `status` event, for the 's' key.
    Renders every event to the console as it arrives; unknown event
    types are shown generically rather than dropped, so nothing is
    silently lost.

    `reply` events additionally trigger client-side TTS playback (Phase
    3) when `tts` is not a NullTTSProvider, replicating the SSoT's
    reply-speaking loop (phantom_conversational_runtime_v22.py:3167-3184)
    verbatim: speak() the reply, then poll is_speaking() until it's done,
    a 10s deadline elapses, or `tts_interrupt_event` is set (the 's' key
    -- see keyboard_bridge.py -- sets this same Event to interrupt).
    """

    def __init__(
        self,
        maxlen: int = 200,
        tts: Optional[TTSProvider] = None,
        tts_interrupt_event: Optional[threading.Event] = None,
    ) -> None:
        self.transcript_log: "deque[LogEntry]" = deque(maxlen=maxlen)
        self.log_lock = threading.Lock()
        self.last_status: Optional[dict] = None
        self.audio_blocks_sent = 0
        self.tts: TTSProvider = tts if tts is not None else NullTTSProvider()
        self.tts_interrupt_event = tts_interrupt_event if tts_interrupt_event is not None else threading.Event()

    def handle_line(self, line: str) -> None:
        try:
            envelope = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            show_warn(f"unparseable event: {line!r}")
            return
        if not isinstance(envelope, dict):
            show_warn(f"unexpected event shape: {line!r}")
            return

        event_type = envelope.get("type", "?")
        payload = envelope.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}

        if event_type == "transcript":
            self._handle_transcript(payload)
        elif event_type == "reply":
            self._handle_reply(payload)
        elif event_type == "status":
            self._handle_status(payload)
        elif event_type == "latency":
            self._handle_latency(payload)
        elif event_type == "error":
            self._handle_error(payload)
        elif event_type == "analysis":
            self._handle_analysis(payload)
        else:
            show_info(f"[{event_type}] {payload}")

    def _handle_transcript(self, payload: dict) -> None:
        text = str(payload.get("text", ""))
        lang = str(payload.get("lang", payload.get("language", "")))
        ts = str(payload.get("ts", "")) or _now_ts()
        speaker = str(payload.get("speaker", "unknown"))
        with self.log_lock:
            self.transcript_log.append(LogEntry(text=text, lang=lang, ts=ts, speaker=speaker))
            line_no = len(self.transcript_log)
        if runtime_trace.enabled():
            runtime_trace.emit(
                "Conversation APPEND (client)",
                event_id=runtime_trace.next_event_id("conv-client"),
                line_no=line_no, ts=ts, speaker=speaker, text_preview=text[:60],
            )
        show_transcript(text, lang, ts)

    def _handle_reply(self, payload: dict) -> None:
        text = str(payload.get("text", ""))
        lang = str(payload.get("lang", ""))
        ts = str(payload.get("ts", "")) or _now_ts()
        speaker = str(payload.get("speaker", "agent"))
        with self.log_lock:
            self.transcript_log.append(LogEntry(text=text, lang=lang, ts=ts, speaker=speaker))
        show_agent_reply(text)
        if text and not isinstance(self.tts, NullTTSProvider):
            threading.Thread(
                target=self._speak_reply, args=(text,), daemon=True, name="tts-reply"
            ).start()

    def _speak_reply(self, text: str) -> None:
        self.tts_interrupt_event.clear()
        self.tts.speak(text)
        deadline = time.monotonic() + _TTS_SPEAK_DEADLINE_SECONDS
        while (
            self.tts.is_speaking()
            and time.monotonic() < deadline
            and not self.tts_interrupt_event.is_set()
        ):
            time.sleep(_TTS_POLL_INTERVAL_SECONDS)
        if self.tts_interrupt_event.is_set():
            self.tts.stop()
            show_info("[TTS] interrupted by operator speech")
        self.tts_interrupt_event.clear()

    def _handle_status(self, payload: dict) -> None:
        self.last_status = payload
        show_info(f"status: {payload.get('state', '?')} (was: {payload.get('previous', '?')})")

    def _handle_latency(self, payload: dict) -> None:
        stt_ms = payload.get("stt_ms")
        total_ms = payload.get("total_ms", payload.get("provider_ms"))
        show_info(f"STT={stt_ms}ms  TOTAL={total_ms}ms")

    def _handle_error(self, payload: dict) -> None:
        show_err(str(payload.get("label", "error")), payload.get("message", ""))

    def _handle_analysis(self, payload: dict) -> None:
        show_sep()
        _print(f"{CYAN}{BOLD}[分析結果]{RESET}\n{payload.get('text', payload)}")
        show_sep()

    def status_line(self) -> str:
        if self.last_status is None:
            return "state=(no status event received yet)"
        return f"state={self.last_status.get('state', '?')}"
