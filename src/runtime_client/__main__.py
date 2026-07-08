"""
Allows `python -m runtime_client` (from inside src/) or
`python -m src.runtime_client` (from the repo root) to launch the
Runtime Client.
"""

import os
import sys

# Must run before the bare `runtime_client.main` import below: when
# launched as `python -m src.runtime_client` from the repo root, only
# the repo root is on sys.path, not src/ itself -- so the top-level
# `runtime_client` package isn't importable yet without this.
_SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)

from runtime_client.main import main

if __name__ == "__main__":
    main(sys.argv[1:])
