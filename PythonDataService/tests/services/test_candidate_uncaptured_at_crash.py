"""Fault-injection coverage for FR-016 (issue #1728).

Replay must recreate a staged EMA candidate a crash left uncaptured -- one
that reached ``SignalSession.advance()`` but never reached Clerk intake --
record it as a named ``CANDIDATE_UNCAPTURED_AT_CRASH`` receipt, apply
``DISCARD``, and never give it a second (or any) broker contact. This is
distinct from the already-merged Resume-after-crash work (PRD #1716): that
restores a crashed bot's process; this proves the evidence trail for one
specific evaluation that never reached custody.

Reuses the proven LEAN-parity EMA crossover fixture and Clerk test double
from ``tests/_helpers/bot_runner/`` (``_ema_parity_bars_through_first_exit``,
``_FakeClerk``, ``_tradable_market_liveness``) instead of re-deriving a
fresh crossover fixture. Modeled on the crash-simulation idiom in
``tests/broker/alpaca/clerk/sqlite/test_atomic_seam_fault_injection.py``:
a crash is simulated by simply never performing the next step, not by
throwing mid-function.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.decision_receipts import MAX_DECISION_RECEIPTS_PER_STRATEGY
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.lean_sidecar.trading_calendar import is_early_close, session_windows_ms_utc
from app.marketdata.feed import ContinuityPolicy, MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from tests._helpers.bot_runner.custody import _SID
from tests._helpers.bot_runner.doubles import _FakeClerk
from tests._helpers.bot_runner.ema_parity import (
    _EMA_FIRST_EXIT_MS,
    _ema_parity_bars_through_first_exit,
    _ema_signal_evaluation_id,
)
from tests._helpers.bot_runner.market import (
    _tradable_market_liveness,
    patch_wall_clock_to_the_fed_bar,
)
from tests._helpers.signal_program import anchor_session_date


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


@pytest.fixture(autouse=True)
def _wall_clock_at_the_fed_bar(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_wall_clock_to_the_fed_bar(monkeypatch)


class _PhaseFeed:
    """``MarketDataFeed`` double with independently controlled live vs
    retained bars.

    ``live_bars`` flow through ``stream_bars`` -- the DECIDE-mode loop a
    genuinely running process consumes and durably captures a receipt for
    every closed 15-minute bucket. ``retained_bars`` flow through
    ``recent_closed_bars`` -- exactly what a durable source-bar ledger hands
    a booting process for warmup/replay. Production
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
        continuity: ContinuityPolicy | None = None,
    ) -> AsyncIterator[MarketDataBar]:
        del symbol, use_rth, continuity
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


def _binding(*, run_id: str, strategy_key: str = "ema_crossover_signal") -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key=strategy_key,
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
        # A completed 15-minute bucket fires on the minute bar that closes it
        # (see `_drain_bar`'s docstring in bot_trade_strategy.py): the bar
        # ending at the EXIT close is that bar, and everything before it is
        # safe to run live.
        crashed_run_bars = [bar for bar in bars if bar.end_ms < _EMA_FIRST_EXIT_MS]
        assert bars[len(crashed_run_bars)].end_ms == _EMA_FIRST_EXIT_MS

        # Phase 1 ("the crashed run"): every bar up to, but not including,
        # the EXIT bucket's closing bar is decided live -- the real ENTER reaches
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
        # SourceBarLedger would hand a resumed bot.
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
        # second time -- every bucket in it, the EXIT included, already has
        # its receipt, so this must never fabricate crash evidence.
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


# ---------------------------------------------------------------------------
# A bot that has decided more buckets than its receipt window retains (#2850).
# ---------------------------------------------------------------------------

_MINUTE_MS = 60_000
_DV = "deployment_validation"
_EARLY_ENTER_MINUTE = 31
"""Minute of the tape whose close stages the first ENTER: the second of two
green minutes, inside the first session's detection window."""
_LATE_ENTER_MINUTE = 1_081
"""Minute whose close stages an ENTER in the third session, after more
decisions than the receipt window retains."""
_OLDEST_RETAINED_ENTER_MINUTE = _LATE_ENTER_MINUTE - MAX_DECISION_RECEIPTS_PER_STRATEGY
"""Minute whose ENTER is the oldest receipt left once ``_LATE_ENTER_MINUTE``
minutes have been decided: the tape holds one decision per bar."""


