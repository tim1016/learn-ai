"""Durable desired-state tests (PRD-A § 16.4 Resolution 7 / PR-D).

Covers the two acceptance tests named in the §16.2 PR-D row —
"PAUSED across crash" and "STOPPED → no restart loop" — plus the
engine-side persistence of operator intent and the
``pause``/``resume``/``stop`` CLI verbs.
"""

from __future__ import annotations

import argparse
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.engine.data.trade_bar import TradeBar
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.config import LiveConfig
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.live_engine import LiveEngine
from app.engine.live.run import build_parser, cmd_start, main
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


class _GoLongStrategy(Strategy):
    """Buys SPY on the first consolidated bar — used to prove that a
    paused engine drops the order before it reaches the broker."""

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


def _writer_for(repo: DesiredStateRepo):
    def _write(state: DesiredState, reason: str) -> None:
        repo.set(state, updated_by="engine", reason=reason, now_ms=1)

    return _write


# ──────────────────── engine boots paused (behavioral) ────────────────


@pytest.mark.asyncio
async def test_engine_drops_orders_when_started_paused(tmp_path: Path) -> None:
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        start_paused=True,
    )
    await engine.run(_GoLongStrategy(), iter_bars([_bar(m) for m in range(5)]))
    assert broker.orders == []


@pytest.mark.asyncio
async def test_engine_places_orders_when_not_paused(tmp_path: Path) -> None:
    """Control for the test above: same strategy, not paused, DOES trade —
    so the empty-orders assertion is meaningful, not vacuous."""
    broker = FakeBroker()
    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=broker,
        output_dir=tmp_path,
        account_id="DU123",
        start_paused=False,
    )
    await engine.run(_GoLongStrategy(), iter_bars([_bar(m) for m in range(5)]))
    assert len(broker.orders) >= 1
    assert broker.orders[0].action == "BUY"


# ──────────────────── PAUSE/STOP persist durable intent ───────────────


@pytest.mark.asyncio
async def test_pause_command_persists_desired_state_paused(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "spy_ema_crossover"))
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.PAUSE)

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=_writer_for(repo),
    )
    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(5)]))

    assert repo.read_state() is DesiredState.PAUSED


@pytest.mark.asyncio
async def test_stop_command_persists_desired_state_stopped(tmp_path: Path) -> None:
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, "spy_ema_crossover"))
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.STOP)

    engine = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=_writer_for(repo),
    )
    await engine.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(200)]))

    assert repo.read_state() is DesiredState.STOPPED


# ──────────────────── PAUSED across crash (end to end) ────────────────


@pytest.mark.asyncio
async def test_paused_desired_state_survives_restart(tmp_path: Path) -> None:
    """Round 1 pauses via the command channel (persisting PAUSED to the
    durable sidecar); a fresh engine "after the crash" reads that same
    sidecar, boots paused, and drops the order it would otherwise place.
    """
    desired_path = stable_desired_state_path(tmp_path, "spy_ema_crossover")
    repo = DesiredStateRepo(desired_path)

    # Round 1: operator pauses a running bot.
    channel = CommandChannel(tmp_path / "commands")
    channel.write_from_operator(CommandVerb.PAUSE)
    engine_a = LiveEngine(
        None,
        LiveConfig(),
        broker=FakeBroker(),
        output_dir=tmp_path,
        account_id="DU123",
        command_channel=channel,
        desired_state_writer=_writer_for(repo),
    )
    await engine_a.run(_NoopStrategy(), iter_bars([_bar(m) for m in range(5)]))
    assert repo.read_state() is DesiredState.PAUSED

    # Round 2: "restart" — a brand-new engine seeded from the durable file.
    start_paused = DesiredStateRepo(desired_path).read_state() is DesiredState.PAUSED
    assert start_paused is True

    broker_b = FakeBroker()
    engine_b = LiveEngine(
        None,
        LiveConfig(),
        broker=broker_b,
        output_dir=tmp_path / "run2",
        account_id="DU123",
        start_paused=start_paused,
    )
    await engine_b.run(_GoLongStrategy(), iter_bars([_bar(m) for m in range(5)]))
    assert broker_b.orders == []


