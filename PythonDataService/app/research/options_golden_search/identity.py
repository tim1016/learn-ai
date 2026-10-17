"""Loaded executable identity for Options selection and qualification (#3332)."""
from __future__ import annotations

import hashlib
import platform
from pathlib import Path

from app.lean_sidecar.trading_calendar import CALENDAR_IDENTITY, CALENDAR_PACKAGE_VERSION
from app.options_bots.golden_proof import current_rules_digest
from app.research.runs.hashing import hash_payload

_ROOT = Path(__file__).resolve().parent


def _selection_digest() -> str:
    return hash_payload({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(_ROOT.glob("*.py"))})


_LOADED_SELECTION = _selection_digest()


def execution_identity() -> dict[str, str]:
    """Refuse a source tree that changed since import; width is operational, not scientific identity."""
    if _selection_digest() != _LOADED_SELECTION:
        raise ValueError("Options search code changed on disk. Restart before locking or resuming a study.")
    from importlib.metadata import version

    return {
        "options_rules": current_rules_digest(), "selection": _LOADED_SELECTION,
        "calendar": CALENDAR_IDENTITY, "calendar_package": CALENDAR_PACKAGE_VERSION,
        "environment": hash_payload({"python": platform.python_version(), **{name: version(name) for name in ("numpy", "pandas", "pydantic", "exchange-calendars")}}),
    }
