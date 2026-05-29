"""Shadow end-to-end smoke run (PRD-C integration test).

Drives a placeholder strategy through the SAME LiveEngine + the
NoSubmitBrokerAdapter + ShadowFillSimulator over a synthetic session.
Asserts: executions.parquet carries shadow_sim rows, the broker's
placeOrder is NEVER called, and no poisoned.flag is written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pandas as pd
import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import LiveEngine
from app.engine.live.no_submit_broker_adapter import NoSubmitBrokerAdapter
from app.engine.strategy.base import DecisionSnapshot, Strategy
from tests.engine.live.fixtures.fake_broker import iter_bars


def _bar(minute: int, close: str = "500") -> TradeBar:
    start = datetime(2026, 5, 4, 14, minute, tzinfo=UTC)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
    )


class _BuyOnceStrategy(Strategy):
    """Placeholder: enters once when flat; publishes a decision snapshot each bar."""

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        flat = not self.ctx.portfolio.get_position("SPY").quantity
        if flat and not self.ctx.portfolio.pending_orders:
            self.ctx.set_holdings("SPY", Decimal("1"))
        self.last_decision_snapshot = DecisionSnapshot(
            bar_close_ms=int(bar.end_time.timestamp() * 1000),
            ema5=1.0,
            ema10=2.0,
            rsi=50.0,
            signal="ENTER" if flat else "HOLD",
            intended_price=float(bar.close),
        )


@pytest.mark.asyncio
async def test_shadow_smoke_produces_shadow_sim_rows_without_submitting(tmp_path) -> None:
    ib = SimpleNamespace(placeOrder=MagicMock(), cancelOrder=MagicMock())
    adapter = NoSubmitBrokerAdapter(
        ib,
        strategy_instance_id="placeholder_shadow_1min",
        bot_order_namespace="learn-ai/placeholder_shadow_1min/abc",
    )
    engine = LiveEngine(
        None,
        LiveConfig(force_flat_at=None),
        broker=adapter,
        output_dir=tmp_path,
        run_mode="shadow",
        bar_source="ibkr_paper_delayed",
    )

    bars = [_bar(m) for m in range(30, 36)]  # 6 one-minute bars
    await engine.run(_BuyOnceStrategy(), iter_bars(bars))

    # The broker order-submission path was never reached.
    ib.placeOrder.assert_not_called()
    ib.cancelOrder.assert_not_called()

    # executions.parquet exists with only shadow_sim rows.
    execs = pd.read_parquet(tmp_path / "executions.parquet")
    assert len(execs) >= 1
    assert (execs["execution_source"] == "shadow_sim").all()
    assert execs["fill_model"].iloc[0] == "NEXT_BAR_OPEN"
    assert execs["source_bar_close_ms"].notna().all()

    # No poisoned.flag — shadow ran cleanly.
    assert not (tmp_path / "poisoned.flag").exists()
