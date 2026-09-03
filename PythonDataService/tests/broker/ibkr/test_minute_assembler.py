"""MinuteAssembler survives an interruption and proves completeness by count (spec §4.2 rules 2–3)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from app.broker.ibkr.minute_assembler import RTH_CONTRIBUTIONS_PER_MINUTE, MinuteAssembler

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
    for second in range(45, 60, 5):  # 3 bars under generation 2
        assert assembler.feed(_raw(second), symbol="SPY", generation=2, venue="ARCA", use_rth=True) is None
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=2, venue="ARCA", use_rth=True)
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
    for second in range(0, 60, 5):
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)
    emitted = assembler.feed(_next_minute_raw(), symbol="SPY", generation=1, venue=None, use_rth=True)
    assert emitted is not None
    assert emitted.spans_interruption is False
    assert emitted.contribution_count == 12


def test_flush_if_complete_emits_only_a_full_open_minute() -> None:
    assembler = MinuteAssembler()
    for second in range(0, 55, 5):
        assembler.feed(_raw(second), symbol="SPY", generation=1, venue=None, use_rth=True)
    assert assembler.flush_if_complete() is None  # 11/12
    assembler.feed(_raw(55), symbol="SPY", generation=1, venue=None, use_rth=True)
    flushed = assembler.flush_if_complete()
    assert flushed is not None and flushed.contribution_count == 12
    assert assembler.open_minute_start_ms is None
    assert assembler.flush_if_complete() is None


def test_exact_redelivery_under_a_new_generation_leaves_the_minute_unflagged() -> None:
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
