"""The argparse flag types and JSON encoding every Alpaca operator CLI shares.

Deliberately small. The ``manage_alpaca_*`` mains keep what is genuinely each
one's own -- its coded-refusal type, its exit-code vocabulary and its payload
envelope -- and share only what has to mean the same thing in all of them: an
instant an operator quotes is bounded identically wherever it is quoted, and a
plan document is encoded identically wherever it is written, because more than
one of these CLIs writes a plan file whose content hash is its confirmation
token.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from app.utils.session_anchors import MAX_TIMESTAMP_MS


def timestamp_ms(raw: str) -> int:
    """One instant, ``int64 ms UTC``, inside the domain's admissible range."""
    value = int(raw)
    if not 0 <= value <= MAX_TIMESTAMP_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_TIMESTAMP_MS} milliseconds since epoch UTC, not {value}"
        )
    return value


def jsonable(value: Any) -> Any:
    """A dataclass tree as plain JSON types, dataclasses recursed, ``Path`` as text.

    Shared because a divergence between two encoders is a silently unverifiable
    plan: the confirmation token is a hash over exactly these bytes, so a CLI
    that encoded a tuple or a ``Path`` differently would mint a token the other
    could not check.
    """
    if is_dataclass(value) and not isinstance(value, type):
        return jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    return value


__all__ = ["jsonable", "timestamp_ms"]
