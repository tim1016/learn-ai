"""Paper-trading order placement (Phase 3a).

Public surface (curated):

* ``place_paper_order(client, spec)`` — places a market or limit order
  via ``ib_async.IB.placeOrder``. Returns an ``IbkrOrderAck`` synchronously
  once IBKR has assigned an ``orderId``. Status transitions after that
  point arrive via Phase 3b's order event stream.

Phase 3a refuses any non-paper context. The four safety layers — env-var
mode, port validator, DU account sentinel (Phase 1), per-request
``confirm_paper`` (this module) — must all be true. Any one false and
the placeOrder call is never reached.

Order types: MKT, LMT only. Time-in-force: DAY, GTC, IOC, OPG. Brackets,
OCO, trailing stops, market-on-close, IB algos — all deferred to Phase 3b
or later.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app.broker.ibkr.client import BrokerError, IbkrClient, _is_paper_account
from app.broker.ibkr.contracts import expiry_ms_to_yyyymmdd
from app.broker.ibkr.models import (
    IbkrOpenOrder,
    IbkrOrderAck,
    IbkrOrderEvent,
    IbkrOrderSpec,
    OrderEventType,
)

logger = logging.getLogger(__name__)


# Bound on the IBKR contract-qualification round-trip. ib_async resolves this
# via reqContractDetailsAsync, whose future only completes on the Gateway's
# contractDetailsEnd callback; on a half-open/app-silent connection that
# callback never arrives and the await would hang forever — and this call sits
# inline on the live engine's bar-processing coroutine, so a hang stalls the
# whole loop. Per .claude/rules/python.md: "Timeouts on all external calls."
_QUALIFY_TIMEOUT_S = 10.0


# Process-level idempotency cache: maps client_order_id → previously-issued
# IbkrOrderAck. Survives across requests within a single uvicorn worker;
# does NOT survive container restart. For durable idempotency we'd need a
# Redis or Postgres-backed cache — Phase 3.5 follow-up.
_IDEMPOTENCY_CACHE: dict[str, IbkrOrderAck] = {}

# Per-client_order_id locks guarding the check→place→store window. The cache
# read and write straddle the qualify/place awaits, so two concurrent retries
# carrying the same id could both miss the cache and both place a real order
# (the idempotency guarantee was sequential-only). Get-or-create below is
# atomic under asyncio — there is no await between the ``get`` and the ``set``
# — so concurrent callers for the same id observe the same lock.
_IDEMPOTENCY_LOCKS: dict[str, asyncio.Lock] = {}


def _idempotency_lookup(client_order_id: str | None) -> IbkrOrderAck | None:
    if client_order_id is None:
        return None
    return _IDEMPOTENCY_CACHE.get(client_order_id)


def _idempotency_store(client_order_id: str | None, ack: IbkrOrderAck) -> None:
    if client_order_id is None:
        return
    _IDEMPOTENCY_CACHE[client_order_id] = ack


def _idempotency_lock(client_order_id: str) -> asyncio.Lock:
    lock = _IDEMPOTENCY_LOCKS.get(client_order_id)
    if lock is None:
        lock = asyncio.Lock()
        _IDEMPOTENCY_LOCKS[client_order_id] = lock
    return lock


def _idempotency_clear_for_testing() -> None:
    """Test-only helper. Clear the idempotency cache and locks between tests."""
    _IDEMPOTENCY_CACHE.clear()
    _IDEMPOTENCY_LOCKS.clear()


class OrderRefusedError(BrokerError):
    """Order placement was refused by a safety check before reaching IBKR."""


class OrderNotFoundError(BrokerError):
    """Cancel or lookup targeted an order that IBKR doesn't know about."""


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _enforce_paper_safety(client: IbkrClient, spec: IbkrOrderSpec) -> str:
    """Run the paper-mode safety checks. Returns the validated account id.

    Any failure raises ``OrderRefusedError`` *before* any contract or order
    is constructed. We never want to come close to placing an order under a
    bad combination.

    Layers:
      0. ``IBKR_READONLY`` kill switch (operator-controlled lockdown).
      1. ``IBKR_MODE`` env var = paper.
      2. Connected port is a paper port.
      3. Connected account id begins with ``DU``.
      4. Per-request ``confirm_paper=true``.
    """
    settings = client.settings
    account_id = client.connected_account
    if account_id is None:
        raise OrderRefusedError("No account id on connected client.")

    # Layer 0: operator kill switch. ib_async's connect-time `readonly`
    # flag only suppresses startup queries (open/completed orders); it does
    # NOT prevent placeOrder at the IBKR protocol layer. We enforce it here
    # in our own code so flipping IBKR_READONLY=true reliably stops trades.
    if settings.readonly:
        raise OrderRefusedError(
            "Refusing to place order: IBKR_READONLY=true (operator lockdown). "
            "Set IBKR_READONLY=false in .env and restart the service to enable "
            "order placement."
        )

    # Layer 1: env-var mode
    if settings.mode != "paper":
        raise OrderRefusedError(
            f"Refusing to place order: IBKR_MODE is {settings.mode!r}, must be "
            "'paper' for Phase 3a."
        )

    # Layer 2: port validator already ran at config time, but cross-check
    # the actually-connected port for paranoia.
    from app.broker.ibkr.config import LIVE_PORTS

    if settings.port in LIVE_PORTS:
        raise OrderRefusedError(
            f"Refusing to place order: connected port {settings.port} is a "
            "LIVE Gateway port. Paper-mode env said paper but port disagrees."
        )

    # Layer 3: account-id sentinel (re-check; client.connect already enforced)
    if not _is_paper_account(account_id):
        raise OrderRefusedError(
            f"Refusing to place order: account {account_id!r} does NOT begin "
            "with 'DU'. Paper-mode env said paper but the broker connected "
            "us to a non-paper account."
        )

    # Layer 4: per-request confirm_paper
    if not spec.confirm_paper:
        raise OrderRefusedError(
            "Refusing to place order: spec.confirm_paper is False. "
            "Set confirm_paper=true in the request body to place a paper order."
        )

    return account_id


