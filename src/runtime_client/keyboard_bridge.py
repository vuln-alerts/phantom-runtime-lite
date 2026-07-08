"""
runtime_client/keyboard_bridge.py
====================================
Wires src/ui/keyboard.py's KeyboardController/RuntimeContext (reused
verbatim, unmodified) into the Client. Every RuntimeContext callable is
either a trivial local console action (the canned phrase keys, help,
log display/clear) or a Control Event send (G/g/r) -- STT, LLM, Meeting
Analysis, and Summary generation themselves remain entirely the
Server's responsibility; this module never performs any of them itself,
it only triggers the existing server-side dispatch remotely (see
runtime/transport_gateway.py's Control Event relay and
phantom_runtime.py's control_loop()).

Design note on 'g'/'G': RuntimeContext.manual_flush_enabled is always
set to False here regardless of the server's own --manual-flush flag,
so KeyboardController's 'g' handler always takes its single-callable
branch (straight to generate_meeting_analysis_fn) rather than the
audio-buffering branch -- the server already knows, from its own CLI
flag, which of the two behaviors to run once it receives the
"generate_meeting_analysis" Control Event, so the Client does not need
to replicate that branch.

EXPORTED API:
  build_keyboard_thread(config, store, loop, control_queue, kb_shutdown)
      -- returns a not-yet-started threading.Thread running
         KeyboardController against a freshly-built RuntimeContext
"""

import asyncio
import json
import os
import random
import sys
import threading
from typing import Callable

_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime.state_machine import ConversationState
from ui.keyboard import KeyboardController, RuntimeContext

from runtime_client.config import ClientConfig
from runtime_client.typed_event import (
    BOLD, CYAN, GRAY, GREEN, RESET, WHITE, YELLOW,
    TypedEventStore, show_clarify, show_delay_en, show_delay_jp, show_hold,
    show_info, show_sep, show_warn,
)

