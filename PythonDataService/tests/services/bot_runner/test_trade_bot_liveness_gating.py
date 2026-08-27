"""Market-liveness gating on entry: unknown/halted/closed clocks and
extended-hours capability scoping.

Split from ``tests/services/test_bot_runner.py`` (issue #1737, seam 3).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)
from app.services.bot_binding_repository import (
    BrokerBotBinding,
    alpaca_v1_action_plan,
)
from app.services.market_liveness import compose_market_liveness, unknown_market_liveness
from tests._helpers.bot_runner.custody import _SID, _T0, _registry
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests._helpers.bot_runner.market import _tradable_market_liveness
from tests._helpers.canary_admission import admit_canary_pairing

from ._support import _WIN_START_MS, _green_bar, _red_bar, _wait_for


@pytest.mark.asyncio
async def test_unknown_liveness_blocks_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        lambda symbol, observed_at_ms: unknown_market_liveness(
            symbol,
            observed_at_ms=observed_at_ms,
        ),
    )
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
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(repo.decision_receipt_tail(strategy_instance_id=_SID, limit=10)) >= 5)
        await registry.stop("alpaca", _SID)

        # Rolled back (#1671 AC6): no real entry means no phantom EXIT either.
        assert clerk.calls == []
        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=5)
        blocked = next(decision for decision in decisions if decision.outcome == "blocked")
        blocked_facts = json.loads(blocked.facts_json)
        assert blocked_facts["reason_code"] == "MARKET_LIVENESS_UNAVAILABLE"
        assert blocked_facts["market_liveness"]["state"] == "UNKNOWN"
        assert all(decision.outcome in {"blocked", "no_action"} for decision in decisions)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_halted_liveness_blocks_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1671 AC4: a market-wide OPEN clock with a HALTED symbol must never
    claim tradability at the actual submit-blocking layer (not just at the
    compose/display layers, which have their own dedicated tests)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    admit_canary_pairing(monkeypatch, "deployment_validation", "paper-account")
    monkeypatch.setattr(
        bot_trade_strategy,
        "market_liveness_fact",
        lambda symbol, observed_at_ms: compose_market_liveness(
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
        ),
    )
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
        await registry.deploy(
            broker="alpaca",
            strategy_instance_id=_SID,
            strategy_key="deployment_validation",
            symbol="SPY",
            mode="trade",
            quantity=1,
        )
        await _wait_for(lambda: len(repo.decision_receipt_tail(strategy_instance_id=_SID, limit=10)) >= 5)
        await registry.stop("alpaca", _SID)

        # No real entry was ever accepted, so no phantom EXIT reaches the
        # Clerk either — the blocked ENTER's state was rolled back (#1671
        # AC6); see test_blocked_entry_is_rolled_back_and_can_re_enter for
        # the regression this rollback specifically targets.
        assert clerk.calls == []
        decisions = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=5)
        blocked = next(decision for decision in decisions if decision.outcome == "blocked")
        blocked_facts = json.loads(blocked.facts_json)
        assert blocked_facts["reason_code"] == "SYMBOL_HALTED"
        assert blocked_facts["market_liveness"]["state"] == "HALTED"
        assert all(decision.outcome in {"blocked", "no_action"} for decision in decisions)
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
        await registry.deploy(
            broker="alpaca",
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


def test_closed_liveness_with_extended_phase_proven_does_not_block_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Alpaca's clock is RTH-only (#1671): a non-RTH binding whose account
    and instrument have a fresh, proven extended-session capability must not
    be blocked just because the RTH-only clock reports CLOSED."""
    from types import SimpleNamespace

    liveness = compose_market_liveness(
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
    monkeypatch.setattr(bot_trade_strategy, "extended_phase_proven_at_ms", lambda **_kwargs: True)
    binding = SimpleNamespace(use_rth=False, symbol="SPY")

    assert bot_trade_strategy._liveness_blocks_entry(binding, "PA-TEST", liveness) is False


def test_closed_liveness_without_extended_phase_proven_still_blocks_entry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a fresh, matching capability the calendar can prove only
    RTH/CLOSED — CLOSED must still block a non-RTH binding's entry."""
    from types import SimpleNamespace

    liveness = compose_market_liveness(
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
    monkeypatch.setattr(bot_trade_strategy, "extended_phase_proven_at_ms", lambda **_kwargs: False)
    binding = SimpleNamespace(use_rth=False, symbol="SPY")

    assert bot_trade_strategy._liveness_blocks_entry(binding, "PA-TEST", liveness) is True


def test_closed_liveness_always_blocks_entry_for_an_rth_only_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An RTH-only binding never consults extended-phase capability — CLOSED
    blocks unconditionally, matching the previous, unambiguous behavior."""
    from types import SimpleNamespace

    liveness = compose_market_liveness(
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
    binding = SimpleNamespace(use_rth=True, symbol="SPY")

    assert bot_trade_strategy._liveness_blocks_entry(binding, "PA-TEST", liveness) is True


@pytest.mark.asyncio
async def test_extended_hours_entry_uses_the_feeds_capability_account_not_the_alpaca_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the Alpaca execution account can never scope an IBKR
    market-data capability snapshot — passing it into the capability lookup
    means the snapshot is never found and every extended-hours entry is
    silently rejected. ``run_trade_bot`` must resolve and pass the market
    data feed's own ``capability_account_id`` instead.

    Calls ``run_trade_bot`` directly rather than through
    ``BotTaskRegistry.deploy()``: Start admission is a separate gate with
    its own market-data-readiness requirements unrelated to what this test
    targets (the per-bar ENTER liveness gate's account-scoping)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-ALPACA-EXEC", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-ALPACA-EXEC"

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
    seen_account_ids: list[str] = []

    def fake_extended_phase_proven_at_ms(*, now_ms: int, symbol: str, account_id: str) -> bool:
        seen_account_ids.append(account_id)
        return True

    monkeypatch.setattr(bot_trade_strategy, "extended_phase_proven_at_ms", fake_extended_phase_proven_at_ms)

    bars = [
        _green_bar(_WIN_START_MS + 60_000),
        _green_bar(_WIN_START_MS + 120_000),
    ]
    feed = _FakeFeed(bars, mode="finite")
    feed.capability_account_id = "IBKR-MKTDATA-ACCT"  # distinct from clerk.account_id above
    binding = BrokerBotBinding(
        strategy_instance_id=_SID,
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
    set_alpaca_clerk(clerk)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed)

        assert [call["purpose"] for call in clerk.calls] == ["ENTER"]
        assert seen_account_ids and all(acct == "IBKR-MKTDATA-ACCT" for acct in seen_account_ids)
    finally:
        set_alpaca_clerk(None)
        repo.close()