def _red_tape(*, minutes: int, enter_minutes: Sequence[int]) -> list[MarketDataBar]:
    """``minutes`` consecutive regular-session one-minute bars, red but for a
    green pair ending at each of ``enter_minutes``.

    ``deployment_validation`` decides on every minute and proposes an ENTER on
    the second of two consecutive green minutes, so the tape holds one
    decision per bar and stages an ENTER exactly at the named minutes.
    """
    first = anchor_session_date()
    starts = [
        start_ms
        for window in session_windows_ms_utc(first, first + timedelta(days=14))
        if not is_early_close(window.session_date)
        for start_ms in range(window.open_ms_utc, window.close_ms_utc, _MINUTE_MS)
    ][:minutes]
    assert len(starts) == minutes
    green = {minute for enter in enter_minutes for minute in (enter - 1, enter)}
    return [
        MarketDataBar(
            symbol="SPY",
            start_ms=start_ms,
            end_ms=start_ms + _MINUTE_MS,
            open=Decimal("500") if minute in green else Decimal("501"),
            high=Decimal("502"),
            low=Decimal("499"),
            close=Decimal("501") if minute in green else Decimal("500"),
            volume=100,
            fetched_at_ms=start_ms + _MINUTE_MS,
            feed_id="fake-phase",
            session_phase="RTH",
        )
        for minute, start_ms in enumerate(starts)
    ]


def _sqlite_clerk(tmp_path: Path) -> tuple[ClerkSqliteRepository, _FakeClerk]:
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path / "clerk")
    repo.register_strategy_instance(strategy_instance_id=_SID, symbol="SPY", config_hash="config-1")
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    return repo, clerk


def _retained(repo: ClerkSqliteRepository) -> list[DecisionReceiptResource]:
    return repo.decision_receipt_tail(
        strategy_instance_id=_SID, limit=MAX_DECISION_RECEIPTS_PER_STRATEGY
    )


def _crash_evidence_closes(repo: ClerkSqliteRepository) -> list[int]:
    return [
        json.loads(receipt.facts_json)["decision_bar_close_ms"]
        for receipt in _retained(repo)
        if receipt.outcome == "candidate_uncaptured_at_crash"
    ]


