"""A resumed run fills the hole after its retained bars from history, or is refused (#2314)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from app.broker.contract.capabilities import ExtendedHoursWindow
from app.lean_sidecar.trading_calendar import session_window_for_date
from app.marketdata.feed import (
    RESUME_HOLE_AFTER_HOURS,
    RESUME_HOLE_UNFILLED,
    MarketDataBar,
    MarketDataFeedError,
)
from app.services.bot_trade_strategy import _RetainedSourceBarFeed
from app.services.decision_session import RunDecisionSession
from app.services.retained_tail_join import (
    first_owed_extended_minute_end,
    join_retained_tail,
    owed_regular_minute_ends,
)
from app.services.session_authority import et_minute_of_day_ms
from app.services.source_bar_ledger import (
    RetainedWarmupJoin,
    SourceBarConflictError,
    SourceBarLedger,
)

_RTH = RunDecisionSession(kind="rth", window=None)
_EXTENDED = RunDecisionSession(
    kind="extended", window=ExtendedHoursWindow(open_minute_et=7 * 60, close_minute_et=18 * 60)
)
_WED, _THU = date(2026, 9, 23), date(2026, 9, 24)
_MIN = 60_000


def _et(day: date, hour: int, minute: int) -> int:
    return et_minute_of_day_ms(day, hour * 60 + minute)


def _minute_bar(start_ms: int, *, phase: str = "RTH", close: str = "500") -> MarketDataBar:
    return MarketDataBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + _MIN,
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=100,
        fetched_at_ms=start_ms + _MIN,
        feed_id="ibkr",
        session_phase=phase,
        provenance="history",
    )


def _regular_bars(day: date, *, until_end_ms: int | None = None) -> list[MarketDataBar]:
    """Every regular-hours minute of ``day`` whose close is at or before ``until_end_ms``."""
    window = session_window_for_date(day)
    last_end = window.close_ms_utc if until_end_ms is None else until_end_ms
    return [
        _minute_bar(start)
        for start in range(window.open_ms_utc, window.close_ms_utc, _MIN)
        if start + _MIN <= last_end
    ]


class _HistoryFeed:
    """IBKR's history endpoint: serves fixed bars and records each lookback asked for."""

    feed_id = "ibkr"

    def __init__(self, bars: list[MarketDataBar]) -> None:
        self._bars = bars
        self.lookbacks: list[int] = []

    async def recent_closed_bars(
        self, symbol: str, *, use_rth: bool = True, lookback_days: int = 5
    ) -> list[MarketDataBar]:
        del symbol
        assert use_rth is False  # the ledger retains unfiltered observations
        self.lookbacks.append(lookback_days)
        return list(self._bars)


# ── owed minutes ─────────────────────────────────────────────────────────────


def test_a_mid_session_stop_owes_every_regular_minute_it_skipped() -> None:
    owed = owed_regular_minute_ends(after_ms=_et(_THU, 10, 7), now_ms=_et(_THU, 13, 0) + 30_000)

    assert owed[0] == _et(_THU, 10, 8)
    assert owed[-1] == _et(_THU, 13, 0)
    assert len(owed) == 173


def test_a_closed_night_owes_no_regular_minute() -> None:
    owed = owed_regular_minute_ends(
        after_ms=session_window_for_date(_WED).close_ms_utc,
        now_ms=session_window_for_date(_THU).open_ms_utc,
    )

    assert owed == ()


def test_an_extended_session_owes_its_first_after_hours_minute() -> None:
    assert first_owed_extended_minute_end(
        _EXTENDED, after_ms=_et(_WED, 17, 0), now_ms=_et(_THU, 10, 0)
    ) == _et(_WED, 17, 1)
    assert first_owed_extended_minute_end(
        _RTH, after_ms=_et(_WED, 17, 0), now_ms=_et(_THU, 10, 0)
    ) is None


# ── join_retained_tail ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_join_fills_the_hole_from_history() -> None:
    now_ms = _et(_THU, 13, 0) + 30_000
    feed = _HistoryFeed(_regular_bars(_THU, until_end_ms=now_ms))

    join = await join_retained_tail(
        feed, symbol="SPY", session=_RTH, retained_end_ms=_et(_THU, 10, 7), now_ms=now_ms, lookback_days=5
    )

    assert feed.lookbacks == [1]
    assert join.filled[0].start_ms == _et(_THU, 10, 7)
    assert join.filled[-1].end_ms == _et(_THU, 13, 0)
    assert len(join.filled) == 173
    assert join.warm_from_ms is None


