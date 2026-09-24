"""Per-program warmup lookback regression coverage.

``replay_warmup_bars`` (``app/services/bot_trade_strategy_warmup.py``) asks
``MarketDataFeed.recent_closed_bars`` for a trailing calendar-day window
before a live strategy starts deciding. That window used to be a single
hardcoded ``_WARMUP_LOOKBACK_DAYS = 5`` for every registered strategy, even
though each sealed program's own ``SignalProgramContract`` (in
``app/engine/strategy/registry.py``) already declares the window its own
math actually needs -- e.g. ``sma_crossover`` declares 7 days, well above
the floor. A strategy needing more than 5 days silently withheld live
decisions (``ready=False``) until enough bars accumulated organically,
while looking "running" the whole time.

These tests prove ``_warmup_lookback_days_for`` (and, through it,
``replay_warmup_bars``) prefers the registered contract's own
``warmup_lookback_days`` over the constant, and that the constant still
floors an unregistered ``strategy_key``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.broker.alpaca.clerk.sqlite.decision_receipts import (
    MAX_DECISION_RECEIPT_READ,
    SqliteDecisionReceipts,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.ibkr.bars import IBKRBarStreamError
from app.engine.execution.portfolio import Portfolio
from app.engine.strategy.base import StrategyContext
from app.marketdata.feed import WARMUP_HISTORY_UNAVAILABLE, MarketDataBar, MarketDataFeedError
from app.marketdata.ibkr_feed import IbkrMarketDataFeed, require_warmup_coverage
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_trade_strategy_warmup import (
    _WARMUP_LOOKBACK_DAYS,
    _warmup_lookback_days_for,
    captured_decision_outcomes,
    replay_warmup_bars,
)


def _binding(*, strategy_key: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="warmup-lookback-test",
        strategy_key=strategy_key,
        broker="alpaca",
        symbol="SPY",
        mode="trade",
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-1",
        created_at_ms=0,
    )


class _RecordingFeed:
    """``MarketDataFeed`` double that records the ``lookback_days`` it was
    asked for and returns no history -- ``replay_warmup_bars`` only needs
    the recorded call for this test, never the bars themselves."""

    feed_id = "fake-recording"

    def __init__(self) -> None:
        self.recorded_lookback_days: int | None = None

    async def recent_closed_bars(
        self,
        symbol: str,
        *,
        use_rth: bool = True,
        lookback_days: int = 5,
    ) -> list[MarketDataBar]:
        del symbol, use_rth
        self.recorded_lookback_days = lookback_days
        return []


class _FakeStrategy:
    def __init__(self) -> None:
        self.force_flat_calls = 0

    def on_force_flat(self) -> None:
        self.force_flat_calls += 1


class _FakeRuntime:
    """Duck-typed ``_LiveSignalRuntime`` stand-in.

    ``replay_warmup_bars`` never reaches ``replay_closed_bar``/
    ``active_stage``/``settle`` when the feed hands back an empty warmup
    window (see ``_RecordingFeed`` above), so only ``strategy.on_force_flat``
    -- called unconditionally at the end of every replay -- needs a real
    implementation here.
    """

    def __init__(self) -> None:
        self.strategy = _FakeStrategy()


def _context() -> StrategyContext:
    return StrategyContext(portfolio=Portfolio(initial_cash=Decimal(0)))


@pytest.mark.parametrize(
    ("strategy_key", "expected_days"),
    [
        pytest.param("sma_crossover", 7, id="sma_crossover-contract-declares-7"),
        pytest.param("spy_strategy_a", 9, id="spy_strategy_a-contract-declares-9"),
    ],
)
def test_warmup_lookback_days_for_prefers_the_registered_contracts_own_window(
    strategy_key: str, expected_days: int
) -> None:
    assert _warmup_lookback_days_for(_binding(strategy_key=strategy_key)) == expected_days


def test_warmup_lookback_days_for_floors_an_unregistered_strategy_key_at_the_constant() -> None:
    binding = _binding(strategy_key="not-a-registered-strategy")
    assert _warmup_lookback_days_for(binding) == _WARMUP_LOOKBACK_DAYS


@pytest.mark.asyncio
async def test_replay_warmup_bars_requests_the_larger_contract_lookback_from_the_feed() -> None:
    """End-to-end through ``replay_warmup_bars``: a strategy whose sealed
    contract declares more than the floor (``sma_crossover`` declares 7, the
    floor is 5) must have that larger window actually requested from the
    feed -- not just resolvable in isolation."""
    feed = _RecordingFeed()
    binding = _binding(strategy_key="sma_crossover")

    result = await replay_warmup_bars(
        _FakeRuntime(),  # type: ignore[arg-type]
        _context(),
        feed,  # type: ignore[arg-type]
        binding,
        captured_decisions=None,
    )

    assert result is None
    assert feed.recorded_lookback_days == 7


@pytest.mark.asyncio
async def test_replay_warmup_bars_requests_the_floor_for_an_unregistered_strategy() -> None:
    feed = _RecordingFeed()
    binding = _binding(strategy_key="not-a-registered-strategy")

    await replay_warmup_bars(
        _FakeRuntime(),  # type: ignore[arg-type]
        _context(),
        feed,  # type: ignore[arg-type]
        binding,
        captured_decisions=None,
    )

    assert feed.recorded_lookback_days == _WARMUP_LOOKBACK_DAYS


# 2026-09-23 (Wed) 15:00 EDT. A 7-day lookback starts on Wed 09-16, so the
# owed sessions are Thu 09-17 .. Wed 09-23 and the earliest closes 16:00 EDT
# on 09-17 (canonical calendar; no hardcoded time in the code under test).
_NOW_MS = 1_790_190_000_000
_EARLIEST_OWED_CLOSE_MS = 1_789_675_200_000  # 2026-09-17 16:00 EDT
_TODAY_OPEN_MS = 1_790_170_200_000  # 2026-09-23 09:30 EDT


def _ibkr_history_bar(start_ms: int) -> SimpleNamespace:
    return SimpleNamespace(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("400"),
        high=Decimal("401"),
        low=Decimal("399"),
        close=Decimal("400.5"),
        volume=1000,
        fetched_at_ms=_NOW_MS,
        provenance="ibkr_historical",
        spans_interruption=False,
        session_phase="RTH",
    )


async def _history_fails(*_args: Any, **_kwargs: Any) -> list[Any]:
    raise IBKRBarStreamError("historical data farm connection is broken")


async def _history_empty(*_args: Any, **_kwargs: Any) -> list[Any]:
    # ib_async ends a request on error 162 (pacing / data farm) with whatever
    # rows arrived -- often none -- and does not raise by default.
    return []


async def _history_last_session_only(*_args: Any, **_kwargs: Any) -> list[Any]:
    return [_ibkr_history_bar(_TODAY_OPEN_MS + minute * 60_000) for minute in range(60)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "history",
    [_history_fails, _history_empty, _history_last_session_only],
    ids=["fetch_raises", "fetch_returns_nothing", "fetch_returns_only_the_last_session"],
)
async def test_replay_warmup_bars_refuses_the_run_when_the_sealed_lookback_is_not_met(
    monkeypatch: pytest.MonkeyPatch, history: Any
) -> None:
    """#2365: a warmup that does not reach the sealed lookback never starts cold.

    Drives the real ``IbkrMarketDataFeed`` warmup path with only the IBKR
    history call faked. Before the fix every case replayed normally -- the
    strategy then decided on the bare indicator minimum (or on one session)
    instead of the sealed 7-day lookback. Now replay ends with the typed
    refusal before any warmup state is built.
    """
    monkeypatch.setattr("app.marketdata.ibkr_feed.fetch_historical_minute_bars", history)
    monkeypatch.setattr("app.marketdata.ibkr_feed.now_ms_utc", lambda: _NOW_MS)
    client = MagicMock()
    client.is_connected.return_value = True
    client.connection_lost = False
    runtime = _FakeRuntime()

    with pytest.raises(MarketDataFeedError) as refused:
        await replay_warmup_bars(
            runtime,  # type: ignore[arg-type]
            _context(),
            IbkrMarketDataFeed(client),
            _binding(strategy_key="sma_crossover"),
            captured_decisions=None,
        )

    assert refused.value.reason == WARMUP_HISTORY_UNAVAILABLE
    assert runtime.strategy.force_flat_calls == 0


def _bar_at(start_ms: int) -> MarketDataBar:
    return IbkrMarketDataFeed._translate(_ibkr_history_bar(start_ms))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "oldest_start_ms",
    [
        pytest.param(_EARLIEST_OWED_CLOSE_MS - 60_000, id="last-minute-of-the-earliest-owed-session"),
        pytest.param(_EARLIEST_OWED_CLOSE_MS - 24 * 3_600_000, id="the-day-before-the-window"),
    ],
)
def test_warmup_coverage_accepts_history_reaching_the_earliest_owed_session(
    oldest_start_ms: int,
) -> None:
    """The rule is "reaches into the earliest owed session", not "starts at its
    first minute": a late-opening vendor window must not refuse a start."""
    bars = [_bar_at(oldest_start_ms), _bar_at(_TODAY_OPEN_MS)]

    require_warmup_coverage(bars, lookback_days=7, now_ms=_NOW_MS)


