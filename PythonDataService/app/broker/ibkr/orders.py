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

import logging
from datetime import UTC, datetime

from app.broker.ibkr.client import BrokerError, IbkrClient, _is_paper_account
from app.broker.ibkr.contracts import expiry_ms_to_yyyymmdd
from app.broker.ibkr.models import IbkrOrderAck, IbkrOrderSpec

logger = logging.getLogger(__name__)


class OrderRefusedError(BrokerError):
    """Order placement was refused by a safety check before reaching IBKR."""


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _enforce_paper_safety(client: IbkrClient, spec: IbkrOrderSpec) -> str:
    """Run all four paper-mode safety checks. Returns the validated account id.

    Any failure raises ``OrderRefusedError`` *before* any contract or order
    is constructed. We never want to come close to placing an order under a
    bad combination.
    """
    settings = client.settings
    account_id = client.connected_account
    if account_id is None:
        raise OrderRefusedError("No account id on connected client.")

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
    return order


async def place_paper_order(
    client: IbkrClient,
    spec: IbkrOrderSpec,
) -> IbkrOrderAck:
    """Place one paper order via ib_async.

    Steps:
    1. Run all four safety layers; refuse on any failure.
    2. Build and qualify the contract (so we get a ``conId``).
    3. Build the order. Submit via ``IB.placeOrder``.
    4. Return ``IbkrOrderAck`` with the broker-assigned ``orderId``.

    Status updates after this point arrive via Phase 3b's order event
    stream — this function doesn't wait for fills.
    """
    client.require_connected()
    account_id = _enforce_paper_safety(client, spec)

    contract = _build_contract(spec)
    qualified = await client.ib.qualifyContractsAsync(contract)
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
    # via events; we capture the snapshot now.
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
