"""Typed broker-port guards for the SQLite Clerk intake boundary.

The intake fence is a dynamic scope, not a global broker mutex.  These
wrappers therefore reject only calls inherited from a fenced scope; another
task may continue a broker conversation while a short repository fold owns
the intake fence.
"""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.errors import BrokerEvidenceUnavailable, BrokerOptionsNotSupported
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
from app.broker.contract.options import (
    BrokerOptionAccountSnapshot,
    BrokerOptionContract,
    BrokerOptionOrder,
    OptionOrderRequest,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort
from app.options.contract import OptionContract

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from typing import Literal

    from app.broker.alpaca.clerk.synthesized_orders import SynthesizedAnchor, SynthesizedOptionFill
    from app.options.quotes import HeldQuote
    from app.services.source_bar_ledger import RetainedSourceBar


class BrokerCallUnderIntakeError(RuntimeError):
    """A SQLite Clerk broker call inherited a fenced intake scope."""

    def __init__(self, method: str) -> None:
        super().__init__(f"SQLite Clerk broker call {method} is forbidden while intake is fenced")


class GuardedBrokerReadPort:
    """Explicit, protocol-complete intake guard for :class:`BrokerReadPort`."""

    def __init__(self, inner: BrokerReadPort, *, intake: ReentrantAsyncLock) -> None:
        self._inner = inner
        self._intake = intake

    @property
    def broker_id(self) -> str:
        return self._inner.broker_id

    @property
    def intake(self) -> ReentrantAsyncLock:
        """Expose the bound marker for composition assertions."""
        return self._intake

    def capabilities(self) -> BrokerCapabilities:
        return self._inner.capabilities()

    async def get_account(self) -> BrokerAccountSnapshot:
        self._assert_unfenced("get_account")
        return await self._inner.get_account()

    async def list_positions(self) -> list[BrokerPosition]:
        self._assert_unfenced("list_positions")
        return await self._inner.list_positions()

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
        symbols: tuple[str, ...] | None = None,
        until_ms: int | None = None,
    ) -> list[BrokerOrder]:
        self._assert_unfenced("list_orders")
        narrowed = {
            **({} if symbols is None else {"symbols": symbols}),
            **({} if until_ms is None else {"until_ms": until_ms}),
        }
        return await self._inner.list_orders(
            status=status, limit=limit, after_ms=after_ms, **narrowed
        )

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
        activity_type: str | None = None,
    ) -> list[BrokerActivity]:
        self._assert_unfenced("list_activities")
        activity_filter = (
            {} if activity_type is None else {"activity_type": activity_type}
        )
        return await self._inner.list_activities(
            after_ms=after_ms,
            limit=limit,
            **activity_filter,
        )

    async def read_activity_evidence(
        self, *, page_token: str | None = None, after_ms: int | None = None, activity_type: str | None = None,
    ) -> BrokerActivityEvidence:
        self._assert_unfenced("read_activity_evidence")
        activity_filter = {} if activity_type is None else {"activity_type": activity_type}
        reader = getattr(self._inner, "read_activity_evidence", None)
        if callable(reader):
            return await reader(page_token=page_token, after_ms=after_ms, **activity_filter)
        if page_token is not None:
            # The fallback can only read the newest rows; answering a
            # continuation with them would fake a contiguous history walk.
            raise BrokerEvidenceUnavailable(
                "The broker cannot continue an activity history walk.",
                detail="Its read port has no paged activity evidence.",
            )
        return BrokerActivityEvidence(
            activities=await self._inner.list_activities(
                after_ms=0 if after_ms is None else after_ms, limit=100, **activity_filter
            ),
            history_complete=False,
        )

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        self._assert_unfenced("list_assets")
        return await self._inner.list_assets(status=status, limit=limit)

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        self._assert_unfenced("get_asset")
        return await self._inner.get_asset(symbol)

    async def get_option_contract(self, contract: OptionContract) -> BrokerOptionContract | None:
        self._assert_unfenced("get_option_contract")
        return await self._inner.get_option_contract(contract)

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        self._assert_unfenced("get_option_account")
        return await self._inner.get_option_account()

    async def get_clock_evidence(self) -> BrokerClockEvidence:
        self._assert_unfenced("get_clock_evidence")
        return await self._inner.get_clock_evidence()

    async def get_portfolio_history(
        self,
        history_range: PortfolioHistoryRange,
    ) -> BrokerPortfolioHistory:
        self._assert_unfenced("get_portfolio_history")
        return await self._inner.get_portfolio_history(history_range)

    def _assert_unfenced(self, method: str) -> None:
        if self._intake.current_scope_depth() != 0:
            raise BrokerCallUnderIntakeError(method)


