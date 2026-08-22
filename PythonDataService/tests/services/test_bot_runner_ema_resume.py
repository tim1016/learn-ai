"""Focused resume regression coverage for issue #1692."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.marketdata.feed import FeedHealth, MarketDataBar
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)
from app.services.bot_runner import BotTaskRegistry
from app.services.bot_runner_errors import RunAdmissionRefusedError
from app.services.market_liveness import compose_market_liveness
from app.utils.timestamps import now_ms_utc

_STRATEGY_INSTANCE_ID = "alpaca-skeleton-1"


def _tradable_market_liveness(
    symbol: str,
    observed_at_ms: int,
) -> MarketLivenessFact:
    return compose_market_liveness(
        symbol,
        now_ms=observed_at_ms,
        market_clock=MarketClockLivenessEvidence(
            state="OPEN",
            source="test.clock",
            observed_at_ms=observed_at_ms,
            vendor_timestamp_ms=observed_at_ms,
        ),
        connected=True,
        connection_changed_at_ms=observed_at_ms,
        symbol_status=SymbolTradingStatusEvidence(
            symbol=symbol,
            state="TRADABLE",
            source="test.symbol-status",
            observed_at_ms=observed_at_ms,
            source_timestamp_ms=observed_at_ms,
        ),
    )


class _FlatBroker:
    """Broker truth needed by the real SQLite Clerk admission boundary."""

    broker_id = "alpaca"

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
    ) -> list[BrokerOrder]:
        del status, limit, after_ms
        return []

    async def list_positions(self) -> list[BrokerPosition]:
        return []

    async def submit(
        self,
        _leg: BrokerOrderLeg,
        *,
        client_order_id: str,
    ) -> BrokerOrder:
        del client_order_id
        raise AssertionError("the warmup bar must not submit an order")

    async def cancel(self, _order_id: str) -> None:
        raise AssertionError("the warmup bar must not cancel an order")

    async def get_order_by_client_order_id(
        self,
        _client_order_id: str,
    ) -> BrokerOrder | None:
        return None


class _ResumeFeed:
    """Replay one explicitly installed batch, then hold or fail."""

    feed_id = "issue-1692"

    def __init__(self) -> None:
        self._bars: tuple[MarketDataBar, ...] = ()
        self._error: Exception | None = None
        self.bars_consumed = 0

    def install(
        self,
        bars: tuple[MarketDataBar, ...],
        *,
        error: Exception | None = None,
    ) -> None:
        self._bars = bars
        self._error = error
        self.bars_consumed = 0

    async def stream_bars(
        self,
        _symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]:
        del use_rth
        for bar in self._bars:
            self.bars_consumed += 1
            yield bar
        if self._error is not None:
            raise self._error
        await asyncio.Event().wait()

    async def recent_closed_bars(
        self,
        _symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del use_rth, lookback_days
        return []

    def health(self, _symbol: str | None = None) -> FeedHealth:
        return FeedHealth(
            connected=True,
            stale=False,
            last_bar_ms=self._bars[-1].start_ms if self._bars else None,
            reason="",
            active_subscription_count=0,
            observed_at_ms=now_ms_utc(),
        )


async def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 2.0,
) -> None:
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.01)


def _first_resumed_bar() -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=1_787_153_280_000,
        end_ms=1_787_153_340_000,
        open=Decimal("771.12"),
        high=Decimal("771.14"),
        low=Decimal("771.01"),
        close=Decimal("771.12"),
        volume=14_463,
        fetched_at_ms=1_787_153_345_348,
        feed_id="ibkr",
        session_phase="RTH",
    )


def _clerk_run_count(db_path: Path) -> int:
    """Count Clerk-registered runs, proving/disproving a phantom registration."""
    with sqlite3.connect(db_path) as connection:
        return connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0]


def _registry_with_sqlite_clerk(
    tmp_path: Path,
    feed: _ResumeFeed,
) -> tuple[ClerkSqliteRepository, SqliteAlpacaClerkFacade, BotTaskRegistry]:
    repository = ClerkSqliteRepository.initialize(
        account_id="PA-TEST",
        artifacts_root=tmp_path / "clerk",
    )
    broker = _FlatBroker()
    clerk = SqliteAlpacaClerkFacade(repo=repository, read=broker, trade=broker)
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        boot_recovery_required=False,
        start_custody_guard=clerk.start_admission_snapshot,
        market_liveness=_tradable_market_liveness,
    )
    return repository, clerk, registry


@pytest.mark.asyncio
async def test_unhandled_error_is_preserved_only_on_immutable_run_evidence(
    tmp_path: Path,
) -> None:
    crash_message = "TradeBar.__init__() got an unexpected keyword argument 'start_ms'"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=TypeError(crash_message))
    repository, clerk, registry = _registry_with_sqlite_clerk(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
        )
        await _wait_for(lambda: not registry.any_running())

        status = registry.status("alpaca", _STRATEGY_INSTANCE_ID)
        current_run = registry.current_run("alpaca", _STRATEGY_INSTANCE_ID)
        assert status.duty_outcome is not None
        assert status.duty_outcome.kind == "CRASHED"
        assert current_run.terminal_outcome is not None
        diagnostic = current_run.terminal_outcome.crash_diagnostic
        assert diagnostic is not None
        assert diagnostic.exception_type == "TypeError"
        assert diagnostic.message == crash_message
        assert diagnostic.source_file.endswith(
            "tests/services/test_bot_runner_ema_resume.py"
        )
        assert diagnostic.source_line > 0

        outcome_path = (
            tmp_path
            / "runner/live_state"
            / _STRATEGY_INSTANCE_ID
            / "run_outcomes"
            / f"{current_run.run_id}.json"
        )
        outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        assert outcome["crash_diagnostic"] == diagnostic.model_dump(mode="json")
        lifecycle = (
            tmp_path
            / "runner/live_state"
            / _STRATEGY_INSTANCE_ID
            / "lifecycle_state.json"
        ).read_text(encoding="utf-8")
        assert "crash_diagnostic" not in lifecycle
    finally:
        set_alpaca_clerk(None)
        repository.close()


@pytest.mark.asyncio
async def test_ema_resume_does_not_decide_on_an_incomplete_signal_bucket(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resumed minute bar waits for its 15-minute signal bucket to close."""
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        _tradable_market_liveness,
    )
    monkeypatch.setattr(
        clerk_runtime,
        "market_liveness_fact",
        _tradable_market_liveness,
    )
    feed = _ResumeFeed()
    repository, clerk, registry = _registry_with_sqlite_clerk(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await registry.stop("alpaca", _STRATEGY_INSTANCE_ID)
        feed.install((_first_resumed_bar(),))

        resumed = await registry.resume_existing("alpaca", _STRATEGY_INSTANCE_ID)
        await _wait_for(lambda: feed.bars_consumed == 1)

        status = registry.status("alpaca", _STRATEGY_INSTANCE_ID)
        decisions = repository.decision_receipt_tail(
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            limit=1,
        )
        assert resumed.active_run_id == status.active_run_id
        assert status.running is True
        assert status.duty_outcome is None
        assert decisions == []
    finally:
        try:
            if registry.any_running():
                await registry.stop("alpaca", _STRATEGY_INSTANCE_ID)
        finally:
            set_alpaca_clerk(None)
            repository.close()


@pytest.mark.asyncio
async def test_resume_after_diagnostic_crash_reuses_the_existing_receipt(
    tmp_path: Path,
) -> None:
    """PRD #1716 AC-1: a diagnostic-bearing crash can Resume into a new run,
    with the previous receipt left byte-for-byte unchanged."""
    crash_message = "TradeBar.__init__() got an unexpected keyword argument 'start_ms'"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=TypeError(crash_message))
    repository, clerk, registry = _registry_with_sqlite_clerk(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
        )
        await _wait_for(lambda: not registry.any_running())

        crashed_run = registry.current_run("alpaca", _STRATEGY_INSTANCE_ID)
        assert crashed_run.terminal_outcome is not None
        assert crashed_run.terminal_outcome.kind == "CRASHED"
        diagnostic = crashed_run.terminal_outcome.crash_diagnostic
        assert diagnostic is not None
        outcome_path = (
            tmp_path
            / "runner/live_state"
            / _STRATEGY_INSTANCE_ID
            / "run_outcomes"
            / f"{crashed_run.run_id}.json"
        )
        original_receipt_bytes = outcome_path.read_bytes()

        feed.install((_first_resumed_bar(),))
        resumed = await registry.resume_existing("alpaca", _STRATEGY_INSTANCE_ID)
        await _wait_for(lambda: feed.bars_consumed == 1)

        assert resumed.active_run_id != crashed_run.run_id
        assert resumed.running is True
        assert outcome_path.read_bytes() == original_receipt_bytes

        preserved = json.loads(outcome_path.read_text(encoding="utf-8"))
        assert preserved["crash_diagnostic"] == diagnostic.model_dump(mode="json")
    finally:
        try:
            if registry.any_running():
                await registry.stop("alpaca", _STRATEGY_INSTANCE_ID)
        finally:
            set_alpaca_clerk(None)
            repository.close()


