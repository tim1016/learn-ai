"""Loader for the examples Alpaca publishes in its documentation.

They are documentation, not recordings: see ``tests/fixtures/alpaca_documented/README.md``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# tests/broker/alpaca/documented_examples.py → tests/fixtures/alpaca_documented/
_DOCUMENTED = Path(__file__).resolve().parents[2] / "fixtures" / "alpaca_documented"


def documented(family: str, filename: str) -> Any:
    """One file of documented examples: (family, filename) → parsed JSON."""
    return json.loads((_DOCUMENTED / family / filename).read_text(encoding="utf-8"))
