"""Put the repo root on the import path so `ml` resolves under any invocation.

`ml` is not an installed package — it is a directory of scripts that import
the installed backend, the same arrangement `/jobs` uses. That works when
pytest is started as `python -m pytest`, which puts the working directory on
`sys.path`, and fails when it is started as `pytest`, which does not. CI runs
the latter.

Rather than pin the invocation in the workflow and leave the trap set for the
next person, the path is established here, where any entry point picks it up.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
