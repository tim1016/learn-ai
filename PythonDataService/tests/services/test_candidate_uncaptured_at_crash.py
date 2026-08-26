"""Fault-injection coverage for FR-016 (issue #1728, PRD section 13.3/13.4):
``docs/prds/sealed-signal-program-to-governed-alpaca-bot.md``.

Replay must recreate a staged EMA candidate a crash left uncaptured -- one
that reached ``SignalSession.advance()`` but never reached Clerk intake --
record it as a named ``CANDIDATE_UNCAPTURED_AT_CRASH`` receipt, apply
``DISCARD``, and never give it a second (or any) broker contact. This is
distinct from the already-merged Resume-after-crash work (PRD #1716): that
restores a crashed bot's process; this proves the evidence trail for one
specific evaluation that never reached custody.

Reuses the proven LEAN-parity EMA crossover fixture and Clerk test double
from ``tests/services/bot_runner/conftest.py`` (``_ema_parity_bars_through_first_exit``,
``_FakeClerk``, ``_tradable_market_liveness``) instead of re-deriving a
fresh crossover fixture -- see CLAUDE.md "don't duplicate utility
functions". Modeled on the crash-simulation idiom in
``tests/broker/alpaca/clerk/sqlite/test_atomic_seam_fault_injection.py``:
a crash is simulated by simply never performing the next step, not by
throwing mid-function.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from tests.services.bot_runner.conftest import (
    _EMA_FIRST_EXIT_MS,
    _SID,
    _ema_parity_bars_through_first_exit,
    _ema_signal_evaluation_id,
    _FakeClerk,
    _tradable_market_liveness,
)


@pytest.fixture(autouse=True)
def _fresh_live_market_liveness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mirror ``bot_runner/conftest.py``'s autouse fixture of the same name.

    This module calls ``run_trade_bot`` directly rather than through
    ``BotTaskRegistry``, so only the two module-level bindings it actually
    reaches (``bot_trade_strategy``'s ENTER liveness gate and
    ``clerk_runtime``'s submission-boundary recheck) need patching.
    """
    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", _tradable_market_liveness)
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", _tradable_market_liveness)


class _PhaseFeed:
    """``MarketDataFeed`` double with independently controlled live vs
    retained bars.

    ``live_bars`` flow through ``stream_bars`` -- the DECIDE-mode loop a
    genuinely running process consumes and durably captures a receipt for
    every closed 15-minute bucket. ``retained_bars`` flow through
    ``recent_closed_bars`` -- exactly what a durable source-bar ledger hands
    a booting process for warmup/replay (PRD section 13.1/13.3). Production
    keeps these two channels just as separate: ``_RetainedSourceBarFeed``
    only appends to the ledger as bars stream live; it never treats one
    boot's ``stream_bars`` output as another boot's retained history.
    """

    feed_id = "fake-phase"

    def __init__(
        self,
        *,
        live_bars: Sequence[MarketDataBar] = (),
        retained_bars: Sequence[MarketDataBar] = (),
    ) -> None:
        self._live_bars = list(live_bars)
        self._retained_bars = list(retained_bars)

    async def stream_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
    ) -> AsyncIterator[MarketDataBar]:
        del symbol, use_rth
        for bar in self._live_bars:
            yield bar

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del symbol, use_rth, lookback_days
        return list(self._retained_bars)


def _binding(*, run_id: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=3,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id=run_id,
        created_at_ms=0,
    )


