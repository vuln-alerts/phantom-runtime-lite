"""
compat.audio
============
Compatibility forwarding — audio domain.

Re-exports the public API of the audio modular package.
Contains no runtime logic. Forwarding only.
"""

from audio.vad_buffering import VADBuffer

__all__ = ["VADBuffer"]