def _build_contract(spec: IbkrOrderSpec):
    """``IbkrOrderSpec`` → unqualified ``ib_async.Stock`` or ``Option``.

    The router's caller must ``qualifyContractsAsync`` before the order
    actually goes out — we do that inside ``place_paper_order``.
    """
    if spec.sec_type == "STK":
        from ib_async import Stock

        return Stock(symbol=spec.symbol, exchange="SMART", currency="USD")

    if spec.sec_type == "OPT":
        from ib_async import Option

        if spec.expiry_ms is None or spec.strike is None or spec.right is None:
            raise OrderRefusedError(
                "OPT order requires expiry_ms, strike, and right."
            )
        return Option(
            symbol=spec.symbol,
            lastTradeDateOrContractMonth=expiry_ms_to_yyyymmdd(spec.expiry_ms),
            strike=float(spec.strike),
            right=spec.right,
            exchange="SMART",
            currency="USD",
            multiplier=str(spec.multiplier),
        )

    raise OrderRefusedError(
        f"sec_type={spec.sec_type!r} is not supported in Phase 3a (STK/OPT only)."
    )


def _build_order(spec: IbkrOrderSpec):
    """``IbkrOrderSpec`` → ``ib_async.MarketOrder`` or ``LimitOrder``."""
    if spec.order_type == "MKT":
        from ib_async import MarketOrder

        order = MarketOrder(action=spec.action, totalQuantity=spec.quantity)
    elif spec.order_type == "LMT":
        if spec.limit_price is None:
            raise OrderRefusedError("LMT order requires limit_price.")
        from ib_async import LimitOrder

        order = LimitOrder(
            action=spec.action,
            totalQuantity=spec.quantity,
            lmtPrice=float(spec.limit_price),
        )
    else:
        raise OrderRefusedError(
            f"order_type={spec.order_type!r} is not supported in Phase 3a (MKT/LMT only)."
        )

    order.tif = spec.time_in_force
    # ADR 0008 / Phase 5A — stamp the deterministic order_ref so the IBKR
    # Gateway echoes it back on every order callback. The runtime joins
    # fills / cancels / cold-start reconciliation by this token; missing
    # it would lose ownership across a restart.
    if spec.order_ref is not None:
        order.orderRef = spec.order_ref
    return order


