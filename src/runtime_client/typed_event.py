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

EXPORTED API:
  LogEntry        -- (text, lang, ts, speaker) NamedTuple, shape-compatible
                      with what ui/keyboard.py's 'l' handler expects
  ANSI colors: CYAN, YELLOW, GREEN, GRAY, RESET, BOLD, WHITE
  show_info/show_warn/show_sep/show_hold/show_clarify/show_delay_en/
  show_delay_jp -- console helpers matching phantom_runtime.py's originals
  TypedEventStore -- local mirror + renderer for the inbound event stream
"""

import datetime
import json
import threading
from collections import deque
from typing import NamedTuple, Optional


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
    """

    def __init__(self, maxlen: int = 200) -> None:
        self.transcript_log: "deque[LogEntry]" = deque(maxlen=maxlen)
        self.log_lock = threading.Lock()
        self.last_status: Optional[dict] = None
        self.audio_blocks_sent = 0

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
        show_transcript(text, lang, ts)

    def _handle_reply(self, payload: dict) -> None:
        text = str(payload.get("text", ""))
        lang = str(payload.get("lang", ""))
        ts = str(payload.get("ts", "")) or _now_ts()
        speaker = str(payload.get("speaker", "agent"))
        with self.log_lock:
            self.transcript_log.append(LogEntry(text=text, lang=lang, ts=ts, speaker=speaker))
        show_agent_reply(text)

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
