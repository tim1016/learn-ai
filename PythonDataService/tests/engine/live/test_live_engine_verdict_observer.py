"""Phase 7B / VCR-0010 — mid-session broker safety verdict observer tests.

Phase 7B order-block (PR #538) refused submission on the next
``submit_pending_orders`` call. This module covers the proactive
observer the engine runs at the top of each bar iteration: when
``verdict_provider`` returns a non-paper-only verdict, the engine writes
``halt.flag`` to ``output_dir``, clears pending orders, and raises
``BrokerSafetyVerdictTransitionHaltError`` before reaching the submit
path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import (
    BrokerSafetyVerdictTransitionHaltError,
    LiveEngine,
)
from app.engine.strategy.base import Strategy
from tests.engine.live.fixtures.fake_broker import FakeBroker, iter_bars


def _bar(minute: int) -> TradeBar:
    start = datetime(2026, 5, 4, 14, 0, tzinfo=UTC) + timedelta(minutes=minute)
    return TradeBar(
        symbol="SPY",
        time=start,
        end_time=start + timedelta(minutes=1),
        open=Decimal("500"),
        high=Decimal("500"),
        low=Decimal("500"),
        close=Decimal("500"),
        volume=100,
    )


class _GoLongStrategy(Strategy):
    """Buys SPY on the first consolidated bar — used to prove that a
    verdict-halted engine drops the order before it reaches the broker."""

    def __init__(self) -> None:
        super().__init__()
        self._bar_count = 0

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        assert self.ctx is not None
        self._bar_count += 1
        if self._bar_count == 1:
            self.ctx.set_holdings("SPY", 1.0)


def _no_strategy() -> _GoLongStrategy:
    return _GoLongStrategy()


@pytest.mark.asyncio
async def test_engine_halts_on_unsafe_verdict_before_submit_vcr_0010(
    tmp_path: Path,
) -> None:
    """Phase 7B mid-session observer — ``unsafe`` verdict on the first
    bar halts the run BEFORE any broker submission."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: "unsafe",
    )

    with pytest.raises(BrokerSafetyVerdictTransitionHaltError) as exc:
        await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(5)]))

    assert exc.value.verdict == "unsafe"
    # Broker stays untouched — order never made it to the wire.
    assert broker.orders == []
    # halt.flag persisted for the failure list / runbook.
    halt_path = tmp_path / "halt.flag"
    assert halt_path.exists()
    payload = halt_path.read_text(encoding="utf-8")
    assert "BROKER_SAFETY_VERDICT_TRANSITION_HALT" in payload
    assert "verdict=unsafe" in payload


@pytest.mark.asyncio
async def test_engine_halts_on_unknown_verdict_vcr_0010(tmp_path: Path) -> None:
    """``unknown`` is start- and submit-blocking outside the diagnostic
    path. The mid-session observer halts on it the same way it halts on
    ``unsafe``."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: "unknown",
    )

    with pytest.raises(BrokerSafetyVerdictTransitionHaltError) as exc:
        await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(3)]))

    assert exc.value.verdict == "unknown"
    assert broker.orders == []
    assert (tmp_path / "halt.flag").exists()


@pytest.mark.asyncio
async def test_engine_proceeds_on_paper_only_verdict(tmp_path: Path) -> None:
    """A consistently ``paper-only`` verdict is permissive — the run
    proceeds and the strategy's BUY hits the broker."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: "paper-only",
    )

    await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(5)]))

    assert len(broker.orders) >= 1
    assert not (tmp_path / "halt.flag").exists()


@pytest.mark.asyncio
async def test_engine_proceeds_when_verdict_provider_returns_none(
    tmp_path: Path,
) -> None:
    """A provider that returns ``None`` (broker not yet probed) does NOT
    halt — the observer only fires on a positive non-paper-only signal."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: None,
    )

    await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(5)]))

    assert len(broker.orders) >= 1


@pytest.mark.asyncio
async def test_engine_proceeds_when_no_verdict_provider_set(tmp_path: Path) -> None:
    """Backward-compat: a run constructed without a verdict provider
    behaves exactly as it did before Phase 7B."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )

    await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(5)]))

    assert len(broker.orders) >= 1


@pytest.mark.asyncio
async def test_engine_halts_on_transition_partway_through_run(
    tmp_path: Path,
) -> None:
    """Verdict provider that returns ``paper-only`` on the first bar and
    ``unsafe`` on subsequent bars: the engine submits the first batch,
    then halts when the next bar observes the transition."""
    broker = FakeBroker()
    state = {"calls": 0}

    def _provider() -> str:
        state["calls"] += 1
        return "paper-only" if state["calls"] == 1 else "unsafe"

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=_provider,
    )

    with pytest.raises(BrokerSafetyVerdictTransitionHaltError) as exc:
        await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(5)]))

    # The transition happens on the second bar — the strategy hasn't
    # emitted an order yet (consolidator fires on second bar) so the
    # halt catches it cleanly before any submission.
    assert exc.value.verdict == "unsafe"


# ─────────────────── Resume guard #1 snapshot ──────────────────────


@pytest.mark.asyncio
async def test_engine_writes_verdict_snapshot_on_paper_only_observation(
    tmp_path: Path,
) -> None:
    """Phase 7B Resume guard #1 (VCR-0010) — every verdict check writes
    ``verdict_snapshot.json`` so ``cmd_resume`` can consult the engine's
    last reading even after the engine exits. A paper-only observation
    must produce a snapshot with ``verdict == "paper-only"``."""
    import json

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: "paper-only",
    )

    await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(3)]))

    snapshot_path = tmp_path / "verdict_snapshot.json"
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["verdict"] == "paper-only"
    assert isinstance(snapshot["observed_at_ms_utc"], int)
    assert snapshot["observed_at_ms_utc"] > 0


@pytest.mark.asyncio
async def test_engine_writes_verdict_snapshot_on_unsafe_observation(
    tmp_path: Path,
) -> None:
    """Phase 7B Resume guard #1 — an unsafe verdict halts the engine,
    but the snapshot is written BEFORE the halt so ``cmd_resume`` finds
    the non-paper-only reading and refuses to flip RUNNING."""
    import json

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        verdict_provider=lambda: "unsafe",
    )

    with pytest.raises(BrokerSafetyVerdictTransitionHaltError):
        await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(3)]))

    snapshot_path = tmp_path / "verdict_snapshot.json"
    assert snapshot_path.exists()
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["verdict"] == "unsafe"


@pytest.mark.asyncio
async def test_engine_writes_no_snapshot_when_no_verdict_provider(
    tmp_path: Path,
) -> None:
    """Backward-compat: a run without a verdict provider does not write
    a snapshot. cmd_resume's missing-snapshot fallback then lets the
    Resume go through (the engine has not yet observed any verdict)."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )

    await engine.run(_no_strategy(), iter_bars([_bar(m) for m in range(3)]))

    assert not (tmp_path / "verdict_snapshot.json").exists()
