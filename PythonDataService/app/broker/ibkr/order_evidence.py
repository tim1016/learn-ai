"""IBKR order evidence builders.

The order API exposes a curated domain model, but audit/UI diagnostics need the
IBKR request, callback, and object payloads that produced that model. This
module keeps that evidence construction out of ``orders.py`` and snapshots only
the ib_async object kinds the order path currently captures.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import SimpleNamespace

from pydantic import JsonValue

from app.broker.ibkr.models import (
    IbkrApiCallbackName,
    IbkrApiRequestEvidence,
    IbkrApiResponseEvidence,
    IbkrObjectSnapshot,
    IbkrTradeEvidence,
    IbkrTradeSnapshot,
)


def build_place_order_evidence(
    contract: object,
    order: object,
    trade: object,
) -> IbkrTradeEvidence:
    return IbkrTradeEvidence(
        request=IbkrApiRequestEvidence(
            call="placeOrder",
            params={
                "contract": _snapshot_fields(contract),
                "order": _snapshot_fields(order),
            },
        ),
        response=IbkrApiResponseEvidence(callback="openOrder", fields={}),
        contract=snapshot_contract(contract),
        order=snapshot_order(order),
        order_status=snapshot_order_status(getattr(trade, "orderStatus", None)),
        trade=snapshot_trade(trade),
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


def cancel_order_request_evidence(order: object) -> IbkrApiRequestEvidence:
    return IbkrApiRequestEvidence(
        call="cancelOrder",
        params={
            "order": _snapshot_fields(order),
            "manualCancelOrderTime": None,
        },
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


def _object_snapshot(obj: object | None) -> IbkrObjectSnapshot | None:
    if obj is None:
        return None
    return IbkrObjectSnapshot(object_type=_object_type(obj), fields=_snapshot_fields(obj))


def _snapshot_fields(obj: object) -> dict[str, JsonValue]:
    return {
        key: _json_value(value)
        for key, value in _typed_fields(obj).items()
    }


def _typed_fields(obj: object) -> Mapping[str, object]:
    if is_dataclass(obj) and not isinstance(obj, type):
        return {field.name: getattr(obj, field.name) for field in fields(obj)}
    if isinstance(obj, SimpleNamespace):
        return vars(obj)
    if hasattr(obj, "__dict__"):
        return {
            key: value
            for key, value in vars(obj).items()
            if not key.startswith("_")
        }
    raise TypeError(
        f"Cannot snapshot unsupported IBKR evidence object {type(obj).__qualname__}"
    )


def _json_value(value: object) -> JsonValue:
    if value is None or isinstance(value, str | int | bool):
        return value
    if isinstance(value, float):
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "Infinity" if value > 0 else "-Infinity"
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return int(value.timestamp() * 1000)
    if isinstance(value, Enum):
        return _json_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _json_value(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item) for item in value]
    if isinstance(value, SimpleNamespace):
        return {key: _json_value(item) for key, item in vars(value).items()}
    if hasattr(value, "__dict__"):
        return {
            key: _json_value(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    raise TypeError(
        f"Cannot convert unsupported IBKR evidence value {type(value).__qualname__}"
    )


def _object_type(obj: object) -> str:
    cls = obj.__class__
    return f"{cls.__module__}.{cls.__qualname__}"
