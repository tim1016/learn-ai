"""MinuteAssembler survives an interruption and proves completeness by count (spec §4.2 rules 2–3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.broker.ibkr.minute_assembler import (
    RTH_CONTRIBUTIONS_PER_MINUTE,
    SPARSE_MINUTE_EMIT_GRACE_MS,
    IBKRBarStreamError,
    IBKRImpossibleBarError,
    MinuteAssembler,
)

_MINUTE = datetime(2026, 9, 2, 19, 0, 0, tzinfo=UTC)  # 15:00 ET, RTH


def _raw(second: int, close: str = "100", volume: int = 1) -> SimpleNamespace:
    return SimpleNamespace(
        time=_MINUTE.replace(second=second),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=volume,
    )


def _next_minute_raw() -> SimpleNamespace:
    return SimpleNamespace(
        time=_MINUTE.replace(minute=1, second=0),
        open=Decimal("101"),
        high=Decimal("101"),
        low=Decimal("101"),
        close=Decimal("101"),
        volume=1,
    )


def test_contributions_from_two_generations_merge_into_one_complete_minute() -> None:
    assembler = MinuteAssembler()
    for second in range(0, 45, 5):  # 9 bars under generation 1
        assert assembler.feed(_raw(second), symbol="SPY", generation=1, venue="ARCA", use_rth=True) is None
    for second in (45, 50):  # 2 more under generation 2 -- 11/12, still open
        assert assembler.feed(_raw(second), symbol="SPY", generation=2, venue="ARCA", use_rth=True) is None
    emitted = assembler.feed(_raw(55), symbol="SPY", generation=2, venue="ARCA", use_rth=True)
    assert emitted is not None
    assert emitted.contribution_count == RTH_CONTRIBUTIONS_PER_MINUTE == 12
    assert emitted.spans_interruption is True
    assert emitted.volume == 12


def test_lost_contribution_is_visible_in_the_count() -> None:
    assembler = MinuteAssembler()
    for second in (0, 5, 10, 15, 20, 25, 30, 35, 40, 50, 55):  # 45 missing
        assembler.feed(_raw(second), symbol="SPY", generation=1 if second < 45 else 2, venue=None, use_rth=True)
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.contribution_count == 11
    assert emitted.spans_interruption is True


def test_redelivered_bar_after_reconnect_is_absorbed_idempotently() -> None:
    assembler = MinuteAssembler()
    assembler.feed(_raw(0), symbol="SPY", generation=1, venue=None, use_rth=True)
    assembler.feed(_raw(0), symbol="SPY", generation=2, venue=None, use_rth=True)  # exact redelivery
    assert assembler.counters.skipped_duplicate == 1
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert emitted is not None and emitted.contribution_count == 1


def test_single_generation_minute_does_not_span_an_interruption() -> None:
    assembler = MinuteAssembler()
    for second in range(0, 55, 5):
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)
    emitted = assembler.feed(_raw(55), symbol="SPY", generation=1, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.spans_interruption is False
    assert emitted.contribution_count == 12


def test_a_complete_minute_is_emitted_by_its_twelfth_contribution() -> None:
    """#2345: a minute complete by count is closed, so it is not held for the next print.

    Before the fix the twelfth contribution returned ``None`` and the minute sat
    in the accumulator until a bar from a later minute arrived.
    """
    assembler = MinuteAssembler()
    for second in range(0, 55, 5):
        assert assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True) is None
    assert assembler.open_minute_start_ms is not None  # 11/12 is not complete: still open

    emitted = assembler.feed(_raw(55), symbol="SPY", generation=1, venue=None, use_rth=True)

    assert emitted is not None and emitted.contribution_count == RTH_CONTRIBUTIONS_PER_MINUTE
    assert emitted.start_ms == int(_MINUTE.timestamp() * 1000)
    assert assembler.open_minute_start_ms is None


def _fill_and_flush(assembler: MinuteAssembler) -> None:
    """Feed a full RTH minute, which emits it, leaving the assembler with no open minute."""
    emitted = [
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)
        for second in range(0, 60, 5)
    ]
    assert emitted[:-1] == [None] * (RTH_CONTRIBUTIONS_PER_MINUTE - 1)
    assert emitted[-1] is not None and emitted[-1].contribution_count == RTH_CONTRIBUTIONS_PER_MINUTE


def test_exact_redelivery_after_a_flush_is_skipped_idempotently() -> None:
    # After a complete minute is emitted the (possibly resubscribed) socket may
    # redeliver the most recent 5-second bar of the minute that was just emitted. An exact
    # redelivery of that one bar carries no new data, so it is absorbed rather
    # than fatal -- the live relaxation temporal-rigor grants, and no more.
    assembler = MinuteAssembler()
    _fill_and_flush(assembler)

    assert assembler.feed(_raw(55), symbol="SPY", generation=2, venue=None, use_rth=True) is None

    assert assembler.counters.skipped_duplicate == 1
    assert assembler.open_minute_start_ms is None


def test_an_older_print_of_a_flushed_minute_is_fatal_even_when_identical() -> None:
    # ``.claude/rules/temporal-rigor.md``: only the most-recently-accepted
    # element may be absorbed; any other timestamp belonging to an
    # already-emitted aggregate is fatal, identical payload or not.
    assembler = MinuteAssembler()
    _fill_and_flush(assembler)

    with pytest.raises(IBKRBarStreamError, match="already emitted"):
        assembler.feed(_raw(20), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert assembler.counters.skipped_duplicate == 0
    assert assembler.open_minute_start_ms is None


def test_a_correction_to_the_last_print_of_an_emitted_minute_is_ignored_and_counted() -> None:
    # IBKR may redeliver the latest 5-second bar with different OHLCV. Once its
    # minute is emitted the correction cannot be applied -- downstream decided
    # on the minute -- but it must not kill the run either (commit 241864a7:
    # a real redelivery once crashed a live run). Ignored, never rebuilt,
    # counted and logged.
    assembler = MinuteAssembler()
    _fill_and_flush(assembler)

    assert assembler.feed(_raw(55, close="101"), symbol="SPY", generation=2, venue=None, use_rth=True) is None

    assert assembler.counters.ignored_post_emit_correction == 1
    assert assembler.counters.skipped_duplicate == 0
    assert assembler.open_minute_start_ms is None
    # The minute is still closed: the next minute opens a fresh accumulator.
    assert assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True) is None
    assert assembler.open_minute_start_ms == int(_MINUTE.replace(minute=1).timestamp() * 1000)


def test_a_new_timestamp_inside_an_emitted_minute_is_refused_rather_than_rebuilt() -> None:
    # A timestamp the emitted minute never held must not silently rebuild an
    # accumulator for a minute the consumer has already decided on.
    assembler = MinuteAssembler()
    _fill_and_flush(assembler)

    with pytest.raises(IBKRBarStreamError, match="already emitted"):
        assembler.feed(_raw(57), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert assembler.open_minute_start_ms is None


def test_a_later_minute_after_a_flush_opens_a_fresh_accumulator() -> None:
    assembler = MinuteAssembler()
    _fill_and_flush(assembler)

    assert assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True) is None
    assert assembler.open_minute_start_ms is not None
    assert assembler.counters.skipped_duplicate == 0


# -- #2444: a bar whose values cannot be real is refused before the fold --


def _raw_with(
    second: int,
    *,
    open_: str = "100",
    high: str = "101",
    low: str = "99",
    close: str = "100.5",
    volume: int = 10,
) -> SimpleNamespace:
    return SimpleNamespace(
        time=_MINUTE.replace(second=second),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=volume,
    )


_IMPOSSIBLE_CASES = [
    (dict(close="nan"), "not finite"),
    (dict(close="0"), "not positive"),
    (dict(high="99", low="101"), "high is below low"),
    (dict(open_="101.5"), "outside the low"),
    (dict(close="98.5"), "outside the low"),
    (dict(volume=-1), "volume is negative"),
]


@pytest.mark.parametrize(
    ("case", "violation"),
    _IMPOSSIBLE_CASES,
    ids=[
        "nan_close",
        "zero_close",
        "high_below_low",
        "open_outside_range",
        "close_outside_range",
        "negative_volume",
    ],
)
def test_impossible_contribution_is_refused_before_folding(case: dict, violation: str) -> None:
    """#2444: a NaN, zero, inconsistent or negative-volume 5-second print never
    reaches the fold, the open minute is left exactly as it was, and the refusal
    is counted once on the assembler's observable counters."""
    assembler = MinuteAssembler()
    assert assembler.feed(_raw(0), symbol="SPY", generation=1, venue=None, use_rth=True) is None

    with pytest.raises(IBKRImpossibleBarError, match=violation):
        assembler.feed(_raw_with(5, **case), symbol="SPY", generation=1, venue=None, use_rth=True)

    assert assembler.counters.refused_impossible_bar == 1
    # The refusal changed nothing: the open minute still holds only its valid
    # first print, and the line's next valid print folds into it again.
    assert assembler.open_minute_start_ms == int(_MINUTE.timestamp() * 1000)
    assert assembler.counters.skipped_duplicate == 0
    assert assembler.last_source_ms == int(_MINUTE.replace(second=0).timestamp() * 1000)
    assert assembler.feed(_raw_with(10), symbol="SPY", generation=1, venue=None, use_rth=True) is None
    assert assembler.last_source_ms == int(_MINUTE.replace(second=10).timestamp() * 1000)


