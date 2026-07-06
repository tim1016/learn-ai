"""Shared timestamp conversion helpers.

Per ``.claude/rules/numerical-rigor.md``, ``int64 ms UTC`` is the canonical
wire and storage format for timestamps. ``to_ms_utc`` is the single
conversion boundary from a tz-aware ``datetime`` to that format;
``now_ms_utc`` is the single sanctioned way to produce a fresh wall-clock
timestamp in the canonical format (replaces the banned ``datetime.utcnow``
and ten copy-pasted ``_now_ms()`` helpers across ``app/broker/ibkr/``).
"""

from __future__ import annotations

import time
from datetime import datetime
from numbers import Real


def now_ms_utc() -> int:
    """Return current wall-clock as ``int64 ms`` since Unix epoch UTC.

    The single canonical clock helper for the data plane. Use this
    everywhere a fresh ms timestamp is needed; the duplicated
    ``_now_ms`` / ``now_ms_utc`` helpers elsewhere in the tree re-export
    this for back-compat.

    ``time.time()`` (not ``datetime.now(UTC)``) keeps the implementation
    single-syscall and matches the original ledger / halt definitions
    byte-for-byte. ``datetime.utcnow`` is banned by the rule file.
    """
    return int(time.time() * 1000)


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
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("timestamp datetime must be timezone-aware")
    return int(dt.timestamp() * 1000)


def timestamp_like_to_ms_utc(value: object, *, field_name: str = "timestamp") -> int:
    """Normalize a timestamp-like value to canonical ``int64 ms UTC``.

    This is for legacy data-frame seams that may still hand internal strategy
    helpers epoch ms, tz-aware datetimes, or timezone-bearing strings. Boundary
    DTOs should stay typed as numeric ms and should not call this to accept
    arbitrary user input.
    """
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must not be a boolean")
    if isinstance(value, Real):
        return int(value)
    if isinstance(value, datetime):
        return to_ms_utc(value)
    if isinstance(value, str):
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError(f"{field_name} string must be ISO-8601 with timezone: {value!r}") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"{field_name} string must include a timezone: {value!r}")
        return to_ms_utc(parsed)
    raise TypeError(f"unsupported {field_name} type: {type(value).__name__}")
