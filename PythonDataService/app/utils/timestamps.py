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
    return int(dt.timestamp() * 1000)
