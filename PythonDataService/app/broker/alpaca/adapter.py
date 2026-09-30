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

import logging
import re
from collections.abc import Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal
from zoneinfo import ZoneInfo

from app.broker.alpaca.config import BROKER_ID
from app.broker.contract.errors import BrokerAccountModeDisagreement
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderEvent,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

# DST-correct ET zone for anchoring bare dates (never a fixed offset).
_ET = ZoneInfo("America/New_York")

# Trailing sub-microsecond digits some feeds emit beyond fromisoformat's range.
_OVERLONG_FRACTION = re.compile(r"(?P<head>.*\.\d{6})\d+(?P<tail>.*)")


now_ms = now_ms_utc


def to_float(value: Any) -> float:
    """Parse a required Alpaca numeric (string or number) to ``float``.

    A JSON boolean is refused: ``float(True) == 1.0``, so a corrupt money field
    would otherwise become $1 or $0 of broker evidence.
    """
    if isinstance(value, bool):
        raise TypeError("Expected an Alpaca numeric, not a boolean")
    return float(value)


def opt_float(value: Any) -> float | None:
    """Parse an optional Alpaca numeric to ``float``; ``None``/empty → ``None``."""
    if value is None or value == "":
        return None
    return to_float(value)


def str_or_blank(value: Any) -> str:
    """Read vendor text, or ``""`` for anything that is not non-blank text.

    Never ``str(None) == "None"``, which reads as a real account number,
    order id or ticker (#2627, #2643). The order mapper reads its text this
    way: one order missing its id or symbol is the Clerk's to contain on its
    own, never a reason to refuse every other order in the answer (#2363).
    """
    return value if isinstance(value, str) and value.strip() else ""


def to_str(value: Any, *, field: str) -> str:
    """Parse a required Alpaca text field, returned unchanged; refuse anything else."""
    text = str_or_blank(value)
    if not text:
        raise ValueError(f"Alpaca field {field!r} must be a non-blank string, got {value!r}")
    return text


def opt_str(value: Any) -> str | None:
    """Coerce an optional value to ``str``; ``None`` stays ``None``."""
    return None if value is None else str(value)


def opt_bool(value: Any, *, field: str) -> bool | None:
    """Parse an optional vendor boolean; reject truthy non-boolean values."""
    if value is None or isinstance(value, bool):
        return value
    raise TypeError(f"Alpaca field {field!r} must be a boolean or null, got {value!r}")


def to_bool(value: Any, *, field: str) -> bool:
    """Parse a required vendor boolean without truthiness coercion."""
    if not isinstance(value, bool):
        raise TypeError(f"Alpaca field {field!r} must be a boolean, got {value!r}")
    return value


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


def epoch_to_ms(value: Any) -> int:
    """Normalize Alpaca epoch timestamps to canonical ``int64`` ms UTC.

    Alpaca's portfolio-history documentation describes seconds while one of its
    published examples uses milliseconds. Accept both vendor representations at
    this single ingestion boundary, rejecting non-finite and non-numeric input.
    """
    if isinstance(value, bool):
        raise TypeError("Alpaca portfolio-history timestamp must be numeric, not boolean")
    try:
        epoch = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TypeError("Alpaca portfolio-history timestamp must be numeric") from exc
    if not epoch.is_finite():
        raise ValueError("Alpaca portfolio-history timestamp must be finite")
    # Unix timestamps expressed in seconds are currently 10 digits; milliseconds
    # are 13. The threshold accepts either documented representation without
    # guessing at an ISO string or carrying a non-canonical value downstream.
    milliseconds = epoch * 1_000 if abs(epoch) < Decimal("100000000000") else epoch
    return int(milliseconds.to_integral_value(rounding=ROUND_HALF_UP))


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


_PAPER_ACCOUNT_NUMBER_PREFIX = "PA"


