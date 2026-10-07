"""Alpaca → contract adapter (Broker System v2, Layer 1 seam).

The adapter is the **single ingestion boundary**: it consumes Alpaca's raw JSON
mappings (from the ``raw_data=True`` client) and produces broker-contract
models. Every vendor→contract conversion happens here, exactly once:

- RFC-3339 timestamp strings → ``int64`` ms UTC (the one
  conversion boundary on ingestion).
- Decimal money/quantity strings → ``float`` (read-only display surface; the
  verbatim decimals remain in the capture journal). The option mappers are
  the exception: an option price stays an exact ``Decimal`` and a quantity a
  whole ``int``, because they are compared with money set aside in cents.

This module holds the shared helpers; each read-path slice adds its per-model
mapper (``from_alpaca_account``, ``from_alpaca_position``, …) built on them.
"""

from __future__ import annotations

import functools
import logging
import math
import re
from collections.abc import Callable, Mapping
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal
from uuid import UUID
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
    OrderSide,
)
from app.broker.contract.option_events import (
    BrokerOptionEventActivity,
    BrokerOptionShareTradeActivity,
    OptionActivityStatus,
)
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionDeliverable,
    BrokerOptionOrder,
    BrokerOptionOrderLeg,
    MultiLegOptionOrderRequest,
    OptionOrderRequest,
    OptionPositionIntent,
    SingleOptionOrderRequest,
)
from app.options.contract import OptionContract
from app.options.event_allocation import OptionEventKind
from app.options.exact import require_everyday
from app.options.expiry import option_expiry_anchor_ms
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
    would otherwise become $1 or $0 of broker evidence. A JSON integer too
    large for a float is a ``ValueError`` like any other unreadable numeric,
    never a raw ``OverflowError`` no caller is written to catch (#2627, #2648).
    """
    if isinstance(value, bool):
        raise TypeError("Expected an Alpaca numeric, not a boolean")
    try:
        return float(value)
    except OverflowError as exc:
        raise ValueError("Alpaca numeric is too large to read as a number") from exc


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
    a calendar day when rendered in ``date-et`` mode (ADR 0022 (a), (e)).
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
        maintenance_margin=opt_float(payload.get("maintenance_margin")),
        initial_margin=opt_float(payload.get("initial_margin")),
        sma=opt_float(payload.get("sma")),
        last_equity=opt_float(payload.get("last_equity")),
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


def _finite_float(value: Any) -> float | None:
    """Parse an optional order numeric that must be a finite number when present (#2648)."""
    number = opt_float(value)
    if number is not None and not math.isfinite(number):
        raise ValueError(f"Alpaca numeric must be finite, got {number!r}")
    return number


def _order_quantity(value: Any) -> float | None:
    """Parse an optional share count: finite and non-negative when present (#2648).

    Prices keep their sign -- a multi-leg order's net price may be negative --
    so only quantities are held to it.
    """
    quantity = _finite_float(value)
    if quantity is not None and quantity < 0:
        raise ValueError(f"Alpaca quantity must be non-negative, got {quantity!r}")
    return quantity


def parse_order_link_id(value: Any) -> str | None:
    """Parse a replacement-link order id (``replaced_by``/``replaces``, #2656).

    ``None`` or blank text is no link. Anything else must be an Alpaca order
    id -- a UUID string -- because the broker-id read that follows a link
    (``GET /v2/orders/{order_id}``) refuses any other spelling before the
    request leaves the process, on every pass. A non-string is refused
    (``TypeError``), never ``str()``-coerced; a string that is not a UUID is
    refused (``ValueError``).
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise TypeError(f"Alpaca order link id must be a string, got {type(value).__name__}")
    text = value.strip()
    UUID(text)
    return text


def _read_or_absent[T](
    payload: Mapping[str, Any],
    unreadable: dict[str, str],
    field: str,
    parse: Callable[[Any], T | None],
) -> T | None:
    """Read one order value, or ``None`` when it will not parse (#2648).

    ``parse`` raises ``TypeError``/``ValueError`` on a value it cannot read;
    the failure is recorded in ``unreadable`` under the broker's field name,
    so the row maps degraded rather than refusing every other order in the
    answer (#2363). ``None`` is the absent form every consumer already
    understands. A parser that must refuse a non-string (a link id, say)
    raises instead of ``str()``-coercing it.
    """
    try:
        return parse(payload.get(field))
    except (TypeError, ValueError) as exc:
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

    Its values -- quantities, prices, timestamps, ``extended_hours`` and
    the replacement links ``replaced_by``/``replaces`` -- read the same way
    (#2648): a value that cannot be parsed (a boolean or non-numeric count,
    a non-finite or out-of-range number, a negative quantity, an unparseable
    time, a non-boolean flag, a link id that is not a UUID string) reads
    absent and is named in ``unreadable_fields``, so an unreadable link is
    never followed (#2656). The broker's own status is kept, so the
    order's lifecycle stays visible and a change in it is still activity.
    The Clerk contains such a row, or one missing its text, on its own: a
    foreign order is recorded unfoldable (#2363/#2643), and an answer about
    this app's own order is withheld as a lost response, recoverable by
    ``client_order_id``. A boolean never becomes a quantity (#2606) and
    ``"false"`` never becomes ``True`` (#2643); a row with an unreadable
    value synthesizes no fill event. A payload that is not an object at all
    is a contract violation and still raises -- there is no order there to
    contain.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"Alpaca order payload must be an object, got {type(payload).__name__}")
    unreadable: dict[str, str] = {}
    read = functools.partial(_read_or_absent, payload, unreadable)
    quantity = read("qty", _order_quantity)
    filled_quantity = read("filled_qty", _order_quantity) or 0.0
    limit_price = read("limit_price", _finite_float)
    stop_price = read("stop_price", _finite_float)
    filled_avg_price = read("filled_avg_price", _finite_float)
    extended_hours = read("extended_hours", functools.partial(opt_bool, field="extended_hours")) or False
    submitted_at_ms = read("submitted_at", opt_rfc3339_to_ms)
    created_at_ms = read("created_at", opt_rfc3339_to_ms)
    updated_at_ms = read("updated_at", opt_rfc3339_to_ms)
    filled_at_ms = read("filled_at", opt_rfc3339_to_ms)
    canceled_at_ms = read("canceled_at", opt_rfc3339_to_ms)
    expired_at_ms = read("expired_at", opt_rfc3339_to_ms)
    replaced_by = read("replaced_by", parse_order_link_id)
    replaces = read("replaces", parse_order_link_id)
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
        status=str_or_blank(payload.get("status")),
        submitted_at_ms=submitted_at_ms,
        created_at_ms=created_at_ms,
        updated_at_ms=updated_at_ms,
        filled_at_ms=filled_at_ms,
        canceled_at_ms=canceled_at_ms,
        expired_at_ms=expired_at_ms,
        replaced_by=replaced_by,
        replaces=replaces,
        events=[] if unreadable else _order_events(payload),
        observed_at_ms=_observed(observed_at_ms),
        fill_latency_seconds=fill_latency_seconds(submitted_at_ms, filled_at_ms),
        unreadable_fields=tuple(sorted(unreadable)),
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


def execution_id_from_activity_id(activity_id: str) -> str:
    """Recover Alpaca's execution identity from its account-activity identity.

    Trade updates expose the bare execution UUID, while account activities may
    prefix that same UUID with the vendor timestamp and ``::``. The full
    activity ID remains opaque everywhere else; only the evidence bridges
    between the two compare the embedded execution identity.
    """
    timestamp_prefix, separator, embedded_execution_id = activity_id.rpartition("::")
    if separator and timestamp_prefix.isdigit() and embedded_execution_id:
        return embedded_execution_id
    return activity_id


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


# ── Options (ADR 0076) ──────────────────────────────────────────────────────
#
# Every field name and value below is Alpaca's own, from its Trading API
# documents as read on 2026-10-06:
#   orders      https://docs.alpaca.markets/reference/postorder
#   multi-leg   https://docs.alpaca.markets/docs/options-level-3-trading
#   options     https://docs.alpaca.markets/docs/options-trading
#   contracts   https://docs.alpaca.markets/reference/get-option-contract-symbol_or_id
#   account     https://docs.alpaca.markets/reference/getaccount-1

_ALPACA_MULTI_LEG_ORDER_CLASS = "mleg"
# The reference names a plain order's class ``simple`` and allows it blank.
_ALPACA_SINGLE_ORDER_CLASSES = frozenset({"simple", ""})
# An option order is good for the day (#2913); Alpaca also takes ``gtc``.
_ALPACA_OPTION_TIME_IN_FORCE = "day"
# The order reference's limit on ``client_order_id``: "<= 128 characters".
_ALPACA_CLIENT_ORDER_ID_MAX_LENGTH = 128

_ALPACA_POSITION_INTENT: dict[tuple[OrderSide, OptionPositionIntent], str] = {
    (OrderSide.BUY, OptionPositionIntent.OPEN): "buy_to_open",
    (OrderSide.BUY, OptionPositionIntent.CLOSE): "buy_to_close",
    (OrderSide.SELL, OptionPositionIntent.OPEN): "sell_to_open",
    (OrderSide.SELL, OptionPositionIntent.CLOSE): "sell_to_close",
}
_SIDE_AND_INTENT_OF_ALPACA: dict[str, tuple[OrderSide, OptionPositionIntent]] = {
    word: pair for pair, word in _ALPACA_POSITION_INTENT.items()
}


def to_alpaca_option_order_request(order: OptionOrderRequest, *, client_order_id: str) -> dict[str, Any]:
    """Build the Alpaca ``POST /v2/orders`` JSON body for one option order.

    A single-leg order is a plain order on the contract's symbol with a
    ``position_intent``. A multi-leg order has ``order_class`` ``mleg``, no
    symbol or side of its own, the number of structures as ``qty``, and each
    leg's symbol, ``ratio_qty``, side and ``position_intent`` under ``legs``.
    Its ``limit_price`` keeps the order's sign: Alpaca reads a positive value
    as a debit and a negative one as a credit.

    ``time_in_force`` is always ``day`` and ``extended_hours`` is never
    written: Alpaca wants it false or absent on an option order.

    ``client_order_id`` must be the caller's own. Alpaca makes one up for an
    order sent without, and no lookup of ours could then find that order.
    One with a space before or after it, or longer than the 128 characters
    Alpaca's reference allows, is refused too: whether Alpaca would trim it,
    cut it or refuse it, the id it kept might not be the id asked for.

    Anything refused here raises ``ValueError`` or ``TypeError`` before a
    body exists; the broker port turns that into ``BrokerRequestInvalid``.
    """
    if not isinstance(client_order_id, str) or not client_order_id.strip():
        raise ValueError("an option order is sent under the client order id its caller minted; none was given")
    if client_order_id != client_order_id.strip():
        raise ValueError(f"a client order id has no space before or after it; got {client_order_id!r}")
    if len(client_order_id) > _ALPACA_CLIENT_ORDER_ID_MAX_LENGTH:
        raise ValueError(
            f"Alpaca takes a client order id of at most {_ALPACA_CLIENT_ORDER_ID_MAX_LENGTH} characters; "
            f"this one has {len(client_order_id)}"
        )
    if isinstance(order, SingleOptionOrderRequest):
        body: dict[str, Any] = {
            "symbol": order.contract.occ_symbol,
            "qty": str(order.quantity),
            "side": str(order.side),
            "position_intent": _ALPACA_POSITION_INTENT[order.side, order.intent],
            "type": str(order.order_type),
            "time_in_force": _ALPACA_OPTION_TIME_IN_FORCE,
            "client_order_id": client_order_id,
        }
        if order.limit_price is not None:
            body["limit_price"] = format(order.limit_price, "f")
        return body
    if not isinstance(order, MultiLegOptionOrderRequest):
        # A stock leg has a quantity and an order type too; it must never be written out as an option order.
        raise TypeError(f"an option order is a single or a multi-leg request, not {type(order).__name__}")
    body = {
        "order_class": _ALPACA_MULTI_LEG_ORDER_CLASS,
        "qty": str(order.quantity),
        "type": str(order.order_type),
        "time_in_force": _ALPACA_OPTION_TIME_IN_FORCE,
        "client_order_id": client_order_id,
        "legs": [
            {
                "symbol": leg.contract.occ_symbol,
                "ratio_qty": str(leg.ratio),
                "side": str(leg.side),
                "position_intent": _ALPACA_POSITION_INTENT[leg.side, leg.intent],
            }
            for leg in order.legs
        ],
    }
    if order.net_limit_price is not None:
        body["limit_price"] = format(order.net_limit_price, "f")
    return body


def _exact_decimal(value: Any, *, field: str) -> Decimal:
    """Parse a required Alpaca decimal (string or number) with no digit lost."""
    if isinstance(value, bool) or not isinstance(value, str | int | float | Decimal):
        raise TypeError(f"Alpaca field {field!r} must be a number, got {value!r}")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError(f"Alpaca field {field!r} is not a number: {value!r}") from exc
    return require_everyday(number, what=f"Alpaca field {field!r}")


def _opt_exact_decimal(value: Any, *, field: str) -> Decimal | None:
    """Parse an optional Alpaca decimal exactly; ``None``/empty → ``None``."""
    if value is None or value == "":
        return None
    return _exact_decimal(value, field=field)


def _whole_count(value: Any, *, field: str) -> int:
    """Parse a required count of contracts or structures: a whole number, zero or more."""
    number = _exact_decimal(value, field=field)
    if number < 0 or number != number.to_integral_value():
        raise ValueError(f"Alpaca field {field!r} must be a whole number, zero or more; got {value!r}")
    return int(number)


def _opt_whole_count(value: Any, *, field: str) -> int | None:
    if value is None or value == "":
        return None
    return _whole_count(value, field=field)


def _opt_text(value: Any, *, field: str) -> str | None:
    """Read optional vendor text: ``None`` or blank is absent, and anything but text is refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"Alpaca field {field!r} must be a string or null, got {value!r}")
    return value if value.strip() else None


def _option_order_intent(value: Any, side: OrderSide, *, field: str) -> OptionPositionIntent | None:
    """Read a ``position_intent``; one that names the other side contradicts the order and is refused."""
    text = _opt_text(value, field=field)
    if text is None:
        return None
    if text not in _SIDE_AND_INTENT_OF_ALPACA:
        raise ValueError(f"Alpaca field {field!r} is not a position intent: {text!r}")
    stated_side, intent = _SIDE_AND_INTENT_OF_ALPACA[text]
    if stated_side is not side:
        raise ValueError(f"Alpaca field {field!r} is {text!r} on an order whose side is {side.value!r}")
    return intent


def _option_order_times(payload: Mapping[str, Any]) -> dict[str, int | None]:
    return {
        "submitted_at_ms": opt_rfc3339_to_ms(payload.get("submitted_at")),
        "created_at_ms": opt_rfc3339_to_ms(payload.get("created_at")),
        "updated_at_ms": opt_rfc3339_to_ms(payload.get("updated_at")),
        "filled_at_ms": opt_rfc3339_to_ms(payload.get("filled_at")),
        "canceled_at_ms": opt_rfc3339_to_ms(payload.get("canceled_at")),
        "expired_at_ms": opt_rfc3339_to_ms(payload.get("expired_at")),
    }


def _from_alpaca_option_order_leg(payload: Any) -> BrokerOptionOrderLeg:
    if not isinstance(payload, Mapping):
        raise TypeError(f"Alpaca order leg must be an object, got {type(payload).__name__}")
    side = OrderSide(to_str(payload.get("side"), field="legs.side"))
    return BrokerOptionOrderLeg(
        order_id=to_str(payload.get("id"), field="legs.id"),
        client_order_id=_opt_text(payload.get("client_order_id"), field="legs.client_order_id"),
        contract=OptionContract.from_occ_symbol(to_str(payload.get("symbol"), field="legs.symbol")),
        side=side,
        intent=_option_order_intent(payload.get("position_intent"), side, field="legs.position_intent"),
        ratio=_opt_whole_count(payload.get("ratio_qty"), field="legs.ratio_qty"),
        quantity=_opt_whole_count(payload.get("qty"), field="legs.qty"),
        filled_quantity=_whole_count(payload.get("filled_qty"), field="legs.filled_qty"),
        filled_avg_price=_opt_exact_decimal(payload.get("filled_avg_price"), field="legs.filled_avg_price"),
        status=to_str(payload.get("status"), field="legs.status"),
        **_option_order_times(payload),
    )


def alpaca_multi_leg_order_lacks_legs(payload: Any) -> bool:
    """Whether an Alpaca answer is a multi-leg parent that came without its legs.

    Alpaca nests the legs when an order is read "in a nested style", and
    documents the parameter for that only on the read by broker order id.
    """
    return (
        isinstance(payload, Mapping)
        and payload.get("order_class") == _ALPACA_MULTI_LEG_ORDER_CLASS
        and not payload.get("legs")
    )


def from_alpaca_option_order(
    payload: Any,
    *,
    observed_at_ms: int | None = None,
) -> BrokerOptionOrder:
    """Map one raw Alpaca option order, with any nested legs, to a ``BrokerOptionOrder``.

    Read whole or not at all: a value that is missing where Alpaca's
    reference requires it, or will not parse, raises ``TypeError`` or
    ``ValueError``. ``from_alpaca_order`` reads a damaged row as far as it
    can because one row must not refuse a whole list. This is the answer
    about one order, and a quantity it could not read must not become zero
    contracts filled.

    A multi-leg parent has no symbol or side of its own and Alpaca writes
    both blank; its legs carry them. A leg's own ``type`` is blank too and is
    not read. A symbol that is not an option contract's is refused, so a
    stock order looked up by mistake is never read as an option order.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"Alpaca order payload must be an object, got {type(payload).__name__}")
    order_class = payload.get("order_class")
    if order_class == _ALPACA_MULTI_LEG_ORDER_CLASS:
        raw_legs = payload.get("legs")
        if not isinstance(raw_legs, list) or not raw_legs:
            raise ValueError("Alpaca's multi-leg order came without its legs")
        legs = tuple(_from_alpaca_option_order_leg(leg) for leg in raw_legs)
        contract = side = intent = None
    elif order_class is None or order_class in _ALPACA_SINGLE_ORDER_CLASSES:
        if payload.get("legs"):
            raise ValueError("Alpaca's single option order came with legs")
        legs = ()
        contract = OptionContract.from_occ_symbol(to_str(payload.get("symbol"), field="symbol"))
        side = OrderSide(to_str(payload.get("side"), field="side"))
        intent = _option_order_intent(payload.get("position_intent"), side, field="position_intent")
    else:
        raise ValueError(f"Alpaca order class {order_class!r} is not an option order's")
    return BrokerOptionOrder(
        broker=BROKER_ID,
        order_id=to_str(payload.get("id"), field="id"),
        client_order_id=_opt_text(payload.get("client_order_id"), field="client_order_id"),
        multi_leg=bool(legs),
        contract=contract,
        side=side,
        intent=intent,
        # ``order_type`` is the reference's deprecated name for ``type``.
        order_type=to_str(payload.get("type") or payload.get("order_type"), field="type"),
        time_in_force=to_str(payload.get("time_in_force"), field="time_in_force"),
        quantity=_whole_count(payload.get("qty"), field="qty"),
        filled_quantity=_whole_count(payload.get("filled_qty"), field="filled_qty"),
        limit_price=_opt_exact_decimal(payload.get("limit_price"), field="limit_price"),
        filled_avg_price=_opt_exact_decimal(payload.get("filled_avg_price"), field="filled_avg_price"),
        status=to_str(payload.get("status"), field="status"),
        **_option_order_times(payload),
        legs=legs,
        observed_at_ms=_observed(observed_at_ms),
    )


def _from_alpaca_option_deliverable(payload: Any) -> BrokerOptionDeliverable:
    if not isinstance(payload, Mapping):
        raise TypeError(f"Alpaca option deliverable must be an object, got {type(payload).__name__}")
    return BrokerOptionDeliverable(
        kind=to_str(payload.get("type"), field="deliverables.type"),
        symbol=to_str(payload.get("symbol"), field="deliverables.symbol"),
        amount=_opt_exact_decimal(payload.get("amount"), field="deliverables.amount"),
    )


def from_alpaca_option_contract(
    payload: Any,
    *,
    observed_at_ms: int | None = None,
) -> BrokerOptionContract:
    """Map a raw Alpaca option contract (``/v2/options/contracts/{symbol}``) to a ``BrokerOptionContract``.

    The fields that decide whether a bot may trade the contract are required,
    as the reference requires them: its status, ``tradable``, expiry date,
    call or put, ``style``, strike, ``multiplier`` and ``size`` (the shares
    it delivers). One that is missing raises, so a listing that cannot be
    shown to be standard is never read as one. The expiry date becomes its
    16:00 New York anchor here, once (ADR 0022 (i)).

    ``root_symbol`` is in every example Alpaca publishes but not among the
    fields its reference requires, so it may be absent. ``deliverables`` is
    present only when the answer carries the list.
    """
    if not isinstance(payload, Mapping):
        raise TypeError(f"Alpaca option contract payload must be an object, got {type(payload).__name__}")
    right = to_str(payload.get("type"), field="type")
    if right not in ("call", "put"):
        raise ValueError(f"Alpaca field 'type' must be 'call' or 'put', got {right!r}")
    raw_deliverables = payload.get("deliverables")
    if raw_deliverables is not None and not isinstance(raw_deliverables, list):
        raise TypeError(f"Alpaca field 'deliverables' must be a list or null, got {raw_deliverables!r}")
    expiry_date = date.fromisoformat(to_str(payload.get("expiration_date"), field="expiration_date"))
    return BrokerOptionContract(
        broker=BROKER_ID,
        contract_id=to_str(payload.get("id"), field="id"),
        symbol=to_str(payload.get("symbol"), field="symbol"),
        name=_opt_text(payload.get("name"), field="name"),
        status=to_str(payload.get("status"), field="status"),
        tradable=to_bool(payload.get("tradable"), field="tradable"),
        expiry_ms=option_expiry_anchor_ms(expiry_date),
        root_symbol=_opt_text(payload.get("root_symbol"), field="root_symbol"),
        underlying_symbol=to_str(payload.get("underlying_symbol"), field="underlying_symbol"),
        underlying_asset_id=_opt_text(payload.get("underlying_asset_id"), field="underlying_asset_id"),
        right=right,
        style=to_str(payload.get("style"), field="style"),
        strike_price=_exact_decimal(payload.get("strike_price"), field="strike_price"),
        multiplier=_exact_decimal(payload.get("multiplier"), field="multiplier"),
        shares_per_contract=_exact_decimal(payload.get("size"), field="size"),
        penny_program=opt_bool(payload.get("ppind"), field="ppind"),
        deliverables=(
            None
            if raw_deliverables is None
            else tuple(_from_alpaca_option_deliverable(item) for item in raw_deliverables)
        ),
        observed_at_ms=_observed(observed_at_ms),
    )


def _opt_option_level(value: Any, *, field: str) -> int | None:
    """Read an option approval level: a whole number, or ``None`` when Alpaca gives none."""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"Alpaca field {field!r} must be a whole number or null, got {value!r}")
    return value


def from_alpaca_option_account(
    payload: Mapping[str, Any],
    *,
    account_mode: Literal["paper", "live"],
    observed_at_ms: int | None = None,
) -> BrokerOptionAccountSnapshot:
    """Map the option fields of a raw Alpaca account payload to a ``BrokerOptionAccountSnapshot``.

    Whose account it is, and the refusal of a number shaped for the other
    mode, are ``from_alpaca_account``'s, read from the same payload. A field
    Alpaca leaves out stays ``None``: its own example answer carries no
    ``options_approved_level``. A level outside the 0 to 3 its reference
    lists is refused, not passed on as a level above three.
    """
    account = from_alpaca_account(payload, account_mode=account_mode, observed_at_ms=observed_at_ms)
    return BrokerOptionAccountSnapshot(
        broker=account.broker,
        account_id=account.account_id,
        account_mode=account.account_mode,
        options_buying_power=_opt_exact_decimal(payload.get("options_buying_power"), field="options_buying_power"),
        options_approved_level=_opt_option_level(
            payload.get("options_approved_level"), field="options_approved_level"
        ),
        options_trading_level=_opt_option_level(payload.get("options_trading_level"), field="options_trading_level"),
        observed_at_ms=account.observed_at_ms,
    )


# ── Option events: assignment, exercise and expiry (ADR 0076) ───────────────
#
# Every code, field and value below is Alpaca's own, from its documents as
# read on 2026-10-06:
#   the events  https://docs.alpaca.markets/docs/non-trade-activities-for-option-events
#   the codes   https://docs.alpaca.markets/reference/getaccountactivities-2
#               https://docs.alpaca.markets/docs/account-activities
#   an activity https://docs.alpaca.markets/reference/getaccountactivities-2 (``NonTradeActivities``)

# Alpaca's pages spell the exercise two ways. The reference's list of
# activity types and the page of examples write ``OPEXC``; the Account
# Activities page's table writes ``OPXRC``. Both are read until an account
# shows which one it sends (#2913).
_ALPACA_OPTION_EVENT_KIND: dict[str, OptionEventKind] = {
    "OPASN": OptionEventKind.ASSIGNED,
    "OPEXC": OptionEventKind.EXERCISED,
    "OPXRC": OptionEventKind.EXERCISED,
    "OPEXP": OptionEventKind.EXPIRED,
}
# The trade in the underlying's shares an exercise or an assignment creates. It is not the option's own fill.
_ALPACA_OPTION_SHARE_TRADE = "OPTRD"
# Every option activity code on Alpaca's pages begins so. That lets a code
# this does not read be named as unread, where it would otherwise pass unseen.
_ALPACA_OPTION_ACTIVITY_PREFIX = "OP"
_ALPACA_ACTIVITY_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
# Money here is dollars and cents. Alpaca does not offer options in another currency.
_ALPACA_OPTION_ACTIVITY_CURRENCY = "USD"


def _as_alpaca_writes_a_code(code: str) -> str:
    """A code without padding and in capitals, the way every one of Alpaca's pages writes it."""
    return code.strip().upper()


def alpaca_option_activity_type(payload: Any) -> str | None:
    """Alpaca's code for the row, as written, when it is an option activity, else ``None``.

    An option activity is any whose code begins ``OP``: the three events, the
    share trade, and the kinds this app does not read (``OPCA``, an option
    corporate action, and ``OPCSH``, a cash deliverable). The prefix is this
    repo's guard, not a statement of Alpaca's: a new code, or a third
    spelling of the exercise, is then refused by name and not skipped.

    The prefix is looked for without regard to padding or case, so a code
    that is not written exactly (``opexp``, ``" OPEXP"``) is still an option
    activity. It is returned as written and is read as nothing: a row that
    may be an expiry must be named, never passed over as some other kind.
    """
    if not isinstance(payload, Mapping):
        return None
    code = payload.get("activity_type")
    if not isinstance(code, str) or not _as_alpaca_writes_a_code(code).startswith(_ALPACA_OPTION_ACTIVITY_PREFIX):
        return None
    return code


def _signed_whole(value: Any, *, field: str) -> int:
    """Parse a required signed count of contracts or shares: a whole number, never zero."""
    number = _exact_decimal(value, field=field)
    if number == 0 or number != number.to_integral_value():
        raise ValueError(f"Alpaca field {field!r} must be a whole number other than zero; got {value!r}")
    return int(number)


def _option_activity_date_ms(value: Any) -> int:
    text = to_str(value, field="date")
    if _ALPACA_ACTIVITY_DATE.fullmatch(text) is None:
        raise ValueError(f"Alpaca field 'date' must be a date written YYYY-MM-DD, got {text!r}")
    return et_date_to_ms(text)


def _option_activity_status(value: Any) -> OptionActivityStatus:
    text = to_str(value, field="status")
    if text not in OptionActivityStatus:
        raise ValueError(f"Alpaca field 'status' is not a status its reference lists: {text!r}")
    return OptionActivityStatus(text)


def _option_activity_header(payload: Mapping[str, Any], code: str) -> dict[str, Any]:
    """What an event and its share trade both carry: who, when, and whether the broker stands by it."""
    # Every documented option activity carries a date and no time. One that
    # states an instant is dated in a way this does not read: the instant
    # could put it on another New York date than the one it carries.
    if payload.get("transaction_time") is not None:
        raise ValueError(
            "Alpaca field 'transaction_time' is on an option activity, which is read by its date alone; "
            f"got {payload['transaction_time']!r}"
        )
    currency = _opt_text(payload.get("currency"), field="currency")
    if currency not in (None, _ALPACA_OPTION_ACTIVITY_CURRENCY):
        raise ValueError(f"Alpaca field 'currency' is {currency!r}; an option activity is read in dollars only")
    return {
        "broker": BROKER_ID,
        "activity_id": to_str(payload.get("id"), field="id"),
        "activity_type": code,
        "status": _option_activity_status(payload.get("status")),
        "activity_date_ms": _option_activity_date_ms(payload.get("date")),
        "group_id": _opt_text(payload.get("group_id"), field="group_id"),
        "created_at_ms": opt_rfc3339_to_ms(payload.get("created_at")),
    }


def from_alpaca_option_activity(
    payload: Any,
    *,
    observed_at_ms: int | None = None,
) -> BrokerOptionEventActivity | BrokerOptionShareTradeActivity:
    """Map one raw Alpaca option activity to the event, or the share trade, it reports.

    Read whole or not at all, like ``from_alpaca_option_order``: a value that
    is missing or will not parse raises ``TypeError`` or ``ValueError``. So
    does a status outside the reference's three, a contract symbol that is
    not one, a quantity that is not whole, a quantity whose sign the kind
    cannot have, and an event that states cash of its own. A quantity that
    could not be read never becomes no contracts.

    An event (``OPASN``, ``OPEXC`` or ``OPXRC``, ``OPEXP``) names its
    contract by OCC symbol and carries the change to the account's position
    in contracts. Its share trade (``OPTRD``) names the underlying and carries
    signed shares, the price a share and the cash. The date becomes 00:00 New
    York time on it, here, once. A row that also states a
    ``transaction_time`` is refused: it is read by its date alone.

    An option activity of another code (``OPCA``, ``OPCSH``) is refused by
    name: this app reads no such kind. So is a code that is not written
    exactly as Alpaca's pages write it. A row that is not an option activity
    at all is the caller's mistake and raises too.
    """
    code = alpaca_option_activity_type(payload)
    if code is None:
        raise ValueError("Alpaca's row is not an option activity")
    if code != _as_alpaca_writes_a_code(code):
        raise ValueError(
            f"Alpaca activity type {code!r} is not written as Alpaca's pages write an option activity code "
            f"({_as_alpaca_writes_a_code(code)!r}); a code is read exactly or not at all"
        )
    if code in _ALPACA_OPTION_EVENT_KIND:
        return BrokerOptionEventActivity(
            **_option_activity_header(payload, code),
            kind=_ALPACA_OPTION_EVENT_KIND[code],
            contract=OptionContract.from_occ_symbol(to_str(payload.get("symbol"), field="symbol")),
            position_change=_signed_whole(payload.get("qty"), field="qty"),
            net_amount=_opt_exact_decimal(payload.get("net_amount"), field="net_amount"),
            observed_at_ms=_observed(observed_at_ms),
        )
    if code == _ALPACA_OPTION_SHARE_TRADE:
        return BrokerOptionShareTradeActivity(
            **_option_activity_header(payload, code),
            underlying_symbol=to_str(payload.get("symbol"), field="symbol"),
            shares=_signed_whole(payload.get("qty"), field="qty"),
            price=_exact_decimal(payload.get("price"), field="price"),
            net_amount=_exact_decimal(payload.get("net_amount"), field="net_amount"),
            observed_at_ms=_observed(observed_at_ms),
        )
    raise ValueError(f"Alpaca activity type {code!r} is an option activity this app does not read")