@pytest.mark.asyncio
async def test_join_does_not_fetch_when_nothing_is_owed() -> None:
    feed = _HistoryFeed([])
    retained_end_ms = _et(_THU, 10, 7)

    join = await join_retained_tail(
        feed, symbol="SPY", session=_RTH, retained_end_ms=retained_end_ms,
        now_ms=retained_end_ms + 59_000, lookback_days=5,
    )

    assert join.filled == ()
    assert feed.lookbacks == []


@pytest.mark.asyncio
async def test_join_refuses_when_history_misses_an_owed_minute() -> None:
    now_ms = _et(_THU, 13, 0) + 30_000
    bars = [bar for bar in _regular_bars(_THU, until_end_ms=now_ms) if bar.end_ms != _et(_THU, 11, 0)]

    with pytest.raises(MarketDataFeedError) as refused:
        await join_retained_tail(
            _HistoryFeed(bars), symbol="SPY", session=_RTH,
            retained_end_ms=_et(_THU, 10, 7), now_ms=now_ms, lookback_days=5,
        )

    assert refused.value.reason == RESUME_HOLE_UNFILLED


@pytest.mark.asyncio
async def test_join_refuses_an_extended_session_hole_without_fetching() -> None:
    feed = _HistoryFeed(_regular_bars(_THU))

    with pytest.raises(MarketDataFeedError) as refused:
        await join_retained_tail(
            feed, symbol="SPY", session=_EXTENDED,
            retained_end_ms=_et(_WED, 17, 0), now_ms=_et(_THU, 10, 0), lookback_days=5,
        )

    assert refused.value.reason == RESUME_HOLE_AFTER_HOURS
    assert feed.lookbacks == []


@pytest.mark.asyncio
async def test_a_hole_longer_than_the_lookback_warms_on_the_lookback_history_only() -> None:
    """A week-old tail is outside a two-day warmup: fetch two days, not the week."""
    now_ms = _et(_THU, 13, 0) + 30_000
    history = [*_regular_bars(_WED), *_regular_bars(_THU, until_end_ms=now_ms)]
    feed = _HistoryFeed(history)

    join = await join_retained_tail(
        feed, symbol="SPY", session=_RTH,
        retained_end_ms=_et(date(2026, 9, 16), 10, 7), now_ms=now_ms, lookback_days=2,
    )

    assert feed.lookbacks == [2]
    assert join.warm_from_ms == history[0].start_ms
    assert len(join.filled) == len(history)


@pytest.mark.asyncio
async def test_an_old_after_hours_gap_is_refused_even_past_the_lookback() -> None:
    """Review P1: a week-old extended-hours gap with a one-day lookback was admitted.
    The refusal is about the hole the bot sat through, not how much warmup replays."""
    now_ms = _et(_THU, 13, 0)
    feed = _HistoryFeed(_regular_bars(_THU, until_end_ms=now_ms))

    with pytest.raises(MarketDataFeedError) as refused:
        await join_retained_tail(
            feed, symbol="SPY", session=_EXTENDED,
            retained_end_ms=_et(date(2026, 9, 17), 17, 0), now_ms=now_ms, lookback_days=1,
        )

    assert refused.value.reason == RESUME_HOLE_AFTER_HOURS
    assert feed.lookbacks == []


@pytest.mark.asyncio
async def test_a_weekend_longer_than_the_lookback_owes_nothing_and_keeps_the_retained_bars() -> None:
    """Review: Friday close to Monday 08:30 with a one-day lookback is not refused,
    and -- nothing being owed -- neither fetches nor drops the retained bars."""
    friday, monday = date(2026, 9, 18), date(2026, 9, 21)
    feed = _HistoryFeed([])

    join = await join_retained_tail(
        feed, symbol="SPY", session=_RTH,
        retained_end_ms=session_window_for_date(friday).close_ms_utc,
        now_ms=_et(monday, 8, 30), lookback_days=1,
    )

    assert (join.filled, join.warm_from_ms, feed.lookbacks) == ((), None, [])


