"""The shared synthesized-order ledger both no-submit worlds write (ADR 0059 D2)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.synthesized_orders import (
    SYNTHESIZED_ORDER_LEDGER_FILENAME,
    SynthesizedAnchor,
    SynthesizedBarBindingError,
    SynthesizedLedgerTransactionError,
    SynthesizedOrderLedger,
    SynthesizedOrderRecord,
    project_positions,
)
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, OrderSide, OrderType, TimeInForce
from app.marketdata.feed import MarketDataBar
from app.services.source_bar_ledger import SourceBarLedger


def _bar(symbol: str = "SPY", *, start_ms: int = 1_000, close: str = "100.00") -> MarketDataBar:
    return MarketDataBar(
        feed_id="fixture",
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
        fetched_at_ms=start_ms + 60_000,
        session_phase="RTH",
    )


def _order(client_order_id: str, *, side: str = "buy", filled: float = 1.0, price: float | None = 100.0) -> BrokerOrder:
    return BrokerOrder(
        broker="synthetic",
        order_id=f"sim-order:{client_order_id}",
        client_order_id=client_order_id,
        symbol="SPY",
        asset_class="us_equity",
        side=side,
        order_type="market",
        time_in_force="day",
        quantity=filled,
        filled_quantity=filled,
        limit_price=None,
        stop_price=None,
        filled_avg_price=price,
        status="filled",
        submitted_at_ms=1,
        created_at_ms=1,
        updated_at_ms=1,
        filled_at_ms=1,
        canceled_at_ms=None,
        expired_at_ms=None,
        observed_at_ms=1,
    )


def _ledger(tmp_path: Path) -> tuple[SynthesizedOrderLedger, SourceBarLedger]:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:ema-1")
    return SynthesizedOrderLedger.beside_source_bars(account_id="sim:ema-1", source_bars=bars, label="test"), bars


def test_ledger_file_name_is_the_sim_worlds_existing_file(tmp_path: Path) -> None:
    ledger, bars = _ledger(tmp_path)
    assert ledger.path == bars.path.with_name(SYNTHESIZED_ORDER_LEDGER_FILENAME)
    assert SYNTHESIZED_ORDER_LEDGER_FILENAME == "simulated_orders.jsonl"


def test_ledger_refuses_a_source_ledger_from_another_account(tmp_path: Path) -> None:
    bars = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:other")
    with pytest.raises(SynthesizedBarBindingError, match="share one account authority"):
        SynthesizedOrderLedger.beside_source_bars(account_id="sim:ema-1", source_bars=bars, label="test")


def test_bar_from_another_authority_is_refused_by_the_verifier(tmp_path: Path) -> None:
    ledger, _bars = _ledger(tmp_path)
    other = SourceBarLedger(artifacts_root=tmp_path, account_id="sim:other").append(_bar(), run_id="run-1")
    with pytest.raises(SynthesizedBarBindingError, match="different account authority"):
        ledger.bind_evaluated_bar("learn-ai/ema-1/v1:a", other)


def test_records_round_trip_with_leg_and_anchor(tmp_path: Path) -> None:
    ledger, bars = _ledger(tmp_path)
    retained = bars.append(_bar(), run_id="run-1")
    leg = BrokerOrderLeg(symbol="SPY", side=OrderSide.BUY, quantity=1.0, order_type=OrderType.MARKET, time_in_force=TimeInForce.DAY)
    anchor = SynthesizedAnchor(
        fill_model="decision_bar_close",
        evidence_account_id=retained.account_id,
        provider=retained.provider,
        bar_identity=retained.bar_identity,
        bar_ref=retained.bar_ref,
        decision_bar_start_ms=retained.start_ms,
        decision_bar_end_ms=retained.end_ms,
        fill_bar_ref=retained.bar_ref,
    )
    with ledger.transaction() as records:
        ledger.append_locked(records, order=_order("learn-ai/ema-1/v1:a"), leg=leg, anchor=anchor)

    record = ledger.latest_records()["learn-ai/ema-1/v1:a"]
    assert record.leg == leg
    assert record.anchor == anchor
    assert ledger.latest_orders()[0].client_order_id == "learn-ai/ema-1/v1:a"


def test_an_append_outside_the_transaction_is_refused(tmp_path: Path) -> None:
    """The ``records`` list is only the transaction's read while the lock is held."""
    ledger, _bars = _ledger(tmp_path)
    with pytest.raises(SynthesizedLedgerTransactionError, match="inside the ledger's transaction"):
        ledger.append_locked([], order=_order("learn-ai/ema-1/v1:a"))

    with ledger.transaction() as records:
        ledger.append_locked(records, order=_order("learn-ai/ema-1/v1:a"))
    # The flag is released with the lock, so the next unguarded append is refused too.
    with pytest.raises(SynthesizedLedgerTransactionError):
        ledger.append_locked(records, order=_order("learn-ai/ema-1/v1:b"))


def test_pre_anchor_rows_still_parse(tmp_path: Path) -> None:
    ledger, _bars = _ledger(tmp_path)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    row = SynthesizedOrderRecord(seq=1, order=_order("learn-ai/ema-1/v1:old"))
    ledger.path.write_text(row.model_dump_json(exclude={"leg", "anchor"}) + "\n", encoding="utf-8")

    assert ledger.latest_orders()[0].client_order_id == "learn-ai/ema-1/v1:old"
    assert ledger.latest_records()["learn-ai/ema-1/v1:old"].anchor is None


def test_bound_bar_is_consumed_once_and_verified(tmp_path: Path) -> None:
    ledger, bars = _ledger(tmp_path)
    retained = bars.append(_bar(), run_id="run-1")
    ledger.bind_evaluated_bar("learn-ai/ema-1/v1:a", retained)

    assert ledger.consume_bound_bar("learn-ai/ema-1/v1:a") == retained
    assert ledger.consume_bound_bar("learn-ai/ema-1/v1:a") is None
    with pytest.raises(SynthesizedBarBindingError, match="does not match the submitted order symbol"):
        ledger.verified_retained_bar(retained, symbol="QQQ")


def test_project_positions_is_the_average_cost_fold() -> None:
    orders = [
        _order("a", side="buy", filled=2.0, price=100.0),
        _order("b", side="sell", filled=1.0, price=110.0),
        _order("c", side="buy", filled=1.0, price=120.0),
    ]
    quantity, notional = project_positions(orders)["SPY"]
    assert quantity == 2.0
    assert notional == pytest.approx(220.0, abs=0.0)
