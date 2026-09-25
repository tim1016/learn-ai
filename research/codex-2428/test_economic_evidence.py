"""Isolated economic-evidence probes; no broker calls or real account state."""
from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.sqlite import repository as repository_module
from app.broker.alpaca import adapter
from app.broker.alpaca.clerk.sqlite.economic_projection import (
    EconomicProjectionUnavailable,
    MarketMark,
    SqliteEconomicProjectionReader,
)
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_order_evidence
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from tests.broker.alpaca.clerk.sqlite.conftest import _broker_order_fixture
from tests.broker.alpaca.clerk.sqlite.test_economic_projection import (
    _repository, _append_slice, _SID,
)
from tests.broker.alpaca.clerk.sqlite.test_reconcile import _FakeRead, _FakeTrade, _position


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "quantity,price,conflicting",
    [(10.0, 100.0, False), (10.0, 90.0, True), (8.0, 100.0, False)],
    ids=["matching-control", "price-restatement", "quantity-drift-control"],
)
async def test_current_aggregate_conflict_cannot_keep_complete_economic_evidence(
    tmp_path, quantity, price, conflicting,
):
    repo, accepted = _repository(tmp_path)
    try:
        now = repo.clock()
        _append_slice(repo, accepted, execution_id="review-original", side="BUY",
                      quantity=10.0, price=100.0, occurred_at_ms=now)
        first = _broker_order_fixture(
            accepted.order_ref, symbol="GOOGL", status="filled", quantity=10,
            filled_quantity=10, filled_avg_price=100,
        ).model_copy(update={"updated_at_ms": now})
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=first)
        latest = first.model_copy(update={
            "filled_quantity": quantity, "filled_avg_price": price, "updated_at_ms": now + 1,
        })
        fold_order_evidence(repo, effect_operation_id=accepted.effect_operation_id, order=latest)
        position = _position("GOOGL", quantity=quantity).model_copy(update={
            "average_entry_price": price, "cost_basis": price * quantity,
            "observed_at_ms": now + 1,
        })
        reconciled = await reconcile_account(
            repo, read=_FakeRead(orders=[latest], positions=[position]),
            trade=_FakeTrade(lookup_result=latest),
        )
        assert reconciled.verdict == ("position_drift" if quantity != 10 else "clean")
        with_reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            try:
                snapshot = with_reader.bot_economic_snapshot(
                    _SID, session_window=None,
                    marks={"GOOGL": MarketMark(price=110.0, observed_at_ms=now + 1)},
                )
            except EconomicProjectionUnavailable:
                assert conflicting
                return
        finally:
            with_reader.close()
        assert snapshot is not None
        assert snapshot.exposure == {"GOOGL": 10.0}
        assert snapshot.open_pnl == 100.0
        assert snapshot.fee_fidelity == "not_reported"
        if conflicting:
            assert snapshot.execution_coverage == "incomplete", (
                "Fresh contradictory broker total was ignored while old economics stayed complete: "
                f"quantity={quantity}, average={price}, snapshot={snapshot}"
            )
        else:
            assert snapshot.execution_coverage == "complete"
    finally:
        repo.close()


@pytest.mark.parametrize("event", ["trade_bust", "trade_correct"])
def test_unsupported_event_names_are_refused_at_the_adapter(event):
    with pytest.raises(ValueError, match="Unrecognized"):
        adapter.from_alpaca_trade_update({"event": event, "timestamp_ms": 1})