@pytest.mark.asyncio
async def test_history_reaching_back_to_the_tail_fills_the_hole_without_a_floor() -> None:
    """An outrun hole whose fetched history still reaches the retained tail keeps the
    retained bars: nothing between them is outside what history covers."""
    now_ms = _et(_THU, 13, 0) + 30_000
    history = [*_regular_bars(_WED), *_regular_bars(_THU, until_end_ms=now_ms)]

    join = await join_retained_tail(
        _HistoryFeed(history), symbol="SPY", session=_RTH,
        retained_end_ms=_et(_WED, 10, 0), now_ms=now_ms, lookback_days=1,
    )

    assert join.warm_from_ms is None
    assert join.filled[0].start_ms == _et(_WED, 10, 0)


def test_replayed_rows_keep_their_provenance() -> None:
    """Review: the replay proof must not replay a history bucket as live-decided."""
    from app.services.run_replay_proof import to_market_bar
    from app.services.source_bar_ledger import RetainedSourceBar

    row = RetainedSourceBar.from_market_bar(seq=1, account_id="acct", bar=_minute_bar(_et(_THU, 10, 0)))

    assert to_market_bar(row).provenance == "history"


# ── _RetainedSourceBarFeed (the resumed run's warmup) ───────────────────────


def _filled_join(run_id: str, start_ms: int) -> RetainedWarmupJoin:
    return RetainedWarmupJoin(
        run_id=run_id,
        outcome="filled",
        retained_end_ms=start_ms,
        joined_at_ms=start_ms + 2 * _MIN,
        filled_count=1,
        filled_start_ms=start_ms,
        filled_end_ms=start_ms + _MIN,
    )


def _ledger_with_live_bars_through(tmp_path: Path, end_ms: int) -> SourceBarLedger:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:resume")
    for bar in _regular_bars(_THU, until_end_ms=end_ms):
        ledger.append(bar.model_copy(update={"provenance": "realtime"}), run_id="run-1")
    return ledger


@pytest.mark.asyncio
async def test_a_resumed_run_warms_on_a_series_with_no_hole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#2314 regression: Stop at 10:07, Resume at 13:00 -- warmup used to jump the gap."""
    now_ms = _et(_THU, 13, 0) + 30_000
    monkeypatch.setattr("app.services.bot_trade_strategy.now_ms_utc", lambda: now_ms)
    ledger = _ledger_with_live_bars_through(tmp_path, _et(_THU, 10, 7))
    try:
        feed = _RetainedSourceBarFeed(
            _HistoryFeed(_regular_bars(_THU, until_end_ms=now_ms)), ledger, run_id="run-2", session=_RTH
        )

        warmup = await feed.recent_closed_bars("SPY", use_rth=True)

        ends = [bar.end_ms for bar in warmup]
        assert all(later - earlier == _MIN for earlier, later in pairwise(ends))
        assert ends[-1] == _et(_THU, 13, 0)
        backfilled = [row for row in ledger.bars(provider="ibkr", symbol="SPY") if row.run_id == "run-2"]
        assert len(backfilled) == 173
        assert {row.provenance for row in backfilled} == {"history"}
        join = ledger.warmup_join(run_id="run-2")
        assert join is not None
        assert (join.outcome, join.filled_count) == ("filled", 173)
        assert (join.filled_start_ms, join.filled_end_ms) == (_et(_THU, 10, 7), _et(_THU, 13, 0))
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_a_resumed_run_with_nothing_missing_records_a_contiguous_join(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.services.bot_trade_strategy.now_ms_utc", lambda: _et(_THU, 10, 7) + 20_000
    )
    ledger = _ledger_with_live_bars_through(tmp_path, _et(_THU, 10, 7))
    try:
        feed = _RetainedSourceBarFeed(_HistoryFeed([]), ledger, run_id="run-2", session=_RTH)

        warmup = await feed.recent_closed_bars("SPY", use_rth=True)

        assert warmup[-1].end_ms == _et(_THU, 10, 7)
        join = ledger.warmup_join(run_id="run-2")
        assert join is not None and join.outcome == "contiguous"
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_an_unfillable_hole_refuses_the_run_and_records_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_ms = _et(_THU, 13, 0) + 30_000
    monkeypatch.setattr("app.services.bot_trade_strategy.now_ms_utc", lambda: now_ms)
    ledger = _ledger_with_live_bars_through(tmp_path, _et(_THU, 10, 7))
    try:
        feed = _RetainedSourceBarFeed(_HistoryFeed([]), ledger, run_id="run-2", session=_RTH)

        with pytest.raises(MarketDataFeedError) as refused:
            await feed.recent_closed_bars("SPY", use_rth=True)

        assert refused.value.reason == RESUME_HOLE_UNFILLED
        join = ledger.warmup_join(run_id="run-2")
        assert join is not None
        assert (join.outcome, join.reason_code) == ("refused", RESUME_HOLE_UNFILLED)
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_a_hole_past_the_lookback_drops_the_stale_retained_bars_from_warmup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now_ms = _et(_THU, 13, 0) + 30_000
    monkeypatch.setattr("app.services.bot_trade_strategy.now_ms_utc", lambda: now_ms)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:resume")
    stale_day = date(2026, 9, 16)
    for bar in _regular_bars(stale_day, until_end_ms=_et(stale_day, 10, 7)):
        ledger.append(bar.model_copy(update={"provenance": "realtime"}), run_id="run-1")
    history = [*_regular_bars(_WED), *_regular_bars(_THU, until_end_ms=now_ms)]
    try:
        feed = _RetainedSourceBarFeed(_HistoryFeed(history), ledger, run_id="run-2", session=_RTH)

        warmup = await feed.recent_closed_bars("SPY", use_rth=True, lookback_days=2)

        assert warmup[0].start_ms == history[0].start_ms
        join = ledger.warmup_join(run_id="run-2")
        assert join is not None and join.warm_from_ms == history[0].start_ms
    finally:
        ledger.close()


