"""
compat.ui
=========
Compatibility forwarding — ui domain.

Re-exports the public API of the ui modular package.
Contains no runtime logic. Forwarding only.
"""

from ui.keyboard import KeyboardController, RuntimeContext

__all__ = ["KeyboardController", "RuntimeContext"]
