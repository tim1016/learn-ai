"""AlpacaBroker — the read-port and trade-port implementation (Broker System v2).

Composes the SDK client, the adapter, and the capability descriptor into a
single object implementing both :class:`BrokerReadPort` and (from phase 2)
:class:`BrokerTradePort`. Each slice adds one method here; the router and Clerk
only ever see contract models.

The port is cheap to construct (the underlying client builds credentials and
network lazily), so it can be registered at startup without keys.

Settings are injectable. Pass a resolved ``AlpacaSettings`` — the one a profile
revision produced (``app/broker/alpaca/profile/``) — and this port and the
client it builds are bound to that revision's mode and credentials, so two
configurations cannot cross-contaminate. Omit it and the port reads the
process-wide settings lazily on first use, exactly as it always has.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable, Mapping
from itertools import pairwise
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.broker.alpaca import adapter
from app.broker.alpaca.active_binding import resolved_alpaca_settings
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import BROKER_ID, AlpacaSettings
from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow
from app.broker.contract.errors import (
    BrokerEvidenceUnavailable,
    BrokerOrderAnswerUnreadable,
    BrokerRequestInvalid,
)
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerActivityEvidence,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.broker.contract.option_events import (
    BrokerOptionActivityUnreadable,
    BrokerOptionEventActivity,
    BrokerOptionEventEvidence,
    BrokerOptionEventResume,
    BrokerOptionShareTradeActivity,
)
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionOrder,
    OptionOrderRequest,
)
from app.broker.contract.registry import BrokerRegistry, get_broker_registry
from app.options.contract import OptionContract
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

logger = logging.getLogger(__name__)

_ACTIVITY_MAX_PAGES = 3
# Alpaca's reference allows an activity page of 1 to 100 rows.
_ACTIVITY_PAGE_SIZE = 100
_CASH_TRANSFER_ACTIVITY_FILTER = "TRANS"


def _mapped_evidence[T](evidence: str, mapping: Callable[[], T]) -> T:
    """Run one adapter mapping; an answer it cannot map is unavailable evidence.

    The broker answered, but not with contract evidence: a missing field, a
    wrong type or an unparseable value. That is ``BrokerEvidenceUnavailable``
    -- a named 503 a caller can hold on -- never the adapter's raw error,
    which no broker-contract consumer is written to catch (#2627).
    ``message`` and ``detail`` stay owner copy, because a router returns both;
    the adapter's own error is chained and logged, with its traceback, for
    the operator: a real adapter bug must not read only as a bad answer.
    """
    try:
        return mapping()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "Alpaca evidence could not be mapped to the broker contract",
            extra={
                "action": "alpaca_evidence_malformed",
                "evidence": evidence,
                "cause": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        raise BrokerEvidenceUnavailable(
            f"Alpaca sent {evidence} data this app could not read.",
            broker=BROKER_ID,
            detail=f"Part of Alpaca's {evidence} data was missing or in a form this app does not recognize.",
        ) from exc


def _transfer_activity_may_overlap_window(
    activity: BrokerActivity,
    *,
    after_ms: int,
) -> bool:
    """Keep every transfer row that could fall after a prior-close boundary.

    Alpaca transfer rows can carry only a calendar ``date``. The adapter
    anchors those at ET midnight, so a row on the boundary date is ambiguous:
    it might have occurred after the close. Preserve it so the day-P&L layer
    can fail closed instead of silently treating it as outside the window.
    """
    if activity.occurred_at_ms is None:
        return True
    return activity.occurred_at_ms >= after_ms or (
        activity.category == "non_trade_activity"
        and et_date_at_ms(activity.occurred_at_ms) == et_date_at_ms(after_ms)
    )


def _activity_page_crossed_window(
    activities: list[BrokerActivity],
    *,
    after_ms: int,
) -> bool:
    """Whether a newest-first page's oldest row falls on an ET date before the window's.

    Dates, not instants: a date-only row is anchored at ET midnight and may
    have occurred any time that day. Crossing is necessary, not sufficient --
    completion also needs the next page to confirm the order held past it.
    """
    if not activities:
        return False
    oldest = activities[-1]
    return oldest.occurred_at_ms is not None and (
        et_date_at_ms(oldest.occurred_at_ms) < et_date_at_ms(after_ms)
    )


def _same_activity_evidence(left: BrokerActivity, right: BrokerActivity) -> bool:
    """Compare broker-authored content, excluding only local ingestion time."""
    return left.model_dump(exclude={"observed_at_ms"}) == right.model_dump(
        exclude={"observed_at_ms"}
    )


def _validate_transfer_payload(payload: object) -> None:
    """Require finality before a transfer can affect day P&L.

    Identity is the adapter's: ``from_alpaca_activity`` refuses a row without
    a non-blank id, as it does for every activity (#2643).
    """
    if not isinstance(payload, Mapping):
        raise TypeError("Alpaca transfer activity row must be an object")
    if payload.get("status") != "executed":
        raise BrokerEvidenceUnavailable(
            "An Alpaca transfer activity was not executed.",
            broker=BROKER_ID,
            detail=(
                "Only definitively executed transfers can adjust account day P&L; "
                "pending or statusless rows are unavailable evidence."
            ),
        )
    try:
        numeric_amount = adapter.to_float(payload.get("net_amount"))
    except (TypeError, ValueError):
        numeric_amount = math.nan
    if not math.isfinite(numeric_amount):
        raise BrokerEvidenceUnavailable(
            "An Alpaca transfer activity had no valid net amount.",
            broker=BROKER_ID,
            detail=(
                "Transfer net_amount must be a finite non-boolean number before "
                "it can adjust account day P&L."
            ),
        )


def _mapped_activities(payloads: list[Mapping[str, Any]]) -> list[BrokerActivity]:
    """Map one page of activity rows; a row the adapter cannot map is unavailable evidence."""
    return _mapped_evidence(
        "activity", lambda: [adapter.from_alpaca_activity(payload) for payload in payloads]
    )


def _mapped_transfer_activities(payloads: list[Mapping[str, Any]]) -> list[BrokerActivity]:
    """Map one transfer page once every row on it proves identity and finality."""

    def validated_page() -> list[BrokerActivity]:
        for payload in payloads:
            _validate_transfer_payload(payload)
        return [adapter.from_alpaca_activity(payload) for payload in payloads]

    return _mapped_evidence("transfer activity", validated_page)


def _activity_page_oldest_ms(
    activities: list[BrokerActivity],
    *,
    previous_page_oldest_ms: int | None,
) -> int | None:
    """Validate newest-first ordering and return the page's oldest instant.

    Raises when a page, or its first row against the previous page's oldest,
    is out of order: the window boundary cannot be proven from such a walk.
    An undated row cannot be placed at all, so its page returns ``None`` and
    proves nothing; callers continue until a short page (or their page bound)
    instead of trusting an order that cannot be checked.
    """
    occurred_at_ms = [activity.occurred_at_ms for activity in activities]
    if not occurred_at_ms or any(value is None for value in occurred_at_ms):
        return None
    dated = [value for value in occurred_at_ms if value is not None]
    if any(newer < older for newer, older in pairwise(dated)) or (
        previous_page_oldest_ms is not None
        and dated[0] > previous_page_oldest_ms
    ):
        raise BrokerEvidenceUnavailable(
            "Alpaca activity history was not newest-first.",
            broker=BROKER_ID,
            detail="Activity pagination cannot prove the window's start boundary.",
        )
    return dated[-1]


# What places a row in a walk: its id, its code, its symbol and its date. None is a figure.
_ACTIVITY_PLACEMENT_FIELDS = ("id", "activity_type", "symbol", "date", "transaction_time")
# An option activity is placed by its date alone. An instant on one makes it
# unreadable, and must not place it before a window its date is in.
_OPTION_ACTIVITY_PLACEMENT_FIELDS = ("id", "activity_type", "symbol", "date")


def _placement(payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = (
        _ACTIVITY_PLACEMENT_FIELDS
        if adapter.alpaca_option_activity_type(payload) is None
        else _OPTION_ACTIVITY_PLACEMENT_FIELDS
    )
    return {field: payload[field] for field in fields if field in payload}


def _placed_activities(payloads: list[Mapping[str, Any]]) -> list[BrokerActivity]:
    """Map one page for a walk that reads the rows itself: each row's place, and none of its figures.

    The adapter maps only the fields that say which row it is and when. A
    quantity or an amount that will not parse is then the row's own fault,
    found when that row is read, and cannot refuse the page. A row with no
    id, no code or an unreadable date still does: it cannot be placed.
    """
    return _mapped_evidence(
        "activity", lambda: [adapter.from_alpaca_activity(_placement(payload)) for payload in payloads]
    )


def _why_unreadable(exc: Exception) -> str:
    """The adapter's refusal in plain words, without a validation library's framing."""
    if isinstance(exc, ValidationError):
        return "; ".join(str(error["msg"]).removeprefix("Value error, ") for error in exc.errors())
    return str(exc)


def _read_option_activity(
    payload: Mapping[str, Any], row: BrokerActivity
) -> BrokerOptionEventActivity | BrokerOptionShareTradeActivity | BrokerOptionActivityUnreadable:
    """Read one option activity whole, or say that it could not be read and why.

    ``row`` is the same payload placed for the walk, which has already given
    it an id and a place in time. An unreadable row keeps those and no
    figure: it is not a partial event.
    """
    try:
        return adapter.from_alpaca_option_activity(payload, observed_at_ms=row.observed_at_ms)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        logger.warning(
            "An Alpaca option activity could not be read whole",
            extra={
                "action": "alpaca_option_activity_unreadable",
                "activity_id": row.activity_id,
                "activity_type": row.activity_type,
                "cause": f"{type(exc).__name__}: {exc}",
            },
            exc_info=True,
        )
        return BrokerOptionActivityUnreadable(
            broker=BROKER_ID,
            activity_id=row.activity_id,
            activity_type=row.activity_type,
            activity_date_ms=(
                None if row.occurred_at_ms is None else et_midnight_ms(et_date_at_ms(row.occurred_at_ms))
            ),
            symbol=row.symbol,
            reason=_why_unreadable(exc),
        )


def _keep_once[T: (BrokerOptionEventActivity, BrokerOptionShareTradeActivity, BrokerOptionActivityUnreadable)](
    kept: dict[str, T], activity: T
) -> None:
    """Keep an activity under its id; the same id carrying something else is evidence that cannot be used."""
    previous = kept.setdefault(activity.activity_id, activity)
    if previous.model_dump(exclude={"observed_at_ms"}) != activity.model_dump(exclude={"observed_at_ms"}):
        raise BrokerEvidenceUnavailable(
            "Alpaca option activity history contains a conflicting duplicate.",
            broker=BROKER_ID,
            detail="One activity id carried two different option activities.",
        )


def _option_order_body(order: OptionOrderRequest, *, client_order_id: str) -> dict[str, Any]:
    """The body to send for one option order, or a refusal of an order that cannot be written as one.

    Refused before anything is sent, so it is ``BrokerRequestInvalid`` and
    definitive: no order exists, and none is of unknown outcome.
    """
    try:
        return adapter.to_alpaca_option_order_request(order, client_order_id=client_order_id)
    except (TypeError, ValueError) as exc:
        raise BrokerRequestInvalid(
            "This option order was not sent to Alpaca.",
            broker=BROKER_ID,
            detail=f"Nothing was asked of Alpaca: {exc}.",
        ) from exc


def _order_uuid(order_id: object) -> UUID | None:
    """``order_id`` as the UUID an Alpaca order id is, or ``None`` when it is not one."""
    if not isinstance(order_id, str):
        return None
    try:
        return UUID(order_id)
    except ValueError:
        return None


def _same_order_id(left: object, right: object) -> bool:
    """Whether two broker order ids name one order.

    One UUID has several spellings (upper case, without hyphens, in braces,
    as a URN), and the SDK asks Alpaca for the same order under each. An id
    that is no UUID names no Alpaca order and equals nothing.
    """
    one, other = _order_uuid(left), _order_uuid(right)
    return one is not None and one == other


def _order_id_of_an_unread_answer(
    payload: Any, *, client_order_id: str | None, order_id: str | None
) -> str | None:
    """The broker order id an answer carries, when the answer is plainly about the order asked for.

    It is what a cancel or a read by id names. So it is given only when it
    is a UUID and every id the answer was asked by is the id it carries:
    another order's id is never handed on as this one's.
    """
    if not isinstance(payload, Mapping) or _order_uuid(payload.get("id")) is None:
        return None
    if client_order_id is not None and payload.get("client_order_id") != client_order_id:
        return None
    if order_id is not None and not _same_order_id(payload["id"], order_id):
        return None
    return payload["id"]


def _a_different_order(asked: str, got: str) -> BrokerEvidenceUnavailable:
    return BrokerEvidenceUnavailable(
        "Alpaca answered about a different option order from the one asked about.",
        broker=BROKER_ID,
        detail=f"Asked about {asked}; the answer names {got}.",
    )


def _option_order_asked_about(
    payload: Any, *, client_order_id: str | None = None, order_id: str | None = None
) -> BrokerOptionOrder:
    """Read Alpaca's answer about one option order, once it is the order that was asked about.

    The order is asked about by its client order id, its broker order id,
    or both, and the answer is held to each one given. An answer that
    cannot be read whole raises ``BrokerOrderAnswerUnreadable``, with the
    order's broker id when that much is plain. One that names another id
    is about some other order, whose fills must not be counted as this
    one's. Either way the outcome stays unknown.
    """
    if client_order_id is None and order_id is None:
        raise ValueError("an option order's answer is held to the id it was asked by, and none was given")
    try:
        answered = _mapped_evidence("option order", lambda: adapter.from_alpaca_option_order(payload))
    except BrokerEvidenceUnavailable as unreadable:
        raise BrokerOrderAnswerUnreadable(
            unreadable.message,
            broker=BROKER_ID,
            detail=unreadable.detail,
            broker_order_id=_order_id_of_an_unread_answer(
                payload, client_order_id=client_order_id, order_id=order_id
            ),
        ) from unreadable
    if client_order_id is not None and answered.client_order_id != client_order_id:
        raise _a_different_order(f"client order id {client_order_id!r}", f"{answered.client_order_id!r}")
    if order_id is not None and not _same_order_id(answered.order_id, order_id):
        raise _a_different_order(f"order id {order_id!r}", f"{answered.order_id!r}")
    return answered


# Alpaca's documented extended session, 04:00–20:00 ET ("Orders at Alpaca" §
# Extended Hours Trading, verified 2026-09-08).
# The overnight session (20:00–04:00) is a separate venue and is not part of
# the decision clock in slice 3 (ADR 0059 D5.2).
ALPACA_EXTENDED_HOURS_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)

# Alpaca free / paper-account capabilities, verified 2026-07. Honest
# differences declared as data so callers gate on capability, not identity:
# IEX gaps on illiquid symbols (bars_may_gap), 30-symbol / 1-connection stream
# cap, 200 REST calls/min. Upgrading to Algo Trader Plus flips data_feed to
# "sip" with no architecture change.
ALPACA_PAPER_CAPABILITIES = BrokerCapabilities(
    broker=BROKER_ID,
    paper_only=True,
    supports_fractional=True,
    supports_extended_hours=True,
    extended_hours_window=ALPACA_EXTENDED_HOURS_WINDOW,
    # Advertise only what BrokerOrderLeg can construct today. ``stop`` /
    # ``stop_limit`` / ``trailing_stop`` are unbuilt (OrderType has no member),
    # so advertising them was a false "yes" to a caller gating on capability.
    # Re-add each here in lockstep with its OrderType member + leg Literal.
    supported_order_types=("market", "limit"),
    # Alpaca takes an option order and a multi-leg one, on paper and live
    # ("Options Trading", "Options Level 3 Trading", read 2026-10-06). Whether
    # this account is approved for them is the account's fact, not the broker's.
    supports_option_orders=True,
    supports_multi_leg_option_orders=True,
    data_feed="iex",
    bars_may_gap=True,
    max_stream_symbols=30,
    max_concurrent_streams=1,
    rest_rate_limit_per_min=200,
)

# The real-money descriptor differs only in paper_only (ADR 0059 D9). Every
# other fact — IEX feed, stream caps, rate limit, buildable order types — is
# the same account tier; keeping one literal per mode makes the difference
# reviewable instead of a boolean flip buried in a constructor.
ALPACA_LIVE_CAPABILITIES = ALPACA_PAPER_CAPABILITIES.revised(paper_only=False)

_PORTFOLIO_HISTORY_QUERY: dict[PortfolioHistoryRange, tuple[str, str]] = {
    PortfolioHistoryRange.ONE_DAY: ("1D", "1Min"),
    PortfolioHistoryRange.THIRTY_DAYS: ("30D", "1D"),
    PortfolioHistoryRange.SIXTY_DAYS: ("60D", "1D"),
}


class AlpacaBroker:
    """Alpaca implementation of :class:`BrokerReadPort` and :class:`BrokerTradePort`."""

    broker_id = BROKER_ID

    def __init__(
        self,
        client: AlpacaTradingClient | None = None,
        *,
        settings: AlpacaSettings | None = None,
    ) -> None:
        self._client = client or AlpacaTradingClient(settings=settings)
        bound = getattr(self._client, "bound_settings", None)
        if settings is not None and isinstance(bound, AlpacaSettings) and bound is not settings:
            # One binding, described twice, disagreeing. The port would stamp
            # this mode on a snapshot the client fetched from the other mode's
            # endpoint — exactly the cross-contamination injected settings
            # exist to prevent. (A test double reports no settings and is
            # unaffected.)
            raise ValueError(
                "AlpacaBroker was given settings that disagree with its client's; "
                "a broker and its client are one binding."
            )
        self._settings = settings

    def _resolved_settings(self) -> AlpacaSettings:
        """The injected settings, else the binding this worker resolved.

        Deferred, not eager: the port is registered at startup before a binding
        exists, and reading settings eagerly would refuse to boot a
        credential-free service. An injected object is the whole binding — a
        broker built from one profile's context never consults another's.

        The uninjected fallback is now the *resolved binding* rather than the
        environment singleton (ADR 0060). That matters for the registry's
        broker, which is constructed settings-free at startup and serves every
        ``/api/brokers/alpaca/...`` read route: before this it answered from
        whatever the process environment said, which after a profile switch is
        a different configuration than the one the worker actually bound.
        ``resolved_alpaca_settings`` reads the environment only when nothing has
        attempted a binding at all, and raises rather than falling back when a
        binding was attempted and refused.
        """
        return self._settings or resolved_alpaca_settings()

    def capabilities(self) -> BrokerCapabilities:
        return (
            ALPACA_LIVE_CAPABILITIES
            if self._resolved_settings().is_live
            else ALPACA_PAPER_CAPABILITIES
        )

    async def get_account(self) -> BrokerAccountSnapshot:
        payload = await self._client.get_account()
        # The mode that selected the endpoint is the only source of the
        # account's mode (ADR 0059 D1); the adapter refuses a disagreeing shape.
        account_mode = self._resolved_settings().mode
        return _mapped_evidence(
            "account",
            lambda: adapter.from_alpaca_account(payload, account_mode=account_mode),
        )

    async def list_positions(self) -> list[BrokerPosition]:
        payloads = await self._client.list_positions()
        return _mapped_evidence(
            "position",
            lambda: [adapter.from_alpaca_position(payload) for payload in payloads],
        )

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
        symbols: tuple[str, ...] | None = None,
        until_ms: int | None = None,
    ) -> list[BrokerOrder]:
        narrowed = {
            **({} if symbols is None else {"symbols": symbols}),
            **({} if until_ms is None else {"until_ms": until_ms}),
        }
        payloads = await self._client.list_orders(
            status=status, limit=limit, after_ms=after_ms, **narrowed
        )
        return _mapped_evidence(
            "order",
            lambda: [adapter.from_alpaca_order(payload) for payload in payloads],
        )

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[BrokerActivity]:
        activity_filter = (
            {} if activity_type is None else {"activity_type": activity_type}
        )
        if after_ms is None:
            payloads = await self._client.list_activities(limit=limit, **activity_filter)
            return _mapped_activities(payloads)

        if activity_type == _CASH_TRANSFER_ACTIVITY_FILTER:
            # The loss gate may call this history complete only after reaching
            # the prior-close boundary. Unlike generic activity recovery, a
            # fixed page cap would silently turn omitted transfers into P&L.
            activities: list[BrokerActivity] = []
            seen_activities: dict[str, BrokerActivity] = {}
            issued_page_tokens: set[str | None] = set()
            previous_page_oldest_ms: int | None = None
            boundary_crossed_on_previous_page = False
            page_token: str | None = None
            while True:
                issued_page_tokens.add(page_token)
                payloads = await self._client.list_activities(
                    limit=limit,
                    page_token=page_token,
                    **activity_filter,
                )
                mapped = _mapped_transfer_activities(payloads)
                page_oldest_ms = _activity_page_oldest_ms(
                    mapped,
                    previous_page_oldest_ms=previous_page_oldest_ms,
                )
                for activity in mapped:
                    previous = seen_activities.get(activity.activity_id)
                    if previous is not None:
                        if not _same_activity_evidence(previous, activity):
                            raise BrokerEvidenceUnavailable(
                                "Alpaca transfer activity history contains a conflicting duplicate.",
                                broker=BROKER_ID,
                                detail=(
                                    "One activity id carried different economic evidence "
                                    "across pages."
                                ),
                            )
                        continue
                    seen_activities[activity.activity_id] = activity
                    if _transfer_activity_may_overlap_window(
                        activity,
                        after_ms=after_ms,
                    ):
                        activities.append(activity)
                if len(payloads) < limit:
                    break
                if boundary_crossed_on_previous_page and page_oldest_ms is not None:
                    break
                boundary_crossed_on_previous_page = (
                    page_oldest_ms is not None
                    and _activity_page_crossed_window(
                        mapped,
                        after_ms=after_ms,
                    )
                )
                previous_page_oldest_ms = page_oldest_ms
                next_page_token = mapped[-1].activity_id
                if next_page_token in issued_page_tokens:
                    raise BrokerEvidenceUnavailable(
                        "Alpaca transfer activity history was incomplete.",
                        broker=BROKER_ID,
                        detail="Pagination ended before the prior-close boundary.",
                    )
                page_token = next_page_token
            return activities

        # Recovery is explicitly bounded. Alpaca's page cursor is not the
        # canonical occurred-at cursor, so follow at most this small fixed
        # number of newest-first pages and filter mapped contract records here.
        activities: list[BrokerActivity] = []
        seen_activity_ids: set[str] = set()
        page_token: str | None = None
        for _ in range(_ACTIVITY_MAX_PAGES):
            payloads = await self._client.list_activities(
                limit=limit,
                page_token=page_token,
                **activity_filter,
            )
            page = _mapped_activities(payloads)
            for activity in page:
                if (
                    activity.activity_id not in seen_activity_ids
                    and activity.occurred_at_ms is not None
                    and activity.occurred_at_ms >= after_ms
                ):
                    seen_activity_ids.add(activity.activity_id)
                    activities.append(activity)
            if len(payloads) < limit:
                break
            next_page_token = page[-1].activity_id
            if next_page_token == page_token:
                break
            page_token = next_page_token
        return activities

    async def read_activity_evidence(
        self, *, page_token: str | None = None, after_ms: int | None = None, activity_type: str | None = None,
    ) -> BrokerActivityEvidence:
        """Read raw dated evidence with the provider's explicit page completion.

        Alpaca Trading API documents page_size 1..100. A bounded unfinished
        walk is useful evidence, but never proof that an empty young account
        or an old fee date has no further rows; its ``next_page_token``
        resumes the newest-first walk from exactly where it stopped.

        ``after_ms`` reads one window instead of the whole history: it keeps
        the rows at or after that instant (and any undated row, which cannot
        be placed outside it). It proves the window the way the transfer walk
        does: pages must be newest-first, within a page and across pages, or
        the read raises ``BrokerEvidenceUnavailable``; and it is complete on a
        short page, or on the first fully dated page after one whose oldest
        row falls on an ET date before the window's -- that extra page is what
        confirms no in-window row sits further back. An undated row on a page
        withholds the proof, so the walk goes on to a short page or its page
        bound, where ``next_page_token`` resumes it.

        ``activity_type`` reads that type's own endpoint
        (``/v2/account/activities/{activity_type}``), so every row of every
        page is one of it.
        """
        return await self._activity_evidence(
            page_size=100, page_token=page_token, after_ms=after_ms, activity_type=activity_type,
        )

    async def _activity_evidence(
        self, *, page_size: int, page_token: str | None, after_ms: int | None = None,
        activity_type: str | None = None,
    ) -> BrokerActivityEvidence:
        activity_filter = {} if activity_type is None else {"activity_type": activity_type}
        activities: list[BrokerActivity] = []
        previous_page_oldest_ms: int | None = None
        boundary_crossed_on_previous_page = False
        for _ in range(_ACTIVITY_MAX_PAGES):
            payloads = await self._client.list_activities(limit=page_size, page_token=page_token, **activity_filter)
            page = _mapped_activities(payloads)
            if after_ms is None:
                activities.extend(page)
                window_proven = False
            else:
                page_oldest_ms = _activity_page_oldest_ms(
                    page, previous_page_oldest_ms=previous_page_oldest_ms,
                )
                activities.extend(
                    activity for activity in page
                    if activity.occurred_at_ms is None or activity.occurred_at_ms >= after_ms
                )
                window_proven = boundary_crossed_on_previous_page and page_oldest_ms is not None
                boundary_crossed_on_previous_page = page_oldest_ms is not None and _activity_page_crossed_window(
                    page, after_ms=after_ms,
                )
                previous_page_oldest_ms = page_oldest_ms
            if window_proven or len(payloads) < page_size:
                return BrokerActivityEvidence(activities=activities, history_complete=True)
            next_token = page[-1].activity_id
            if next_token == page_token:
                return BrokerActivityEvidence(activities=activities, history_complete=False)
            page_token = next_token
        return BrokerActivityEvidence(activities=activities, history_complete=False, next_page_token=page_token)

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> BrokerOptionEventEvidence:
        """Read the account's assignments, exercises and expiries, with the share trades they created.

        Alpaca does not send these over the trade-updates stream ("Options
        assignments are not delivered through websocket events"), so they are
        polled from the account's non-trade activities. The walk asks for
        that whole category and keeps the option activities: it names no
        activity type, so the exercise is read under either code Alpaca's
        pages give it.

        An activity carries a date and no time, so the window is whole New
        York dates: everything dated on or after the date ``after_ms`` falls
        on. The walk is ``read_activity_evidence``'s: newest first or it
        raises ``BrokerEvidenceUnavailable``, a bounded number of pages a
        call, complete on a short page or on the dated page after the one
        that crossed the window's start, and otherwise resumed from the
        evidence's ``resume``.

        A resumed call goes on as the same walk. ``resume`` carries the
        cursor and the oldest time placed on the last page read, so the
        first page of this call is held to the page before it exactly as a
        later page of one call is: a row newer than that time refuses the
        read as not newest-first, and a page that follows one which crossed
        the window's start proves the window. It is for the same
        ``after_ms`` as the read it came from.

        Each option activity is read whole or listed under ``unreadable``
        with the reason, never read in part. A row of any kind whose id, code
        or date cannot be read refuses the whole read: it cannot be placed in
        the walk. No other row's figures are read at all.

        An option activity is placed in the window by its ``date`` alone. One
        that also states a ``transaction_time`` is unreadable, and that
        instant never puts it outside a window its date is in.
        """
        since = et_date_at_ms(after_ms)
        events: dict[str, BrokerOptionEventActivity] = {}
        share_trades: dict[str, BrokerOptionShareTradeActivity] = {}
        unreadable: dict[str, BrokerOptionActivityUnreadable] = {}

        def evidence(
            *, history_complete: bool, resume: BrokerOptionEventResume | None = None
        ) -> BrokerOptionEventEvidence:
            return BrokerOptionEventEvidence(
                events=tuple(events.values()),
                share_trades=tuple(share_trades.values()),
                unreadable=tuple(unreadable.values()),
                history_complete=history_complete,
                resume=resume,
            )

        # A resumed walk starts where the last call's loop stood, not from nothing.
        page_token = None if resume is None else resume.page_token
        previous_page_oldest_ms = None if resume is None else resume.last_page_oldest_ms
        # The page before crossed the window's start when its oldest row is dated before it.
        boundary_crossed_on_previous_page = (
            previous_page_oldest_ms is not None and et_date_at_ms(previous_page_oldest_ms) < since
        )
        for _ in range(_ACTIVITY_MAX_PAGES):
            payloads = await self._client.list_non_trade_activities(limit=_ACTIVITY_PAGE_SIZE, page_token=page_token)
            page = _placed_activities(payloads)
            page_oldest_ms = _activity_page_oldest_ms(page, previous_page_oldest_ms=previous_page_oldest_ms)
            for payload, row in zip(payloads, page, strict=True):
                if adapter.alpaca_option_activity_type(payload) is None:
                    continue
                if row.occurred_at_ms is not None and et_date_at_ms(row.occurred_at_ms) < since:
                    continue
                activity = _read_option_activity(payload, row)
                if isinstance(activity, BrokerOptionEventActivity):
                    _keep_once(events, activity)
                elif isinstance(activity, BrokerOptionShareTradeActivity):
                    _keep_once(share_trades, activity)
                else:
                    _keep_once(unreadable, activity)
            window_proven = boundary_crossed_on_previous_page and page_oldest_ms is not None
            boundary_crossed_on_previous_page = page_oldest_ms is not None and _activity_page_crossed_window(
                page, after_ms=after_ms,
            )
            previous_page_oldest_ms = page_oldest_ms
            if window_proven or len(payloads) < _ACTIVITY_PAGE_SIZE:
                return evidence(history_complete=True)
            next_token = page[-1].activity_id
            if next_token == page_token:
                return evidence(history_complete=False)
            page_token = next_token
        return evidence(
            history_complete=False,
            resume=BrokerOptionEventResume(page_token=page_token, last_page_oldest_ms=previous_page_oldest_ms),
        )

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        payloads = await self._client.list_assets(status=status, limit=limit)
        return _mapped_evidence(
            "asset", lambda: [adapter.from_alpaca_asset(payload) for payload in payloads]
        )

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        """Return one Alpaca asset, or ``None`` when the symbol is unlisted."""
        payload = await self._client.get_asset(symbol)
        if payload is None:
            return None
        return _mapped_evidence("asset", lambda: adapter.from_alpaca_asset(payload))

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        """Return Alpaca's listing of one option contract, or ``None`` when it lists none.

        Asked by the contract's own symbol, from the Trading API's contract
        reference; no market-data subscription is involved.
        """
        payload = await self._client.get_option_contract(contract.occ_symbol)
        if payload is None:
            return None
        return _mapped_evidence("option contract", lambda: adapter.from_alpaca_option_contract(payload))

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        """Return the account's option buying power and approval levels.

        Read from the same ``/v2/account`` answer as :meth:`get_account`, in
        a request of its own, into a model of its own: the account snapshot a
        route serves is unchanged.
        """
        payload = await self._client.get_account()
        account_mode = self._resolved_settings().mode
        return _mapped_evidence(
            "account",
            lambda: adapter.from_alpaca_option_account(payload, account_mode=account_mode),
        )

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        payload = await self._client.get_clock()
        return _mapped_evidence("market clock", lambda: adapter.from_alpaca_clock(payload))

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory:
        """Return the custodian's portfolio curve at the range's safe resolution."""
        period, timeframe = _PORTFOLIO_HISTORY_QUERY[history_range]
        payload = await self._client.get_portfolio_history(
            period=period,
            timeframe=timeframe,
        )
        return _mapped_evidence(
            "portfolio history", lambda: adapter.from_alpaca_portfolio_history(payload)
        )

    # ── Trade port (phase 2) ────────────────────────────────────────────────

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        """Submit one equity leg; map the accepted order to a ``BrokerOrder``.

        The vendor request body is built by the adapter (contract → vendor),
        POSTed over the capturing session, and the raw response mapped back
        (vendor → contract). A vendor rejection raises a ``BrokerError`` the
        Clerk journals as ``submit_failed``.
        """
        body = adapter.to_alpaca_order_request(leg, client_order_id=client_order_id)
        payload = await self._client.submit_order(body)
        return adapter.from_alpaca_order(payload)

    async def cancel(self, order_id: str) -> None:
        """Cancel one working order by its broker-assigned id (S3).

        Delegates to the SDK client's ``DELETE /v2/orders/{order_id}``. A
        non-cancelable order (already filled/canceled) surfaces as a vendor 422,
        which ``map_api_error`` translates to a ``BrokerError`` the Clerk
        journals as ``cancel_failed``. There is no order body to map back —
        Alpaca returns 204 — so this returns ``None``.
        """
        await self._client.cancel_order(order_id)

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None:
        """Look an order up by the ``client_order_id`` the Clerk minted (S5).

        Resolves an uncertain submit: the Clerk asks whether the order it may
        have sent actually landed. Delegates to the SDK client's
        ``GET /v2/orders:by_client_order_id`` and maps the raw payload to a
        ``BrokerOrder`` when found. Returns ``None`` when Alpaca reports the
        order definitively absent (HTTP 404 — it never landed); a
        ``BrokerUnavailable`` from the client (timeout / 5xx / network) keeps the
        outcome uncertain and propagates so the Clerk leaves the intent
        unresolved rather than fabricating a terminal state.
        """
        payload = await self._client.get_order_by_client_order_id(client_order_id)
        if payload is None:
            return None
        return adapter.from_alpaca_order(payload)

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        """Look one order up by its broker-assigned id (#2656).

        Follows a manual order's Alpaca replacement chain: the replacement
        Alpaca books carries no client order id of ours, so only its broker
        order id can answer for it. Same evidence discipline as the
        client-id lookup: ``None`` only on a definitive 404, transport
        failures stay ``BrokerUnavailable`` and propagate.
        """
        payload = await self._client.get_order_by_broker_order_id(order_id)
        if payload is None:
            return None
        return adapter.from_alpaca_order(payload)

    # ── Option orders (ADR 0076) ────────────────────────────────────────────

    async def submit_option_order(
        self, order: OptionOrderRequest, *, client_order_id: str
    ) -> BrokerOptionOrder:
        """Submit one option order, of one leg or several, and map Alpaca's answer.

        The body goes out through the same ``submit_order`` a stock leg
        uses: one ``POST /v2/orders``, retried only on a throttle, with an
        unknown outcome marked for the lookup's visibility grace.

        Once Alpaca has accepted the order, nothing here may say it was
        refused. An answer that cannot be read, a multi-leg one without its
        legs included, or one naming another order raises
        ``BrokerEvidenceUnavailable``: an unknown outcome, which the caller
        settles through :meth:`get_option_order_by_client_order_id`. An
        unreadable one raises it as ``BrokerOrderAnswerUnreadable``, with
        the order's broker id when the answer plainly gave it. No second
        request is made here, because its failure could read as the order's.

        That unknown outcome gets the lookup's visibility grace too, started
        here: Alpaca's 200 ended the one the client keeps, and an order it
        took a moment ago can still answer 404 to a lookup. The same holds
        for a read by the broker order id an unreadable answer handed on.

        An order that cannot be written as one, or a client order id Alpaca
        could not keep as given, is ``BrokerRequestInvalid``: nothing was
        sent.
        """
        body = _option_order_body(order, client_order_id=client_order_id)
        payload = await self._client.submit_order(body)
        try:
            return _option_order_asked_about(payload, client_order_id=client_order_id)
        except Exception as not_taken:
            self._answered_and_not_taken(not_taken, client_order_id=client_order_id)
            raise

    def _answered_and_not_taken(
        self, not_taken: Exception, *, client_order_id: str | None = None, order_id: str | None = None
    ) -> None:
        """Alpaca answered for an order, and the answer was not taken as that order's.

        ``client_order_id`` and ``order_id`` are the ids Alpaca was asked by.
        Whatever stopped the answer, a 404 in the next seconds is not
        "absent": not to the lookup by the client order id, not to a read
        by the broker order id that was asked for, and not to a read by the
        one an unreadable answer handed on. Each gets the visibility grace,
        counted from this answer.

        Holding a grace under the id asked by hands nothing on: the only
        id a caller is given is the one an unreadable answer can vouch for.
        """
        if client_order_id is not None:
            self._client.hold_submission_uncertain(client_order_id)
        asked_by = _order_uuid(order_id)
        if asked_by is not None:
            self._client.hold_submission_uncertain(str(asked_by))
        handed_on = _order_uuid(not_taken.broker_order_id) if isinstance(not_taken, BrokerOrderAnswerUnreadable) else None
        if handed_on is not None:
            self._client.hold_submission_uncertain(str(handed_on))

    async def get_option_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOptionOrder | None:
        """Look an option order up by the ``client_order_id`` it was sent under.

        The parent is read as a stock order is, with the same 404
        discipline: ``None`` is Alpaca saying no such order exists, except
        inside the visibility grace after an unknown submit, where a 404
        stays unknown. Unlike the stock read, a found order does not end
        that grace by being found. It ends once the answer has been read
        whole and is about the order asked for; an answer refused here
        starts it again, so the 404 that follows is still not "absent".

        Alpaca documents no way to ask this read for nested legs, so a
        multi-leg parent that comes back without them is read again by its
        broker order id, which has one. When that second read is answered
        and its answer is not taken, the grace is held under the parent's
        id too, as it is for any read by broker order id.
        """
        payload = await self._client.get_order_by_client_order_id(client_order_id, found_settles_submission=False)
        if payload is None:
            return None
        order_id = None
        try:
            if adapter.alpaca_multi_leg_order_lacks_legs(payload):
                # The second answer is held to both ids: the one first asked by, and the one it was read again by.
                order_id, payload = await self._nested_order(payload, client_order_id=client_order_id)
            accepted = _option_order_asked_about(payload, client_order_id=client_order_id, order_id=order_id)
        except Exception as not_taken:
            self._answered_and_not_taken(not_taken, client_order_id=client_order_id, order_id=order_id)
            raise
        self._client.settle_submission(client_order_id)
        return accepted

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        """Look an option order up by its broker-assigned id, legs nested under a multi-leg parent.

        ``order_id`` is the parent's. ``None`` only on a definitive 404:
        for the visibility grace after an answer about this order that could
        not be read, which is where a caller gets the id from, a 404 stays
        unknown. So it does after any answer to this read that is not taken
        as the order's, an answer naming another order included: Alpaca
        answered for this id, so its not-found a moment later proves
        nothing. The grace ends once an answer is read whole here.
        """
        payload = await self._client.get_nested_order_by_broker_order_id(order_id)
        if payload is None:
            return None
        try:
            accepted = _option_order_asked_about(payload, order_id=order_id)
        except Exception as not_taken:
            self._answered_and_not_taken(not_taken, order_id=order_id)
            raise
        self._client.settle_submission(str(UUID(order_id)))
        return accepted

    async def _nested_order(self, parent: Mapping[str, Any], *, client_order_id: str) -> tuple[str, Any]:
        """Read a multi-leg parent again, by its own id, with its legs nested: that id, and the answer."""
        order_id = _mapped_evidence("option order", lambda: adapter.parse_order_link_id(parent.get("id")))
        nested = None if order_id is None else await self._client.get_nested_order_by_broker_order_id(order_id)
        if order_id is None or nested is None:
            # Alpaca just answered for this order; that it now has no id, or none Alpaca knows, proves nothing.
            raise BrokerOrderAnswerUnreadable(
                "Alpaca answered for a multi-leg order but would not show its legs.",
                broker=BROKER_ID,
                detail="The order was found by its client order id and not by its own broker order id.",
                broker_order_id=_order_id_of_an_unread_answer(parent, client_order_id=client_order_id, order_id=None),
            )
        return order_id, nested


def register_default_brokers(registry: BrokerRegistry | None = None) -> BrokerRegistry:
    """Register the phase-1 brokers (Alpaca only) into the registry."""
    registry = registry or get_broker_registry()
    registry.register(AlpacaBroker())
    return registry