def from_alpaca_account(
    payload: Mapping[str, Any],
    *,
    account_mode: Literal["paper", "live"],
    observed_at_ms: int | None = None,
) -> BrokerAccountSnapshot:
    """Map a raw Alpaca account payload to a ``BrokerAccountSnapshot``.

    ``account_mode`` is the settings mode that selected the endpoint — backend
    configuration truth, never inferred from the payload (ADR 0059 D1). The
    account-number shape is a refusal input only: a paper-shaped number under
    ``live``, or a live-shaped number under ``paper``, is a disagreement.

    A number that is not a non-blank string is no account at all, in any
    mode: ``str(None)`` once became live account ``"None"`` (#2627).
    """
    account_number = to_str(payload["account_number"], field="account_number")
    looks_paper = account_number.startswith(_PAPER_ACCOUNT_NUMBER_PREFIX)
    if looks_paper != (account_mode == "paper"):
        raise BrokerAccountModeDisagreement(
            "The configured Alpaca mode and the observed account disagree.",
            broker=BROKER_ID,
            detail=(
                f"ALPACA_MODE={account_mode!r} but the account number "
                f"{'begins' if looks_paper else 'does not begin'} with "
                f"{_PAPER_ACCOUNT_NUMBER_PREFIX!r}, which is the paper-account shape."
            ),
        )
    return BrokerAccountSnapshot(
        broker=BROKER_ID,
        account_id=account_number,
        account_mode=account_mode,
        account_status=to_str(payload["status"], field="status"),
        currency=str(payload.get("currency") or "USD"),
        cash=to_float(payload["cash"]),
        equity=to_float(payload["equity"]),
        buying_power=to_float(payload["buying_power"]),
        portfolio_value=to_float(payload["portfolio_value"]),
        long_market_value=to_float(payload["long_market_value"]),
        short_market_value=to_float(payload["short_market_value"]),
        multiplier=opt_float(payload.get("multiplier")),
        regt_buying_power=opt_float(payload.get("regt_buying_power")),
        daytrading_buying_power=opt_float(payload.get("daytrading_buying_power")),
        maintenance_margin=opt_float(payload.get("maintenance_margin")),
        initial_margin=opt_float(payload.get("initial_margin")),
        sma=opt_float(payload.get("sma")),
        last_equity=opt_float(payload.get("last_equity")),
        pattern_day_trader=opt_bool(payload.get("pattern_day_trader"), field="pattern_day_trader"),
        trading_blocked=to_bool(payload["trading_blocked"], field="trading_blocked"),
        account_blocked=to_bool(payload["account_blocked"], field="account_blocked"),
        created_at_ms=opt_rfc3339_to_ms(payload.get("created_at")),
        observed_at_ms=_observed(observed_at_ms),
    )


def from_alpaca_portfolio_history(
    payload: Mapping[str, Any],
) -> BrokerPortfolioHistory:
    """Map Alpaca's account history to the broker-owned C1 curve contract."""
    return BrokerPortfolioHistory(
        timestamps=[epoch_to_ms(value) for value in payload["timestamp"]],
        equity=[to_float(value) for value in payload["equity"]],
        profit_loss=[to_float(value) for value in payload["profit_loss"]],
        base_value=opt_float(payload.get("base_value")),
        timeframe=to_str(payload["timeframe"], field="timeframe"),
    )


