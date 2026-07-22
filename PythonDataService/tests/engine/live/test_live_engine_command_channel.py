"""Integration tests for the LiveEngine ↔ CommandChannel wire-up.

The CommandChannel module owns the file mechanics; these tests prove
the engine actually polls it, dispatches the six verbs, and acks.
Engine-side state changes (PAUSE drops new orders; STOP signals
shutdown; MARK_POISONED writes the flag) are exercised through the
existing FakeBroker bar loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json as _json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrPositionsSnapshot
from app.engine.data.trade_bar import TradeBar
from app.engine.execution.portfolio import Position
from app.engine.live.account_owner import AccountOwnerSubmitResult
from app.engine.live.account_registry import bot_order_namespace_for_instance
from app.engine.live.clock_out import clock_out_is_in_progress, read_clock_out_receipt
from app.engine.live.command_channel import Command, CommandChannel, CommandVerb
from app.engine.live.config import LiveConfig
from app.engine.live.engine_runtime import CommandLoopBlock
from app.engine.live.halt import PoisonedHaltTrigger, read_poisoned_flag
from app.engine.live.live_engine import LiveEngine
from app.engine.live.live_portfolio import LivePortfolio
from app.engine.strategy.base import Strategy
from app.operator.incidents.store import IncidentStore
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
async def test_command_poll_loop_refreshes_heartbeat_after_poll_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A slow-but-successful command-channel pass must not age the command-loop
    heartbeat until the next poll cycle starts.
    """

    from app.engine.live import live_engine as live_engine_mod

    class _EmptyCommandChannel:
        def read_pending(self) -> list:
            return []

    class _RuntimeAggregator:
        def __init__(self) -> None:
            self.blocks: list[CommandLoopBlock] = []
            self.second_update = asyncio.Event()

        async def update_command_loop(self, block: CommandLoopBlock) -> None:
            self.blocks.append(block)
            if len(self.blocks) >= 2:
                self.second_update.set()

    timestamps = iter([10.0, 13.2, 14.0])
    monkeypatch.setattr(live_engine_mod.time, "time", lambda: next(timestamps))
    aggregator = _RuntimeAggregator()
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        command_channel=_EmptyCommandChannel(),  # type: ignore[arg-type]
        runtime_aggregator=aggregator,
    )
    shutdown_event = asyncio.Event()

    task = asyncio.create_task(engine._command_poll_loop(shutdown_event))  # type: ignore[attr-defined]
    try:
        await asyncio.wait_for(aggregator.second_update.wait(), timeout=0.2)
    finally:
        shutdown_event.set()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    assert [block.heartbeat_at_ms for block in aggregator.blocks[:2]] == [10_000, 13_200]


@pytest.mark.asyncio
async def test_command_poll_loop_acks_pending_pause(tmp_path: Path) -> None:
    """Operator writes PAUSE; engine poll task picks it up within
    one tick, dispatches, and acks. After ack, read_pending is empty.
    """
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.PAUSE)

    broker = FakeBroker()
    durable_writes: list[tuple[object, str]] = []
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=lambda state, reason: durable_writes.append(
            (state, reason)
        ),
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
    assert len(durable_writes) == 1


@pytest.mark.parametrize(
    ("verb", "initial_paused"),
    [
        (CommandVerb.PAUSE, False),
        (CommandVerb.RESUME, True),
        (CommandVerb.STOP, False),
    ],
)
def test_durable_intent_failure_blocks_runtime_actuation(
    verb: CommandVerb,
    initial_paused: bool,
) -> None:
    def failing_writer(_state, _reason) -> None:
        raise OSError("read-only filesystem")

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        desired_state_writer=failing_writer,
    )
    engine._paused = initial_paused
    shutdown_event = asyncio.Event()

    outcome = engine._dispatch_command(
        Command(seq=1, verb=verb),
        shutdown_event,
    )

    assert outcome["status"] == "error"
    assert outcome["reason_code"] == "DURABLE_CONTROL_WRITE_FAILED"
    assert "read-only filesystem" in outcome["effect"]
    assert engine._paused is initial_paused
    assert not shutdown_event.is_set()


