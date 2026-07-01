"""
compat.profiles
===============
Compatibility forwarding — profiles domain.

Re-exports the public API of the profiles modular package.
Contains no runtime logic. Forwarding only.
"""

from profiles.loader import load_profile, parse_md_profile, parse_json_profile
from profiles.schema import validate_profile, normalise_profile, PROFILE_DEFAULTS

__all__ = [
    "load_profile",
    "parse_md_profile",
    "parse_json_profile",
    "validate_profile",
    "normalise_profile",
    "PROFILE_DEFAULTS",
]
