"""Rejected-exit rollback and RSI warmup/session-boundary decision timing
for the ``deployment_validation`` live trade bot.

Split from ``tests/services/test_bot_runner.py`` (issue #1737, seam 3).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.models import (
    EffectPurpose,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.marketdata.feed import MarketDataBar
from tests._helpers.canary_admission import admit_canary_pairing

from .conftest import (
    _SESSION_CLOSE_MS,
    _SESSION_OPEN_MS,
    _SID,
    _WIN_START_MS,
    _FakeClerk,
    _FakeEffectResult,
    _FakeFeed,
    _green_bar,
    _red_bar,
    _registry,
    _trade_bar,
    _wait_for,
)


class _RejectFirstExitClerk(_FakeClerk):
    """Reject exactly the first EXIT submission; accept every other call."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._exit_attempts = 0

    async def execute_for_instance(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        decision_id: str,
        purpose,
        action_plan,
        quantity: int,
        use_rth: bool = True,
        capability_account_id: str | None = None,
        decision_evidence=None,
    ) -> _FakeEffectResult:
        if purpose == EffectPurpose.EXIT:
            self._exit_attempts += 1
            self._effect_state = "rejected" if self._exit_attempts == 1 else "submitted"
        return await super().execute_for_instance(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            decision_id=decision_id,
            purpose=purpose,
            action_plan=action_plan,
            quantity=quantity,
            use_rth=use_rth,
            capability_account_id=capability_account_id,
            decision_evidence=decision_evidence,
        )


@pytest.mark.asyncio
async def test_rejected_exit_is_rolled_back_and_retried(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1708 review finding 1 regression: before the rollback fix, a
    rejected EXIT left the strategy's own position-lifecycle state cleared
    even though the broker never actually closed the position -- it believed
    it was flat with custody still open. With the rollback
    (``DeploymentValidationConsecutiveGreen.rollback_blocked_exit``, issue
    #1730 Slice 5's Signal Program promotion), the rejected bar leaves no
    trace: the very next eligible bar re-fires EXIT and it succeeds, rather
    than silently starting a fresh ENTER cycle over an unclosed position."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _RejectFirstExitClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")

    base = _WIN_START_MS + 60_000
    bars = [
        _green_bar(base),
        _green_bar(base + 60_000),  # ENTER
        _red_bar(base + 120_000),  # in-position bar 1
        _red_bar(base + 180_000),  # in-position bar 2
        _green_bar(base + 240_000),  # in-position bar 3 → EXIT attempt #1, rejected
        _red_bar(base + 300_000),  # EXIT attempt #2 (retry) → accepted
    ]
    registry = _registry(tmp_path, _FakeFeed(bars, mode="hold"))
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(clerk.calls) == 3)
        await registry.stop("alpaca", _SID)

        # ENTER, the rejected EXIT attempt, then the retried EXIT that
        # actually closes the position -- not a fresh ENTER over an
        # unclosed position.
        assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT", "EXIT"]

        decisions = [
            d for d in repo.decision_receipt_tail(strategy_instance_id=_SID, limit=len(bars)) if d.outcome != "no_action"
        ]
        assert [d.outcome for d in decisions] == ["enter_intent", "blocked", "exit_intent"]
        assert json.loads(decisions[1].facts_json)["reason_code"] == "CLERK_ADMISSION_REJECTED"
    finally:
        set_alpaca_clerk(None)
        repo.close()


def _rsi_warmup_bars(end_ms: int, *, consolidated_bars: int, symbol: str = "SPY") -> list[MarketDataBar]:
    """``consolidated_bars`` * 15 one-minute bars immediately before ``end_ms``,
    oscillating gently so RSI(14) settles to a stable mid-range reading
    without itself crossing either threshold."""
    minute_count = consolidated_bars * 15
    start_ms = end_ms - minute_count * 60_000
    bars: list[MarketDataBar] = []
    price = Decimal("400.00")
    for i in range(minute_count):
        bump = Decimal("0.05") if i % 2 == 0 else Decimal("-0.05")
        new_price = price + bump
        bars.append(
            _trade_bar(start_ms + (i + 1) * 60_000, open_price=str(price), close_price=str(new_price), symbol=symbol)
        )
        price = new_price
    return bars


