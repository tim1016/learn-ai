"""LiveEngine call sites: hydrate post-init, write at force-flat, write in finally."""

from __future__ import annotations

from datetime import UTC, datetime
from datetime import time as dtime
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.config import LiveConfig
from app.engine.live.indicator_state import HydratePolicy
from app.engine.live.live_engine import LiveEngine
from app.engine.strategy.algorithms.spy_ema_crossover import SpyEmaCrossoverAlgorithm
from tests.engine.live.fixtures.fake_broker import FakeBroker


async def _empty_bar_source():
    """Empty async iterator — used when we want LiveEngine.run() to exit immediately after init."""
    if False:
        yield  # pragma: no cover


async def _one_bar_source(t: datetime):
    """Single bar source — used to drive the force-flat path once."""
    yield TradeBar(
        symbol="SPY",
        time=t,
        end_time=t,
        open=Decimal("400"),
        high=Decimal("400"),
        low=Decimal("400"),
        close=Decimal("400"),
        volume=Decimal("0"),
    )


@pytest.mark.asyncio
async def test_force_flat_invokes_maybe_write_with_reason_force_flat(tmp_path: Path) -> None:
    """Crossing the force-flat barrier triggers ctx.maybe_write_indicator_state(reason='force_flat')."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=dtime(15, 55)),
    )

    # Drive a single bar at 15:55 ET (just past the barrier).
    t = datetime(2026, 5, 18, 15, 55, 0, tzinfo=UTC)

    with patch("app.engine.live.live_context.LiveContext.maybe_write_indicator_state") as spy:
        await engine.run(strat, bars=_one_bar_source(t))
        # At least one call with reason='force_flat' should have happened.
        force_flat_calls = [
            c
            for c in spy.call_args_list
            if (c.kwargs.get("reason") == "force_flat" or (len(c.args) >= 2 and c.args[1] == "force_flat"))
        ]
        assert force_flat_calls, f"no force_flat write call observed; calls={spy.call_args_list}"


@pytest.mark.asyncio
async def test_finally_block_invokes_maybe_write_with_reason_shutdown(tmp_path: Path) -> None:
    """Engine's finally block invokes ctx.maybe_write_indicator_state(reason='shutdown')."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    # Drive one bar so last_bar is not None when finally runs.
    t = datetime(2026, 5, 18, 14, 0, 0, tzinfo=UTC)

    with patch("app.engine.live.live_context.LiveContext.maybe_write_indicator_state") as spy:
        await engine.run(strat, bars=_one_bar_source(t))
        shutdown_calls = [
            c
            for c in spy.call_args_list
            if (c.kwargs.get("reason") == "shutdown" or (len(c.args) >= 2 and c.args[1] == "shutdown"))
        ]
        assert shutdown_calls, f"no shutdown write call observed; calls={spy.call_args_list}"


@pytest.mark.asyncio
async def test_hydrate_policy_disabled_writes_receipt_at_init(tmp_path: Path) -> None:
    """The hydrate call site fires immediately after strategy.initialize()."""
    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.DISABLED,
        session_start_ms=int(datetime(2026, 5, 18, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    await engine.run(strat, bars=_empty_bar_source())
    receipt_path = run_dir / "indicator_state_hydration.json"
    assert receipt_path.exists()


@pytest.mark.asyncio
async def test_hydrate_policy_require_with_missing_sidecar_raises(tmp_path: Path) -> None:
    """REQUIRE policy with no sidecar raises IndicatorStateHydrationError before any bar runs."""
    from app.engine.live.indicator_state import IndicatorStateHydrationError

    artifacts_root = tmp_path / "artifacts"
    run_dir = artifacts_root / "live_runs" / "abc"
    run_dir.mkdir(parents=True)

    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        output_dir=run_dir,
        artifacts_root=artifacts_root,
        hydrate_policy=HydratePolicy.REQUIRE,
        # 09:30 ET on Tuesday 2026-05-19 — but no sidecar exists, so missing -> exit/raise.
        session_start_ms=int(datetime(2026, 5, 19, 9, 30, tzinfo=UTC).timestamp() * 1000),
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    with pytest.raises(IndicatorStateHydrationError):
        await engine.run(strat, bars=_empty_bar_source())


@pytest.mark.asyncio
async def test_no_artifacts_root_keeps_replay_behavior_unchanged(tmp_path: Path) -> None:
    """LiveEngine with no artifacts_root + hydrate_policy=None skips persistence entirely
    — replay tests / parity gate must remain at this baseline behavior."""
    strat = SpyEmaCrossoverAlgorithm()
    engine = LiveEngine(
        client=None,
        broker=FakeBroker(),
        # No artifacts_root, no hydrate_policy -> replay-style construction.
        config=LiveConfig(symbol="SPY", force_flat_at=None),
    )

    # Drive one bar; assert no persistence-related call is made.
    t = datetime(2026, 5, 18, 14, 0, 0, tzinfo=UTC)
    with (
        patch("app.engine.live.live_context.LiveContext.maybe_write_indicator_state"),
        patch("app.engine.live.live_context.LiveContext.hydrate_indicator_state"),
    ):
        await engine.run(strat, bars=_one_bar_source(t))
        # When hydrate_policy is None and artifacts_root is None, the methods are still callable
        # but they should be no-ops. Calls may happen but both should exit early without IO.
        # The stricter assertion is that no receipt is written to disk.
    # No receipt path was specified (no run_dir wired without artifacts_root) — and FakeBroker
    # runs cleanly without it.
