"""
compat.conversation
===================
Compatibility forwarding — conversation domain.

Re-exports the public API of the conversation modular package.
Contains no runtime logic. Forwarding only.
"""

from conversation.speaker_inference import infer_speaker, reset_speaker_state
from conversation.hallucination_guard import is_meaningful

__all__ = ["infer_speaker", "reset_speaker_state", "is_meaningful"]
