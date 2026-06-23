"""Integration tests for the LiveEngine ↔ CommandChannel wire-up.

The CommandChannel module owns the file mechanics; these tests prove
the engine actually polls it, dispatches the six verbs, and acks.
Engine-side state changes (PAUSE drops new orders; STOP signals
shutdown; MARK_POISONED writes the flag) are exercised through the
existing FakeBroker bar loop.
"""

from __future__ import annotations

import asyncio
import json as _json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.config import LiveConfig
from app.engine.live.halt import PoisonedHaltTrigger, read_poisoned_flag
from app.engine.live.live_engine import LiveEngine
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
    def __init__(self) -> None:
        super().__init__()
        self.trade_log: list = []

    def initialize(self) -> None:
        assert self.ctx is not None
        self.ctx.add_equity("SPY")
        self.ctx.register_consolidator("SPY", timedelta(minutes=15), self.on_bar)

    def on_bar(self, bar: TradeBar) -> None:
        return None


@pytest.mark.asyncio
async def test_command_poll_loop_acks_pending_pause(tmp_path: Path) -> None:
    """Operator writes PAUSE; engine poll task picks it up within
    one tick, dispatches, and acks. After ack, read_pending is empty.
    """
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.PAUSE)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    # Drive the bar loop enough wall-clock time for one 1s poll tick.
    bars = [_bar(minute) for minute in range(30, 35)]

    async def _drive() -> None:
        # Bar loop's own iteration is fast (no awaiting external events
        # under FakeBroker), so the 1s sleep inside the poll task is
        # what makes "one tick" take ~1s. Give it ~1.5s of wall time.
        await engine.run(_NoopStrategy(), iter_bars(bars))

    await asyncio.wait_for(_drive(), timeout=10.0)

    assert channel.read_pending() == []
    ack_files = list(commands_dir.glob("*.ack.json"))
    assert len(ack_files) == 1
    ack_payload = _json.loads(ack_files[0].read_text(encoding="utf-8"))
    assert ack_payload["verb"] == "PAUSE"
    assert ack_payload["outcome"]["status"] == "success"


@pytest.mark.asyncio
async def test_stop_command_signals_shutdown(tmp_path: Path) -> None:
    """STOP sets the shutdown_event so the bar loop exits via the
    existing graceful-shutdown path."""
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.STOP)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    bars = [_bar(minute) for minute in range(30, 200)]  # plenty
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    # The STOP was acked.
    ack_files = list(commands_dir.glob("*.ack.json"))
    assert len(ack_files) == 1


@pytest.mark.asyncio
async def test_mark_poisoned_writes_structured_operator_flag(tmp_path: Path) -> None:
    """MARK_POISONED writes a *structured* poisoned.flag carrying the
    operator's reason, under the OPERATOR_DECLARED trigger.

    Asserting structure (not just substring) guards the fix for the
    PR #371 reviewer concern: a plain-text flag would make the
    boot-time ``read_poisoned_flag`` parser reject the run as corrupt.
    """
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(
        CommandVerb.MARK_POISONED,
        payload={"reason": "manual_trade_observed"},
    )

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    bars = [_bar(minute) for minute in range(30, 200)]
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    flag_path = tmp_path / "poisoned.flag"
    assert flag_path.exists()
    assert "manual_trade_observed" in flag_path.read_text(encoding="utf-8")

    # The boot-time parser loads it cleanly — the whole point of the
    # structured-flag fix — and surfaces the operator's reason.
    reason = read_poisoned_flag(tmp_path)
    assert reason is not None
    assert reason.trigger is PoisonedHaltTrigger.OPERATOR_DECLARED
    assert reason.details["source"] == "operator_command"
    assert reason.details["reason"] == "manual_trade_observed"


@pytest.mark.asyncio
async def test_corrupt_command_halts_engine(tmp_path: Path) -> None:
    """A malformed command.*.pending.json must HALT the engine for
    operator inspection, not spin in a log-and-retry loop while the bot
    keeps trading against a corrupt control channel (PR #373 P1).

    The poll loop catches CommandChannelCorruptError, sets the
    shutdown_event, and returns — so engine.run() exits via the normal
    graceful path within a couple of poll ticks rather than running to
    source exhaustion (here: 400 bars).
    """
    commands_dir = tmp_path / "commands"
    commands_dir.mkdir(parents=True)
    # A visible (.json), unparseable pending command.
    (commands_dir / "command.1.PAUSE.pending.json").write_text(
        "{ this is not valid json", encoding="utf-8"
    )
    channel = CommandChannel(commands_dir)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    total_bars = 200
    consumed = 0

    async def _slow_bars():
        nonlocal consumed
        for minute in range(30, 30 + total_bars):
            consumed += 1
            await asyncio.sleep(0.02)
            yield _bar(minute)

    await asyncio.wait_for(engine.run(_NoopStrategy(), _slow_bars()), timeout=15.0)

    # The halt fired early — only a small fraction of the source was
    # consumed before shutdown. A swallow-and-retry loop would have
    # drained all 200.
    assert consumed < total_bars // 2, f"consumed {consumed}/{total_bars}; expected early halt"
    # The corrupt file is left in place for the operator to inspect.
    assert (commands_dir / "command.1.PAUSE.pending.json").exists()


@pytest.mark.asyncio
async def test_reconcile_returns_accepted_when_runtime_prereqs_missing(
    tmp_path: Path,
) -> None:
    """Reconciliation PR 2 — runtime ``RECONCILE`` is now wired, but a replay /
    FakeBroker engine lacks the real prereqs (``client`` is None, no
    ``artifacts_root`` / ``strategy_instance_id``). The dispatcher must still
    return the ``accepted`` envelope (request_id + accepted_at_ms) so the
    cockpit can render IN_PROGRESS; the async task then ack-completes with
    ``verdict="error"`` because the runtime reconcile path requires a real
    broker. The dedicated runtime-reconcile test file exercises the
    Continue / Adopt / Poison / already_running branches under a fake
    broker that simulates the orchestrator call sites.
    """
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.RECONCILE)

    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
    )

    bars = [_bar(minute) for minute in range(30, 200)]
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    ack_files = sorted(commands_dir.glob("command.*.RECONCILE.ack.json"))
    assert ack_files, "RECONCILE pending file must be acked to the .ack.json sidecar"
    payload = _json.loads(ack_files[0].read_text(encoding="utf-8"))
    outcome = payload["outcome"]
    # The async task overwrote the initial accepted ack with completion.
    assert outcome["status"] == "completed"
    assert outcome["verdict"] == "error"
    assert "strategy_instance_id" in outcome["detail"]