# ── ledger ───────────────────────────────────────────────────────────────────


def test_backfill_is_accepted_after_live_delivery_but_stays_monotonic(tmp_path: Path) -> None:
    ledger = _ledger_with_live_bars_through(tmp_path, _et(_THU, 10, 7))
    try:
        ledger.retain_warmup_join([_minute_bar(_et(_THU, 10, 7))], _filled_join("run-2", _et(_THU, 10, 7)))

        with pytest.raises(SourceBarConflictError, match="NON_MONOTONIC_BACKFILL"):
            ledger.retain_warmup_join([_minute_bar(_et(_WED, 15, 0))], _filled_join("run-3", _et(_WED, 15, 0)))
    finally:
        ledger.close()


def test_a_fill_and_its_join_commit_together_or_not_at_all(tmp_path: Path) -> None:
    """Review P1: a crash mid-fill must not leave bars with no join explaining them."""
    ledger = _ledger_with_live_bars_through(tmp_path, _et(_THU, 10, 7))
    try:
        # The second bar is out of order, so the batch fails after the first insert.
        with pytest.raises(SourceBarConflictError):
            ledger.retain_warmup_join(
                [_minute_bar(_et(_THU, 10, 7)), _minute_bar(_et(_THU, 10, 0), close="1")],
                _filled_join("run-2", _et(_THU, 10, 7)),
            )

        assert ledger.bars(provider="ibkr", symbol="SPY")[-1].end_ms == _et(_THU, 10, 7)
        assert ledger.warmup_join(run_id="run-2") is None
    finally:
        ledger.close()


@pytest.mark.parametrize(
    "fields",
    [
        {"outcome": "filled"},
        {"outcome": "contiguous", "filled_count": 3, "filled_start_ms": 1, "filled_end_ms": 2},
        {"outcome": "refused"},
        {"outcome": "contiguous", "reason_code": "RESUME_HOLE_UNFILLED"},
        {"outcome": "contiguous", "retained_end_ms": 253_402_300_800_000},
    ],
)
def test_contradictory_join_evidence_is_unrepresentable(fields: dict[str, object]) -> None:
    from pydantic import ValidationError

    from app.services.source_bar_ledger import RetainedWarmupJoin

    with pytest.raises(ValidationError):
        RetainedWarmupJoin.model_validate(
            {"run_id": "r", "retained_end_ms": 1, "joined_at_ms": 2, **fields}
        )


