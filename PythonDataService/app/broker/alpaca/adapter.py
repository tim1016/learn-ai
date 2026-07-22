"""Alpaca → contract adapter (Broker System v2, Layer 1 seam).

The adapter is the **single ingestion boundary**: it consumes Alpaca's raw JSON
mappings (from the ``raw_data=True`` client) and produces broker-contract
models. Every vendor→contract conversion happens here, exactly once:

- RFC-3339 timestamp strings → ``int64`` ms UTC (temporal-rigor: the one
  conversion boundary on ingestion).
- Decimal money/quantity strings → ``float`` (read-only display surface; the
  verbatim decimals remain in the capture journal).

This module holds the shared helpers; each read-path slice adds its per-model
mapper (``from_alpaca_account``, ``from_alpaca_position``, …) built on them.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

# DST-correct ET zone for anchoring bare dates (never a fixed offset).
_ET = ZoneInfo("America/New_York")

# Trailing sub-microsecond digits some feeds emit beyond fromisoformat's range.
_OVERLONG_FRACTION = re.compile(r"(?P<head>.*\.\d{6})\d+(?P<tail>.*)")


def now_ms() -> int:
    """Current instant as ``int64`` ms UTC (default ``observed_at_ms``)."""
    return int(datetime.now(UTC).timestamp() * 1000)


def to_float(value: Any) -> float:
    """Parse a required Alpaca numeric (string or number) to ``float``."""
    return float(value)


def opt_float(value: Any) -> float | None:
    """Parse an optional Alpaca numeric to ``float``; ``None``/empty → ``None``."""
    if value is None or value == "":
        return None
    return float(value)


def _parse_rfc3339(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        match = _OVERLONG_FRACTION.match(text)
        if match is None:
            raise
        return datetime.fromisoformat(match.group("head") + match.group("tail"))


def rfc3339_to_ms(value: str) -> int:
    """Convert a tz-aware RFC-3339 timestamp to ``int64`` ms UTC.

    Fails fast on a naive timestamp — Alpaca always sends a timezone, so a naive
    value signals corruption, not something to silently assume into UTC.
    """
    parsed = _parse_rfc3339(value)
    if parsed.tzinfo is None:
        raise ValueError(f"Alpaca timestamp is not timezone-aware: {value!r}")
    return int(parsed.timestamp() * 1000)


def opt_rfc3339_to_ms(value: Any) -> int | None:
    """Optional RFC-3339 → ms; ``None``/empty → ``None``."""
    if value is None or value == "":
        return None
    return rfc3339_to_ms(str(value))


def et_date_to_ms(value: str) -> int:
    """Anchor a bare ``YYYY-MM-DD`` at 00:00 America/New_York → ``int64`` ms UTC.

    Non-trade activity rows carry a settlement/record *date*, not an instant.
    Anchoring at the start of the ET calendar day keeps the value from drifting
    a calendar day when rendered in ``date-et`` mode (temporal-rigor).
    """
    day = date.fromisoformat(value)
    anchored = datetime(day.year, day.month, day.day, tzinfo=_ET)
    return int(anchored.timestamp() * 1000)


def occurred_at_ms(payload: Mapping[str, Any]) -> int | None:
    """Best occurred-at for an activity: trade ``transaction_time`` or date."""
    if payload.get("transaction_time"):
        return rfc3339_to_ms(str(payload["transaction_time"]))
    if payload.get("date"):
        return et_date_to_ms(str(payload["date"]))
    return None