def test_poisoned_flag_failure_is_acked_as_durable_control_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.engine.live import live_engine as live_engine_module

    monkeypatch.setattr(
        live_engine_module,
        "poison_and_record_incident",
        lambda **_kwargs: (_ for _ in ()).throw(OSError("permission denied")),
    )
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
    )
    shutdown_event = asyncio.Event()

    outcome = engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.MARK_POISONED),
        shutdown_event,
    )

    assert outcome["status"] == "error"
    assert outcome["reason_code"] == "DURABLE_CONTROL_WRITE_FAILED"
    assert "permission denied" in outcome["effect"]
    assert not shutdown_event.is_set()


def test_missing_desired_state_writer_is_a_typed_command_failure() -> None:
    engine = LiveEngine(None, LiveConfig(), broker=FakeBroker())

    outcome = engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.PAUSE),
        asyncio.Event(),
    )

    assert outcome["status"] == "error"
    assert outcome["reason_code"] == "DURABLE_CONTROL_WRITE_FAILED"
    assert "desired_state_writer is not configured" in outcome["effect"]
    assert engine._paused is False


def test_missing_run_directory_blocks_mark_poisoned() -> None:
    engine = LiveEngine(None, LiveConfig(), broker=FakeBroker())
    shutdown_event = asyncio.Event()

    outcome = engine._dispatch_command(
        Command(seq=1, verb=CommandVerb.MARK_POISONED),
        shutdown_event,
    )

    assert outcome["status"] == "error"
    assert outcome["reason_code"] == "DURABLE_CONTROL_WRITE_FAILED"
    assert "run output directory is not configured" in outcome["effect"]
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_clock_out_only_stops_after_fresh_flat_broker_evidence(tmp_path: Path) -> None:
    """Clock-out liquidates through Clerk intake before fresh flat proof ends duty."""

    channel = CommandChannel(tmp_path / "commands")
    command = channel.write_from_operator(CommandVerb.CLOCK_OUT)
    durable_writes: list[tuple[object, str]] = []
    broker = FakeBroker()
    clerk_intents = []

    async def clerk_submitter(intent) -> AccountOwnerSubmitResult:
        clerk_intents.append(intent)
        return AccountOwnerSubmitResult(
            status="accepted",
            trace_id=intent.trace_id,
            account_id=intent.account_id,
            strategy_instance_id=intent.strategy_instance_id,
            run_id=intent.run_id,
            intent_id=intent.intent_id,
            order_ref=intent.order_ref,
            owner_generation=intent.owner_generation,
            order_id=77,
        )

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=lambda state, reason: durable_writes.append((state, reason)),
        run_id="run-clock-out",
        strategy_instance_id="clock-out-bot",
        account_owner_submitter=clerk_submitter,
        owner_generation_provider=lambda: 3,
    )
    shutdown_event = asyncio.Event()

    async def reconcile_owned_state() -> None:
        return None

    portfolio = LivePortfolio(
        broker=broker,
        account_owner_submitter=clerk_submitter,
        account_id="DU123",
        strategy_instance_id="clock-out-bot",
        run_id="run-clock-out",
        bot_order_namespace=bot_order_namespace_for_instance("clock-out-bot"),
        owner_generation_provider=lambda: 3,
    )
    portfolio.positions["SPY"] = Position(symbol="SPY", quantity=2, average_price=Decimal("500"))

    class _Context:
        def log(self, _message: str) -> None:
            return None

    accepted = engine._dispatch_command(command, shutdown_event)
    completed = await engine._complete_clock_out(
        command,
        portfolio=portfolio,
        ctx=_Context(),
        bar_time=_bar(0).time,
        reconcile_owned_state=reconcile_owned_state,  # type: ignore[arg-type]
        shutdown_event=shutdown_event,
    )

    receipt = read_clock_out_receipt(tmp_path)
    ack = _json.loads(next((tmp_path / "commands").glob("*.ack.json")).read_text(encoding="utf-8"))
    assert accepted == {"status": "accepted", "effect": "clock_out_queued"}
    assert len(completed) == 1
    assert shutdown_event.is_set()
    assert receipt is not None and receipt.status == "flat"
    assert ack["outcome"]["effect"] == "clocked_out_flat"
    assert len(clerk_intents) == 1
    assert clerk_intents[0].intent_kind == "STRATEGY"
    assert clerk_intents[0].order_spec["action"] == "SELL"
    assert clerk_intents[0].order_ref.startswith("learn-ai/clock-out-bot/v1:")
    assert [reason for _state, reason in durable_writes] == [
        "command_channel:CLOCK_OUT",
        "command_channel:CLOCK_OUT_FLAT",
    ]