def test_a_volume_zero_bar_is_admitted() -> None:
    """IBKR prints a volume-0 bar when nothing traded in the 5 seconds (#2299
    evidence); zero volume is real, so only negative volume is impossible."""
    assembler = MinuteAssembler()

    emitted = assembler.feed(_raw_with(0, volume=0), symbol="SPY", generation=1, venue=None, use_rth=True)

    assert emitted is None
    assert assembler.open_minute_start_ms == int(_MINUTE.timestamp() * 1000)
    assert assembler.counters.refused_impossible_bar == 0


def test_impossible_correction_after_a_flush_is_refused_not_ignored() -> None:
    """#2444: a redelivered print whose values cannot be real is corruption even
    on the channel where an ordinary post-emit correction is absorbed -- the
    absorb path may ignore a *changed* payload, never an impossible one."""
    assembler = MinuteAssembler()
    _fill_and_flush(assembler)

    with pytest.raises(IBKRImpossibleBarError, match="not finite"):
        assembler.feed(_raw_with(55, close="nan"), symbol="SPY", generation=2, venue=None, use_rth=True)

    assert assembler.counters.refused_impossible_bar == 1
    assert assembler.counters.ignored_post_emit_correction == 0
    assert assembler.open_minute_start_ms is None

    # ``spans_interruption`` claims *contributions* arrived over more than one
    # generation. An exact redelivery contributes nothing — it is skipped — so
    # the minute's data still came wholly from generation 1 and flagging it
    # would make the field say something untrue.
    assembler = MinuteAssembler()
    assembler.feed(_raw(0), symbol="SPY", generation=1, venue=None, use_rth=True)
    assembler.feed(_raw(0), symbol="SPY", generation=2, venue=None, use_rth=True)
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.spans_interruption is False