@pytest.mark.asyncio
async def test_replay_recreates_a_crashed_candidate_as_uncaptured_with_no_broker_contact(
    tmp_path: Path,
) -> None:
    """Stage a real EMA EXIT candidate, drop the process before Clerk
    intake ever sees it, then prove replay recreates it, records it as
    ``CANDIDATE_UNCAPTURED_AT_CRASH`` with ``DISCARD``, and never routes it
    to the Clerk's execute path (i.e. never reaches, let alone repeats, a
    broker contact)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    try:
        bars = _ema_parity_bars_through_first_exit()
        # The consolidator only fires a completed 15-minute bucket lazily,
        # when the *next* minute bar arrives (see `_drain_bar`'s docstring
        # in bot_trade_strategy.py) -- bars[-1] is that triggering bar for
        # the EXIT bucket, and everything before it is safe to run live.
        crashed_run_bars, crash_bar = bars[:-1], bars[-1]
        assert crash_bar.end_ms > _EMA_FIRST_EXIT_MS

        # Phase 1 ("the crashed run"): every bar up to, but not including,
        # the EXIT-triggering bar is decided live -- the real ENTER reaches
        # the Clerk and is captured. The process then "crashes": simply
        # never being fed the final bar is the same crash-simulation idiom
        # test_atomic_seam_fault_injection.py uses ("simulated here by
        # simply never calling ...").
        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-1"), _PhaseFeed(live_bars=crashed_run_bars)
        )
        calls_before_resume = len(clerk.calls)
        assert calls_before_resume >= 1  # the real ENTER already landed
        assert clerk.calls[-1]["purpose"] == "ENTER"

        # Phase 2 ("resume/replay"): a fresh process, a new run_id, and no
        # live stream of its own -- its only input is the durable retained
        # ledger, which now includes the crash bar exactly as the real
        # SourceBarLedger would hand a resumed bot (PRD section 13.1/13.3).
        await bot_trade_strategy.run_trade_bot(_binding(run_id="run-2"), _PhaseFeed(retained_bars=bars))

        # No new broker-bound call was ever made for the recovered candidate.
        assert len(clerk.calls) == calls_before_resume

        receipts = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=500)
        newest = receipts[-1]
        assert newest.outcome == "candidate_uncaptured_at_crash"
        assert newest.intent_id == _ema_signal_evaluation_id(_EMA_FIRST_EXIT_MS)
        facts = json.loads(newest.facts_json)
        assert facts["reason_code"] == "CANDIDATE_UNCAPTURED_AT_CRASH"
        assert facts["retention_class"] == "protected_crash_evidence"
        assert facts["run_id"] == "run-2"
        # Direction 2: the reconstructed stage's trace is carried into the
        # crash-recovered evaluation, so this protected crash-window receipt
        # captures its per-bucket digest -- crash-window decision content stays
        # content-verifiable, not digest-less (Codex PR #1766).
        assert len(facts["trace_digest"]) == 64
        assert facts["decision_bar_close_ms"] == _EMA_FIRST_EXIT_MS
        # Nothing else ever recorded this exact candidate as reaching custody.
        assert not any(receipt.outcome in ("exit_intent", "exited") for receipt in receipts)
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_replay_of_a_clean_stop_never_fabricates_crash_evidence(
    tmp_path: Path,
) -> None:
    """A graceful boundary (every retained bar already durably captured) is
    not a crash: replaying the exact same retained window a second time
    must not append a second, spurious ``CANDIDATE_UNCAPTURED_AT_CRASH``
    receipt (FR-017's idempotent-replay guarantee applied to this new
    outcome)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    try:
        bars = _ema_parity_bars_through_first_exit()

        # Run the entire window live, uninterrupted -- the EXIT bucket is
        # captured normally, same as test_ema_trade_bot_matches_first_lean_round_trip.
        await bot_trade_strategy.run_trade_bot(_binding(run_id="run-1"), _PhaseFeed(live_bars=bars))
        receipts_after_first_run = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=500)
        assert any(receipt.outcome == "exit_intent" for receipt in receipts_after_first_run)

        # Resume with the identical window handed back as retained history a
        # second time -- the only true crash-window bar (the one after the
        # EXIT decision) does not exist yet, so this must never fabricate
        # crash evidence.
        await bot_trade_strategy.run_trade_bot(_binding(run_id="run-2"), _PhaseFeed(retained_bars=bars))

        receipts_after_resume = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=500)
        assert receipts_after_resume == receipts_after_first_run
        assert not any(
            receipt.outcome == "candidate_uncaptured_at_crash" for receipt in receipts_after_resume
        )
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_never_run_instance_gets_no_crash_evidence_from_cold_warmup(
    tmp_path: Path,
) -> None:
    """A brand-new instance's very first warmup replays retained history
    that predates its own existence (the ledger is shared across bots on a
    symbol). None of that is a crash artifact: with no prior durable
    decision receipt at all, replay must never flag a candidate."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    set_alpaca_clerk(clerk)
    try:
        bars = _ema_parity_bars_through_first_exit()

        # A cold-start bot whose symbol's history is already retained in the
        # shared ledger: recent_closed_bars hands it the full window on its
        # very first run, with no live bars of its own yet.
        await bot_trade_strategy.run_trade_bot(_binding(run_id="run-1"), _PhaseFeed(retained_bars=bars))

        receipts = repo.decision_receipt_tail(strategy_instance_id=_SID, limit=500)
        assert not any(receipt.outcome == "candidate_uncaptured_at_crash" for receipt in receipts)
    finally:
        set_alpaca_clerk(None)
        repo.close()