def from_alpaca_position(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerPosition:
    """Map a raw Alpaca position payload to a ``BrokerPosition`` (signed qty)."""
    return BrokerPosition(
        broker=BROKER_ID,
        symbol=to_str(payload["symbol"], field="symbol"),
        asset_id=opt_str(payload.get("asset_id")),
        asset_class=opt_str(payload.get("asset_class")),
        quantity=to_float(payload["qty"]),
        side=to_str(payload["side"], field="side"),
        average_entry_price=to_float(payload["avg_entry_price"]),
        market_value=to_float(payload["market_value"]),
        cost_basis=to_float(payload["cost_basis"]),
        current_price=opt_float(payload.get("current_price")),
        unrealized_pl=to_float(payload["unrealized_pl"]),
        unrealized_plpc=opt_float(payload.get("unrealized_plpc")),
        observed_at_ms=_observed(observed_at_ms),
        prior_close_price=opt_float(payload.get("lastday_price")),
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


def fill_latency_seconds(
    submitted_at_ms: int | None,
    filled_at_ms: int | None,
) -> float | None:
    """Return elapsed broker submission-to-fill time in seconds.

    Formula: fill latency seconds = (filled_at_ms - submitted_at_ms) / 1,000.
    Reference: Alpaca Trading API order properties (``submitted_at`` and
      ``filled_at``), https://docs.alpaca.markets/us/docs/brokerapi-trading
    Canonical implementation: this file.
    Validated against:
      tests/broker/alpaca/test_adapter_orders.py::test_filled_order_maps_every_field_and_synthesizes_fill_event
      and ::test_fill_latency_is_unknown_until_both_broker_clocks_exist
    """
    if submitted_at_ms is None or filled_at_ms is None:
        return None
    return (filled_at_ms - submitted_at_ms) / 1_000


def to_alpaca_order_request(leg: BrokerOrderLeg, *, client_order_id: str) -> dict[str, Any]:
    """Build the Alpaca ``POST /v2/orders`` JSON body for one equity leg.

    The **outbound** boundary sibling of ``from_alpaca_order``: contract →
    vendor. Vendor field names (``qty``, ``type``, ``time_in_force``,
    ``limit_price``) stay inside this layer. S2 sends EQUITY MARKET or LIMIT with
    the operator's chosen ``time_in_force``; a limit leg adds ``limit_price`` and
    a market leg omits it (the contract validator guarantees this pairing). S3
    always forwards ``extended_hours`` explicitly (never omitted) so the vendor
    default cannot silently diverge from the leg's declared session.
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
        "extended_hours": leg.extended_hours,
        "client_order_id": client_order_id,
    }
    if leg.limit_price is not None:
        # Alpaca expects the price as a string; only present for a limit order.
        body["limit_price"] = _decimal_string(leg.limit_price)
    return body


def _lenient_order_float(
    payload: Mapping[str, Any],
    field: str,
    unreadable: dict[str, str],
) -> float | None:
    """Read one order numeric; a value that will not parse reads absent (#2648).

    The failure is recorded in ``unreadable`` so the row maps degraded rather
    than refusing every other order in the answer (#2363); ``None`` is the
    absent form every consumer already understands.
    """
    try:
        return opt_float(payload.get(field))
    except (TypeError, ValueError) as exc:
        unreadable[field] = f"{type(exc).__name__}: {exc}"
        return None


def _lenient_order_rfc3339(
    payload: Mapping[str, Any],
    field: str,
    unreadable: dict[str, str],
) -> int | None:
    """Read one order timestamp; a string that will not parse reads absent (#2648)."""
    try:
        return opt_rfc3339_to_ms(payload.get(field))
    except (TypeError, ValueError) as exc:
        unreadable[field] = f"{type(exc).__name__}: {exc}"
        return None


def _lenient_order_bool(
    payload: Mapping[str, Any],
    field: str,
    unreadable: dict[str, str],
) -> bool | None:
    """Read one order flag; a non-boolean reads absent, never truthy (#2648, #2643)."""
    try:
        return opt_bool(payload.get(field), field=field)
    except TypeError as exc:
        unreadable[field] = f"{type(exc).__name__}: {exc}"
        return None


def from_alpaca_order(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerOrder:
    """Map a raw Alpaca order payload to a ``BrokerOrder`` with any fill event.

    Its text -- id, symbol, side, type, time in force and status -- reads
    ``""`` when missing (:func:`str_or_blank`), never refusing the answer.
    Alpaca omits a multi-leg parent's symbol and side and a leg's type, and
    one bad order must not refuse every other order: that answer holds the
    account stale with no reductions, the exit freeze #2363 exists to end.

    Its values -- quantities, prices, timestamps and ``extended_hours`` --
    read the same way (#2648): a value that cannot be parsed reads absent
    and marks the row **degraded**, its status blank, because the Clerk
    cannot state that order truthfully either. The Clerk contains a degraded
    or blank order on its own instead: a foreign order is recorded
    unfoldable (#2363/#2643), and an answer about this app's own order is
    withheld as a lost response, recoverable by ``client_order_id``. A
    boolean never becomes a quantity (#2606) and ``"false"`` never becomes
    ``True`` (#2643); a degraded row synthesizes no fill event. A payload
    that is not an object at all is a contract violation and still raises --
    there is no order there to contain.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"Alpaca order payload must be an object, got {type(payload).__name__}")
    unreadable: dict[str, str] = {}
    quantity = _lenient_order_float(payload, "qty", unreadable)
    filled_quantity = _lenient_order_float(payload, "filled_qty", unreadable) or 0.0
    limit_price = _lenient_order_float(payload, "limit_price", unreadable)
    stop_price = _lenient_order_float(payload, "stop_price", unreadable)
    filled_avg_price = _lenient_order_float(payload, "filled_avg_price", unreadable)
    extended_hours = _lenient_order_bool(payload, "extended_hours", unreadable) or False
    submitted_at_ms = _lenient_order_rfc3339(payload, "submitted_at", unreadable)
    created_at_ms = _lenient_order_rfc3339(payload, "created_at", unreadable)
    updated_at_ms = _lenient_order_rfc3339(payload, "updated_at", unreadable)
    filled_at_ms = _lenient_order_rfc3339(payload, "filled_at", unreadable)
    canceled_at_ms = _lenient_order_rfc3339(payload, "canceled_at", unreadable)
    expired_at_ms = _lenient_order_rfc3339(payload, "expired_at", unreadable)
    if unreadable:
        logger.warning(
            "An Alpaca order row carried values this app could not parse; the row is marked unreadable",
            extra={
                "action": "alpaca_order_row_unreadable",
                "order_id": str_or_blank(payload.get("id")),
                "fields": sorted(unreadable),
                "causes": unreadable,
            },
        )
    return BrokerOrder(
        broker=BROKER_ID,
        order_id=str_or_blank(payload.get("id")),
        client_order_id=opt_str(payload.get("client_order_id")),
        symbol=str_or_blank(payload.get("symbol")),
        asset_class=opt_str(payload.get("asset_class")),
        side=str_or_blank(payload.get("side")),
        order_type=str_or_blank(payload.get("order_type")) or str_or_blank(payload.get("type")),
        time_in_force=str_or_blank(payload.get("time_in_force")),
        quantity=quantity,
        filled_quantity=filled_quantity,
        limit_price=limit_price,
        stop_price=stop_price,
        extended_hours=extended_hours,
        filled_avg_price=filled_avg_price,
        # The one containment signal the Clerk already knows (#2643): a row
        # this app cannot state truthfully carries no lifecycle state.
        status="" if unreadable else str_or_blank(payload.get("status")),
        submitted_at_ms=submitted_at_ms,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        filled_at_ms=filled_at_ms,
        canceled_at_ms=canceled_at_ms,
        expired_at_ms=expired_at_ms,
        events=[] if unreadable else _order_events(payload),
        observed_at_ms=_observed(observed_at_ms),
        fill_latency_seconds=fill_latency_seconds(submitted_at_ms, filled_at_ms),
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

    Canonical captured frames provide ``timestamp_ms``. Raw Alpaca frames use
    RFC-3339 ``timestamp`` and are converted exactly once at this ingestion
    boundary. A fill/partial_fill also carries the embedded order's
    ``filled_at``; the frame event instant is authoritative. Fails fast (no
    timestamp) — a lifecycle event with no instant is corruption, not
    something to default to "now".
    """
    timestamp_ms = payload.get("timestamp_ms")
    if timestamp_ms is not None:
        if (
            isinstance(timestamp_ms, bool)
            or not isinstance(timestamp_ms, int)
            or timestamp_ms < 0
        ):
            raise ValueError("Alpaca trade_updates timestamp_ms must be a non-negative int64.")
        return timestamp_ms
    timestamp = payload.get("timestamp")
    if timestamp:
        return rfc3339_to_ms(str(timestamp))
    raise ValueError("Alpaca trade_updates event is missing its timestamp.")


def from_alpaca_trade_update(payload: Mapping[str, Any]) -> BrokerOrderEvent:
    """Map one Alpaca ``trade_updates`` ``data`` payload to a ``BrokerOrderEvent``.

    ``payload`` is the ``data`` object of a ``{"stream":"trade_updates","data":…}``
    frame: ``event`` (the lifecycle transition), canonical ``timestamp_ms``
    (or raw vendor ``timestamp`` at the ingestion edge), an embedded ``order``
    object, and — on a fill/partial_fill —
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
    occurred_at_ms = trade_update_occurred_at_ms(payload)
    execution_id = opt_str(payload.get("execution_id"))
    if execution_id is not None:
        execution_id = execution_id.strip() or None
    if event in {"fill", "partial_fill"} and execution_id is None:
        raise ValueError(f"Alpaca {event} trade_updates event is missing its execution_id.")
    return BrokerOrderEvent(
        event_type=event,
        occurred_at_ms=occurred_at_ms,
        price=opt_float(payload.get("price")),
        quantity=opt_float(payload.get("qty")),
        execution_id=(execution_id if event in {"fill", "partial_fill"} else None),
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
        activity_id=to_str(payload["id"], field="id"),
        native_order_id=opt_str(payload.get("order_id")),
        activity_type=to_str(payload["activity_type"], field="activity_type"),
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
    ``tradable`` and ``fractionable`` are required booleans: ``bool("false")``
    is true (#2643).
    """
    class_key = "class" if "class" in payload else "asset_class"
    return BrokerAsset(
        broker=BROKER_ID,
        asset_id=to_str(payload["id"], field="id"),
        symbol=to_str(payload["symbol"], field="symbol"),
        name=opt_str(payload.get("name")),
        asset_class=to_str(payload[class_key], field=class_key),
        exchange=opt_str(payload.get("exchange")),
        status=to_str(payload["status"], field="status"),
        tradable=to_bool(payload["tradable"], field="tradable"),
        fractionable=to_bool(payload["fractionable"], field="fractionable"),
        shortable=opt_bool(payload.get("shortable"), field="shortable"),
        marginable=opt_bool(payload.get("marginable"), field="marginable"),
    )


def from_alpaca_clock(
    payload: Mapping[str, Any],
    *,
    observed_at_ms: int | None = None,
) -> BrokerClockEvidence:
    """Map a raw Alpaca ``/v2/clock`` payload to ``BrokerClockEvidence``.

    This is only a market-wide live liveness input. The canonical calendar
    remains the scheduled-session authority, and an open clock cannot prove a
    particular symbol tradable or not halted; that needs symbol-status evidence.

    An open answer must name its close: liveness reads it as the instant the
    answer stops proving the market open (#2596), so one without it raises
    rather than proving the market open with no end. ``is_open`` must be a
    boolean: ``bool("false")`` would prove a closed market open (#2643).
    """
    is_open = to_bool(payload["is_open"], field="is_open")
    next_close_ms = opt_rfc3339_to_ms(payload.get("next_close"))
    if is_open and next_close_ms is None:
        raise ValueError("Alpaca clock reports the market open but names no next close.")
    return BrokerClockEvidence(
        broker=BROKER_ID,
        is_open=is_open,
        vendor_timestamp_ms=rfc3339_to_ms(str(payload["timestamp"])),
        next_open_ms=opt_rfc3339_to_ms(payload.get("next_open")),
        next_close_ms=next_close_ms,
        observed_at_ms=_observed(observed_at_ms),
    )
