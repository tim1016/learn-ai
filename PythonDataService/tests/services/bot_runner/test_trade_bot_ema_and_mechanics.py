"""EMA live round-trip parity and the consecutive-green-bar trade bot's
window mechanics (entry, exit, flatten, quantity, log_only).

Split from ``tests/services/test_bot_runner.py`` (issue #1737).
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import pytest

from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import StrategyContext
from tests._helpers.canary_admission import admit_canary_pairing

from .conftest import (
    _EMA_FIRST_ENTER_MS,
    _EMA_FIRST_EXIT_MS,
    _SESSION_OPEN_MS,
    _SID,
    _T0,
    _WIN_END_MS,
    _WIN_START_MS,
    _bar,
    _ema_parity_bars_through_first_exit,
    _ema_signal_evaluation_id,
    _FakeClerk,
    _FakeFeed,
    _green_bar,
    _install_fake_clerk,
    _red_bar,
    _registry,
    _strategy_instance_json,
    _wait_for,
)


@pytest.mark.asyncio
async def test_ema_trade_bot_matches_first_lean_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "ema_crossover_signal", "paper-account")
    bars = _ema_parity_bars_through_first_exit()
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="trade",
        quantity=3,
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: len(clerk.calls) >= 2)
    await registry.stop("alpaca", _SID)

    # Slice 2 (#1728): decision_id is now evaluation_id -- a content-addressed
    # SHA-256 of program identity + settings + bar-close, not the old
    # "{bar_close_ms}:{kind}" string. Assert both the shape (a 64-char lower-
    # hex digest, the same pattern EffectDecisionEvidence.evaluation_id
    # requires) and the meaning (it equals the documented per-bar formula),
    # so this test still fails if decision_id ever stops being evaluation_id.
    enter_call, exit_call = clerk.calls[:2]
    for call in (enter_call, exit_call):
        assert re.fullmatch(r"[0-9a-f]{64}", call["decision_id"])
    assert [(call["decision_id"], call["purpose"], call["quantity"]) for call in (enter_call, exit_call)] == [
        (_ema_signal_evaluation_id(_EMA_FIRST_ENTER_MS), "ENTER", 3),
        (_ema_signal_evaluation_id(_EMA_FIRST_EXIT_MS), "EXIT", 3),
    ]


@pytest.mark.asyncio
async def test_ema_trade_bot_releases_backtest_chart_bars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import bot_trade_strategy

    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "ema_crossover_signal", "paper-account")
    contexts: list[StrategyContext] = []
    context_factory = bot_trade_strategy.StrategyContext

    def capture_context(*, portfolio: Portfolio) -> StrategyContext:
        context = context_factory(portfolio=portfolio)
        contexts.append(context)
        return context

    monkeypatch.setattr(bot_trade_strategy, "StrategyContext", capture_context)
    bars = _ema_parity_bars_through_first_exit()
    feed = _FakeFeed(bars, mode="finite")
    registry = _registry(tmp_path, feed)

    await registry.deploy(
        broker="alpaca",
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        symbol="SPY",
        mode="trade",
    )
    await _wait_for(lambda: feed.bars_consumed == len(bars))
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    assert len(contexts) == 1
    assert contexts[0].consolidated_bars == []
    assert isinstance(contexts[0].current_time_ms, int)
    assert not any(isinstance(value, datetime) for value in vars(contexts[0]).values())


@pytest.mark.asyncio
async def test_trade_bot_enters_after_two_green_bars_in_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    # One red bar then two green bars inside the detection window, then hold.
    bars = [
        _red_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),
        _green_bar(_WIN_START_MS + 180_000),
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=2)
    await _wait_for(lambda: feed.bars_consumed == 3)
    await registry.stop("alpaca", _SID)

    # One semantic ENTER after the second consecutive green bar.  The runtime
    # never supplies a broker side; the Clerk derives that from the plan.
    assert len(clerk.calls) == 1
    assert clerk.calls[0]["purpose"] == "ENTER"
    assert clerk.calls[0]["quantity"] == 2
    assert clerk.calls[0]["strategy_instance_id"] == _SID


@pytest.mark.asyncio
async def test_trade_bot_no_entry_before_window(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    # Two green bars strictly before the 09:45 ET window start.
    pre_window_1 = _SESSION_OPEN_MS + 60_000  # 09:31 ET
    pre_window_2 = _SESSION_OPEN_MS + 120_000  # 09:32 ET
    bars = [
        _green_bar(pre_window_1),
        _green_bar(pre_window_2),
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade")
    await _wait_for(lambda: feed.bars_consumed == 2)
    await registry.stop("alpaca", _SID)

    assert clerk.calls == []


@pytest.mark.asyncio
async def test_trade_bot_exits_three_bars_after_entry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),  # entry triggered after this bar
        _red_bar(base + 120_000),  # in-position bar 1
        _red_bar(base + 180_000),  # in-position bar 2
        _green_bar(base + 240_000),  # in-position bar 3 → exit
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=3)
    await _wait_for(lambda: feed.bars_consumed == 5)
    await registry.stop("alpaca", _SID)

    assert len(clerk.calls) == 2
    assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]
    assert clerk.calls[1]["quantity"] == 3


@pytest.mark.asyncio
async def test_trade_bot_flattens_at_window_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),  # triggers BUY
        _red_bar(base + 120_000),  # bar 1 in position
        _green_bar(_WIN_END_MS + 1),  # past window end → FLATTEN
    ]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=1)
    await _wait_for(lambda: feed.bars_consumed == 4)
    await registry.stop("alpaca", _SID)

    assert len(clerk.calls) == 2
    assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]


@pytest.mark.asyncio
async def test_trade_bot_quantity_plumbed_from_binding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clerk = _FakeClerk()
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    base = _WIN_START_MS + 60_000
    bars = [_green_bar(base), _green_bar(base + 60_000)]
    feed = _FakeFeed(bars, mode="hold")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade", quantity=7)
    await _wait_for(lambda: feed.bars_consumed == 2)
    await registry.stop("alpaca", _SID)

    assert clerk.calls[0]["quantity"] == 7

    # Immutable instance artifact carries deployment semantics.
    instance = _strategy_instance_json(tmp_path)
    assert instance["quantity"] == 7
    assert instance["mode"] == "trade"
    assert instance["action_plan"]["on_enter"][0]["position"] == "long"


@pytest.mark.asyncio
async def test_trade_bot_submit_exception_crashes_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.broker.contract.errors import BrokerError

    error = BrokerError("forced failure", detail="test")
    clerk = _FakeClerk(should_raise=error)
    _install_fake_clerk(monkeypatch, clerk)
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    base = _WIN_START_MS + 60_000
    bars = [_green_bar(base), _green_bar(base + 60_000)]
    feed = _FakeFeed(bars, mode="finite")
    registry = _registry(tmp_path, feed)

    await registry.deploy(broker="alpaca", strategy_instance_id=_SID, symbol="SPY", mode="trade")
    await _wait_for(lambda: not registry.status("alpaca", _SID).running)

    view = registry.status("alpaca", _SID)
    assert view.duty_outcome is not None
    assert view.duty_outcome.kind == "CRASHED"
    assert view.duty_outcome.reason_code == "BrokerError"


@pytest.mark.asyncio
async def test_log_only_bot_unchanged_after_trade_mode_added(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Regression: adding trade mode must not alter log_only behavior."""
    feed = _FakeFeed([_bar(_T0), _bar(_T0 + 60_000)], mode="hold")
    registry = _registry(tmp_path, feed)

    with caplog.at_level("INFO", logger="app.services.bot_runtime"):
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            symbol="SPY",
            mode="log_only",
        )
        await _wait_for(lambda: feed.bars_consumed == 2)
        await registry.stop("alpaca", _SID)

    decisions = [r for r in caplog.records if getattr(r, "action", None) == "bot_decision"]
    assert len(decisions) == 2
    assert all(d.decision == "HOLD" for d in decisions)

    instance = _strategy_instance_json(tmp_path)
    assert instance["mode"] == "log_only"