def test_correction_under_a_new_generation_flags_the_minute() -> None:
    # The sibling case: a redelivery carrying different OHLCV *does* replace
    # the stored contribution, so that contribution came from generation 2.
    assembler = MinuteAssembler()
    assembler.feed(_raw(0), symbol="SPY", generation=1, venue=None, use_rth=True)
    assembler.feed(_raw(0, close="101"), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert assembler.counters.applied_correction == 1
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.spans_interruption is True
    assert emitted.close == Decimal("101")


# ── #2376: a sparse extended-hours minute is emitted on the wall clock ──────

_PRE_MINUTE = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)  # 08:00 ET, PRE
_PRE_MINUTE_MS = int(_PRE_MINUTE.timestamp() * 1000)
_PRE_CLOSE_MS = _PRE_MINUTE_MS + 60_000


def _pre_raw(second: int, *, minute: int = 0, close: str = "100") -> SimpleNamespace:
    return SimpleNamespace(
        time=_PRE_MINUTE.replace(minute=minute, second=second),
        open=Decimal(close),
        high=Decimal(close),
        low=Decimal(close),
        close=Decimal(close),
        volume=1,
    )


def _sparse_pre_minute(assembler: MinuteAssembler, *, minute: int = 0) -> None:
    """Five prints of twelve -- normal outside RTH."""
    for second in (0, 10, 25, 30, 45):
        assert (
            assembler.feed(_pre_raw(second, minute=minute), symbol="SPY", generation=1, venue=None, use_rth=False)
            is None
        )


def test_a_sparse_pre_minute_is_emitted_once_its_close_passes_the_grace() -> None:
    assembler = MinuteAssembler()
    _sparse_pre_minute(assembler)

    assert assembler.emit_if_elapsed(_PRE_CLOSE_MS + SPARSE_MINUTE_EMIT_GRACE_MS - 1) is None
    emitted = assembler.emit_if_elapsed(_PRE_CLOSE_MS + SPARSE_MINUTE_EMIT_GRACE_MS)

    assert emitted is not None
    assert emitted.start_ms == _PRE_MINUTE_MS
    assert emitted.contribution_count == 5
    assert emitted.session_phase == "PRE"
    assert assembler.open_minute_start_ms is None
    assert assembler.emit_if_elapsed(_PRE_CLOSE_MS + 30_000) is None  # nothing left open