@pytest.mark.asyncio
async def test_resume_past_the_receipt_window_fabricates_no_crash_evidence(tmp_path: Path) -> None:
    """#2850: a clean stop is not a crash, however long the bot has run.

    The first run decides more buckets than the receipt window retains, so
    its early ENTER's receipt has rotated out by the time it stops. A Resume
    replays every retained bar (#2314), restages that ENTER and finds no
    receipt for it in the window -- which is retention, not a crash. Before
    the fix it was flagged, and recording it as crash evidence collided with
    the ENTER's own protected receipt: the Resume ended in
    ``DecisionReceiptConflictError``.
    """
    repo, clerk = _sqlite_clerk(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        bars = _red_tape(minutes=_LATE_ENTER_MINUTE, enter_minutes=[_EARLY_ENTER_MINUTE])
        early_enter_close_ms = bars[_EARLY_ENTER_MINUTE].end_ms

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-1", strategy_key=_DV), _PhaseFeed(live_bars=bars)
        )
        after_first_run = _retained(repo)
        assert clerk.calls[0]["purpose"] == "ENTER"
        assert clerk.calls[0]["decision_evidence"].decision_bar_close_ms == early_enter_close_ms
        # The window no longer reaches back to that ENTER.
        assert after_first_run[0].seq > 1
        assert json.loads(after_first_run[0].facts_json)["decision_bar_close_ms"] > early_enter_close_ms

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-2", strategy_key=_DV), _PhaseFeed(retained_bars=bars)
        )

        assert _retained(repo) == after_first_run
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_resume_reads_how_far_back_the_window_reaches_off_a_clerk_effect_receipt(
    tmp_path: Path,
) -> None:
    """The oldest receipt a window retains can be one the Clerk wrote (#2850).

    An ENTER is receipted by the Clerk, with the effect it admits, not by the
    runner. Here the run stops with a second ENTER's receipt as the oldest one
    retained, the first ENTER's having rotated out. The Clerk records the
    decision bar's close on that row as the runner does on its own, so the
    Resume reads how far back the window reaches off it and leaves the first
    ENTER unflagged, where flagging it collided with its protected receipt.
    """
    repo, clerk = _sqlite_clerk(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        bars = _red_tape(
            minutes=_LATE_ENTER_MINUTE,
            enter_minutes=[_EARLY_ENTER_MINUTE, _OLDEST_RETAINED_ENTER_MINUTE],
        )

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-1", strategy_key=_DV), _PhaseFeed(live_bars=bars)
        )
        after_first_run = _retained(repo)
        second_enter = clerk.calls[2]
        assert second_enter["purpose"] == "ENTER"
        # The window opens on the Clerk's receipt for that second ENTER, which
        # says when its decision bar closed.
        oldest_retained = after_first_run[0]
        assert oldest_retained.seq > 1
        assert oldest_retained.outcome == "enter_intent"
        assert oldest_retained.intent_id == second_enter["decision_evidence"].evaluation_id
        assert (
            json.loads(oldest_retained.facts_json)["decision_bar_close_ms"]
            == bars[_OLDEST_RETAINED_ENTER_MINUTE].end_ms
        )

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-2", strategy_key=_DV), _PhaseFeed(retained_bars=bars)
        )

        assert _retained(repo) == after_first_run
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_resume_past_the_receipt_window_still_names_the_candidate_a_crash_left_staged(
    tmp_path: Path,
) -> None:
    """The exemption for rotated-out buckets must not reach the real crash window.

    The crashed run decided more buckets than the receipt window retains,
    then died on the bar that closes a staged ENTER. Replay restages two
    uncaptured ENTERs: the early one, whose receipt rotated out, and the one
    the crash left staged. Only the second is crash evidence, and it is the
    one named -- before #2850 the early one was flagged first, which hid it.
    """
    repo, clerk = _sqlite_clerk(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        bars = _red_tape(
            minutes=_LATE_ENTER_MINUTE + 1, enter_minutes=[_EARLY_ENTER_MINUTE, _LATE_ENTER_MINUTE]
        )
        crash_close_ms = bars[_LATE_ENTER_MINUTE].end_ms

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-1", strategy_key=_DV), _PhaseFeed(live_bars=bars[:_LATE_ENTER_MINUTE])
        )
        calls_before_resume = len(clerk.calls)
        assert _retained(repo)[0].seq > 1

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-2", strategy_key=_DV), _PhaseFeed(retained_bars=bars)
        )

        assert len(clerk.calls) == calls_before_resume
        assert _crash_evidence_closes(repo) == [crash_close_ms]
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_a_bucket_older_than_every_receipt_is_still_named_while_none_has_rotated_out(
    tmp_path: Path,
) -> None:
    """A store that has dropped no receipt exempts nothing (#2850).

    The instance's first run died on its very first decision: the bar that
    staged an ENTER was retained, no receipt was ever written. A Resume with
    no receipts on file flags nothing, so the bucket goes unnamed and that run
    writes the instance's first receipts -- all for later bars. At the next
    Resume the staged bucket is older than every receipt, but nothing has
    rotated out, so its missing receipt was never written: it is named.
    """
    repo, clerk = _sqlite_clerk(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        tape = _red_tape(minutes=_EARLY_ENTER_MINUTE + 6, enter_minutes=[_EARLY_ENTER_MINUTE])
        warmup = [bar.model_copy(update={"provenance": "history"}) for bar in tape[:_EARLY_ENTER_MINUTE]]
        crash_bar = tape[_EARLY_ENTER_MINUTE]
        later = tape[_EARLY_ENTER_MINUTE + 1 :]

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-2", strategy_key=_DV),
            _PhaseFeed(retained_bars=[*warmup, crash_bar], live_bars=later),
        )
        assert [receipt.outcome for receipt in _retained(repo)] == ["no_action"] * len(later)

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-3", strategy_key=_DV),
            _PhaseFeed(retained_bars=[*warmup, crash_bar, *later]),
        )

        assert _crash_evidence_closes(repo) == [crash_bar.end_ms]
        assert clerk.calls == []
    finally:
        set_alpaca_clerk(None)
        repo.close()


