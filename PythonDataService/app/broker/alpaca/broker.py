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

from app.broker.alpaca import adapter
from app.broker.alpaca.active_binding import resolved_alpaca_settings
from app.broker.alpaca.client import AlpacaTradingClient
from app.broker.alpaca.config import BROKER_ID, AlpacaSettings
from app.broker.contract.capabilities import BrokerCapabilities, ExtendedHoursWindow
from app.broker.contract.errors import BrokerEvidenceUnavailable
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
from app.broker.contract.registry import BrokerRegistry, get_broker_registry
from app.utils.session_anchors import et_date_at_ms

logger = logging.getLogger(__name__)

_ACTIVITY_MAX_PAGES = 3
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


# Alpaca's documented extended session, 04:00–20:00 ET ("Orders at Alpaca" §
# Extended Hours Trading, verified 2026-09-08; docs/references/alpaca-extended-hours.md).
# The overnight session (20:00–04:00) is a separate venue and is not part of
# the decision clock in slice 3 (ADR 0059 D5.2; ruling R1 in the slice-3 plan).
ALPACA_EXTENDED_HOURS_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)

# Alpaca free / paper-account capabilities, verified 2026-07 (spec §3). Honest
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
    ) -> list[BrokerOrder]:
        payloads = await self._client.list_orders(
            status=status, limit=limit, after_ms=after_ms
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
        self, *, page_token: str | None = None, after_ms: int | None = None,
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
        """
        return await self._activity_evidence(page_size=100, page_token=page_token, after_ms=after_ms)

    async def _activity_evidence(
        self, *, page_size: int, page_token: str | None, after_ms: int | None = None,
    ) -> BrokerActivityEvidence:
        activities: list[BrokerActivity] = []
        previous_page_oldest_ms: int | None = None
        boundary_crossed_on_previous_page = False
        for _ in range(_ACTIVITY_MAX_PAGES):
            payloads = await self._client.list_activities(limit=page_size, page_token=page_token)
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

    async def get_order(self, order_id: str) -> BrokerOrder | None:
        """Look one order up by its broker-assigned id (#2656).

        Follows a manual order's Alpaca replacement chain: the replacement
        Alpaca books carries no client order id of ours, so only its broker
        order id can answer for it. Same evidence discipline as the
        client-id lookup: ``None`` only on a definitive 404, transport
        failures stay ``BrokerUnavailable`` and propagate.
        """
        payload = await self._client.get_order(order_id)
        if payload is None:
            return None
        return adapter.from_alpaca_order(payload)


def register_default_brokers(registry: BrokerRegistry | None = None) -> BrokerRegistry:
    """Register the phase-1 brokers (Alpaca only) into the registry."""
    registry = registry or get_broker_registry()
    registry.register(AlpacaBroker())
    return registry
