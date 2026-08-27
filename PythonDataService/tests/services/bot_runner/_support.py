"""Local-only shared test doubles, constants, and fixture-data builders for
the bot_runner test package -- helpers used by two or more local test
modules but consumed nowhere outside ``tests/services/bot_runner/``.

Cross-directory-boundary support (test doubles, custody/registry
construction, market liveness, EMA-parity fixtures) lives in
``tests/_helpers/bot_runner/`` instead; see that package for the themed
split. Split out of ``tests/services/bot_runner/conftest.py`` per issue
#1810 (the "undeclared public library" review finding).

Two deviations from a pure per-name move, both to avoid a shared module
depending on a single leaf test file (see the PR description for the
full rationale):
- ``_trade_bar`` stays here (rather than moving beside its one direct
  test-file importer) because ``_red_bar``/``_green_bar`` below -- each
  used by 4 local test modules -- call it internally.
- ``_SESSION_CLOSE_MS`` and ``_WIN_END_MS`` stay grouped with
  ``_SESSION_OPEN_MS``/``_WIN_START_MS`` rather than splitting off the
  single-consumer half of the pair; all four share one documented
  derivation comment block that would otherwise fragment.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.models import InstanceCustodyProof
from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.bot_runner.doubles import _CustodyClerk, _FakeClerk

_RTH_MS = 1_700_060_400_000
_EMA_FIRST_ENTER_MS = 1_770_389_100_000


def _bar(start_ms: int, symbol: str = "SPY") -> MarketDataBar:
    return MarketDataBar(
        symbol=symbol,
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal("400.5"),
        volume=100,
        fetched_at_ms=start_ms + 500,
        feed_id="ibkr",
        session_phase="RTH",
    )


class _OrderingClerk(_CustodyClerk):
    def __init__(self, proof: InstanceCustodyProof) -> None:
        super().__init__(proof)
        self.registration_saw_bot_task = False
        self.stop_committed = asyncio.Event()
        self.fail_stop = False

    async def register_strategy_run(self, binding: BrokerBotBinding) -> None:
        self.registration_saw_bot_task = any(
            task.get_name() == f"bot:{binding.strategy_instance_id}"
            for task in asyncio.all_tasks()
        )
        await super().register_strategy_run(binding)

    async def stop_strategy_run(
        self,
        *,
        strategy_instance_id: str,
        run_id: str,
        reason: str | None = None,
    ) -> None:
        if self.fail_stop:
            raise RuntimeError("durable STOP failed")
        await super().stop_strategy_run(
            strategy_instance_id=strategy_instance_id,
            run_id=run_id,
            reason=reason,
        )
        self.stop_committed.set()


def _current_run_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "current_run.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _strategy_instance_json(tmp_path: Path, sid: str = _SID) -> dict:
    path = tmp_path / "live_state" / sid / "strategy_instance.json"
    return json.loads(path.read_text(encoding="utf-8"))


async def _wait_for(predicate, *, timeout_s: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


# 2024-01-02 is a regular NYSE trading day (Tuesday after New Year's).
# All bar timestamps below are int64 ms UTC (temporal-rigor rule).
#
# ET = EST on 2024-01-02 (UTC-5):
#   session_open  = 09:30 ET = 14:30 UTC = 1_704_205_800_000 ms
#   session_close = 16:00 ET = 21:00 UTC = 1_704_229_200_000 ms
#   window_start  = open  + 15min = 1_704_206_700_000 ms  (09:45 ET)
#   window_end    = close - 15min = 1_704_228_300_000 ms  (15:45 ET)
#
# Verified against the canonical calendar module (session_window_for_date).
# bar.end_ms is the bar-close boundary per MarketDataBar semantics.

_SESSION_OPEN_MS = 1_704_205_800_000  # 2024-01-02 09:30 ET (EST = UTC-5)
_SESSION_CLOSE_MS = 1_704_229_200_000  # 2024-01-02 16:00 ET
_WIN_START_MS = _SESSION_OPEN_MS + 15 * 60 * 1_000  # 09:45 ET = 1_704_206_700_000
_WIN_END_MS = _SESSION_CLOSE_MS - 15 * 60 * 1_000  # 15:45 ET = 1_704_228_300_000


def _trade_bar(
    end_ms: int,
    *,
    open_price: str = "400.00",
    close_price: str = "401.00",
    symbol: str = "SPY",
) -> MarketDataBar:
    """A single 1-minute bar whose end_ms (bar-close) falls at a specific instant."""
    return MarketDataBar(
        symbol=symbol,
        start_ms=end_ms - 60_000,
        end_ms=end_ms,
        open=Decimal(open_price),
        high=Decimal(close_price),
        low=Decimal(open_price),
        close=Decimal(close_price),
        volume=500,
        fetched_at_ms=end_ms + 100,
        feed_id="fake",
        session_phase="RTH",
    )


def _red_bar(end_ms: int, symbol: str = "SPY") -> MarketDataBar:
    """A bar where close < open (red candle — no green streak contribution)."""
    return _trade_bar(end_ms, open_price="401.00", close_price="400.00", symbol=symbol)


def _green_bar(end_ms: int, symbol: str = "SPY") -> MarketDataBar:
    """A bar where close > open (green candle)."""
    return _trade_bar(end_ms, open_price="400.00", close_price="401.00", symbol=symbol)


def _install_fake_clerk(monkeypatch: pytest.MonkeyPatch, clerk: _FakeClerk) -> None:
    """Patch the process-level Alpaca clerk for the duration of a test."""
    del monkeypatch
    set_alpaca_clerk(clerk)
