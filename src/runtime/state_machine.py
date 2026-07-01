"""
runtime/state_machine.py
=========================
Conversation state and runtime mode enums for Phantom Runtime Lite.

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
