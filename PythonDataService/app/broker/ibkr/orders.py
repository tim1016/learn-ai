"""Read-only IBKR order projections and non-submitting preview builders."""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import AsyncIterator

from app.broker.ibkr.api_evidence import (
    evidence_response,
    get_ibkr_api_evidence_recorder,
)
from app.broker.ibkr.client import BrokerError, IbkrClient, _is_paper_account
from app.broker.ibkr.config import LIVE_PORTS
from app.broker.ibkr.contracts import expiry_ms_to_yyyymmdd
from app.broker.ibkr.models import (
    IbkrApiCallbackName,
    IbkrApiRequestEvidence,
    IbkrOpenOrder,
    IbkrOrderEvent,
    IbkrOrderSpec,
)
from app.broker.ibkr.order_error_stream import read_order_error_events
from app.broker.ibkr.order_evidence import (
    all_open_orders_request_evidence,
    build_execution_recovery_evidence,
    build_fill_event_evidence,
    build_open_order_evidence,
    build_status_event_evidence,
)
from app.broker.ibkr.order_projection import (
    event_order_type,
    event_side,
    event_symbol,
    order_belongs_to_account,
    resolve_event_type,
    trade_order_event_fields,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


# Bound on the reconnect-recovery executions fetch. ``reqExecutionsAsync``
# completes only when IBKR fires ``execDetailsEnd``; on a half-open or
# silent-after-reconnect connection that callback never arrives and the
# await would hang. The sweep is invoked from the
# ``AutoReconnectMonitor`` recovery chain. Thirty seconds is generous:
# a healthy Gateway returns the day's executions in well under a second;
# anything longer signals a degraded connection that the sweep cannot
# usefully complete on. On timeout we raise ``BrokerError`` so the
# publisher's ``finally`` clears its recovery classification and the next
# reconnect cycle can retry the sweep cleanly.
_RECOVERY_EXECUTIONS_TIMEOUT_S = 30.0
_OPEN_ORDERS_TIMEOUT_S = 8.0
_OPEN_ORDERS_LOCK_ATTR = "_learn_ai_open_orders_request_lock"
_OPEN_ORDERS_TIMEOUT_EVENT_ATTR = "_learn_ai_open_orders_timeout_event_ms"


def _finite_float(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _client_event_ms(client: IbkrClient) -> int | None:
    event_ms = getattr(client, "_last_event_ms", None)
    return event_ms if isinstance(event_ms, int) else None


def _open_orders_request_lock(client: IbkrClient) -> asyncio.Lock:
    lock = getattr(client, _OPEN_ORDERS_LOCK_ATTR, None)
    if lock is None:
        lock = asyncio.Lock()
        setattr(client, _OPEN_ORDERS_LOCK_ATTR, lock)
    return lock


def _open_orders_timeout_guard_active(client: IbkrClient) -> bool:
    timed_out_event_ms = getattr(client, _OPEN_ORDERS_TIMEOUT_EVENT_ATTR, None)
    if not isinstance(timed_out_event_ms, int):
        return False
    event_ms = _client_event_ms(client)
    if event_ms is not None and event_ms > timed_out_event_ms:
        delattr(client, _OPEN_ORDERS_TIMEOUT_EVENT_ATTR)
        return False
    return True


def _mark_open_orders_timed_out(client: IbkrClient) -> None:
    setattr(
        client,
        _OPEN_ORDERS_TIMEOUT_EVENT_ATTR,
        _client_event_ms(client) or now_ms_utc(),
    )


class OrderRefusedError(BrokerError):
    """A non-submitting paper preview failed a safety check."""


def _enforce_paper_account_context(client: IbkrClient, *, operation: str) -> str:
    """Run paper-account safety layers for the retained what-if preview.

    Any failure raises ``OrderRefusedError`` before the retained what-if
    preview constructs a contract or non-transmitting order.

    Layers:
      0. ``IBKR_READONLY`` kill switch (operator-controlled lockdown).
      1. ``IBKR_MODE`` env var = paper.
      2. Connected port is a paper port.
      3. Connected account id begins with ``DU``.
    """
    settings = client.settings
    account_id = client.connected_account
    if account_id is None:
        raise OrderRefusedError("No account id on connected client.")

    # Layer 0: operator lock. What-if previews remain opt-in even though the
    # application exposes no transmitting IBKR order surface.
    if settings.readonly:
        raise OrderRefusedError(
            f"Refusing to {operation}: IBKR_READONLY=true (operator lockdown). "
            "Set IBKR_READONLY=false in .env and restart the service to enable "
            "paper what-if previews."
        )

    # Layer 1: env-var mode
    if settings.mode != "paper":
        raise OrderRefusedError(
            f"Refusing to {operation}: IBKR_MODE is {settings.mode!r}, must be "
            "'paper'."
        )

    # Layer 2: port validator already ran at config time, but cross-check
    # the actually-connected port for paranoia.
    if settings.port in LIVE_PORTS:
        raise OrderRefusedError(
            f"Refusing to {operation}: connected port {settings.port} is a "
            "LIVE Gateway port. Paper-mode env said paper but port disagrees."
        )

    # Layer 3: account-id sentinel (re-check; client.connect already enforced)
    if not _is_paper_account(account_id):
        raise OrderRefusedError(
            f"Refusing to {operation}: account {account_id!r} does NOT begin "
            "with 'DU'. Paper-mode env said paper but the broker connected "
            "us to a non-paper account."
        )

    return account_id


def _build_contract(spec: IbkrOrderSpec):
    """``IbkrOrderSpec`` → unqualified ``ib_async.Stock`` or ``Option``.

    The non-submitting preview qualifies this contract before asking IBKR for
    margin and commission estimates.
    """
    if spec.sec_type == "STK":
        from ib_async import Stock

        contract = Stock(symbol=spec.symbol, exchange="SMART", currency="USD")
        if spec.con_id is not None:
            contract.conId = spec.con_id
        return contract

    if spec.sec_type == "OPT":
        from ib_async import Option

        if spec.expiry_ms is None or spec.strike is None or spec.right is None:
            raise OrderRefusedError(
                "OPT order requires expiry_ms, strike, and right."
            )
        contract = Option(
            symbol=spec.symbol,
            lastTradeDateOrContractMonth=expiry_ms_to_yyyymmdd(spec.expiry_ms),
            strike=float(spec.strike),
            right=spec.right,
            exchange="SMART",
            currency="USD",
            multiplier=str(spec.multiplier),
        )
        if spec.con_id is not None:
            contract.conId = spec.con_id
        return contract

    raise OrderRefusedError(
        f"sec_type={spec.sec_type!r} is not supported for what-if (STK/OPT only)."
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
            f"order_type={spec.order_type!r} is not supported for what-if (MKT/LMT only)."
        )

    order.tif = spec.time_in_force
    order.outsideRth = bool(spec.outside_rth)
    # Preserve an optional reference on the non-transmitting what-if request so
    # its evidence can be compared with historical order rows. This does not
    # authorize submission; ``preview_paper_order`` always sets ``whatIf``.
    if spec.order_ref is not None:
        order.orderRef = spec.order_ref
    return order


def _trade_to_open_order(
    trade,
    account_id: str,
    client_id: int,
    *,
    request: IbkrApiRequestEvidence | None = None,
    response_callback: IbkrApiCallbackName = "openOrder",
) -> IbkrOpenOrder:
    """``ib_async.Trade`` → ``IbkrOpenOrder`` wire model."""
    contract = trade.contract
    order = trade.order
    status_obj = trade.orderStatus

    sec_type = contract.secType
    order_type = "LMT" if order.lmtPrice and order.lmtPrice > 0 else "MKT"
    total_quantity = _finite_float(getattr(order, "totalQuantity", 0.0))
    filled_quantity = _finite_float(getattr(order, "filledQuantity", 0.0))
    status_filled = _finite_float(getattr(status_obj, "filled", 0.0))
    cumulative_filled = status_filled or filled_quantity
    # Completed-order snapshots from IBKR can zero ``totalQuantity`` and
    # ``orderStatus.filled`` while still preserving the actual fill size on
    # ``Order.filledQuantity``. Keep normal open-order fields authoritative,
    # but fall back so completed order rows match execution evidence.
    quantity = total_quantity or filled_quantity or cumulative_filled
    return IbkrOpenOrder(
        account_id=account_id,
        order_id=int(order.orderId),
        perm_id=int(order.permId) if order.permId else None,
        client_id=client_id,
        con_id=int(contract.conId),
        symbol=contract.symbol,
        sec_type=sec_type,
        action=order.action,
        quantity=quantity,
        order_type=order_type,
        limit_price=float(order.lmtPrice) if order.lmtPrice else None,
        time_in_force=order.tif or "DAY",
        status=getattr(status_obj, "status", "Unknown") or "Unknown",
        cumulative_filled=cumulative_filled,
        remaining=float(getattr(status_obj, "remaining", 0.0) or 0.0),
        avg_fill_price=(
            float(status_obj.avgFillPrice)
            if getattr(status_obj, "avgFillPrice", 0.0)
            else None
        ),
        # ADR 0008 / Phase 5A — coerce the library's empty-string default to
        # None so a missing echo stays distinguishable from a present orderRef
        # downstream (the cold-start reconciliation orchestrator treats
        # absence as "not ours via ref").
        order_ref=(getattr(order, "orderRef", "") or None),
        ibkr_evidence=build_open_order_evidence(
            trade,
            request=request,
            response_callback=response_callback,
        ),
        fetched_at_ms=now_ms_utc(),
    )


async def list_open_orders(
    client: IbkrClient,
    *,
    timeout_s: float = _OPEN_ORDERS_TIMEOUT_S,
) -> list[IbkrOpenOrder]:
    """All open orders the connected client has placed.

    ``ib_async.IB.openOrdersAsync`` returns ``Trade`` objects across the
    session; we filter to the currently-connected account.
    """
    client.require_connected()
    account_id = client.connected_account
    if account_id is None:
        raise BrokerError("connected client has no account_id")

    request_snapshot = all_open_orders_request_evidence()
    try:
        async with _open_orders_request_lock(client):
            if _open_orders_timeout_guard_active(client):
                raise BrokerError(
                    "IBKR open orders request previously timed out; reconnect IBKR "
                    "before retrying open-order sweeps."
                )
            try:
                trades = await asyncio.wait_for(
                    client.ib.reqAllOpenOrdersAsync(),
                    timeout=timeout_s,
                )
            except TimeoutError:
                _mark_open_orders_timed_out(client)
                raise
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR open orders request timed out after {timeout_s:g}s."
        ) from exc
    get_ibkr_api_evidence_recorder().record(
        source="orders.list_open_orders",
        account_id=account_id,
        request=request_snapshot,
        response=evidence_response("openOrder", fields={"trade_count": len(trades)}, objects=trades),
    )
    out: list[IbkrOpenOrder] = []
    for trade in trades:
        if not order_belongs_to_account(trade, account_id):
            continue
        try:
            out.append(
                _trade_to_open_order(
                    trade,
                    account_id,
                    client.settings.client_id,
                    request=all_open_orders_request_evidence(),
                )
            )
        except Exception as exc:
            logger.warning(
                "Skipping unparseable open order conId=%s: %s",
                getattr(trade.contract, "conId", "?"),
                exc,
            )
    return out


def _trade_to_status_event(
    trade,
    account_id: str,
) -> IbkrOrderEvent:
    """Translate the current Trade snapshot into a status-type event."""
    return IbkrOrderEvent(
        **trade_order_event_fields(trade, account_id),
        event_type=resolve_event_type(trade, is_fill=False),
        ibkr_evidence=build_status_event_evidence(trade),
        ts_ms=now_ms_utc(),
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
    # ib_async populates ``Execution.orderRef`` from the broker's echo of the
    # token we stamped on the outbound order (ADR 0008 / Phase 5A). Empty
    # string is the library's "field absent" default — coerce to None so a
    # missing echo stays distinguishable from a real, present orderRef
    # downstream (the reconciliation publisher treats absence as foreign).
    # Prefer the Execution's value (broker-authoritative on a fill) but fall
    # back to the Order's value when the Execution omits it.
    exec_order_ref = getattr(exec_obj, "orderRef", "") if exec_obj is not None else ""
    order_ref = exec_order_ref or getattr(trade.order, "orderRef", "") or None
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
        order_ref=order_ref,
        symbol=event_symbol(trade),
        side=event_side(trade),
        order_type=event_order_type(trade),
        exec_id=str(exec_id) if exec_id else None,
        client_id=int(client_id_raw) if client_id_raw is not None else None,
        fill_quantity=float(getattr(exec_obj, "shares", 0.0) or 0.0),
        avg_fill_price=running_avg,
        cumulative_filled=running_shares,
        remaining=running_remaining,
        last_fill_price=float(getattr(exec_obj, "price", 0.0) or 0.0) or None,
        exec_time_ms=exec_time_ms,
        fee=float(fee) if fee is not None else None,
        ibkr_evidence=build_fill_event_evidence(trade, fill, exec_obj, commission_obj),
        ts_ms=now_ms_utc(),
    )


async def executions_for_reconnect_recovery(
    client: IbkrClient,
) -> list[IbkrOrderEvent]:
    """Adapt the day's IBKR executions into ``IbkrOrderEvent``s for the
    broker-activity publisher's reconnect-recovery sweep.

    Calls ``IB.reqExecutionsAsync()`` to fetch every execution the
    Gateway is willing to report for this client (typically the current
    trading day). For each ``Fill``, builds an ``IbkrOrderEvent`` with
    the four truthfulness-contract keys the ``reconnect_recovery``
    template requires (``quantity``, ``symbol``, ``price``,
    ``order_type``):

    * ``symbol`` comes from ``Fill.contract.symbol`` directly.
    * ``quantity`` and ``price`` come from ``Fill.execution.shares`` and
      ``Fill.execution.price``.
    * ``side`` is derived from ``Fill.execution.side`` (IBKR sends
      "BOT" / "SLD" — translated to "BUY" / "SELL").
    * ``order_type`` is recovered from ``ib.trades()`` when the original
      Trade is still cached (the live API session keeps Trade objects
      for the session's open and recently-closed orders). When the
      Trade is absent (e.g. a fill on a long-since-completed order),
      ``order_type`` is left as ``None`` — the publisher's authoring
      path catches the resulting ``UnauthorableEventError`` and skips
      that Fill with a structured log. The truthfulness contract
      (ADR 0014 §3) forbids substituting a placeholder; an unauthored
      row is honest, a placeholder row is not.
    * ``commission`` rides on ``Fill.commissionReport.commission`` once
      IBKR reports it (a beat after the fill); ``None`` otherwise.

    Refuses (raises ``NotConnectedError`` from ``require_live``) if the
    client is not currently connected — the caller (the
    ``AutoReconnectMonitor`` post-reconnect chain) only invokes this
    after a successful reconnect, so a still-disconnected client here
    is a true error.
    """
    client.require_live()
    account_id = client.connected_account
    if account_id is None:
        raise BrokerError("connected client has no account_id")

    # Bounded fetch: a hung ``reqExecutionsAsync`` would pin the publisher's
    # reconnect-recovery classification indefinitely (the sweep's ``finally``
    # only runs when this await returns or raises). See
    # ``_RECOVERY_EXECUTIONS_TIMEOUT_S`` for the rationale on 30s.
    try:
        fills = await asyncio.wait_for(
            client.ib.reqExecutionsAsync(),
            timeout=_RECOVERY_EXECUTIONS_TIMEOUT_S,
        )
    except TimeoutError as exc:
        raise BrokerError(
            f"IBKR reqExecutionsAsync timed out after "
            f"{_RECOVERY_EXECUTIONS_TIMEOUT_S:.0f}s; the Gateway connection "
            "may be half-open. Reconnect-recovery sweep aborted; the "
            "publisher's recovery classification has been cleared so the next "
            "reconnect cycle can retry the sweep."
        ) from exc

    # Index existing trades by orderId / permId so we can recover the
    # original order_type for each fill. Trade objects carry the Order
    # for the session's open and recently-completed orders; a fill on a
    # purged Trade falls through to the MKT default below.
    trades_by_order_id: dict[int, object] = {}
    trades_by_perm_id: dict[int, object] = {}
    for trade in client.ib.trades():
        try:
            trades_by_order_id[int(trade.order.orderId)] = trade
            if trade.order.permId:
                trades_by_perm_id[int(trade.order.permId)] = trade
        except (AttributeError, TypeError, ValueError):
            continue

    events: list[IbkrOrderEvent] = []
    for fill in fills:
        event = _fill_to_recovery_event(
            fill,
            account_id=account_id,
            trades_by_order_id=trades_by_order_id,
            trades_by_perm_id=trades_by_perm_id,
        )
        if event is not None:
            events.append(event)
    return events


def _fill_to_recovery_event(
    fill,
    *,
    account_id: str,
    trades_by_order_id: dict[int, object],
    trades_by_perm_id: dict[int, object],
) -> IbkrOrderEvent | None:
    """Standalone Fill → IbkrOrderEvent adapter for the recovery sweep.

    Distinct from ``_fill_to_event`` (which composes off an active Trade
    object known to ib_async) because ``reqExecutionsAsync`` returns
    free-standing ``Fill`` records whose Trade may have been purged from
    the live cache. Returns ``None`` only when the Fill is too degenerate
    to author truthfully — typically a missing ``Fill.execution`` (which
    never happens on a real Fill but is defended against because the
    sweep runs on every reconnect and one bad row would skip every
    following row).
    """
    execution = getattr(fill, "execution", None)
    contract = getattr(fill, "contract", None)
    if execution is None or contract is None:
        return None

    exec_id = getattr(execution, "execId", None)
    perm_id_raw = getattr(execution, "permId", None)
    order_id_raw = getattr(execution, "orderId", None)
    client_id_raw = getattr(execution, "clientId", None)
    order_ref = getattr(execution, "orderRef", "") or None

    symbol = getattr(contract, "symbol", None)
    if not symbol:
        return None

    # IBKR sends "BOT" / "SLD" on the Execution; the row's side enum is
    # "BUY" / "SELL". Anything else is non-equity-style and falls back
    # to None (the reconciler treats absence as unauthorable, which is
    # the right halt path for an unrecognised side).
    raw_side = getattr(execution, "side", "")
    side: str | None
    if raw_side == "BOT":
        side = "BUY"
    elif raw_side == "SLD":
        side = "SELL"
    else:
        side = None

    # Look up the original Trade to recover the order_type the operator
    # saw at submit time. Prefer permId (stable across reconnects) over
    # orderId (per-client-session). When both miss, leave ``order_type``
    # as ``None`` — the truthfulness contract (ADR 0014 §3 / briefing)
    # forbids substituting a placeholder ("MKT" or otherwise) for a
    # field we cannot prove. The publisher's authoring path catches
    # ``UnauthorableEventError`` on the missing ``order_type`` and
    # skips this Fill with a structured log; an unauthored row is
    # honest, a placeholder row is not.
    trade: object | None = None
    if perm_id_raw:
        trade = trades_by_perm_id.get(int(perm_id_raw))
    if trade is None and order_id_raw is not None:
        try:
            trade = trades_by_order_id.get(int(order_id_raw))
        except (TypeError, ValueError):
            trade = None
    order_type = event_order_type(trade)

    # Commission rides on the fill once IBKR reports it (a beat after
    # the execution). None until reported — never a fabricated zero so
    # downstream COMMISSION_MISSING vs COMMISSION_DRIFT stays
    # distinguishable.
    commission_obj = getattr(fill, "commissionReport", None)
    fee = (
        getattr(commission_obj, "commission", None)
        if commission_obj is not None
        else None
    )

    exec_time = getattr(execution, "time", None)
    exec_time_ms = (
        int(exec_time.timestamp() * 1000) if exec_time is not None else None
    )

    shares = float(getattr(execution, "shares", 0.0) or 0.0)
    price = float(getattr(execution, "price", 0.0) or 0.0) or None
    cumulative_filled = float(getattr(execution, "cumQty", 0.0) or 0.0) or shares

    return IbkrOrderEvent(
        account_id=account_id,
        order_id=int(order_id_raw) if order_id_raw is not None else 0,
        perm_id=int(perm_id_raw) if perm_id_raw else None,
        con_id=int(getattr(contract, "conId", 0) or 0) or None,
        event_type="fill",
        status="Filled",
        order_ref=order_ref,
        symbol=str(symbol),
        side=side,  # type: ignore[arg-type]
        order_type=order_type,
        exec_id=str(exec_id) if exec_id else None,
        client_id=int(client_id_raw) if client_id_raw is not None else None,
        fill_quantity=shares,
        avg_fill_price=price,
        cumulative_filled=cumulative_filled,
        remaining=0.0,
        last_fill_price=price,
        exec_time_ms=exec_time_ms,
        fee=float(fee) if fee is not None else None,
        ibkr_evidence=build_execution_recovery_evidence(
            fill,
            contract,
            execution,
            commission_obj,
        ),
        ts_ms=now_ms_utc(),
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
    last_error_seq = 0

    try:
        while True:
            # ib_async's ``trades()`` is an in-memory cache that never raises
            # when the connection drops, so without this gate a mid-stream
            # disconnect would freeze the cache and we'd poll it forever,
            # silently missing fills while the engine keeps submitting orders.
            client.require_live()
            trades = list(client.ib.trades())
            error_events, last_error_seq = read_order_error_events(
                client=client,
                trades=trades,
                account_id=account_id,
                after_seq=last_error_seq,
            )
            for error_event in error_events:
                yield error_event
            for trade in trades:
                if not order_belongs_to_account(trade, account_id):
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