class GuardedBrokerTradePort:
    """Explicit, protocol-complete intake guard for :class:`BrokerTradePort`."""

    def __init__(self, inner: BrokerTradePort, *, intake: ReentrantAsyncLock) -> None:
        self._inner = inner
        self._intake = intake

    @property
    def broker_id(self) -> str:
        return self._inner.broker_id

    @property
    def intake(self) -> ReentrantAsyncLock:
        """Expose the bound marker for composition assertions."""
        return self._intake

    @property
    def submission_response_is_authoritative_evidence(self) -> bool:
        """Preserve a deterministic adapter's optional evidence capability."""
        return bool(
            getattr(self._inner, "submission_response_is_authoritative_evidence", False)
        )

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self._assert_unfenced("submit")
        return await self._inner.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        self._assert_unfenced("cancel")
        await self._inner.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self._assert_unfenced("get_order_by_client_order_id")
        return await self._inner.get_order_by_client_order_id(client_order_id)

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        self._assert_unfenced("get_order_by_broker_order_id")
        return await self._inner.get_order_by_broker_order_id(order_id)

    async def submit_option_order(
        self, order: OptionOrderRequest, *, client_order_id: str
    ) -> BrokerOptionOrder:
        self._assert_unfenced("submit_option_order")
        return await self._inner.submit_option_order(order, client_order_id=client_order_id)

    async def get_option_order_by_client_order_id(self, client_order_id: str) -> BrokerOptionOrder | None:
        self._assert_unfenced("get_option_order_by_client_order_id")
        return await self._inner.get_option_order_by_client_order_id(client_order_id)

    async def get_option_order_by_broker_order_id(self, order_id: str) -> BrokerOptionOrder | None:
        self._assert_unfenced("get_option_order_by_broker_order_id")
        return await self._inner.get_option_order_by_broker_order_id(order_id)

    def bind_evaluated_bar(
        self, client_order_id: str, retained_bar: RetainedSourceBar, *, fill_at_ms: int | None = None
    ) -> None:
        """Forward the synthetic-only capability to bind the exact bar that prices an order.

        This is deliberately not part of :class:`BrokerTradePort`: live
        broker adapters neither understand nor need retained source-bar
        evidence. The SQLite runtime calls it only for synthetic custody and
        fails closed if its selected port does not provide the capability.

        ``fill_at_ms`` is the instant the order was decided, which tells an
        older bar from the decision minute's own
        (``SynthesizedOrderLedger.bind_evaluated_bar``).
        """
        self._assert_unfenced("bind_evaluated_bar")
        bind = getattr(self._inner, "bind_evaluated_bar", None)
        if not callable(bind):
            raise RuntimeError("Synthetic Clerk trade port cannot bind an evaluated source bar.")
        bind(client_order_id, retained_bar, fill_at_ms=fill_at_ms)

    def recovery_price_available(self, symbol: str, *, side: Literal["buy", "sell"] | None = None) -> bool:
        """Whether a no-submit port can price a recovery EXIT now; a vendor always can.

        ``side`` is named for a position in an option contract, whose close
        is priced from one side of a quote kept for it (ADR 0076). A
        reduction in shares names none and is asked as it always was.
        """
        self._assert_unfenced("recovery_price_available")
        probe = getattr(self._inner, "recovery_price_available", None)
        if not callable(probe):
            return True
        return bool(probe(symbol) if side is None else probe(symbol, side=side))

    def bind_latest_recovery_bar(self, client_order_id: str, *, symbol: str, side: str) -> bool:
        """Bind recovery-only synthetic evidence when the inner port requires it."""
        self._assert_unfenced("bind_latest_recovery_bar")
        bind = getattr(self._inner, "bind_latest_recovery_bar", None)
        if not callable(bind):
            return True
        return bool(bind(client_order_id, symbol=symbol, side=side))

    def bind_run_end_close_bar(self, client_order_id: str, *, symbol: str) -> bool:
        """Bind a Dry Run's run-end close price; a port that is not a simulation refuses."""
        self._assert_unfenced("bind_run_end_close_bar")
        bind = getattr(self._inner, "bind_run_end_close_bar", None)
        if not callable(bind):
            return False
        return bool(bind(client_order_id, symbol=symbol))

    def bind_option_entry_quote(self, client_order_id: str, *, symbol: str, quote: HeldQuote) -> None:
        """Forward the simulation-only capability to bind the quote an option entry fills from (ADR 0076)."""
        self._assert_unfenced("bind_option_entry_quote")
        bind = getattr(self._inner, "bind_option_entry_quote", None)
        if not callable(bind):
            # A port that fills no option order from a quote: a Live Shadow's, or a real broker's.
            # Definitive, as any refusal before contact is: nothing was sent and no order exists.
            raise BrokerOptionsNotSupported(
                "This account's trade port takes no option order filled from a quote; only a Dry Run's does.",
                broker=self._inner.broker_id,
                detail="Nothing was sent and no order exists.",
            )
        bind(client_order_id, symbol=symbol, quote=quote)

    def bind_option_close_quotes(
        self,
        client_order_id: str,
        *,
        symbol: str,
        side: Literal["buy", "sell"],
        quotes: Sequence[HeldQuote],
        usable_generation: int | None,
    ) -> bool:
        """Bind a simulated option close to the newest quote kept; a port that is not a simulation refuses."""
        self._assert_unfenced("bind_option_close_quotes")
        bind = getattr(self._inner, "bind_option_close_quotes", None)
        if not callable(bind):
            return False
        return bool(bind(client_order_id, symbol=symbol, side=side, quotes=quotes, usable_generation=usable_generation))

    def keep_option_quotes(self, quotes: Mapping[str, HeldQuote]) -> None:
        """Hand a simulation the newest quotes held for its contracts. A vendor's port keeps none.

        It reaches no broker: the simulation writes them beside its own
        orders, so it is not fenced.
        """
        keep = getattr(self._inner, "keep_option_quotes", None)
        if callable(keep):
            keep(quotes)

    def synthesized_option_fills(self) -> Mapping[str, SynthesizedOptionFill]:
        """How a simulation priced each option order it answered, by client order id. Not fenced: a ledger read."""
        read = getattr(self._inner, "synthesized_option_fills", None)
        return read() if callable(read) else {}

    def synthesized_fill_anchors(self) -> Mapping[str, SynthesizedAnchor]:
        """How a no-submit port priced each order it filled, by client order id (#2912).

        A vendor's port fills nothing itself and has none. This reads the
        port's own order ledger and reaches no broker, so it is not fenced.
        """
        read = getattr(self._inner, "synthesized_fill_anchors", None)
        return read() if callable(read) else {}

    def _assert_unfenced(self, method: str) -> None:
        if self._intake.current_scope_depth() != 0:
            raise BrokerCallUnderIntakeError(method)