def test_an_impossible_late_print_after_a_sparse_emit_is_refused_not_dropped() -> None:
    """#2444: a late print the sparse-minute timer path would merely drop is
    still refused when its values cannot be real -- a dropped print is
    surfaced, but impossible values are corruption, not a late delivery."""
    assembler = MinuteAssembler()
    _sparse_pre_minute(assembler)
    assert assembler.emit_if_elapsed(_PRE_CLOSE_MS + SPARSE_MINUTE_EMIT_GRACE_MS) is not None

    with pytest.raises(IBKRImpossibleBarError, match="not positive"):
        assembler.feed(_pre_raw(50, close="0"), symbol="SPY", generation=1, venue=None, use_rth=False)

    assert assembler.counters.refused_impossible_bar == 1
    assert assembler.counters.ignored_late_print_after_emit == 0


def test_the_grace_leaves_the_decision_allowance_room() -> None:
    """The emit must land well inside the 20 s allowance DECISION_LATE enforces."""
    from app.marketdata.feed import DELIVERY_ALLOWANCE_MS

    assert SPARSE_MINUTE_EMIT_GRACE_MS < DELIVERY_ALLOWANCE_MS / 2


def test_a_timer_emitted_non_join_pre_minute_is_decidable() -> None:
    """Sparse outside RTH is normal: the classifier calls it complete, not unprovable."""
    assembler = MinuteAssembler()
    _sparse_pre_minute(assembler, minute=0)  # the join minute
    joined = assembler.feed(_pre_raw(0, minute=1), symbol="SPY", generation=1, venue=None, use_rth=False)
    assert joined is not None  # closed by the next minute's first print
    for second in (20, 40):
        assembler.feed(_pre_raw(second, minute=1), symbol="SPY", generation=1, venue=None, use_rth=False)

    emitted = assembler.emit_if_elapsed(_PRE_CLOSE_MS + 60_000 + SPARSE_MINUTE_EMIT_GRACE_MS)

    assert emitted is not None and emitted.contribution_count == 3
    assert assembler.completeness(emitted, touched=False) == "complete"


def test_an_rth_minute_is_never_emitted_by_the_timer() -> None:
    """In RTH a short minute is already unprovable; the twelfth print is what proves it."""
    assembler = MinuteAssembler()
    for second in range(0, 55, 5):  # 11 of 12
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)

    rth_close_ms = int(_MINUTE.timestamp() * 1000) + 60_000
    assert assembler.emit_if_elapsed(rth_close_ms + 60_000) is None
    assert assembler.open_minute_start_ms is not None


def test_a_print_arriving_after_a_timer_emit_is_ignored_and_counted() -> None:
    """The run survives a late print: the emitted minute is never rebuilt."""
    assembler = MinuteAssembler()
    _sparse_pre_minute(assembler)
    assert assembler.emit_if_elapsed(_PRE_CLOSE_MS + SPARSE_MINUTE_EMIT_GRACE_MS) is not None

    late = assembler.feed(_pre_raw(55, close="101"), symbol="SPY", generation=1, venue=None, use_rth=False)

    assert late is None
    assert assembler.counters.ignored_late_print_after_emit == 1
    assert assembler.open_minute_start_ms is None
    # The next minute opens normally.
    assembler.feed(_pre_raw(5, minute=1), symbol="SPY", generation=1, venue=None, use_rth=False)
    assert assembler.open_minute_start_ms == _PRE_CLOSE_MS


def test_an_older_print_after_a_timer_emit_is_still_fatal() -> None:
    """Only a *later* slot of a sparse minute may still arrive; an earlier one is non-monotonic."""
    assembler = MinuteAssembler()
    _sparse_pre_minute(assembler)
    assert assembler.emit_if_elapsed(_PRE_CLOSE_MS + SPARSE_MINUTE_EMIT_GRACE_MS) is not None

    with pytest.raises(IBKRBarStreamError, match="already emitted"):
        assembler.feed(_pre_raw(20), symbol="SPY", generation=1, venue=None, use_rth=False)
