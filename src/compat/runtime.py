"""
compat.runtime
==============
Compatibility forwarding — runtime domain.

Re-exports the public API of the runtime modular package.
Contains no runtime logic. Forwarding only.
"""

from runtime.runtime_logger import RuntimeLogger

__all__ = ["RuntimeLogger"]
