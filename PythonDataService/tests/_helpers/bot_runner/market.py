"""Market-liveness test support shared by the bot_runner test package and
outside suites that exercise ``BotTaskRegistry`` / ``run_trade_bot`` end to
end.

Split out of ``tests/services/bot_runner/conftest.py`` (issue #1810): the
review found that file acting as an undeclared public library, with an
autouse fixture that outside modules imported purely for its registration
side effect. ``patch_fresh_live_market_liveness`` is the extracted
implementation -- every consumer (the bot_runner package's own autouse
fixture, and each outside module's own explicit autouse fixture) calls it
directly instead of importing a fixture to trigger it by side effect.
"""

from __future__ import annotations

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_runner as bot_runner
import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.engine.data.trade_bar import TradeBar
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    SymbolTradingStatusEvidence,
)
from app.schemas.run_admission import StrategyValidationAdmissionFact
from app.services.market_liveness import compose_market_liveness


def _tradable_market_liveness(symbol: str, observed_at_ms: int):
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


def _verified_validation_fact(_binding: object, observed_at_ms: int) -> StrategyValidationAdmissionFact:
    """Keep runner tests focused on task/custody behavior, not manifest fixtures."""
    return StrategyValidationAdmissionFact(
        state="VERIFIED",
        strategy_key="deployment_validation",
        evidence_status="accepted",
        event_id="test-validation-event",
        evidence_snapshot_sha256="a" * 64,
        verified_at_ms=observed_at_ms,
        explanation="Test validation evidence is current.",
    )


def patch_fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch every module-level ``market_liveness_fact`` binding (and the
    strategy-validation admission fact) the bot_runner code paths read, so
    a test starts with every symbol live/tradable and validation VERIFIED.

    Callers wrap this in their own ``@pytest.fixture(autouse=True)`` --
    registration is explicit at each call site rather than an import-only
    side effect (issue #1810).
    """
    monkeypatch.setattr(bot_runner, "market_liveness_fact", _tradable_market_liveness)
    # #1671: the Clerk's own submission-boundary recheck (runtime.py) reads
    # this module's import of the same name -- a separate binding from
    # bot_trade_strategy's, so it needs its own patch or it falls through to
    # the real (unconfigured, fail-closed) store and every ENTER is rejected.
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        _tradable_market_liveness,
    )
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _tradable_market_liveness)
    monkeypatch.setattr(bot_runner, "current_strategy_validation_fact", _verified_validation_fact)
    from datetime import date
    from types import ModuleType, SimpleNamespace

    from app.lean_sidecar.trading_calendar import session_open_ms_utc
    from app.utils import timestamps

    # One controllable source backs imported/default clock callables too.
    start = session_open_ms_utc(date(2026, 9, 25)) + 60_000
    if isinstance(timestamps.time, ModuleType):
        monkeypatch.setattr(timestamps, "time", SimpleNamespace(time=lambda: start / 1000))


def patch_wall_clock_to_the_fed_bar(
    monkeypatch: pytest.MonkeyPatch, *, start_ms: int | None = None,
) -> None:
    """Pin the staleness gate's wall clock to the close of the bar just fed (#2303/#2345).

    Runner suites replay fixed, historical bar timestamps as if they were live;
    against the real wall clock every decision would be days late. This clock
    reads "now" as the ``end_ms`` of the last bar the adapter drained -- the bar
    that fired the bucket -- so the real gate (``feed_continuity_policy.late_decision``)
    still runs on every decision and sees exactly what a promptly delivered live
    bar would show it. Before any bar is fed it falls through to the real clock.
    For a session-boundary replay, pass ``start_ms`` before constructing the
    runner or Clerk. All imported/default ``now_ms_utc`` callables, including
    the execution lease, then share that initial instant and advance together.
    The caller chooses a lease TTL covering its replayed span. Other mechanics
    tests may keep their explicitly pinned admission clock.
    A test that needs a late decision pins ``feed_continuity_policy.now_ms_utc``
    itself after this fixture ran.
    """
    fed_bar_end_ms: list[int] = []
    real_drain_bar = bot_trade_strategy._drain_bar
    from app.utils import timestamps

    initial_ms = timestamps.now_ms_utc() if start_ms is None else start_ms
    def replay_now() -> int:
        return fed_bar_end_ms[0] if fed_bar_end_ms else initial_ms

    if start_ms is not None:
        from types import SimpleNamespace

        monkeypatch.setattr(timestamps, "time", SimpleNamespace(time=lambda: replay_now() / 1000))

    def _drain_and_tick(strategy: object, context: object, bar: TradeBar) -> None:
        fed_bar_end_ms[:] = [bar.end_ms]
        real_drain_bar(strategy, context, bar)  # type: ignore[arg-type]

    monkeypatch.setattr(bot_trade_strategy, "_drain_bar", _drain_and_tick)
    monkeypatch.setattr(feed_continuity_policy, "now_ms_utc", replay_now)