# ──────────────────── STOPPED → no restart loop (cmd_start) ───────────


def _build_started_ledger(tmp_path: Path):
    from app.engine.live.run_ledger import build_ledger, write_ledger

    spec = tmp_path / "spec.json"
    spec.write_text('{"strategy": "spy_ema_crossover"}', encoding="utf-8")
    qc = tmp_path / "qc_audit.py"
    qc.write_text("# QC audit copy stub\n", encoding="utf-8")

    ledger = build_ledger(
        code_sha="deadbeef" * 5,
        strategy_spec_path=spec,
        qc_audit_copy_path=qc,
        qc_cloud_backtest_id="bt-desired-1",
        account_id="DU123",
        start_date_ms=1714838400000,
        live_config={},
    )
    run_dir = tmp_path / "run"
    write_ledger(run_dir / "run_ledger.json", ledger)
    return run_dir


async def _empty_bars() -> AsyncIterator[TradeBar]:
    return
    yield  # pragma: no cover - makes this an async generator


def test_start_refuses_when_desired_state_stopped(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    run_dir = _build_started_ledger(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    DesiredStateRepo(stable_desired_state_path(artifacts_root, "spy_ema_crossover")).set(
        DesiredState.STOPPED, updated_by="operator", now_ms=1, reason="end of week"
    )

    broker = FakeBroker()
    args = argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="spy_ema_crossover",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="disabled",
        artifacts_root=artifacts_root,
        broker=broker,
        bars=_empty_bars(),
        client=None,
    )
    rc = cmd_start(args)

    assert rc == 1
    assert "STOPPED" in capsys.readouterr().err
    # The bot never ran: no orders touched the broker.
    assert broker.orders == []


def test_start_does_not_refuse_when_desired_state_running(tmp_path: Path) -> None:
    """Sanity: a RUNNING (or absent) desired-state must NOT trigger the
    STOPPED refusal — only STOPPED does."""
    run_dir = _build_started_ledger(tmp_path)
    artifacts_root = tmp_path / "artifacts"
    artifacts_root.mkdir()
    # No desired_state.json written → defaults to RUNNING.

    args = argparse.Namespace(
        command="start",
        run_dir=run_dir,
        strategy="spy_ema_crossover",
        readonly=False,
        max_orders_per_day=4,
        hydrate_policy="disabled",
        artifacts_root=artifacts_root,
        broker=FakeBroker(),
        bars=_empty_bars(),
        client=None,
    )
    rc = cmd_start(args)
    # Whatever the engine does with zero bars, it is NOT the STOPPED
    # refusal (which would be exit 1 with no run attempted).
    assert rc != 1


# ──────────────────── pause / resume / stop CLI verbs ─────────────────


def test_pause_resume_stop_subcommands_write_versioned_state(tmp_path: Path) -> None:
    sid = "spy_ema_crossover"
    common = ["--strategy-instance-id", sid, "--artifacts-root", str(tmp_path)]
    repo = DesiredStateRepo(stable_desired_state_path(tmp_path, sid))

    assert main(["pause", *common, "--reason", "manual hold"]) == 0
    rec = repo.read()
    assert rec is not None
    assert rec.desired_state is DesiredState.PAUSED
    assert rec.version == 1
    assert rec.reason == "manual hold"
    assert rec.updated_by == "operator"

    assert main(["stop", *common]) == 0
    rec = repo.read()
    assert rec is not None
    assert rec.desired_state is DesiredState.STOPPED
    assert rec.version == 2

    assert main(["resume", *common, "--updated-by", "alice"]) == 0
    rec = repo.read()
    assert rec is not None
    assert rec.desired_state is DesiredState.RUNNING
    assert rec.version == 3
    assert rec.updated_by == "alice"


def test_pause_subcommand_parses() -> None:
    args = build_parser().parse_args(
        ["pause", "--strategy-instance-id", "x", "--artifacts-root", "/tmp/a"]
    )
    assert args.command == "pause"
    assert args.strategy_instance_id == "x"
    assert args.updated_by == "operator"
