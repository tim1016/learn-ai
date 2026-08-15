"""Typed broker-port guards for the SQLite Clerk intake boundary.

The intake fence is a dynamic scope, not a global broker mutex.  These
wrappers therefore reject only calls inherited from a fenced scope; another
task may continue a broker conversation while a short repository fold owns
the intake fence.
"""

from __future__ import annotations

import inspect

from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import (
    BrokerAccountSnapshot,
    BrokerActivity,
    BrokerAsset,
    BrokerClockEvidence,
    BrokerOrder,
    BrokerOrderLeg,
    BrokerPortfolioHistory,
    BrokerPosition,
    PortfolioHistoryRange,
)
from app.broker.contract.ports import BrokerReadPort, BrokerTradePort


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
    ) -> list[BrokerOrder]:
        self._assert_unfenced("list_orders")
        return await self._inner.list_orders(status=status, limit=limit, after_ms=after_ms)

    async def list_activities(
        self,
        *,
        after_ms: int | None = None,
        limit: int = 100,
    ) -> list[BrokerActivity]:
        self._assert_unfenced("list_activities")
        return await self._inner.list_activities(after_ms=after_ms, limit=limit)

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

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self._assert_unfenced("submit")
        return await self._inner.submit(leg, client_order_id=client_order_id)

    async def cancel(self, order_id: str) -> None:
        self._assert_unfenced("cancel")
        await self._inner.cancel(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        self._assert_unfenced("get_order_by_client_order_id")
        return await self._inner.get_order_by_client_order_id(client_order_id)

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