def test_warmup_coverage_refuses_history_ending_at_the_earliest_owed_close() -> None:
    bars = [_bar_at(_EARLIEST_OWED_CLOSE_MS), _bar_at(_TODAY_OPEN_MS)]

    with pytest.raises(MarketDataFeedError) as refused:
        require_warmup_coverage(bars, lookback_days=7, now_ms=_NOW_MS)

    assert refused.value.reason == WARMUP_HISTORY_UNAVAILABLE


def test_warmup_coverage_is_monotone_in_time_so_a_first_start_s_window_covers_a_later_resume() -> None:
    """A resumed run warms from its retained ledger and never reaches the IBKR
    check, but the rule itself is still monotone: history that covered a
    start covers the same window seen from any later instant."""
    retained = [_bar_at(_EARLIEST_OWED_CLOSE_MS - 60_000), _bar_at(_TODAY_OPEN_MS)]
    two_days_later_ms = _NOW_MS + 2 * 24 * 3_600_000

    require_warmup_coverage(retained, lookback_days=7, now_ms=two_days_later_ms)


def test_warmup_coverage_owes_nothing_before_the_first_owed_session_opens() -> None:
    """A one-day lookback before today's open owes no session: empty is not a refusal."""
    before_open_ms = _TODAY_OPEN_MS - 3_600_000

    require_warmup_coverage([], lookback_days=1, now_ms=before_open_ms)


