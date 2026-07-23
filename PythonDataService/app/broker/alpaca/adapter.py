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
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPosition,
)

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


def opt_bool(value: Any) -> bool | None:
    """Parse an optional vendor boolean; reject truthy non-boolean values."""
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"Expected a boolean or null, got {type(value).__name__}")


def to_bool(value: Any) -> bool:
    """Parse a required vendor boolean without truthiness coercion."""
    parsed = opt_bool(value)
    if parsed is None:
        raise TypeError("Expected a boolean, got null")
    return parsed


def _decimal_string(value: float) -> str:
    """Format a numeric contract value as non-scientific vendor text."""
    return format(Decimal(str(value)), "f")


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
    # ``int`` truncates fractional milliseconds toward zero. The boundary
    # contract is ms precision, so retain the closest representable instant
    # rather than silently biasing every sub-millisecond timestamp earlier.
    return round(parsed.timestamp() * 1000)


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
        pattern_day_trader=opt_bool(payload.get("pattern_day_trader")),
        trading_blocked=to_bool(payload["trading_blocked"]),
        account_blocked=to_bool(payload["account_blocked"]),
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


def _order_events(payload: Mapping[str, Any]) -> list[BrokerOrderEvent]:
    """Synthesize the events the REST order payload carries.

    Phase-1 REST orders expose only lifecycle timestamps, so a filled order
    yields one ``fill`` event. The richer per-event history arrives with the
    phase-2 ``trade_updates`` consumer.
    """
    filled_at = payload.get("filled_at")
    filled_avg_price = payload.get("filled_avg_price")
    if filled_at and filled_avg_price is not None:
        return [
            BrokerOrderEvent(
                event_type="fill",
                occurred_at_ms=rfc3339_to_ms(str(filled_at)),
                price=opt_float(filled_avg_price),
                quantity=opt_float(payload.get("filled_qty")),
            )
        ]
    return []


def to_alpaca_order_request(leg: BrokerOrderLeg, *, client_order_id: str) -> dict[str, Any]:
    """Build the Alpaca ``POST /v2/orders`` JSON body for one equity leg.

    The **outbound** boundary sibling of ``from_alpaca_order``: contract →
    vendor. Vendor field names (``qty``, ``type``, ``time_in_force``,
    ``limit_price``) stay inside this layer. S2 sends EQUITY MARKET or LIMIT with
    the operator's chosen ``time_in_force``; a limit leg adds ``limit_price`` and
    a market leg omits it (the contract validator guarantees this pairing).
    ``client_order_id`` is the Clerk-minted ``order_ref`` — Alpaca echoes it back
    so ownership is recoverable from a read.
    """
    body: dict[str, Any] = {
        "symbol": leg.symbol,
        # Alpaca accepts the qty as a string; send the operator's share count.
        "qty": _decimal_string(leg.quantity),
        "side": str(leg.side),
        "type": str(leg.order_type),
        "time_in_force": str(leg.time_in_force),
        "client_order_id": client_order_id,
    }
    if leg.limit_price is not None:
        # Alpaca expects the price as a string; only present for a limit order.
        body["limit_price"] = _decimal_string(leg.limit_price)
    return body