@pytest.mark.asyncio
async def test_clock_out_exits_after_stop_latch_when_final_receipt_rewrite_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A durable STOPPED latch must not strand a paused runner on I/O failure."""

    from app.engine.live import live_engine as live_engine_mod
    from app.engine.live.clock_out import write_clock_out_receipt as real_write_receipt

    channel = CommandChannel(tmp_path / "commands")
    command = channel.write_from_operator(CommandVerb.CLOCK_OUT)
    writes = 0

    def fail_second_receipt_write(run_dir: Path, receipt: object) -> Path:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise OSError("injected final receipt rewrite failure")
        return real_write_receipt(run_dir, receipt)  # type: ignore[arg-type]

    monkeypatch.setattr(live_engine_mod, "write_clock_out_receipt", fail_second_receipt_write)
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=lambda _state, _reason: None,
        run_id="run-finalization-failure",
        strategy_instance_id="finalization-failure-bot",
    )
    shutdown_event = asyncio.Event()

    async def reconcile_owned_state() -> None:
        return None

    engine._dispatch_command(command, shutdown_event)
    await engine._complete_clock_out(
        command,
        portfolio=LivePortfolio(broker=engine._broker),  # type: ignore[arg-type]
        ctx=object(),  # type: ignore[arg-type]
        bar_time=_bar(0).time,
        reconcile_owned_state=reconcile_owned_state,
        shutdown_event=shutdown_event,
    )

    receipt = read_clock_out_receipt(tmp_path)
    ack = _json.loads(next((tmp_path / "commands").glob("*.ack.json")).read_text(encoding="utf-8"))
    assert shutdown_event.is_set()
    assert receipt is not None and receipt.stop_persisted_at_ms is None
    assert ack["outcome"]["status"] == "failed"
    assert ack["outcome"]["reason_code"] == "CLOCK_OUT_FINALIZATION_FAILED_OSERROR"


def test_failed_clock_out_settles_a_historical_already_running_follower(tmp_path: Path) -> None:
    """A follower ack cannot hide the leader's durable failure forever."""

    channel = CommandChannel(tmp_path / "commands")
    leader = channel.write_from_operator(CommandVerb.CLOCK_OUT)
    channel.ack(leader, outcome={"status": "accepted"})
    follower = channel.write_from_operator(CommandVerb.CLOCK_OUT)
    channel.ack(follower, outcome={"status": "already_running"})
    assert clock_out_is_in_progress(tmp_path)

    channel.ack_completion(
        leader,
        outcome={"status": "failed", "effect": "clock_out_failed", "reason_code": "BROKER_DOWN"},
    )

    assert not clock_out_is_in_progress(tmp_path)


