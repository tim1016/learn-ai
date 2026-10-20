"""Market-liveness gating on entry: unknown/halted/closed clocks and the
declared extended-hours window.

Split from ``tests/services/test_bot_runner.py`` (issue #1737, seam 3).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.marketdata.feed import FeedHealth
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.decision_session import RunDecisionSession
from app.services.market_liveness import compose_market_liveness, unknown_market_liveness
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID, _T0, _registry, deploy_bot
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.bot_runner.market import _tradable_market_liveness
from tests._helpers.canary_admission import admit_canary_pairing
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

from ._support import _WIN_START_MS, _green_bar, _red_bar, _wait_for

_RTH_SESSION = RunDecisionSession(kind="rth", window=None)


def _unknown_liveness(symbol: str, observed_at_ms: int) -> MarketLivenessFact:
    return unknown_market_liveness(
        symbol,
        observed_at_ms=observed_at_ms,
    )


def _halted_symbol_under_an_open_clock(symbol: str, observed_at_ms: int) -> MarketLivenessFact:
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
            state="HALTED",
            source="test.symbol-status",
            observed_at_ms=observed_at_ms,
            source_timestamp_ms=observed_at_ms,
        ),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("liveness", "reason_code", "state"),
    [
        pytest.param(_unknown_liveness, "MARKET_LIVENESS_UNAVAILABLE", "UNKNOWN", id="unknown liveness"),
        # #1671 AC4: a market-wide OPEN clock with a HALTED symbol must never claim tradability at the actual
        # submit-blocking layer (not just at the compose/display layers, which have their own dedicated tests).
        pytest.param(_halted_symbol_under_an_open_clock, "SYMBOL_HALTED", "HALTED", id="halted symbol under an open clock (#1671 AC4)"),
    ],
)
async def test_liveness_that_does_not_prove_the_symbol_tradable_blocks_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    liveness: Callable[[str, int], MarketLivenessFact],
    reason_code: str,
    state: str,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),  # ENTER is refused by liveness.
        _red_bar(_WIN_START_MS + 180_000),
        _red_bar(_WIN_START_MS + 240_000),
        _red_bar(_WIN_START_MS + 300_000),
    ]
    registry = _registry(tmp_path, _FakeFeed(bars, mode="hold"))
    set_alpaca_clerk(clerk)
    try:
        await deploy_bot(registry,
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(repo.decision_receipt_tail(strategy_instance_id=_SID, limit=10)) >= 5)
        await registry.stop("alpaca", _SID)

        # Rolled back (#1671 AC6): no real entry means no phantom EXIT either, so nothing reaches the Clerk;
        # see test_blocked_entry_is_rolled_back_and_can_re_enter for the regression this rollback targets.
        assert clerk.calls == [], "the blocked ENTER was rolled back: no ENTER and no phantom EXIT reached the Clerk"
        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=5)
        blocked = next(decision for decision in decisions if decision.outcome == "blocked")
        blocked_facts = json.loads(blocked.facts_json)
        assert blocked_facts["reason_code"] == reason_code, "the blocked decision names the liveness reason"
        assert blocked_facts["market_liveness"]["state"] == state, "the blocked decision carries the liveness state"
        assert all(decision.outcome in {"blocked", "no_action"} for decision in decisions), "every decision is blocked or no_action"
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_blocked_entry_is_rolled_back_and_can_re_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1671 AC6 regression: before the rollback fix, a blocked ENTER left
    the strategy's own position-lifecycle state set (``_entry_pending``/
    ``_in_position``), so the *next* green-bar pair was consumed by the
    stale exit countdown instead of starting a fresh entry attempt — and the
    phantom EXIT it eventually emitted had no real custody to close,
    crashing the bot run with ``MissingEntryCustodyError``. With the
    rollback (``DeploymentValidationConsecutiveGreen.rollback_blocked_
    entry``, issue #1730 Slice 5's Signal Program promotion), the blocked
    bar leaves no trace: the following two green bars start a clean entry,
    and the three red bars after that close it out normally."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    # market_liveness_fact is only ever queried on an ENTER intent (#1671
    # AC3), so the Nth call corresponds exactly to the Nth ENTER attempt —
    # a reliable way to block just the first one. Bar-relative fixture
    # constants can't be compared against `observed_at_ms`: the real gate
    # passes it `now_ms_utc()`, actual wall-clock time, not the bar's.
    entry_attempts = {"count": 0}

    def liveness(symbol: str, observed_at_ms: int):
        entry_attempts["count"] += 1
        if entry_attempts["count"] == 1:
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
                    state="HALTED",
                    source="test.symbol-status",
                    observed_at_ms=observed_at_ms,
                    source_timestamp_ms=observed_at_ms,
                ),
            )
        return _tradable_market_liveness(symbol, observed_at_ms)

    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),  # ENTER attempt #1 — blocked, rolled back.
        _green_bar(_WIN_START_MS + 180_000),
        _green_bar(_WIN_START_MS + 240_000),  # ENTER attempt #2 — fresh streak, TRADABLE.
        _red_bar(_WIN_START_MS + 300_000),
        _red_bar(_WIN_START_MS + 360_000),
        _red_bar(_WIN_START_MS + 420_000),  # EXIT for the real entry.
    ]
    registry = _registry(tmp_path, _FakeFeed(bars, mode="hold"))
    set_alpaca_clerk(clerk)
    try:
        await deploy_bot(registry,
            exit_terms=DEPLOY_EXIT_TERMS, broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(clerk.calls) == 2)
        await registry.stop("alpaca", _SID)

        assert [call["purpose"] for call in clerk.calls] == ["ENTER", "EXIT"]
    finally:
        set_alpaca_clerk(None)
        repo.close()


def _closed_clock_liveness() -> MarketLivenessFact:
    """The one fact all four gate cases share: a fresh, CLOSED broker clock."""
    return compose_market_liveness(
        "SPY",
        now_ms=1_700_000_000_000,
        market_clock=MarketClockLivenessEvidence(
            state="CLOSED",
            source="test.clock",
            observed_at_ms=1_700_000_000_000,
            vendor_timestamp_ms=1_700_000_000_000,
        ),
        connected=True,
        connection_changed_at_ms=1_700_000_000_000,
        symbol_status=None,
    )


def _feed_with_health(*, connected: bool, stale: bool) -> _FakeFeed:
    """A feed whose ``health`` reports exactly this liveness."""
    feed = _FakeFeed([], mode="finite")
    feed.health = lambda _symbol=None: FeedHealth(  # type: ignore[method-assign]
        connected=connected,
        stale=stale,
        last_bar_ms=None,
        reason="" if connected and not stale else "test",
        active_subscription_count=1,
        observed_at_ms=1_700_000_000_000,
    )
    return feed


def _feed_whose_health_probe_raises() -> _FakeFeed:
    """A health probe that raises proves nothing."""

    def _raise(_symbol: str | None = None):
        raise RuntimeError("probe exploded")

    feed = _FakeFeed([], mode="finite")
    feed.health = _raise  # type: ignore[method-assign]
    return feed


# Alpaca's clock is RTH-only (#1671): a non-RTH binding whose schedule proves PRE/POST and whose feed is printing must
# not be blocked just because the RTH-only clock reports CLOSED. The declared window proves the *schedule*, not
# liveness: Alpaca's clock reports CLOSED through every scheduled extended session, so it cannot distinguish one from
# an unscheduled PRE/POST closure. The live evidence that separates them is the feed printing bars for the symbol;
# without it, a static window would have admitted new exposure straight into a closed venue. A health probe that
# raises proves nothing, so it refuses.
@pytest.mark.parametrize(
    ("make_feed", "blocked"),
    [
        pytest.param(lambda: _feed_with_health(connected=True, stale=False), False, id="fresh bars: not blocked"),
        pytest.param(lambda: _feed_with_health(connected=False, stale=False), True, id="no fresh bars: blocked"),
        pytest.param(_feed_whose_health_probe_raises, True, id="unreadable feed health: blocked"),
    ],
)
def test_closed_liveness_with_a_proven_phase_blocks_entry_unless_the_feed_proves_fresh_bars(
    monkeypatch: pytest.MonkeyPatch,
    make_feed: Callable[[], _FakeFeed],
    blocked: bool,
) -> None:
    from types import SimpleNamespace

    monkeypatch.setattr("app.services.market_liveness.declared_extended_phase_at_ms", lambda **_kwargs: True)
    binding = SimpleNamespace(use_rth=False, symbol="SPY")

    result = bot_trade_strategy._liveness_blocks_entry(
        binding,
        _closed_clock_liveness(),
        _RTH_SESSION,
        make_feed(),
    )

    assert result is blocked, "a CLOSED clock with a proven phase blocks entry exactly when the feed does not prove fresh bars"


@pytest.mark.asyncio
async def test_extended_hours_entry_is_proven_by_the_authoritys_declared_window(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-bar ENTER gate proves an extended phase from the executing
    authority's declared window (ADR 0059 D5.2) and from nothing else: with
    the RTH-only clock reporting CLOSED, ``run_trade_bot`` admits the ENTER
    only because the gate consulted the Clerk's declared window.

    Calls ``run_trade_bot`` directly rather than through
    ``BotTaskRegistry.deploy_with_admission()``: Start admission is a separate gate with
    its own market-data-readiness requirements unrelated to what this test
    targets (the per-bar ENTER liveness gate's window)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-ALPACA-EXEC", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-ALPACA-EXEC"
    # An extended run needs an authority that declares a window; without one
    # ``run_trade_bot`` refuses before it streams (Start admission refuses the
    # same deploy), so this gate would never be reached.
    clerk.program_leg_policy = ProgramLegPolicy(
        window=ALPACA_EXTENDED_HOURS_WINDOW, allowances=None
    )

    def liveness(symbol: str, observed_at_ms: int) -> MarketLivenessFact:
        return compose_market_liveness(
            symbol,
            now_ms=observed_at_ms,
            market_clock=MarketClockLivenessEvidence(
                state="CLOSED",
                source="test.clock",
                observed_at_ms=observed_at_ms,
                vendor_timestamp_ms=observed_at_ms,
            ),
            connected=True,
            connection_changed_at_ms=observed_at_ms,
            symbol_status=None,
        )

    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
    seen_windows: list[object | None] = []

    def fake_declared_extended_phase_at_ms(*, now_ms: int, extended_window: object | None) -> bool:
        seen_windows.append(extended_window)
        return extended_window is not None

    monkeypatch.setattr("app.services.market_liveness.declared_extended_phase_at_ms", fake_declared_extended_phase_at_ms)

    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),
    ]
    feed = _FakeFeed(bars, mode="finite")
    binding = BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=False,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-ALPACA-EXEC")
    set_alpaca_clerk(clerk)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger)

        assert [call["purpose"] for call in clerk.calls] == ["ENTER"]
        assert seen_windows and all(window == ALPACA_EXTENDED_HOURS_WINDOW for window in seen_windows)
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()
