"""Phase 3 reconnect re-validation / VCR-0006 follow-up.

The PRD §3 "Re-check on every reconnect" contract: when ``IbkrClient``
reports a connectivity-lost-then-restored cycle, the engine re-runs
``account_identity.verify_account_match`` against the (now-restored)
``connected_account``. Mismatch → halt.flag + raise + pending orders
cleared. Match → connection_epoch increments so the failure list / a
future ``session_metadata.json`` write can distinguish reconnects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.live_engine import (
    LiveEngine,
    ReconnectAccountMismatchHaltError,
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


class _NoopStrategy(Strategy):
    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=1), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        return None


@dataclass
class _FakeSettings:
    mode: str = "paper"
    port: int = 7497


@dataclass
class _FakeReconnectClient:
    """Just enough surface for the engine: settings (mode+port for the
    start-time paper validator), connected_account, and the three
    reconnect-relevant fields. Tests mutate the latter three between bars
    to script reconnect lifecycles."""

    connectivity_lost_count: int = 0
    connection_lost: bool = False
    connected_account: str = "DU123"
    settings: _FakeSettings = field(default_factory=_FakeSettings)
    ib: object = field(default_factory=object)


@pytest.mark.asyncio
async def test_engine_halts_on_reconnect_to_different_account_vcr_0006(
    tmp_path: Path,
) -> None:
    """Load-bearing positive case: the broker reconnects to a different
    account → halt.flag + raise + connection_epoch bumped."""
    broker = FakeBroker()
    client = _FakeReconnectClient(
        connectivity_lost_count=0,
        connection_lost=False,
        connected_account="DU123",
    )
    engine = LiveEngine(
        client,  # type: ignore[arg-type]
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )

    # Script: between bar 1 and bar 2 the client reports a reconnect to a
    # different account. The verdict provider isn't used here — this gate
    # runs strictly on the IbkrClient connectivity surface.
    bar_count = {"n": 0}
    original_check = engine._check_reconnect_revalidation

    def _bump_then_check(portfolio):  # type: ignore[no-untyped-def]
        bar_count["n"] += 1
        if bar_count["n"] == 2:
            client.connectivity_lost_count = 1
            client.connection_lost = False
            client.connected_account = "DU999"  # different account
        original_check(portfolio)

    engine._check_reconnect_revalidation = _bump_then_check  # type: ignore[assignment]

    with pytest.raises(ReconnectAccountMismatchHaltError) as exc:
        await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(5)]))

    assert exc.value.ledger_account_id == "DU123"
    assert exc.value.connected_account == "DU999"
    assert exc.value.connection_epoch == 1
    halt_path = tmp_path / "halt.flag"
    assert halt_path.exists()
    payload = halt_path.read_text(encoding="utf-8")
    assert "RECONNECT_ACCOUNT_MISMATCH_HALT" in payload
    assert "DU123" in payload
    assert "DU999" in payload


@pytest.mark.asyncio
async def test_engine_does_not_halt_on_reconnect_to_same_account(
    tmp_path: Path,
) -> None:
    """Reconnect that lands on the SAME account → no halt, connection_epoch
    bumps to 1 (for the first observed reconnect)."""
    broker = FakeBroker()
    client = _FakeReconnectClient(
        connectivity_lost_count=0,
        connection_lost=False,
        connected_account="DU123",
    )
    engine = LiveEngine(
        client,  # type: ignore[arg-type]
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    bar_count = {"n": 0}
    original_check = engine._check_reconnect_revalidation

    def _bump_then_check(portfolio):  # type: ignore[no-untyped-def]
        bar_count["n"] += 1
        if bar_count["n"] == 2:
            client.connectivity_lost_count = 1
            client.connection_lost = False  # restored to same account
        original_check(portfolio)

    engine._check_reconnect_revalidation = _bump_then_check  # type: ignore[assignment]

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(5)]))

    assert engine._connection_epoch == 1
    assert not (tmp_path / "halt.flag").exists()


@pytest.mark.asyncio
async def test_engine_does_not_halt_when_connection_lost_not_yet_restored(
    tmp_path: Path,
) -> None:
    """``connectivity_lost_count`` has advanced but ``connection_lost`` is
    still True → no halt yet. Waits for restore before re-validating."""
    broker = FakeBroker()
    client = _FakeReconnectClient(
        connectivity_lost_count=1,
        connection_lost=True,
        connected_account="DU123",
    )
    engine = LiveEngine(
        client,  # type: ignore[arg-type]
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(3)]))

    # connectivity_lost_count never observed via the reconnect path because
    # the connection didn't restore. Snapshot stays at 0 so the next genuine
    # restore is still observable.
    assert engine._connection_epoch == 0
    assert not (tmp_path / "halt.flag").exists()


@pytest.mark.asyncio
async def test_engine_does_not_halt_when_no_reconnect_observed(
    tmp_path: Path,
) -> None:
    """A run with no connectivity loss at all → no halt, epoch stays at 0."""
    broker = FakeBroker()
    client = _FakeReconnectClient()
    engine = LiveEngine(
        client,  # type: ignore[arg-type]
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(3)]))

    assert engine._connection_epoch == 0


@pytest.mark.asyncio
async def test_engine_does_not_revalidate_repeatedly_on_same_reconnect(
    tmp_path: Path,
) -> None:
    """Multiple bars after a single restore must NOT bump the epoch
    multiple times — the engine snapshots the count after a successful
    re-validation so the same restore is observed exactly once."""
    broker = FakeBroker()
    client = _FakeReconnectClient(
        connectivity_lost_count=0,
        connection_lost=False,
        connected_account="DU123",
    )
    engine = LiveEngine(
        client,  # type: ignore[arg-type]
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    bar_count = {"n": 0}
    original_check = engine._check_reconnect_revalidation

    def _bump_then_check(portfolio):  # type: ignore[no-untyped-def]
        bar_count["n"] += 1
        if bar_count["n"] == 2:
            client.connectivity_lost_count = 1
            client.connection_lost = False
        original_check(portfolio)

    engine._check_reconnect_revalidation = _bump_then_check  # type: ignore[assignment]

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(5)]))

    # Only ONE reconnect was observed across the 5 bars.
    assert engine._connection_epoch == 1


@pytest.mark.asyncio
async def test_engine_handles_multiple_reconnects(tmp_path: Path) -> None:
    """Two distinct reconnects within a run → epoch advances to 2."""
    broker = FakeBroker()
    client = _FakeReconnectClient(
        connectivity_lost_count=0,
        connection_lost=False,
        connected_account="DU123",
    )
    engine = LiveEngine(
        client,  # type: ignore[arg-type]
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
    )
    bar_count = {"n": 0}
    original_check = engine._check_reconnect_revalidation

    def _bump_then_check(portfolio):  # type: ignore[no-untyped-def]
        bar_count["n"] += 1
        if bar_count["n"] == 2:
            client.connectivity_lost_count = 1
            client.connection_lost = False
        elif bar_count["n"] == 4:
            client.connectivity_lost_count = 2
            client.connection_lost = False
        original_check(portfolio)

    engine._check_reconnect_revalidation = _bump_then_check  # type: ignore[assignment]

    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(6)]))

    assert engine._connection_epoch == 2
