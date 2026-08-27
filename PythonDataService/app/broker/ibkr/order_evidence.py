"""IBKR order evidence builders.

The order API exposes a curated domain model, but audit/UI diagnostics need the
IBKR request, callback, and object payloads that produced that model. This
module keeps that evidence construction out of ``orders.py`` and snapshots only
the ib_async object kinds the order path currently captures.
"""

from __future__ import annotations

from app.broker.ibkr.api_evidence import _object_snapshot
from app.broker.ibkr.models import (
    IbkrApiCallbackName,
    IbkrApiRequestEvidence,
    IbkrApiResponseEvidence,
    IbkrObjectSnapshot,
    IbkrTradeEvidence,
    IbkrTradeSnapshot,
)


def build_open_order_evidence(
    trade: object,
    *,
    request: IbkrApiRequestEvidence | None,
    response_callback: IbkrApiCallbackName,
) -> IbkrTradeEvidence:
    return IbkrTradeEvidence(
        request=request,
        response=IbkrApiResponseEvidence(callback=response_callback, fields={}),
        contract=snapshot_contract(getattr(trade, "contract", None)),
        order=snapshot_order(getattr(trade, "order", None)),
        order_status=snapshot_order_status(getattr(trade, "orderStatus", None)),
        trade=snapshot_trade(trade),
    )


def build_status_event_evidence(trade: object) -> IbkrTradeEvidence:
    return IbkrTradeEvidence(
        response=IbkrApiResponseEvidence(callback="orderStatus", fields={}),
        contract=snapshot_contract(getattr(trade, "contract", None)),
        order=snapshot_order(getattr(trade, "order", None)),
        order_status=snapshot_order_status(getattr(trade, "orderStatus", None)),
        trade=snapshot_trade(trade),
    )


def build_fill_event_evidence(
    trade: object,
    fill: object,
    execution: object | None,
    commission_report: object | None,
) -> IbkrTradeEvidence:
    return IbkrTradeEvidence(
        response=IbkrApiResponseEvidence(callback="execDetails", fields={}),
        contract=snapshot_contract(getattr(trade, "contract", None)),
        order=snapshot_order(getattr(trade, "order", None)),
        order_status=snapshot_order_status(getattr(trade, "orderStatus", None)),
        trade=snapshot_trade(trade),
        fill=snapshot_fill(fill),
        execution=snapshot_execution(execution),
        commission_report=snapshot_commission_report(commission_report),
    )


def build_execution_recovery_evidence(
    fill: object,
    contract: object,
    execution: object,
    commission_report: object | None,
) -> IbkrTradeEvidence:
    return IbkrTradeEvidence(
        request=IbkrApiRequestEvidence(call="reqExecutionsAsync", params={}),
        response=IbkrApiResponseEvidence(callback="execDetails", fields={}),
        contract=snapshot_contract(contract),
        fill=snapshot_fill(fill),
        execution=snapshot_execution(execution),
        commission_report=snapshot_commission_report(commission_report),
    )


def all_open_orders_request_evidence() -> IbkrApiRequestEvidence:
    return IbkrApiRequestEvidence(call="reqAllOpenOrders", params={})


def snapshot_contract(contract: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(contract)


def snapshot_order(order: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(order)


def snapshot_order_status(order_status: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(order_status)


def snapshot_fill(fill: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(fill)


def snapshot_execution(execution: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(execution)


def snapshot_commission_report(report: object | None) -> IbkrObjectSnapshot | None:
    return _object_snapshot(report)


def snapshot_trade(trade: object | None) -> IbkrTradeSnapshot | None:
    if trade is None:
        return None
    fills_out = [
        snap
        for fill in list(getattr(trade, "fills", []) or [])
        if (snap := snapshot_fill(fill)) is not None
    ]
    logs_out = [
        snap
        for row in list(getattr(trade, "log", []) or [])
        if (snap := _object_snapshot(row)) is not None
    ]
    advanced_error = getattr(trade, "advancedError", None)
    return IbkrTradeSnapshot(
        trade=_object_snapshot(trade),
        contract=snapshot_contract(getattr(trade, "contract", None)),
        order=snapshot_order(getattr(trade, "order", None)),
        order_status=snapshot_order_status(getattr(trade, "orderStatus", None)),
        fills=fills_out,
        log=logs_out,
        advanced_error=str(advanced_error) if advanced_error else None,
    )
