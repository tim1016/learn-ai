"""Root conftest — early bootstrap for every pytest session in this service.

Two jobs, in order:

* make the service root importable (``scripts.*`` tests);
* establish, before any collection import happens, the two facts a test
  session needs regardless of which root is collected (#2485):
  ``POLYGON_API_KEY`` (``app.config.Settings`` requires it at import) and
  the Signal Program source anchor (#2450) — a declared source imported
  before the anchor runs makes the anchor refuse to boot, and collecting a
  root inside ``app/`` imports package ``__init__`` chains before any
  per-root conftest body can run.

``tests/conftest.py`` keeps the test-surface environment opt-outs that are
specifically about router tests; these two facts live here, once.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ.setdefault("POLYGON_API_KEY", "test-key-for-testing")

from app.services.program_source_anchor import record_imported_program_sources

record_imported_program_sources()