_DELAY_EN_DEFAULT = [
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

_DELAY_JP_DEFAULT = [
    "少し考えさせてください",
    "少々お待ちください",
    "今整理しています",
    "ちょっと考えます",
    "えーと、少し考えます",
    "今まとめています",
    "確認させてください",
]

_CLARIFY_DEFAULT = [
    "Sorry, could you repeat the question?",
    "I want to make sure I understood correctly — could you say that once more?",
    "Sorry, the audio broke up a little bit. Could you repeat that?",
    "Could you clarify which part you'd like me to focus on?",
    "Just to make sure I heard that right — could you say it again?",
]

_DELAY_EN_SLOTS = _DELAY_EN_DEFAULT[:5]


class NotifyingEvent(threading.Event):
    """
    A threading.Event whose set()/clear() also fire a callback -- used
    for `recording_active` so KeyboardController's 'r' handler (which
    calls .set()/.clear() directly, with no callback hook of its own)
    can be observed without touching ui/keyboard.py at all.
    """

    def __init__(self, on_change: Callable[[bool], None]) -> None:
        super().__init__()
        self._on_change = on_change

    def set(self) -> None:
        super().set()
        self._on_change(True)

    def clear(self) -> None:
        super().clear()
        self._on_change(False)


class _NullTTS:
    """Phase 1-4 stub: TTS itself is Phase 3 scope. Always reports idle."""

    def is_speaking(self) -> bool:
        return False

    def stop(self) -> None:
        return None


def _send_control(
    loop: asyncio.AbstractEventLoop,
    control_queue: "asyncio.Queue[str]",
    command: str,
) -> None:
    line = json.dumps({"command": command})

    def _enqueue() -> None:
        try:
            control_queue.put_nowait(line)
        except asyncio.QueueFull:
            show_warn(f"control queue full — '{command}' dropped")

    loop.call_soon_threadsafe(_enqueue)


def build_keyboard_thread(
    config: ClientConfig,
    store: TypedEventStore,
    loop: asyncio.AbstractEventLoop,
    control_queue: "asyncio.Queue[str]",
    kb_shutdown: threading.Event,
) -> threading.Thread:
    """
    Build (but do not start) the thread running KeyboardController
    against a Client-local RuntimeContext. kb_shutdown is set by the 'q'
    key (via ctx.shutdown_event); the caller is responsible for bridging
    that into the asyncio side's stop_event (see main.py).
    """
    state_holder = {"state": ConversationState.IDLE}

    recording_active = NotifyingEvent(
        on_change=lambda is_on: _send_control(
            loop, control_queue, "toggle_recording"
        )
    )
    # Initializes local state to match VADBuffer's own ON-by-default --
    # calls threading.Event.set() directly (bypassing NotifyingEvent's
    # override) so this bookkeeping does not itself fire a spurious
    # "toggle_recording" Control Event at connect time, before the user
    # has pressed 'r' at all.
    threading.Event.set(recording_active)

    def show_recording_status() -> None:
        if recording_active.is_set():
            show_info(f"● RECORDING  (blocks sent: {store.audio_blocks_sent})")
        else:
            show_info(f"○ IDLE  (blocks sent: {store.audio_blocks_sent})")

    ctx = RuntimeContext(
        shutdown_event=kb_shutdown,
        manual_flush_enabled=False,  # see module docstring
        sample_rate=config.sample_rate,
        enqueue_latest_fn=lambda audio: None,  # unreachable: manual_flush_enabled is False
        manual_buf_flush_fn=lambda: None,      # unreachable: manual_flush_enabled is False
        manual_buf_status_fn=lambda: "",       # unreachable: manual_flush_enabled is False
        manual_buf_lock=threading.Lock(),
        manual_audio_buffer=[],
        recording_active=recording_active,
        show_recording_status_fn=show_recording_status,
        tts=_NullTTS(),
        tts_interrupt_event=threading.Event(),
        get_state_fn=lambda: state_holder["state"],
        set_state_fn=lambda s: state_holder.__setitem__("state", s),
        idle_state=ConversationState.IDLE,
        transcript_log=store.transcript_log,
        log_lock=store.log_lock,
        show_info=show_info,
        show_warn=show_warn,
        show_sep=show_sep,
        show_hold=show_hold,
        show_clarify_fn=lambda: show_clarify(random.choice(_CLARIFY_DEFAULT)),
        show_delay_en_fn=lambda: show_delay_en(random.choice(_DELAY_EN_DEFAULT)),
        show_delay_jp_fn=lambda: show_delay_jp(random.choice(_DELAY_JP_DEFAULT)),
        show_delay_slot_fn=lambda n: show_delay_en(
            _DELAY_EN_SLOTS[max(0, min(n - 1, len(_DELAY_EN_SLOTS) - 1))]
        ),
        print_fn=lambda *parts, end="\n": print("".join(parts), end=end, flush=True),
        delay_en_list=_DELAY_EN_DEFAULT,
        delay_jp_list=_DELAY_JP_DEFAULT,
        delay_en_slots=_DELAY_EN_SLOTS,
        generate_summary_fn=lambda: _send_control(loop, control_queue, "generate_summary"),
        generate_meeting_analysis_fn=lambda: _send_control(
            loop, control_queue, "generate_meeting_analysis"
        ),
        debug_audio_save=False,
        save_debug_audio_fn=lambda audio: None,
        set_runtime_mode_fn=lambda mode: None,  # unreachable: manual_flush_enabled is False
        runtime_mode_meeting=None,
        agent_mode=False,
        tts_name=config.tts,
        agent_env_flag=False,
        CYAN=CYAN, YELLOW=YELLOW, GREEN=GREEN,
        GRAY=GRAY, RESET=RESET, BOLD=BOLD, WHITE=WHITE,
    )

    controller = KeyboardController(ctx)
    return threading.Thread(target=controller.run, daemon=True, name="keyboard")