@pytest.mark.asyncio
async def test_clock_out_exits_after_stop_latch_when_completion_ack_write_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The completion receipt is useful evidence, but cannot strand STOPPED."""

    channel = CommandChannel(tmp_path / "commands")
    command = channel.write_from_operator(CommandVerb.CLOCK_OUT)
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=lambda _state, _reason: None,
        run_id="run-ack-failure",
        strategy_instance_id="ack-failure-bot",
    )
    shutdown_event = asyncio.Event()

    def fail_completion_ack(*_args: object, **_kwargs: object) -> None:
        raise OSError("injected completion ack failure")

    async def reconcile_owned_state() -> None:
        return None

    monkeypatch.setattr(channel, "ack_completion", fail_completion_ack)
    engine._dispatch_command(command, shutdown_event)
    await engine._complete_clock_out(
        command,
        portfolio=LivePortfolio(broker=engine._broker),  # type: ignore[arg-type]
        ctx=object(),  # type: ignore[arg-type]
        bar_time=_bar(0).time,
        reconcile_owned_state=reconcile_owned_state,
        shutdown_event=shutdown_event,
    )

    receipt = read_clock_out_receipt(tmp_path)
    assert shutdown_event.is_set()
    assert receipt is not None and receipt.stop_persisted_at_ms is not None


@pytest.mark.asyncio
async def test_clock_out_refuses_cached_broker_positions(tmp_path: Path) -> None:
    """A cache fallback cannot prove the account flat enough to stop the bot."""

    channel = CommandChannel(tmp_path / "commands")
    command = channel.write_from_operator(CommandVerb.CLOCK_OUT)
    broker = FakeBroker()
    broker.position_snapshot = IbkrPositionsSnapshot(
        account_id="DU123",
        is_paper=True,
        positions=[],
        fetched_at_ms=1,
        used_cache_fallback=True,
    )
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=lambda _state, _reason: None,
        run_id="run-clock-out-cache",
        strategy_instance_id="clock-out-cache-bot",
    )
    shutdown_event = asyncio.Event()

    async def no_orders(*_args: object, **_kwargs: object) -> list:
        return []

    async def reconcile_owned_state() -> None:
        return None

    engine._flatten = no_orders  # type: ignore[method-assign]
    engine._dispatch_command(command, shutdown_event)
    await engine._complete_clock_out(
        command,
        portfolio=LivePortfolio(broker=broker),
        ctx=object(),  # type: ignore[arg-type]
        bar_time=_bar(0).time,
        reconcile_owned_state=reconcile_owned_state,
        shutdown_event=shutdown_event,
    )

    receipt = read_clock_out_receipt(tmp_path)
    assert receipt is not None and receipt.status == "failed"
    assert receipt.reason_code == "CLOCK_OUT_FAILED_RUNTIMEERROR"
    assert not shutdown_event.is_set()


@pytest.mark.asyncio
async def test_stop_command_signals_shutdown(tmp_path: Path) -> None:
    """STOP sets the shutdown_event so the bar loop exits via the
    existing graceful-shutdown path."""
    commands_dir = tmp_path / "commands"
    channel = CommandChannel(commands_dir)
    channel.write_from_operator(CommandVerb.STOP)

    broker = FakeBroker()
    durable_writes: list[tuple[object, str]] = []
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=lambda state, reason: durable_writes.append(
            (state, reason)
        ),
    )

    bars = [_bar(minute) for minute in range(30, 200)]  # plenty
    await asyncio.wait_for(engine.run(_NoopStrategy(), iter_bars(bars)), timeout=10.0)

    # The STOP was acked.
    ack_files = list(commands_dir.glob("*.ack.json"))
    assert len(ack_files) == 1
    assert len(durable_writes) == 1


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
        run_id="run-operator-poisoned",
        strategy_instance_id="operator-poisoned-bot",
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
    incidents = IncidentStore(tmp_path).list_unresolved()
    assert len(incidents) == 1
    assert incidents[0].category == "safety-halt"
    assert incidents[0].notice.code == "safety_halt.poisoned"
    assert incidents[0].evidence["run_id"] == "run-operator-poisoned"
    assert incidents[0].evidence["strategy_instance_id"] == "operator-poisoned-bot"
    assert incidents[0].evidence["halt_trigger"] == "operator_declared"


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
