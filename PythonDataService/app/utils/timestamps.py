"""Shared timestamp conversion helpers.

Per ``.claude/rules/numerical-rigor.md``, ``int64 ms UTC`` is the canonical
wire and storage format for timestamps. ``to_ms_utc`` is the single
conversion boundary from a tz-aware ``datetime`` to that format.
"""

from __future__ import annotations

from datetime import datetime


def to_ms_utc(dt: datetime) -> int:
    """Convert a tz-aware ``datetime`` to ``int64 ms`` since Unix epoch UTC.

    POSIX ``timestamp()`` is timezone-independent, so multiplying by 1000
    yields the canonical wire format regardless of the input's tzinfo
    (``America/New_York`` engine timestamps and pure UTC datetimes both
    produce the correct epoch ms).

    Truncation (``int(...)``) is intentional: every caller currently passes
    sub-millisecond-aligned datetimes (whole-minute bar timestamps), so
    truncation and ``round()`` agree. If sub-ms resolutions ever land,
    revisit with banker's rounding.
    """
    return int(dt.timestamp() * 1000)