async def place_paper_order(
    client: IbkrClient,
    spec: IbkrOrderSpec,
    *,
    perm_id_wait_s: float = 0.0,
) -> IbkrOrderAck:
    """Place one paper order via ib_async.

    Steps:
    1. Run all four safety layers; refuse on any failure.
    2. Build and qualify the contract (so we get a ``conId``).
    3. Build the order. Submit via ``IB.placeOrder``.
    4. Return ``IbkrOrderAck`` with the broker-assigned ``orderId``.

    Status updates after this point arrive via Phase 3b's order event
    stream — this function doesn't wait for fills.

    ``perm_id_wait_s`` bounds an optional wait for IBKR to assign the
    order's ``permId``. ``IB.placeOrder`` returns synchronously while the
    order is still ``PendingSubmit``, before ``permId`` exists; it arrives a
    beat later on the ``openOrder`` callback and ib_async back-fills
    ``trade.order.permId`` in place. The hot path leaves this at ``0.0`` (no
    wait — the event stream carries permIds afterward). The recovery-flatten
    path opts in: it runs *after* the engine's event stream has stopped, so
    the synchronous ``permId`` is its only chance to capture the stable id
    that the next same-account relaunch needs to recognize the replayed
    recovery fill as bot-owned (see ``run._recovery_flatten``).
    """
    client.require_connected()
    account_id = _enforce_paper_safety(client, spec)

    # Without an idempotency key each call is independent — place directly.
    if spec.client_order_id is None:
        return await _place_and_build_ack(
            client, spec, account_id, perm_id_wait_s=perm_id_wait_s
        )

    # Serialize the check→place→store window per client_order_id. The cache
    # read and write straddle the qualify/place awaits below, so without this
    # lock two concurrent retries with the same id would both miss the cache
    # and both place a real order. The lock makes the second caller wait and
    # return the first caller's cached ack instead of placing a duplicate.
    async with _idempotency_lock(spec.client_order_id):
        cached = _idempotency_lookup(spec.client_order_id)
        if cached is not None:
            logger.info(
                "[PAPER ORDER] idempotent replay: client_order_id=%s → order_id=%d",
                spec.client_order_id,
                cached.order_id,
            )
            return cached

        ack = await _place_and_build_ack(
            client, spec, account_id, perm_id_wait_s=perm_id_wait_s
        )
        _idempotency_store(spec.client_order_id, ack)
        return ack


async def _place_and_build_ack(
    client: IbkrClient,
    spec: IbkrOrderSpec,
    account_id: str,
    *,
    perm_id_wait_s: float,
) -> IbkrOrderAck:
    """Qualify, submit, and snapshot one order into an ``IbkrOrderAck``.

    Extracted from ``place_paper_order`` so the idempotency lock can wrap the
    cache read/write around the placement without holding both in one block.
    Carries no idempotency logic itself — the caller owns the cache.
    """
    contract = _build_contract(spec)
    try:
        qualified = await asyncio.wait_for(
            client.ib.qualifyContractsAsync(contract), timeout=_QUALIFY_TIMEOUT_S
        )
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR contract qualification for {spec.symbol} ({spec.sec_type}) "
            f"timed out after {_QUALIFY_TIMEOUT_S:.0f}s; the Gateway connection "
            "may be half-open. Order not placed."
        ) from exc
    if not qualified:
        raise BrokerError(
            f"IBKR could not qualify contract for {spec.symbol} "
            f"({spec.sec_type})."
        )
    qualified_contract = qualified[0]

    order = _build_order(spec)
    logger.info(
        "[PAPER ORDER] account=%s %s %s %s%s%s%s",
        account_id,
        spec.action,
        spec.quantity,
        spec.symbol,
        f" @ {spec.limit_price}" if spec.order_type == "LMT" else " MKT",
        f" exp={expiry_ms_to_yyyymmdd(spec.expiry_ms)}" if spec.expiry_ms else "",
        f" {spec.strike}{spec.right}" if spec.right else "",
    )

    trade = client.ib.placeOrder(qualified_contract, order)

    # ib_async.placeOrder returns synchronously with a Trade whose
    # order.orderId is set. Status starts as 'PendingSubmit' and updates
    # via events. Optionally wait for IBKR to assign the permId: sleeping
    # yields to the asyncio loop so ib_async can process the openOrder
    # callback that back-fills trade.order.permId. Bounded by
    # perm_id_wait_s so a degraded connection can't hang the caller.
    if perm_id_wait_s > 0:
        poll_interval_s = 0.05
        remaining_s = perm_id_wait_s
        while not trade.order.permId and remaining_s > 0:
            await asyncio.sleep(min(poll_interval_s, remaining_s))
            remaining_s -= poll_interval_s

    # Capture the snapshot now (after any permId wait so status/permId
    # reflect the post-acknowledgement state).
    order_status = getattr(trade.orderStatus, "status", "Unknown") or "Unknown"
    return IbkrOrderAck(
        account_id=account_id,
        is_paper=True,  # already enforced by safety layers
        order_id=int(trade.order.orderId),
        perm_id=int(trade.order.permId) if trade.order.permId else None,
        client_id=int(client.settings.client_id),
        con_id=int(qualified_contract.conId),
        symbol=spec.symbol,
        action=spec.action,
        quantity=float(spec.quantity),
        order_type=spec.order_type,
        limit_price=spec.limit_price,
        status=order_status,
        placed_at_ms=_now_ms(),
    )


