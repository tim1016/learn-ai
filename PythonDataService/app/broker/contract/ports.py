"""Broker port protocols (Broker System v2, Layer 3).

Ports are the structural interface a vendor layer implements. Phase 1 defines
the read port; phase 2 adds ``BrokerTradePort`` (submit; cancel lands in S3);
``BrokerBarStreamPort`` (live bars) arrives in phase 3. All methods are
``async`` — vendor implementations wrap synchronous SDK calls in a threadpool so
the service layer stays non-blocking.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.broker.contract.capabilities import BrokerCapabilities
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
from app.broker.contract.option_events import BrokerOptionEventEvidence, BrokerOptionEventResume
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionOrder,
    OptionOrderRequest,
)
from app.options.contract import OptionContract


@runtime_checkable
class BrokerReadPort(Protocol):
    """Read-only broker surface: account, positions, orders, activities, assets, clock.

    Implementations must expose ``broker_id`` (the registry key and the
    ``{broker}`` path segment) and ``capabilities()``. Every data method may
    raise a ``BrokerError`` subclass; the router translates those to HTTP.
    """

    broker_id: str

    def capabilities(self) -> BrokerCapabilities: ...

    async def get_account(self) -> BrokerAccountSnapshot: ...

    async def list_positions(self) -> list[BrokerPosition]: ...

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
        symbols: tuple[str, ...] | None = None,
        until_ms: int | None = None,
    ) -> list[BrokerOrder]:
        """Return orders newest first by submitted timestamp.

        Consumers may retain the first rows as the most recent per-symbol
        activity, so every broker adapter must establish this ordering before
        applying its vendor-side limit.

        ``symbols`` narrows the answer to orders in those symbols, and
        ``until_ms`` to orders submitted before it. With ``after_ms`` they
        let a caller read one symbol's orders in a window a page at a time:
        the next page ends where the oldest order of the last one was
        submitted.
        """
        ...

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[BrokerActivity]: ...

    async def read_option_event_activities(
        self, *, after_ms: int, resume: BrokerOptionEventResume | None = None
    ) -> BrokerOptionEventEvidence:
        """The account's assignments, exercises and expiries, and the share trades they created (ADR 0076).

        These end an option position without an order, and a broker reports
        them as account activities with a date and no time. The read covers
        whole New York dates: every such activity dated on or after the date
        ``after_ms`` falls on, newest first, in bounded steps. It is complete
        only when the walk proved it reached that date; otherwise the
        evidence carries a ``resume``, and handing it back whole, with the
        same ``after_ms``, goes on as the same walk: the next page is held
        to the order of the pages already read.

        Each activity is read whole or listed as unreadable, never in part.
        No event in the window means nothing happened only when the
        evidence's ``read_whole`` is true: the walk is complete, no row is
        unreadable, and the broker reports every row as executed. A broker
        whose book can hold no option position answers with none, complete.
        """
        ...

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        """Return one broker asset, or ``None`` when the symbol is absent."""
        ...

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        """The broker's listing of one option contract, or ``None`` when it lists none.

        A read of the broker's own reference, never of market data. Whether a
        bot may trade what comes back is ``option_contract_refusal``'s to say.
        """
        ...

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        """The account's option buying power and approval levels, each absent when the broker gives none."""
        ...

    async def get_clock_evidence(self) -> BrokerClockEvidence: ...

    async def get_portfolio_history(
        self, history_range: PortfolioHistoryRange
    ) -> BrokerPortfolioHistory: ...


@runtime_checkable
class BrokerActivityEvidencePort(Protocol):
    """Optional read capability: the bounded, cross-date activity evidence walk.

    Adapters that implement it let the fee-evidence sync claim provable
    ``history_complete``; a plain ``BrokerReadPort`` without it falls back to
    the bounded ``list_activities`` read, which never claims completeness.
    ``page_token`` resumes a previous read's ``next_page_token`` so a busy
    account's history is walked in bounded steps instead of never.
    ``after_ms`` bounds the walk to one window: rows older than it are left
    out, and the read completes on a short page or on the dated page after
    the one that crossed the window's start -- the page that confirms nothing
    in-window lies further back. A walk that is out of order raises instead.
    ``activity_type`` walks one type of activity only, as ``list_activities``
    filters it.
    """

    async def read_activity_evidence(
        self, *, page_token: str | None = None, after_ms: int | None = None, activity_type: str | None = None,
    ) -> BrokerActivityEvidence: ...