def guard_broker_read_port(
    read: BrokerReadPort,
    *,
    intake: ReentrantAsyncLock,
) -> GuardedBrokerReadPort:
    """Bind a read port to one fence without adding wrapper layers."""
    if isinstance(read, GuardedBrokerReadPort):
        _require_same_intake(read.intake, intake)
        return read
    return GuardedBrokerReadPort(read, intake=intake)


def guard_broker_trade_port(
    trade: BrokerTradePort,
    *,
    intake: ReentrantAsyncLock,
) -> GuardedBrokerTradePort:
    """Bind a trade port to one fence without adding wrapper layers."""
    if isinstance(trade, GuardedBrokerTradePort):
        _require_same_intake(trade.intake, intake)
        return trade
    return GuardedBrokerTradePort(trade, intake=intake)


def guard_broker_ports(
    *,
    read: BrokerReadPort,
    trade: BrokerTradePort,
    intake: ReentrantAsyncLock,
) -> tuple[GuardedBrokerReadPort, GuardedBrokerTradePort]:
    """Return the shared SQLite Clerk guarded broker-port bundle."""
    return (
        guard_broker_read_port(read, intake=intake),
        guard_broker_trade_port(trade, intake=intake),
    )


def missing_guarded_async_methods(protocol: type[object], wrapper: type[object]) -> frozenset[str]:
    """Report asynchronous protocol growth that lacks an explicit guard method.

    The test suite passes a synthetic future protocol through this helper so a
    newly added broker operation cannot silently bypass the intake invariant.
    """
    return frozenset(
        name
        for name, member in vars(protocol).items()
        if inspect.iscoroutinefunction(member)
        and not inspect.iscoroutinefunction(getattr(wrapper, name, None))
    )


def _require_same_intake(existing: ReentrantAsyncLock, requested: ReentrantAsyncLock) -> None:
    if existing is not requested:
        raise ValueError("a guarded SQLite Clerk broker port cannot be rebound to another intake")


__all__ = [
    "BrokerCallUnderIntakeError",
    "GuardedBrokerReadPort",
    "GuardedBrokerTradePort",
    "guard_broker_ports",
    "guard_broker_read_port",
    "guard_broker_trade_port",
    "missing_guarded_async_methods",
]