# ── Phase 3b: cancel, list open, event stream ──────────────────────────


def _trade_to_open_order(
    trade,
    account_id: str,
    client_id: int,
) -> IbkrOpenOrder:
    """``ib_async.Trade`` → ``IbkrOpenOrder`` wire model."""
    contract = trade.contract
    order = trade.order
    status_obj = trade.orderStatus

    sec_type = contract.secType
    order_type = "LMT" if order.lmtPrice and order.lmtPrice > 0 else "MKT"
    return IbkrOpenOrder(
        account_id=account_id,
        order_id=int(order.orderId),
        perm_id=int(order.permId) if order.permId else None,
        client_id=client_id,
        con_id=int(contract.conId),
        symbol=contract.symbol,
        sec_type=sec_type,
        action=order.action,
        quantity=float(order.totalQuantity),
        order_type=order_type,
        limit_price=float(order.lmtPrice) if order.lmtPrice else None,
        time_in_force=order.tif or "DAY",
        status=getattr(status_obj, "status", "Unknown") or "Unknown",
        cumulative_filled=float(getattr(status_obj, "filled", 0.0) or 0.0),
        remaining=float(getattr(status_obj, "remaining", 0.0) or 0.0),
        avg_fill_price=(
            float(status_obj.avgFillPrice)
            if getattr(status_obj, "avgFillPrice", 0.0)
            else None
        ),
        fetched_at_ms=_now_ms(),
    )


def _order_belongs_to_account(trade: object, account_id: str) -> bool:
    """Whether ``trade`` belongs to the connected account.

    Orders we place via ``_build_order`` (``MarketOrder``/``LimitOrder``) do not
    set ``order.account`` — ib_async leaves it ``""``. So an empty account means
    "this single-account client's own order" and belongs to ``account_id``; only
    a *non-empty* account that differs is genuinely foreign (e.g. another client
    on the same gateway).

    The previous check ``order.account != account_id`` dropped our OWN orders
    (``"" != "DU…"``), which blinded the live engine to its own fills, left the
    position tally at zero (→ fleet "unattributed" contamination), and tripped a
    false lost-fill fatal halt. See #441.
    """
    order_account = getattr(getattr(trade, "order", None), "account", "") or ""
    return order_account in ("", account_id)


