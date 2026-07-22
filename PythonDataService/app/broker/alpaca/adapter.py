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

from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.models import BrokerAccountSnapshot, BrokerPosition

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


def opt_str(value: Any) -> str | None:
    """Coerce an optional value to ``str``; ``None`` stays ``None``."""
    return None if value is None else str(value)


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


def _observed(observed_at_ms: int | None) -> int:
    """Resolve the ingestion instant (injectable for deterministic tests)."""
    return observed_at_ms if observed_at_ms is not None else now_ms()


# ── Per-model mappers ───────────────────────────────────────────────────────


def from_alpaca_account(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerAccountSnapshot:
    """Map a raw Alpaca account payload to a ``BrokerAccountSnapshot``."""
    return BrokerAccountSnapshot(
        broker=BROKER_ID,
        account_id=str(payload["account_number"]),
        account_status=str(payload["status"]),
        currency=str(payload.get("currency") or "USD"),
        cash=to_float(payload["cash"]),
        equity=to_float(payload["equity"]),
        buying_power=to_float(payload["buying_power"]),
        portfolio_value=to_float(payload["portfolio_value"]),
        long_market_value=to_float(payload["long_market_value"]),
        short_market_value=to_float(payload["short_market_value"]),
        pattern_day_trader=bool(payload["pattern_day_trader"]),
        trading_blocked=bool(payload["trading_blocked"]),
        account_blocked=bool(payload["account_blocked"]),
        created_at_ms=opt_rfc3339_to_ms(payload.get("created_at")),
        observed_at_ms=_observed(observed_at_ms),
    )


def from_alpaca_position(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerPosition:
    """Map a raw Alpaca position payload to a ``BrokerPosition`` (signed qty)."""
    return BrokerPosition(
        broker=BROKER_ID,
        symbol=str(payload["symbol"]),
        asset_id=opt_str(payload.get("asset_id")),
        asset_class=opt_str(payload.get("asset_class")),
        quantity=to_float(payload["qty"]),
        side=str(payload["side"]),
        average_entry_price=to_float(payload["avg_entry_price"]),
        market_value=to_float(payload["market_value"]),
        cost_basis=to_float(payload["cost_basis"]),
        current_price=opt_float(payload.get("current_price")),
        unrealized_pl=to_float(payload["unrealized_pl"]),
        unrealized_plpc=opt_float(payload.get("unrealized_plpc")),
        observed_at_ms=_observed(observed_at_ms),
    )
