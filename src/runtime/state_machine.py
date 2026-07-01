"""
runtime/state_machine.py
=========================
Conversation state and runtime mode enums for the Phantom Conversational Runtime.

Relocated from phantom_conversational_runtime_v22.py (M5 Runtime Core Separation).
Original location: inline ConversationState and RuntimeMode class definitions in v22.

EXPORTED API:
  ConversationState — conversation lifecycle states
  RuntimeMode       — runtime operating modes
"""

import enum


class ConversationState(enum.Enum):
    IDLE               = "idle"
    RECRUITER_SPEAKING = "recruiter_speaking"
    USER_SPEAKING      = "user_speaking"
    WAITING_FOR_REPLY  = "waiting_for_reply"
    GENERATING         = "generating"
    SPEAKING           = "speaking"


class RuntimeMode(enum.Enum):
    INTERVIEW = "interview"
    MEETING   = "meeting"
    SUMMARY   = "summary"
