"""Loaded backtest transport/decoder identity, separate from approval identity."""
from __future__ import annotations

import hashlib
from pathlib import Path

from app.research.options_golden_search.identity import execution_identity as options_identity
from app.research.runs.hashing import hash_payload

_ROOT = Path(__file__).resolve().parent


def _source_digest() -> str:
    sources = list(_ROOT.glob("*.py"))
    lake = _ROOT.parent / "data_lake"
    sources.extend(path for name in ("option_session.py", "option_bars.py", "option_listing.py", "lean_writer.py")
                   if (path := lake / name).exists())
    return hash_payload({str(path.relative_to(_ROOT.parent)): hashlib.sha256(path.read_bytes()).hexdigest()
                         for path in sorted(sources)})


_LOADED = _source_digest()


def backtest_execution_identity() -> dict[str, str]:
    if _source_digest() != _LOADED:
        raise ValueError("Options Backtest decoder or execution code changed; restart before dispatch")
    return {**options_identity(), "backtest_execution": _LOADED}
