# Ensure PythonDataService/ (the package root) is on sys.path so
# imports of 'scripts.*' resolve when this test module runs. Redundant
# with the root conftest.py and pytest.ini's pythonpath= setting, but
# kept as a safety net if either is removed.

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parents[2])
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