async def list_open_orders(client: IbkrClient) -> list[IbkrOpenOrder]:
    """All open orders the connected client has placed.

    ``ib_async.IB.openOrdersAsync`` returns ``Trade`` objects across the
    session; we filter to the currently-connected account.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise BrokerError("connected client has no account_id")

    trades = await client.ib.reqAllOpenOrdersAsync()
    out: list[IbkrOpenOrder] = []
    for trade in trades:
        if not _order_belongs_to_account(trade, account_id):
            continue
        try:
            out.append(_trade_to_open_order(trade, account_id, client.settings.client_id))
        except Exception as exc:
            logger.warning(
                "Skipping unparseable open order conId=%s: %s",
                getattr(trade.contract, "conId", "?"),
                exc,
            )
    return out


async def cancel_paper_order(
    client: IbkrClient,
    order_id: int,
) -> IbkrOpenOrder:
    """Cancel one paper order by ``order_id``.

    Looks up the open Trade by ``order.orderId``, calls
    ``IB.cancelOrder``, and returns the snapshot post-cancel-request
    (status will typically be ``PendingCancel``; the terminal
    ``Cancelled`` status arrives via the order event stream).

    Refuses if mode is not paper. Mirrors the safety pattern from
    ``place_paper_order`` — we never want to cancel a live order from
    a paper-mode build by accident.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise OrderNotFoundError("No account id on connected client.")

    settings = client.settings
    if settings.mode != "paper":
        raise OrderRefusedError(
            f"Refusing to cancel: IBKR_MODE is {settings.mode!r}, must be 'paper'."
        )
    if not _is_paper_account(account_id):
        raise OrderRefusedError(
            f"Refusing to cancel: account {account_id!r} is not a paper (DU) account."
        )

    # Find the open trade with this orderId. ib_async caches them on `trades()`.
    matching = [t for t in client.ib.trades() if int(t.order.orderId) == int(order_id)]
    if not matching:
        raise OrderNotFoundError(
            f"No open order with order_id={order_id} on this client."
        )
    trade = matching[0]
    # Ownership guard: ib_async's trades() cache can hold orders from other
    # clients (or manual TWS) on the same DU account, and orderIds are small
    # per-client integers that can collide. Without this check a caller-supplied
    # order_id could cancel a foreign order. Mirrors the guard list_open_orders
    # and stream_order_events already apply.
    if not _order_belongs_to_account(trade, account_id):
        raise OrderNotFoundError(
            f"No open order with order_id={order_id} owned by this client."
        )
    client.ib.cancelOrder(trade.order)
    return _trade_to_open_order(trade, account_id, client.settings.client_id)


def _resolve_event_type(
    trade,
    *,
    is_fill: bool,
) -> OrderEventType:
    if is_fill:
        return "fill"
    status = getattr(trade.orderStatus, "status", "")
    if status in {"Cancelled", "ApiCancelled"}:
        return "cancel"
    return "status"


def _trade_to_status_event(
    trade,
    account_id: str,
) -> IbkrOrderEvent:
    """Translate the current Trade snapshot into a status-type event."""
    return IbkrOrderEvent(
        account_id=account_id,
        order_id=int(trade.order.orderId),
        perm_id=int(trade.order.permId) if trade.order.permId else None,
        con_id=int(trade.contract.conId) if trade.contract else None,
        event_type=_resolve_event_type(trade, is_fill=False),
        status=getattr(trade.orderStatus, "status", None),
        cumulative_filled=float(getattr(trade.orderStatus, "filled", 0.0) or 0.0),
        remaining=float(getattr(trade.orderStatus, "remaining", 0.0) or 0.0),
        ts_ms=_now_ms(),
    )


