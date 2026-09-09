"""The argparse flag types every Alpaca operator CLI must agree on.

Deliberately small. The ``manage_alpaca_*`` mains keep what is genuinely each
one's own -- its coded-refusal type, its exit-code vocabulary and its payload
envelope -- and share only the parsing that has to mean the same thing in both,
so an instant an operator quotes is bounded identically wherever it is quoted.
"""

from __future__ import annotations

import argparse

from app.utils.session_anchors import MAX_TIMESTAMP_MS


def timestamp_ms(raw: str) -> int:
    """One instant, ``int64 ms UTC``, inside the domain's admissible range."""
    value = int(raw)
    if not 0 <= value <= MAX_TIMESTAMP_MS:
        raise argparse.ArgumentTypeError(
            f"must be between 0 and {MAX_TIMESTAMP_MS} milliseconds since epoch UTC, not {value}"
        )
    return value


__all__ = ["timestamp_ms"]