def _plunge_bucket_bars(bucket_end_ms: int, *, flat_price: str, plunge_close: str, symbol: str = "SPY") -> list[MarketDataBar]:
    """One 15-min bucket's worth of one-minute bars: flat, then a sharp
    single-minute plunge on the bucket's final bar -- enough to carry a
    warmed-up RSI(14) below the oversold gate."""
    bucket_start_ms = bucket_end_ms - 15 * 60_000
    bars = [
        _trade_bar(bucket_start_ms + (i + 1) * 60_000, open_price=flat_price, close_price=flat_price, symbol=symbol)
        for i in range(14)
    ]
    bars.append(_trade_bar(bucket_end_ms, open_price=flat_price, close_price=plunge_close, symbol=symbol))
    return bars


class _WarmableFeed(_FakeFeed):
    """A ``_FakeFeed`` that also serves historical bars for warmup replay."""

    def __init__(self, bars: list[MarketDataBar], warmup_bars: list[MarketDataBar], *, mode: str = "hold") -> None:
        super().__init__(bars, mode=mode)
        self._warmup_bars = warmup_bars

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        return self._warmup_bars


@pytest.mark.asyncio
async def test_signal_strategy_decides_on_the_first_live_bucket_after_warmup_backfill(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1708 review finding 3 regression: RSI(14) on 15-min bars needs 14
    consolidated bars of history, but a fresh RTH session provides none —
    a strategy deployed cold would silently withhold decisions through its
    first day(s) of live trading. With warmup backfill, the very first live
    bucket of a brand-new deployment can already decide.
    """
    admit_canary_pairing(monkeypatch, "rsi_mean_reversion", "paper-account")
    clerk = _FakeClerk()
    set_alpaca_clerk(clerk)
    try:
        warmup_bars = _rsi_warmup_bars(_SESSION_OPEN_MS, consolidated_bars=20)
        live_bars = _plunge_bucket_bars(
            _SESSION_OPEN_MS + 15 * 60_000,
            flat_price=str(warmup_bars[-1].close),
            plunge_close="380.00",
        )
        # One more bar so the consolidator's normal lazy fire closes the
        # plunge bucket -- this test isolates warmup backfill (finding 3)
        # from the session-close force-flush (finding 2), which is
        # covered separately below.
        live_bars.append(_trade_bar(_SESSION_OPEN_MS + 16 * 60_000, open_price="380.00", close_price="380.00"))
        feed = _WarmableFeed(live_bars, warmup_bars, mode="hold")
        registry = _registry(tmp_path, feed)

        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="rsi_mean_reversion",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(clerk.calls) == 1)
        await registry.stop("alpaca", _SID)

        assert clerk.calls[0]["purpose"] == "ENTER"
    finally:
        set_alpaca_clerk(None)


@pytest.mark.asyncio
async def test_final_rth_bucket_decides_without_waiting_for_the_next_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1708 review finding 2 regression: the consolidator only fires a
    working bucket lazily, when a *later* bar arrives -- but an RTH-only
    stream never delivers one after the session closes. Before the fix,
    the final 15:45-16:00 bucket's decision would strand until the next
    session's bars started arriving. With the session-close force-flush,
    it decides immediately, from the same bar that closes the session.
    """
    admit_canary_pairing(monkeypatch, "rsi_mean_reversion", "paper-account")
    clerk = _FakeClerk()
    set_alpaca_clerk(clerk)
    try:
        warmup_bars = _rsi_warmup_bars(_SESSION_CLOSE_MS - 15 * 60_000, consolidated_bars=20)
        # The live stream ends exactly at the session close, in "hold"
        # mode -- no further bar will ever arrive to naturally fire the
        # final bucket via the consolidator's ordinary lazy-fire path.
        live_bars = _plunge_bucket_bars(
            _SESSION_CLOSE_MS,
            flat_price=str(warmup_bars[-1].close),
            plunge_close="380.00",
        )
        feed = _WarmableFeed(live_bars, warmup_bars, mode="hold")
        registry = _registry(tmp_path, feed)

        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="rsi_mean_reversion",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: feed.bars_consumed == len(live_bars))
        await _wait_for(lambda: len(clerk.calls) == 1)
        await registry.stop("alpaca", _SID)

        assert clerk.calls[0]["purpose"] == "ENTER"
    finally:
        set_alpaca_clerk(None)