@runtime_checkable
class BrokerTradePort(Protocol):
    """Write surface: submit an order (phase 2); cancel a working order (S3).

    ``submit`` takes a single, already-identity-minted leg plus the
    ``client_order_id`` the caller (the Clerk) minted — the port does **not**
    mint identity; that is the Clerk's job so the journal and the wire carry the
    same ``order_ref``. The vendor layer maps the accepted order to a
    ``BrokerOrder`` (the same contract type the read path returns) or raises a
    ``BrokerError`` subclass. A submit-side ``BrokerUnavailable`` is not a
    definitive failure: the Clerk resolves it through
    ``get_order_by_client_order_id`` before journaling a terminal outcome.

    ``cancel`` requests cancellation of a working order by its **broker-assigned
    order id** (the ``BrokerOrder.order_id`` — a UUID for Alpaca), not the
    ``client_order_id``. Alpaca's cancel endpoint returns no order body (HTTP
    204), so the port returns ``None`` on success; a non-cancelable order (HTTP
    422) raises a ``BrokerError`` subclass the Clerk journals as
    ``cancel_failed``. The port does not read the journal or resolve ownership —
    that is the Clerk's concern; the port only speaks to the vendor.

    ``get_order_by_client_order_id`` looks an order up by the
    ``client_order_id`` the Clerk minted (== ``order_ref``). It exists solely to
    **resolve an uncertain submit** (S5 crash safety): when a submit's HTTP
    outcome is unknown, the Clerk asks the vendor whether the order actually
    landed. It lives on the *trade* port — not the read port — because it is part
    of the write/resolution flow, and because keeping it here leaves the
    read-only :class:`BrokerReadPort` (and its consumers, e.g. the live-lifecycle
    stream) untouched. Returns the order when it exists, ``None`` when the vendor
    reports it definitively absent (HTTP 404), and raises a ``BrokerError``
    subclass on any other failure — a ``BrokerUnavailable`` from the lookup keeps
    the outcome uncertain, so the Clerk leaves the intent unresolved rather than
    fabricating a terminal state.

    ``get_order_by_broker_order_id`` looks an order up by its
    **broker-assigned** id, with the same ``None``-only-on-404 discipline. It
    follows a manual order the owner replaced on the broker's own website
    (#2656): the replacement is a new broker order that carries no client id
    of ours, so only its broker id can answer for it.

    ``submit_option_order`` sends one option order, of one leg or of two to
    four (ADR 0076), under the ``client_order_id`` the caller minted, and
    returns it as the vendor reports it. The discipline is ``submit``'s: the
    port mints no identity and sends the order once; a ``BrokerUnavailable``
    is an unknown outcome, resolved through
    ``get_option_order_by_client_order_id``, and any other ``BrokerError``
    means the order does not exist. An accepted order whose answer cannot be
    read, or names a different order, is unknown too, never refused. An
    order refused before anything was sent is ``BrokerRequestInvalid``. A
    broker that takes no such order raises ``BrokerOptionsNotSupported``.
    ``cancel`` cancels an option order by its parent's broker order id.

    The two option lookups answer with the parent order and, under a
    multi-leg parent, its legs: each with its own order id, filled quantity,
    average price and status. ``None`` is the vendor saying no such order
    exists. An answer that cannot be read whole, or that names a different
    order from the one asked about, raises rather than state an outcome.

    From the submit or a lookup, an answer that cannot be read whole raises
    ``BrokerOrderAnswerUnreadable``. It carries the order's broker order id
    when the answer plainly gave it, so the order can still be cancelled or
    read by that id.

    After any unknown submit, the port's own refusal of an accepted answer
    included, the lookup by client order id keeps the stock lookup's grace:
    "no such order" stays unknown until the vendor has had time to show the
    order. Only an answer taken as that order's ends the grace early; one
    the lookup refuses starts it again. A read by the broker order id that
    an unreadable answer carried keeps the same grace, counted from that
    answer. So does a read by any broker order id, after an answer to it
    that was not taken as that order's. Outside a grace, ``None`` from
    either lookup is the vendor saying no such order exists.
    """

    broker_id: str

    async def submit(
        self, leg: BrokerOrderLeg, *, client_order_id: str
    ) -> BrokerOrder: ...

    async def cancel(self, order_id: str) -> None: ...

    async def get_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOrder | None: ...

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None: ...

    async def submit_option_order(
        self, order: OptionOrderRequest, *, client_order_id: str
    ) -> BrokerOptionOrder: ...

    async def get_option_order_by_client_order_id(
        self, client_order_id: str
    ) -> BrokerOptionOrder | None: ...

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None: ...


@runtime_checkable
class AuthoritativeSubmissionEvidencePort(Protocol):
    """Optional capability for deterministic no-submit execution adapters.

    Real broker submit acknowledgements are not execution feeds; their fill
    accounting arrives through trade updates or bounded recovery.  A
    deterministic adapter that executes entirely inside ``submit`` may expose
    this capability so the returned aggregate order is folded as execution
    evidence before control returns to the strategy runtime.
    """

    @property
    def submission_response_is_authoritative_evidence(self) -> bool: ...