def from_alpaca_order(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerOrder:
    """Map a raw Alpaca order payload to a ``BrokerOrder`` with any fill event."""
    return BrokerOrder(
        broker=BROKER_ID,
        order_id=str(payload["id"]),
        client_order_id=opt_str(payload.get("client_order_id")),
        symbol=str(payload["symbol"]),
        asset_class=opt_str(payload.get("asset_class")),
        side=str(payload["side"]),
        order_type=str(payload.get("order_type") or payload.get("type")),
        time_in_force=str(payload["time_in_force"]),
        quantity=opt_float(payload.get("qty")),
        filled_quantity=to_float(payload.get("filled_qty") or 0),
        limit_price=opt_float(payload.get("limit_price")),
        stop_price=opt_float(payload.get("stop_price")),
        filled_avg_price=opt_float(payload.get("filled_avg_price")),
        status=str(payload["status"]),
        submitted_at_ms=opt_rfc3339_to_ms(payload.get("submitted_at")),
        created_at_ms=opt_rfc3339_to_ms(payload.get("created_at")),
        updated_at_ms=opt_rfc3339_to_ms(payload.get("updated_at")),
        filled_at_ms=opt_rfc3339_to_ms(payload.get("filled_at")),
        canceled_at_ms=opt_rfc3339_to_ms(payload.get("canceled_at")),
        expired_at_ms=opt_rfc3339_to_ms(payload.get("expired_at")),
        events=_order_events(payload),
        observed_at_ms=_observed(observed_at_ms),
    )


# The full set of Alpaca ``trade_updates`` event names, per Alpaca's websocket
# docs (verified 2026-07 against alpaca-py TradingStream, the schema authority).
# Kept as data so an unrecognized event surfaces (see ``from_alpaca_trade_update``)
# rather than being silently mapped — a new vendor event is a schema signal.
ALPACA_TRADE_UPDATE_EVENTS: frozenset[str] = frozenset(
    {
        # Common lifecycle.
        "new",
        "fill",
        "partial_fill",
        "canceled",
        "expired",
        "done_for_day",
        "replaced",
        # Less common but documented.
        "accepted",
        "rejected",
        "pending_new",
        "stopped",
        "pending_cancel",
        "pending_replace",
        "calculated",
        "suspended",
        "order_replace_rejected",
        "order_cancel_rejected",
    }
)


def trade_update_occurred_at_ms(payload: Mapping[str, Any]) -> int:
    """Resolve a ``trade_updates`` event's instant as ``int64`` ms UTC.

    Alpaca stamps each frame with a top-level ``timestamp`` (the event instant).
    A fill/partial_fill also carries the embedded order's ``filled_at``; the
    event ``timestamp`` is the authoritative per-event instant and is always
    present, so it is preferred. Fails fast (no timestamp) — a lifecycle event
    with no instant is corruption, not something to default to "now".
    """
    timestamp = payload.get("timestamp")
    if timestamp:
        return rfc3339_to_ms(str(timestamp))
    raise ValueError("Alpaca trade_updates event is missing its timestamp.")


def from_alpaca_trade_update(payload: Mapping[str, Any]) -> BrokerOrderEvent:
    """Map one Alpaca ``trade_updates`` ``data`` payload to a ``BrokerOrderEvent``.

    ``payload`` is the ``data`` object of a ``{"stream":"trade_updates","data":…}``
    frame: ``event`` (the lifecycle transition), ``timestamp`` (the event
    instant), an embedded ``order`` object, and — on a fill/partial_fill —
    top-level ``price``/``qty`` (the **execution slice** that filled, distinct
    from the order's cumulative ``filled_avg_price``/``filled_qty``).

    ``BrokerOrderEvent.price``/``quantity`` are the *per-execution* figures, so
    they carry the top-level ``price``/``qty`` when present (fills) and are
    ``None`` otherwise (``new``/``canceled``/``rejected`` carry no execution).
    The order's cumulative fill totals live on the ``BrokerOrder`` the caller
    maps separately from the embedded ``order`` object — they are deliberately
    NOT folded in here, which would mislabel a cumulative figure as a slice.

    An unrecognized ``event`` is surfaced by name — a new vendor event is a
    schema signal, never silently coerced.
    """
    event = str(payload["event"])
    if event not in ALPACA_TRADE_UPDATE_EVENTS:
        raise ValueError(
            f"Unrecognized Alpaca trade_updates event {event!r}; "
            "alpaca-py may have added a lifecycle event — extend the adapter."
        )
    return BrokerOrderEvent(
        event_type=event,
        occurred_at_ms=trade_update_occurred_at_ms(payload),
        price=opt_float(payload.get("price")),
        quantity=opt_float(payload.get("qty")),
    )


def from_alpaca_activity(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerActivity:
    """Map a raw Alpaca activity payload (trade or non-trade) to ``BrokerActivity``."""
    is_trade = "transaction_time" in payload
    return BrokerActivity(
        broker=BROKER_ID,
        activity_id=str(payload["id"]),
        activity_type=str(payload["activity_type"]),
        category="trade_activity" if is_trade else "non_trade_activity",
        symbol=opt_str(payload.get("symbol")),
        side=opt_str(payload.get("side")),
        quantity=opt_float(payload.get("qty")),
        price=opt_float(payload.get("price")),
        net_amount=opt_float(payload.get("net_amount")),
        occurred_at_ms=occurred_at_ms(payload),
        observed_at_ms=_observed(observed_at_ms),
    )


def from_alpaca_asset(payload: Mapping[str, Any]) -> BrokerAsset:
    """Map a raw Alpaca asset payload to a ``BrokerAsset``.

    Alpaca's ``/v2/assets`` payload names the asset class ``class`` (the SDK
    aliases it to ``asset_class``); prefer the raw key, fall back to the alias.
    A missing class fails loudly like every other required field — no sentinel
    default that would mask a schema change (the drift guard catches renames).
    """
    return BrokerAsset(
        broker=BROKER_ID,
        asset_id=str(payload["id"]),
        symbol=str(payload["symbol"]),
        name=opt_str(payload.get("name")),
        asset_class=str(payload["class"] if "class" in payload else payload["asset_class"]),
        exchange=opt_str(payload.get("exchange")),
        status=str(payload["status"]),
        tradable=bool(payload.get("tradable", False)),
        fractionable=bool(payload.get("fractionable", False)),
        shortable=opt_bool(payload.get("shortable")),
        marginable=opt_bool(payload.get("marginable")),
    )


def from_alpaca_clock(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerClockEvidence:
    """Map a raw Alpaca ``/v2/clock`` payload to ``BrokerClockEvidence``.

    **Evidence only.** The canonical calendar module remains the sole authority
    for scheduled session structure; nothing here feeds session/calendar logic.
    """
    return BrokerClockEvidence(
        broker=BROKER_ID,
        is_open=bool(payload["is_open"]),
        vendor_timestamp_ms=rfc3339_to_ms(str(payload["timestamp"])),
        next_open_ms=opt_rfc3339_to_ms(payload.get("next_open")),
        next_close_ms=opt_rfc3339_to_ms(payload.get("next_close")),
        observed_at_ms=_observed(observed_at_ms),
    )
