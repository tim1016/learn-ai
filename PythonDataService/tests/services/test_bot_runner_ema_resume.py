"""Focused resume regression coverage for issue #1692."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg, BrokerPosition
from app.engine.live.account_artifacts import RestartIntensityPolicy
from app.marketdata.feed import ContinuityPolicy, FeedHealth, MarketDataBar
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)
from app.services.bot_runner import BotTaskRegistry
from app.services.market_liveness import compose_market_liveness
from app.utils.timestamps import now_ms_utc
from tests._helpers.bot_runner.custody import admission_guard_for
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

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
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]:
        del use_rth, continuity
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
    clerk = SqliteAlpacaClerkFacade(repo=repository, read=broker, trade=broker, account_mode="paper")
    registry = BotTaskRegistry(
        tmp_path / "runner",
        feed_resolver=lambda: feed,
        restart_policy=RestartIntensityPolicy(threshold=100),
        boot_recovery_required=False,
        start_custody_guard=admission_guard_for(clerk),
        market_liveness=_tradable_market_liveness,
    )
    return repository, clerk, registry


@pytest.mark.asyncio
async def test_unhandled_error_is_preserved_only_on_immutable_run_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # This test proves crash-diagnostic plumbing, not deploy admission.
    crash_message = "TradeBar.__init__() got an unexpected keyword argument 'start_ms'"
    feed = _ResumeFeed()
    feed.install((_first_resumed_bar(),), error=TypeError(crash_message))
    repository, clerk, registry = _registry_with_sqlite_clerk(tmp_path, feed)
    set_alpaca_clerk(clerk)
    try:
        await registry.deploy(
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
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


@pytest.fixture(autouse=True)
def _open_start_window(monkeypatch):
    from tests._helpers.bot_runner.market import patch_fresh_live_market_liveness
    patch_fresh_live_market_liveness(monkeypatch)