def _fill_to_event(
    trade, fill, account_id: str, *, fills_through: list | None = None
) -> IbkrOrderEvent:
    """Translate one Fill into a fill-type event.

    ``exec_id`` and ``client_id`` come from the underlying ib_async
    ``Execution`` object — those are the broker primary keys the
    live-runtime § 7 fatal-halt check needs to detect outside-mutation
    (any execution under our DU account whose clientId is not ours,
    or whose execId we never originated, is foreign).

    ``fills_through`` is the list of executions up to and including this one.
    The running cumulative_filled / remaining / avg_fill_price are derived
    from it rather than read off ``trade.orderStatus`` — that single snapshot
    reflects the order's *final* state, so a collapsed partial fill (two
    executions between polls) would otherwise stamp the first event with the
    order's terminal totals instead of the values true after that execution.
    Defaults to ``[fill]`` (this execution only) when the caller has no broader
    context.
    """
    if fills_through is None:
        fills_through = [fill]

    exec_obj = getattr(fill, "execution", None)
    exec_id = getattr(exec_obj, "execId", None) if exec_obj is not None else None
    client_id_raw = getattr(exec_obj, "clientId", None) if exec_obj is not None else None
    # ib_async populates ``Execution.time`` as a tz-aware UTC datetime. Carry
    # it as ``int64 ms UTC`` so the § 7 outside-mutation floor can distinguish
    # a stale connect-time replay from a concurrent fill. ``ts_ms`` below stays
    # wall-clock observation time for the SSE stream's existing consumers.
    exec_time = getattr(exec_obj, "time", None) if exec_obj is not None else None
    exec_time_ms = int(exec_time.timestamp() * 1000) if exec_time is not None else None
    # Commission rides on the polled Fill once IBKR reports it (a beat after the
    # execution). Read it off the cached object — no eventkit subscription, per
    # this module's poll-based design. None until reported (PRD-B).
    commission_obj = getattr(fill, "commissionReport", None)
    fee = getattr(commission_obj, "commission", None) if commission_obj is not None else None

    # Running totals from the executions up to and including this fill (see
    # docstring) — not the terminal orderStatus snapshot.
    running_shares = 0.0
    running_notional = 0.0
    for prior in fills_through:
        prior_exec = getattr(prior, "execution", None)
        if prior_exec is None:
            continue
        shares = float(getattr(prior_exec, "shares", 0.0) or 0.0)
        price = float(getattr(prior_exec, "price", 0.0) or 0.0)
        running_shares += shares
        running_notional += shares * price
    total_qty = float(getattr(trade.order, "totalQuantity", 0.0) or 0.0)
    running_remaining = max(total_qty - running_shares, 0.0)
    running_avg = (running_notional / running_shares) if running_shares else None

    return IbkrOrderEvent(
        account_id=account_id,
        order_id=int(trade.order.orderId),
        perm_id=int(trade.order.permId) if trade.order.permId else None,
        con_id=int(trade.contract.conId) if trade.contract else None,
        event_type="fill",
        status=getattr(trade.orderStatus, "status", None),
        exec_id=str(exec_id) if exec_id else None,
        client_id=int(client_id_raw) if client_id_raw is not None else None,
        fill_quantity=float(getattr(exec_obj, "shares", 0.0) or 0.0),
        avg_fill_price=running_avg,
        cumulative_filled=running_shares,
        remaining=running_remaining,
        last_fill_price=float(getattr(exec_obj, "price", 0.0) or 0.0) or None,
        exec_time_ms=exec_time_ms,
        fee=float(fee) if fee is not None else None,
        ts_ms=_now_ms(),
    )


async def stream_order_events(
    client: IbkrClient,
    *,
    poll_seconds: float = 0.5,
) -> AsyncIterator[IbkrOrderEvent]:
    """Yield order lifecycle events as they happen on the connected client.

    Implementation: ib_async fires ``orderStatusEvent`` and ``execDetailsEvent``
    when transitions happen. Rather than wire those eventkit hooks (which
    couples this module to ib_async's event model and complicates
    cancellation), we poll the cached ``trades()`` list per
    ``poll_seconds`` and diff against the last-seen snapshot. Any new
    fills or status changes yield events.

    Trade-off: a high-frequency burst could collapse two transitions
    into a single yielded event. For paper trading at 1 Hz polling that
    almost never matters — and the tests verify the per-transition
    delta logic. If we ever need true edge-trigger semantics, swap to
    ``orderStatusEvent`` subscription in a Phase 3.5 follow-up.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise BrokerError("connected client has no account_id")

    # Last-seen snapshots keyed by orderId. We compare against these to
    # detect transitions on the next poll.
    last_status: dict[int, str] = {}
    last_fill_count: dict[int, int] = {}

    try:
        while True:
            # ib_async's ``trades()`` is an in-memory cache that never raises
            # when the connection drops, so without this gate a mid-stream
            # disconnect would freeze the cache and we'd poll it forever,
            # silently missing fills while the engine keeps submitting orders.
            client.require_live()
            trades = list(client.ib.trades())
            for trade in trades:
                if not _order_belongs_to_account(trade, account_id):
                    continue
                oid = int(trade.order.orderId)

                # Status transition?
                cur_status = getattr(trade.orderStatus, "status", "Unknown") or "Unknown"
                if last_status.get(oid) != cur_status:
                    last_status[oid] = cur_status
                    yield _trade_to_status_event(trade, account_id)

                # New fills?
                fills = list(getattr(trade, "fills", []) or [])
                prev = last_fill_count.get(oid, 0)
                if len(fills) > prev:
                    for i in range(prev, len(fills)):
                        yield _fill_to_event(
                            trade, fills[i], account_id, fills_through=fills[: i + 1]
                        )
                    last_fill_count[oid] = len(fills)

            await asyncio.sleep(poll_seconds)
    except asyncio.CancelledError:
        raise
