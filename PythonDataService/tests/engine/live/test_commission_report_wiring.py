"""Tests for the poll-based commissionReport → ExecutionRow.fee wiring (PRD-B).

Honors the existing no-eventkit design in ``orders.py``: the commission is
read off the polled ``Fill.commissionReport`` object rather than via a
``commissionReportEvent`` subscription. A fill whose commission has not yet
been reported carries ``fee = None`` (→ COMMISSION_MISSING downstream),
never a fabricated zero.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.broker.ibkr import orders as orders_module
from app.broker.ibkr.models import IbkrOrderEvent
from app.engine.execution.order import Direction, OrderEvent
from app.engine.live.live_engine import LiveEngine


def _trade(*, order_id: int = 100, account: str = "DU1234567"):
    contract = SimpleNamespace(secType="STK", conId=12345, symbol="SPY")
    order = SimpleNamespace(account=account, orderId=order_id, permId=order_id + 1000)
    order_status = SimpleNamespace(
        status="Filled", filled=10.0, remaining=0.0, avgFillPrice=100.0
    )
    return SimpleNamespace(contract=contract, order=order, orderStatus=order_status)


def _fill(*, exec_id="ex-1", shares=10.0, price=100.0, commission=1.37, currency="USD"):
    execution = SimpleNamespace(execId=exec_id, clientId=7, shares=shares, price=price)
    commission_report = (
        None
        if commission is None
        else SimpleNamespace(commission=commission, currency=currency)
    )
    return SimpleNamespace(execution=execution, commissionReport=commission_report)


def test_fill_event_captures_commission_from_commission_report() -> None:
    event = orders_module._fill_to_event(_trade(), _fill(commission=1.37), "DU1234567")

    assert event.fee == 1.37


def test_fill_event_fee_is_none_when_commission_not_yet_reported() -> None:
    event = orders_module._fill_to_event(_trade(), _fill(commission=None), "DU1234567")

    assert event.fee is None  # not a fabricated 0.0


def _engine_stub():
    # _convert_ibkr_fill only reads self._order_meta — call it unbound on a stub.
    meta = SimpleNamespace(signed_qty=10, symbol="SPY", tag="entry")
    return SimpleNamespace(_order_meta={100: meta})


def _ibkr_fill_event(*, fee: float | None) -> IbkrOrderEvent:
    return IbkrOrderEvent(
        account_id="DU1234567",
        order_id=100,
        event_type="fill",
        fill_quantity=10.0,
        last_fill_price=100.0,
        fee=fee,
        ts_ms=1_700_000_000_000,
    )


def test_engine_conversion_threads_reported_commission() -> None:
    oe = LiveEngine._convert_ibkr_fill(_engine_stub(), _ibkr_fill_event(fee=1.37))

    # Portfolio-facing fee carries the real commission; recorded_fee mirrors it.
    assert oe.fee == Decimal("1.37")
    assert oe.recorded_fee == Decimal("1.37")


def test_engine_conversion_unreported_commission_is_none_not_zero() -> None:
    oe = LiveEngine._convert_ibkr_fill(_engine_stub(), _ibkr_fill_event(fee=None))

    # Portfolio cannot deduct an unknown fee → 0; the artifact records "unknown".
    assert oe.fee == Decimal("0")
    assert oe.recorded_fee is None


def _order_event(*, recorded_fee: Decimal | None, fee: Decimal) -> OrderEvent:
    return OrderEvent(
        order_id=100,
        symbol="SPY",
        time=datetime(2026, 5, 29, 14, 0, tzinfo=UTC),
        fill_price=Decimal("100.00"),
        fill_quantity=10,
        direction=Direction.LONG,
        fee=fee,
        recorded_fee=recorded_fee,
    )


def _capture_writers():
    rows: list = []
    writers = SimpleNamespace(
        executions=SimpleNamespace(append_row=rows.append)
    )
    return writers, rows


def test_write_execution_records_real_fee_when_reported() -> None:
    writers, rows = _capture_writers()
    engine = SimpleNamespace(_account_id="DU1234567")

    LiveEngine._write_execution(
        engine, writers, _order_event(recorded_fee=Decimal("1.37"), fee=Decimal("1.37"))
    )

    assert rows[0].fee == 1.37


def test_write_execution_records_nan_when_commission_unknown() -> None:
    writers, rows = _capture_writers()
    engine = SimpleNamespace(_account_id="DU1234567")

    LiveEngine._write_execution(
        engine, writers, _order_event(recorded_fee=None, fee=Decimal("0"))
    )

    # Unknown commission → NaN in the artifact, not a fabricated 0.0.
    assert math.isnan(rows[0].fee)


def test_commission_observed_counts_only_captured_fees() -> None:
    from app.engine.live.artifacts import commission_observed_count

    # Two captured commissions, one not-yet-reported (NaN) → COMMISSION_OBSERVED = 2.
    assert commission_observed_count([1.37, float("nan"), 2.00]) == 2
    assert commission_observed_count([]) == 0