@pytest.mark.asyncio
async def test_resume_with_an_unreadable_receipt_is_denied_before_clerk_registration(
    tmp_path: Path,
) -> None:
    """PRD #1716 FR-3/FR-4: an unreadable receipt denies admission with
    TERMINAL_EVIDENCE_UNREADABLE before any Clerk registration, process
    activity, or current_run.json advancement is attempted."""
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=TypeError("boom"))
    repository, clerk, registry = _registry_with_sqlite_clerk(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_STRATEGY_INSTANCE_ID,
            strategy_key="ema_crossover_signal",
            symbol="SPY",
        )
        await _wait_for(lambda: not registry.any_running())
        crashed_run = registry.current_run("alpaca", _STRATEGY_INSTANCE_ID)
        outcome_path = (
            tmp_path
            / "runner/live_state"
            / _STRATEGY_INSTANCE_ID
            / "run_outcomes"
            / f"{crashed_run.run_id}.json"
        )
        outcome_path.write_text("{not valid json", encoding="utf-8")
        current_run_path = (
            tmp_path / "runner/live_state" / _STRATEGY_INSTANCE_ID / "current_run.json"
        )
        original_current_run_bytes = current_run_path.read_bytes()
        clerk_run_count_before = _clerk_run_count(repository.db_path)

        # AC-4b / FR-3: preview and execution evaluate the identical rule, so
        # the panel can present Resume as unavailable before the click.
        preview = await registry.preview_resume_admission("alpaca", _STRATEGY_INSTANCE_ID)
        assert preview.allowed is False
        assert preview.reason_code == "TERMINAL_EVIDENCE_UNREADABLE"

        feed.install((_first_resumed_bar(),))
        with pytest.raises(RunAdmissionRefusedError) as exc_info:
            await registry.resume_existing("alpaca", _STRATEGY_INSTANCE_ID)

        decision = exc_info.value.admission_decision
        assert decision is not None
        assert decision.reason_code == "TERMINAL_EVIDENCE_UNREADABLE"
        assert registry.any_running() is False
        assert current_run_path.read_bytes() == original_current_run_bytes
        assert outcome_path.read_text(encoding="utf-8") == "{not valid json"
        assert _clerk_run_count(repository.db_path) == clerk_run_count_before
    finally:
        set_alpaca_clerk(None)
        repository.close()