def test_warmup_lookback_days_for_reads_the_seal_over_the_live_registry() -> None:
    """A sealed instance warms to the window its own seal attests.

    Admission has already proven that field against the live registry, so the
    seal is authoritative. Reading the registry instead would let an edit
    between sealing and Resume silently change how far back the bot warms,
    with the seal still claiming the original number — the same divergence
    between attested and actual that was repaired for the decision cadence.
    """
    from app.engine.strategy.registry import _STRATEGY_REGISTRY
    from app.schemas.run_admission import StrategyValidationAdmissionFact
    from app.services.signal_program_admission import build_start_program_seal

    binding = _binding(strategy_key="spy_strategy_a").model_copy(
        update={"sealed_account_id": "sim:warmup-seal", "strategy_params": {}, "strategy_param_origins": {}}
    )
    seal = build_start_program_seal(
        binding,
        StrategyValidationAdmissionFact(
            state="VERIFIED",
            strategy_key="spy_strategy_a",
            evidence_status="accepted",
            event_id="validation-warmup-1",
            evidence_snapshot_sha256="d" * 64,
            verified_at_ms=1_787_356_800_000,
            explanation="The exact validation snapshot was re-hashed.",
        ),
        parameter_origins={},
    )
    assert seal is not None
    sealed_binding = binding.model_copy(update={"sealed_program": seal})

    contract = _STRATEGY_REGISTRY["spy_strategy_a"].signal_program_contract
    assert contract is not None
    assert seal.configured_signal.clock.warmup_lookback_days == contract.warmup_lookback_days

    # The seal is the source: a stale seal keeps its own window rather than
    # silently adopting whatever the registry says today.
    stale_clock = seal.configured_signal.clock.model_copy(update={"warmup_lookback_days": 11})
    stale_configured = seal.configured_signal.model_copy(update={"clock": stale_clock})
    stale_binding = sealed_binding.model_copy(
        update={"sealed_program": seal.model_copy(update={"configured_signal": stale_configured})}
    )

    assert _warmup_lookback_days_for(sealed_binding) == contract.warmup_lookback_days
    assert _warmup_lookback_days_for(stale_binding) == 11


def test_captured_decision_outcomes_sees_decisions_older_than_the_presentation_cap(
    tmp_path: Path,
) -> None:
    """``deployment_validation`` decides every minute, so a one-day warmup is
    ~780 buckets -- more than ``MAX_DECISION_RECEIPT_READ``. Reading only the
    presentation cap would make the earliest replayed bucket that staged an
    intent look like the crash-recreated candidate (#1740)."""
    repo = ClerkSqliteRepository.initialize(account_id="PA-TEST", artifacts_root=tmp_path)
    try:
        repo.register_strategy_instance(
            strategy_instance_id="spy-bot", symbol="SPY", config_hash="test-config-hash"
        )
        receipts = SqliteDecisionReceipts(repo, strategy_instance_id="spy-bot")
        receipts.append(
            outcome="entered",
            symbol="SPY",
            observed_at_ms=0,
            intent_id="0:ENTER",
            facts={"bar_ref": "SPY@0", "reason_code": "STRATEGY_ENTER"},
        )
        for index in range(1, MAX_DECISION_RECEIPT_READ + 1):
            receipts.append(
                outcome="no_action",
                symbol="SPY",
                observed_at_ms=index,
                intent_id=f"{index}:NO_ACTION",
                facts={"bar_ref": f"SPY@{index}", "reason_code": "NO_ACTION"},
            )

        captured = captured_decision_outcomes(receipts)
    finally:
        repo.close()

    assert captured["0:ENTER"] == "entered"
    assert len(captured) == MAX_DECISION_RECEIPT_READ + 1