# ---------------------------------------------------------------------------
# Replay follows each run's own position, not the instance's (#2859).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_after_a_stop_in_position_names_no_exit_the_next_run_never_staged(
    tmp_path: Path,
) -> None:
    """#2859: a run starts flat, so replay drops the position the run before it held.

    The first run enters and is stopped holding the position. The second
    starts flat, decides two quiet minutes and dies on a third, which staged
    nothing. Replay used to carry the first run's ENTER through the second
    run's bars, count its exit down, and name the quiet minute as an EXIT the
    crash left staged -- one no live run staged.
    """
    repo, clerk = _sqlite_clerk(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        tape = _red_tape(minutes=_EARLY_ENTER_MINUTE + 5, enter_minutes=[_EARLY_ENTER_MINUTE])
        first_run = tape[: _EARLY_ENTER_MINUTE + 2]
        second_run = tape[_EARLY_ENTER_MINUTE + 2 : _EARLY_ENTER_MINUTE + 4]

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-1", strategy_key=_DV), _PhaseFeed(live_bars=first_run)
        )
        assert [call["purpose"] for call in clerk.calls] == ["ENTER"]

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-2", strategy_key=_DV),
            _PhaseFeed(retained_bars=first_run, live_bars=second_run),
        )
        before_resume = _retained(repo)
        assert [receipt.outcome for receipt in before_resume[-2:]] == ["no_action", "no_action"]

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-3", strategy_key=_DV), _PhaseFeed(retained_bars=tape)
        )

        assert _retained(repo) == before_resume
        assert [call["purpose"] for call in clerk.calls] == ["ENTER"]
    finally:
        set_alpaca_clerk(None)
        repo.close()


@pytest.mark.asyncio
async def test_resume_names_the_exit_a_later_run_left_staged_after_an_earlier_run_stopped_in_position(
    tmp_path: Path,
) -> None:
    """#2859: the earlier run's position must not stand in for the later run's own.

    The second run starts flat, enters on its own signal and dies on the bar
    that stages its EXIT. Replay used to reach the second run's ENTER still
    holding the first run's position, stage an EXIT there, commit that in
    the ENTER's place, and so arrive flat at the crash bucket: the EXIT the
    crash left staged went unnamed.
    """
    repo, clerk = _sqlite_clerk(tmp_path)
    set_alpaca_clerk(clerk)
    try:
        second_enter_minute = _EARLY_ENTER_MINUTE + 5
        crash_minute = second_enter_minute + 3
        tape = _red_tape(
            minutes=crash_minute + 1, enter_minutes=[_EARLY_ENTER_MINUTE, second_enter_minute]
        )
        first_run = tape[: _EARLY_ENTER_MINUTE + 2]

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-1", strategy_key=_DV), _PhaseFeed(live_bars=first_run)
        )
        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-2", strategy_key=_DV),
            _PhaseFeed(retained_bars=first_run, live_bars=tape[len(first_run) : crash_minute]),
        )
        assert [call["purpose"] for call in clerk.calls] == ["ENTER", "ENTER"]
        assert clerk.calls[1]["decision_evidence"].decision_bar_close_ms == tape[second_enter_minute].end_ms

        await bot_trade_strategy.run_trade_bot(
            _binding(run_id="run-3", strategy_key=_DV), _PhaseFeed(retained_bars=tape)
        )

        assert [call["purpose"] for call in clerk.calls] == ["ENTER", "ENTER"]
        assert _crash_evidence_closes(repo) == [tape[crash_minute].end_ms]
        assert json.loads(_retained(repo)[-1].facts_json)["explanation"]["holding"] is True
    finally:
        set_alpaca_clerk(None)
        repo.close()