def test_the_join_table_admits_exactly_the_typed_refusal_reasons() -> None:
    """Parity: the SQL CHECK and ceiling restate the Literal and ``MAX_TIMESTAMP_MS``."""
    import inspect

    from app.marketdata.feed import WARMUP_REFUSAL_REASONS
    from app.services import source_bar_store_schema
    from app.utils.session_anchors import MAX_TIMESTAMP_MS

    ddl = inspect.getsource(source_bar_store_schema)
    assert all(f"'{reason}'" in ddl for reason in WARMUP_REFUSAL_REASONS)
    assert f"BETWEEN 0 AND {MAX_TIMESTAMP_MS}" in ddl


def test_a_fresh_run_records_no_warmup_join(tmp_path: Path) -> None:
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:resume")
    try:
        assert ledger.warmup_join(run_id="run-1") is None
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_a_later_resume_inherits_the_warm_floor_of_an_outrun_hole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M3: run 3 resumes contiguously after run 2 warmed from history only.
    It must not warm on run 1's stale tail again -- that reopens the hole."""
    stale_day = date(2026, 9, 16)
    ledger = SourceBarLedger(artifacts_root=tmp_path, account_id="paper:resume")
    for bar in _regular_bars(stale_day, until_end_ms=_et(stale_day, 10, 7)):
        ledger.append(bar.model_copy(update={"provenance": "realtime"}), run_id="run-1")
    now_ms = _et(_THU, 13, 0) + 30_000
    history = [*_regular_bars(_WED), *_regular_bars(_THU, until_end_ms=now_ms)]
    try:
        monkeypatch.setattr("app.services.bot_trade_strategy.now_ms_utc", lambda: now_ms)
        await _RetainedSourceBarFeed(
            _HistoryFeed(history), ledger, run_id="run-2", session=_RTH
        ).recent_closed_bars("SPY", use_rth=True, lookback_days=2)

        monkeypatch.setattr("app.services.bot_trade_strategy.now_ms_utc", lambda: now_ms + 10_000)
        warmup = await _RetainedSourceBarFeed(
            _HistoryFeed([]), ledger, run_id="run-3", session=_RTH
        ).recent_closed_bars("SPY", use_rth=True, lookback_days=2)

        assert warmup[0].start_ms == history[0].start_ms
        assert ledger.warm_floor_ms(run_id="run-3") == history[0].start_ms
        # An earlier run's replay is never cut by a later run's floor.
        assert ledger.warm_floor_ms(run_id="run-1") is None
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_history_buckets_are_never_flagged_as_crash_candidates() -> None:
    """A filled bucket was never decided, so staging an intent there is catch-up,
    not a candidate a crash left uncaptured (FR-016)."""
    from types import SimpleNamespace

    from app.engine.execution.portfolio import Portfolio
    from app.engine.strategy.base import StrategyContext
    from app.services.bot_trade_strategy_warmup import replay_warmup_bars
    from tests.services.test_bot_trade_strategy_warmup import _binding

    class _StagingRuntime:
        def __init__(self) -> None:
            self.strategy = SimpleNamespace(on_force_flat=lambda: None)
            self._stage: SimpleNamespace | None = None

        def replay_closed_bar(self, context: object, bar: MarketDataBar, *, mode: object) -> None:
            del context, mode
            self._stage = SimpleNamespace(
                trace=SimpleNamespace(evaluation_id=f"ev-{bar.start_ms}"),
                decision=SimpleNamespace(intent="ENTER"),
            )

        def active_stage(self) -> SimpleNamespace | None:
            return self._stage

        def settle(self, settlement: object) -> None:
            del settlement
            self._stage = None

    filled = _minute_bar(_et(_THU, 10, 7))
    live = _minute_bar(_et(_THU, 10, 8)).model_copy(update={"provenance": "realtime"})

    class _Feed:
        feed_id = "ibkr"

        async def recent_closed_bars(self, symbol: str, *, use_rth: bool, lookback_days: int) -> list[MarketDataBar]:
            del symbol, use_rth, lookback_days
            return [filled, live]

    uncaptured = await replay_warmup_bars(
        _StagingRuntime(),  # type: ignore[arg-type]
        StrategyContext(portfolio=Portfolio(initial_cash=Decimal(0))),
        _Feed(),  # type: ignore[arg-type]
        _binding(strategy_key="sma_crossover"),
        captured_decisions={"ev-earlier": "EXECUTED"},
    )

    assert uncaptured is not None
    assert uncaptured[0].start_ms == live.start_ms

