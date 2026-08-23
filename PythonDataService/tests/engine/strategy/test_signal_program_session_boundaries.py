"""Session-boundary evidence for issue #1730's acceptance criterion:

    "Ordinary day, early close, DST, missing/duplicate/revised minute,
    history/live overlap, Pause, and relevant multi-timeframe boundaries
    are covered per program."

Every real session boundary (open, close, half-day close, DST transition)
is derived from the canonical NYSE calendar module
(``app.lean_sidecar.trading_calendar``, the sole ``mcal.get_calendar``
caller) or from ``ZoneInfo("America/New_York")`` directly -- never a
hardcoded ``09:30``/``13:00``/``16:00`` literal -- per
``.claude/rules/temporal-rigor.md``.

Coverage is derived from the registry (``SEALED_KEYS``, see
``tests/_helpers/signal_program.py``), never a hand-written key list.

Scope and granularity, stated up front so gaps are explicit rather than
implied:

  * Most cases here drive ``SignalSession.advance`` (or
    ``SignalProgram.on_consolidated_bar``) directly with decision-bucket
    -shaped ``TradeBar``s -- the same granularity the two sibling test
    files use -- because that is the public interface at which
    ``TIMEFRAME_MISMATCH`` / ``NON_MONOTONIC_DECISION_CLOCK`` /
    ``UNSETTLED_STAGE`` are decided. "Missing/duplicate/revised minute"
    is therefore proven at the decision-bucket granularity (a whole
    bucket skipped, redelivered, revised, or reordered), not at the
    raw-minute granularity feeding the consolidator -- the consolidator's
    own floor-rounding/firing correctness is a separate, already-documented
    concern (see its own module docstring) and is only re-exercised here
    once, end-to-end, for the early-close case below.
  * One test (``test_early_close_minute_feed_through_real_consolidator_...``)
    runs real 1-minute bars through the actual
    ``TradeBarConsolidator`` for a single representative program, to prove
    the literal minute -> consolidated-bucket -> ``SignalProgram`` pipeline
    handles a half-day's trailing partial bucket correctly. It is not
    repeated for all six sealed programs: the consolidator's bucket-shaping
    logic is program-agnostic (it only ever sees bar timestamps), so
    proving it once, combined with the decision-bucket-level early-close
    proof for every program, gives full coverage without a 6x runtime cost
    for logic that cannot vary by program.
  * DST is proven two ways: a calendar-only test shows the canonical
    module's real ZoneInfo-derived open differs from what a banned fixed
    -05:00 offset would have produced, by exactly one hour, across a real
    transition discovered by scanning ``ZoneInfo`` offsets (never a
    hardcoded March/November date); a per-program test then shows the
    decision clock accepts real bars from both sides of that transition
    without quarantine.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.engine.consolidators.trade_bar_consolidator import TradeBarConsolidator
from app.engine.data.trade_bar import TradeBar
from app.engine.strategy.signal_program import (
    EvaluationMode,
    Settlement,
    SignalProgram,
    StageQuarantine,
)
from app.lean_sidecar import trading_calendar
from tests._helpers.signal_program import (
    SEALED_KEYS,
    bucket,
    custody_snapshot,
    custody_surface,
    sequential_bucket,
)
from tests.engine.strategy.conftest import PreparedProgram, build_program


def _construct_program(key: str) -> PreparedProgram:
    """Build a registered program and require an explicit mode for every bar.

    ``activate_for_runner`` matches the live runner's contract; this file's
    ``on_consolidated_bar`` cases must not rely on the backtest-only
    default-DECIDE convenience.
    """
    prepared = build_program(key)
    prepared.program.activate_for_runner()
    return prepared


def _stage(
    program: SignalProgram,
    bar: TradeBar,
    *,
    mode: EvaluationMode = EvaluationMode.DECIDE,
) -> Any:
    """Drive one bar directly through the session and settle any real stage.

    Returns whatever ``SignalSession.advance`` returned, so callers can assert
    ``not isinstance(result, StageQuarantine)`` (accepted) or inspect
    ``result.reason`` (refused) precisely -- the same shape the sibling
    discard-safety and decision-clock tests use.
    """
    result = program.session.advance(bar, mode=mode)
    if not isinstance(result, StageQuarantine):
        program.session.settle(Settlement.DISCARD)
    return result


def _drain_quarantines(program: SignalProgram) -> dict[str, int]:
    """Drain the refusals the program retained, tallied by reason.

    Mirrors what the live runner does with ``take_quarantine()``. The sealed
    ``SignalProgram`` deliberately keeps no counter of its own: an accessor
    read only by tests, living in a file whose bytes *are* the sealed
    decision identity, is what moved these diagnostics out to
    ``app/services/bot_trade_strategy.py`` in the first place.
    """
    tally: dict[str, int] = {}
    while (quarantine := program.take_quarantine()) is not None:
        tally[quarantine.reason] = tally.get(quarantine.reason, 0) + 1
    return tally


def _feed_through_program(
    program: SignalProgram,
    bar: TradeBar,
    *,
    mode: EvaluationMode = EvaluationMode.DECIDE,
) -> dict[str, int]:
    """Drive one bar through the production entrypoint (capture +
    ``on_consolidated_bar``) and settle any real stage it creates.

    Returns the refusals *this bar* produced. Per-bar rather than cumulative:
    asserting a running total stayed put is an indirect way of saying "this
    bar was not refused", and says it less clearly.
    """
    program.capture_source_bar(bar, mode=mode)
    program.on_consolidated_bar(bar)
    refused = _drain_quarantines(program)
    if program.session.active_stage is not None:
        program.session.settle(Settlement.DISCARD)
    return refused


# ---------------------------------------------------------------------------
# Calendar-derived date search -- never a hardcoded date literal.
# ---------------------------------------------------------------------------


def _first_ordinary_trading_day(start: date, *, horizon_days: int = 30) -> date:
    windows = trading_calendar.session_windows_ms_utc(start, start + timedelta(days=horizon_days))
    for window in windows:
        if not trading_calendar.is_early_close(window.session_date):
            return window.session_date
    raise AssertionError(f"no ordinary (non-early-close) trading day found within {horizon_days} days of {start}")


def _first_early_close_day(start: date, *, horizon_days: int = 800) -> date:
    windows = trading_calendar.session_windows_ms_utc(start, start + timedelta(days=horizon_days))
    for window in windows:
        if trading_calendar.is_early_close(window.session_date):
            return window.session_date
    raise AssertionError(f"no early-close session found within {horizon_days} days of {start}")


def _nearest_trading_day_at_or_before(d: date) -> date:
    while not trading_calendar.is_trading_day(d):
        d -= timedelta(days=1)
    return d


def _nearest_trading_day_at_or_after(d: date) -> date:
    while not trading_calendar.is_trading_day(d):
        d += timedelta(days=1)
    return d


@lru_cache(maxsize=1)
def _dst_spring_forward_trading_days() -> tuple[date, date]:
    """Return ``(last_est_trading_day, first_edt_trading_day)`` bracketing the
    US spring-forward transition, discovered by scanning real UTC-offset
    changes via ``ZoneInfo("America/New_York")`` -- never a hardcoded
    month/day literal, per ``.claude/rules/temporal-rigor.md``.
    """
    tz = ZoneInfo("America/New_York")
    year = 2024
    d = date(year, 1, 15)
    end = date(year, 6, 15)
    prev_offset = datetime(d.year, d.month, d.day, 12, tzinfo=tz).utcoffset()
    while d < end:
        nxt = d + timedelta(days=1)
        offset = datetime(nxt.year, nxt.month, nxt.day, 12, tzinfo=tz).utcoffset()
        if offset is not None and prev_offset is not None and offset > prev_offset:
            return _nearest_trading_day_at_or_before(d), _nearest_trading_day_at_or_after(nxt)
        prev_offset = offset
        d = nxt
    raise AssertionError(f"no spring-forward DST transition found scanning {year}")


def _fixed_est_open_ms(d: date) -> int:
    """Simulate the BANNED fixed -05:00 conversion, purely as a contrast value.

    Never used in production code -- ``.claude/rules/temporal-rigor.md``
    explicitly bans a fixed ET offset. This exists only so the DST test below
    can show the canonical module's real value differs from it.
    """
    naive_open = datetime(d.year, d.month, d.day, 9, 30)
    return int(naive_open.replace(tzinfo=timezone(timedelta(hours=-5))).timestamp() * 1000)


# ---------------------------------------------------------------------------
# 1. Ordinary day.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_ordinary_day_full_session_accepted_end_to_end(key: str) -> None:
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms
    day = _first_ordinary_trading_day(date(2024, 1, 2))

    open_ms = trading_calendar.session_open_ms_utc(day)
    close_ms = trading_calendar.session_close_ms_utc(day)
    session_len = close_ms - open_ms
    assert session_len % width == 0, (
        f"'{key}' timeframe_ms={width} does not evenly divide the ordinary "
        f"{session_len}ms session on {day}"
    )
    bucket_count = session_len // width

    for i in range(bucket_count):
        start = open_ms + i * width
        bar = bucket(params.symbol, start, start + width, str(100 + (i % 11)))
        stage = _stage(program, bar)
        assert not isinstance(stage, StageQuarantine), (
            f"'{key}' quarantined bucket {i}/{bucket_count} of the ordinary {day} "
            f"session: {stage.reason if isinstance(stage, StageQuarantine) else ''}"
        )


# ---------------------------------------------------------------------------
# 2. Early close.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_early_close_short_session_accepted_end_to_end(key: str) -> None:
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms
    day = _first_early_close_day(date(2024, 1, 1))
    assert trading_calendar.is_early_close(day)

    open_ms = trading_calendar.session_open_ms_utc(day)
    close_ms = trading_calendar.session_close_ms_utc(day)
    session_len = close_ms - open_ms
    assert session_len % width == 0, (
        f"'{key}' timeframe_ms={width} does not evenly divide the {session_len}ms "
        f"early-close session on {day}"
    )
    bucket_count = session_len // width

    last_end: int | None = None
    for i in range(bucket_count):
        start = open_ms + i * width
        end = start + width
        bar = bucket(params.symbol, start, end, str(100 + (i % 11)))
        stage = _stage(program, bar)
        assert not isinstance(stage, StageQuarantine), (
            f"'{key}' quarantined bucket {i}/{bucket_count} of the {day} early-close "
            f"session: {stage.reason if isinstance(stage, StageQuarantine) else ''}"
        )
        last_end = end

    assert last_end == close_ms, (
        f"'{key}' early-close session on {day} built a last bucket ending {last_end}, "
        f"not the real half-day close {close_ms} -- the boundary must come from the "
        "canonical calendar, never a hardcoded 16:00"
    )


def test_early_close_minute_feed_through_real_consolidator_flushes_finalbucket() -> None:
    """End-to-end proof using the REAL per-minute ``TradeBarConsolidator``.

    LEAN's consolidator (ported here) never closes its trailing working bar
    until a LATER bar arrives, so the half-day's final decision bucket would
    sit open forever unless explicitly flushed via ``.scan()`` at the real
    session close. This proves that flushed final bucket is legitimate and
    correctly bounded -- not something the width or monotonic-clock gate
    silently drops.

    Scoped to one representative program (see the module docstring for why).
    """
    key = SEALED_KEYS[0]
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms

    day = _first_early_close_day(date(2024, 1, 1))
    open_ms = trading_calendar.session_open_ms_utc(day)
    close_ms = trading_calendar.session_close_ms_utc(day)
    assert (close_ms - open_ms) % width == 0

    consolidator = TradeBarConsolidator(timedelta(milliseconds=width))
    consolidated: list[TradeBar] = []
    consolidator.on_data_consolidated = consolidated.append

    minute_ms = 60_000
    price = Decimal("100")
    minute_start = open_ms
    while minute_start < close_ms:
        price += Decimal("1") if (minute_start // minute_ms) % 2 == 0 else Decimal("-1")
        minute_bar = TradeBar(
            symbol=params.symbol,
            start_ms=minute_start,
            end_ms=minute_start + minute_ms,
            open=price,
            high=price + Decimal("0.5"),
            low=price - Decimal("0.5"),
            close=price,
            volume=100,
        )
        consolidator.update(minute_bar)
        minute_start += minute_ms

    # The consolidator never closes its own trailing bucket -- flush it at
    # the real (calendar-derived) session close.
    consolidator.scan(close_ms)

    expected_buckets = (close_ms - open_ms) // width
    assert len(consolidated) == expected_buckets, (
        f"expected {expected_buckets} consolidated buckets across the {day} early "
        f"close, got {len(consolidated)}"
    )
    assert consolidated[-1].end_ms == close_ms, (
        f"the final flushed bucket ended at {consolidated[-1].end_ms}, not the real "
        f"half-day close {close_ms}"
    )

    for fired in consolidated:
        stage = _stage(program, fired)
        assert not isinstance(stage, StageQuarantine), (
            f"'{key}' quarantined a real consolidator-produced bucket ending "
            f"{fired.end_ms} during the {day} early close: "
            f"{stage.reason if isinstance(stage, StageQuarantine) else ''}"
        )


# ---------------------------------------------------------------------------
# 3. DST.
# ---------------------------------------------------------------------------


def test_dst_transition_fixed_offset_would_have_been_wrong_by_one_hour() -> None:
    day_before, day_after = _dst_spring_forward_trading_days()
    tz = ZoneInfo("America/New_York")

    before_offset = datetime(day_before.year, day_before.month, day_before.day, 12, tzinfo=tz).utcoffset()
    after_offset = datetime(day_after.year, day_after.month, day_after.day, 12, tzinfo=tz).utcoffset()
    assert before_offset == timedelta(hours=-5), f"expected {day_before} to be EST, got offset {before_offset}"
    assert after_offset == timedelta(hours=-4), f"expected {day_after} to be EDT, got offset {after_offset}"

    canonical_before = trading_calendar.session_open_ms_utc(day_before)
    canonical_after = trading_calendar.session_open_ms_utc(day_after)
    fixed_before = _fixed_est_open_ms(day_before)
    fixed_after = _fixed_est_open_ms(day_after)

    assert fixed_before == canonical_before, (
        "control check failed: a fixed -05:00 model should agree with the canonical "
        f"calendar on an EST day ({day_before})"
    )
    assert fixed_after - canonical_after == 3_600_000, (
        f"a fixed -05:00 offset would have placed {day_after}'s 09:30 ET open "
        f"{(fixed_after - canonical_after) / 3_600_000}h away from the true EDT open -- "
        "DST must be resolved through ZoneInfo, never a fixed offset"
    )


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_dst_transition_decision_clock_accepts_bars_from_both_sides(key: str) -> None:
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms
    day_before, day_after = _dst_spring_forward_trading_days()

    for day in (day_before, day_after):
        open_ms = trading_calendar.session_open_ms_utc(day)
        bar = bucket(params.symbol, open_ms, open_ms + width, "100")
        stage = _stage(program, bar)
        assert not isinstance(stage, StageQuarantine), (
            f"'{key}' quarantined the first decision bucket of {day} while crossing "
            f"the DST transition {day_before} -> {day_after}: "
            f"{stage.reason if isinstance(stage, StageQuarantine) else ''}"
        )


# ---------------------------------------------------------------------------
# 4. Missing / duplicate / revised minute.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_missing_bucket_gap_is_tolerated(key: str) -> None:
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms

    first = sequential_bucket(params.symbol, 0, width, "100")
    stage = _stage(program, first)
    assert not isinstance(stage, StageQuarantine), "setup failed: first bucket was refused"

    # Bucket index 1 is never delivered at all -- a missing minute that left
    # a whole decision bucket undelivered.
    after_gap = sequential_bucket(params.symbol, 2, width, "101")
    stage = _stage(program, after_gap)
    assert not isinstance(stage, StageQuarantine), (
        f"'{key}' refused a legitimate bucket after a gap: "
        f"{stage.reason if isinstance(stage, StageQuarantine) else ''}"
    )


@pytest.mark.parametrize("key", SEALED_KEYS)
@pytest.mark.parametrize(
    ("label", "offending_index_delta", "offending_close"),
    [
        ("duplicate", 0, "100"),
        ("revised", 0, "107"),
        ("backwards", -1, "100"),
    ],
)
def test_duplicate_revised_or_backwards_bucket_is_refused_and_counted(
    key: str,
    label: str,
    offending_index_delta: int,
    offending_close: str,
) -> None:
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms

    first = sequential_bucket(params.symbol, 0, width, "100")
    refused_first = _feed_through_program(program, first)
    assert refused_first == {}, f"setup failed for '{key}'/{label}: first bucket was refused"

    offending = sequential_bucket(params.symbol, offending_index_delta, width, offending_close)
    refused_offending = _feed_through_program(program, offending)

    assert refused_offending == {"NON_MONOTONIC_DECISION_CLOCK": 1}, (
        f"'{key}' accepted a {label} decision bucket (end_ms={offending.end_ms}) after "
        f"already deciding through end_ms={first.end_ms}: {refused_offending}"
    )
    assert program.session.active_stage is None, f"'{key}' left an active stage after refusing a {label} bucket"

    # Recovery: a genuinely new, later bucket must still be accepted.
    following = sequential_bucket(params.symbol, 1, width, "101")
    refused_following = _feed_through_program(program, following)
    assert refused_following == {}, (
        f"'{key}' quarantined the recovery bucket after a {label} refusal: {refused_following}"
    )


# ---------------------------------------------------------------------------
# 5. History/live overlap.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_history_live_overlap_bar_produces_no_second_decision(key: str) -> None:
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms

    # A short warmup/history replay: three sequential decision buckets.
    warmup = [sequential_bucket(params.symbol, i, width, str(100 + i)) for i in range(3)]
    for bar in warmup:
        refused_warmup = _feed_through_program(program, bar)
        assert refused_warmup == {}, f"setup failed for '{key}': a warmup bucket was refused"

    # The live feed re-delivers the LAST warmup bucket verbatim (same
    # end_ms) -- e.g. a reconnect replaying the tail of history it already
    # consumed. This must not produce a second decision.
    redelivered = warmup[-1]
    refused_overlap = _feed_through_program(program, redelivered)

    assert refused_overlap == {"NON_MONOTONIC_DECISION_CLOCK": 1}, (
        f"'{key}' let a history/live overlap bucket (end_ms={redelivered.end_ms}) "
        f"produce a second decision instead of refusing it: {refused_overlap}"
    )
    assert program.session.active_stage is None, f"'{key}' left an active stage after an overlap redelivery"

    # The program must still work afterward: a genuinely new bucket is accepted.
    fresh = sequential_bucket(params.symbol, 3, width, "104")
    refused_fresh = _feed_through_program(program, fresh)
    assert refused_fresh == {}, (
        f"'{key}' quarantined the post-overlap recovery bucket: {refused_fresh}"
    )


# ---------------------------------------------------------------------------
# 6. Pause.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_pause_observe_only_records_trace_without_commit_and_program_still_decides_after(
    key: str,
) -> None:
    program, params, executor, _context = _construct_program(key)
    strategy = program.strategy
    session = program.session
    width = session.timeframe_ms

    surface = custody_surface(strategy)
    assert surface, (
        f"'{key}' declares no custody surface via rollback_blocked_entry/exit; "
        "this Pause invariant would hold vacuously"
    )
    before = custody_snapshot(strategy, surface)
    traces_before = len(session.traces)

    paused = [sequential_bucket(params.symbol, i, width, str(100 + (i % 11))) for i in range(20)]
    for bar in paused:
        # Deliberately NOT ``_feed_through_program``: that helper settles any
        # leftover active stage itself, which would silently paper over a
        # missing auto-settle inside ``advance()`` and make the assertion
        # below vacuous (verified: it does, when the auto-settle is
        # temporarily removed). Call the production entrypoint raw instead,
        # so this assertion observes exactly what ``advance()`` left behind.
        program.capture_source_bar(bar, mode=EvaluationMode.OBSERVE_ONLY)
        program.on_consolidated_bar(bar)
        assert session.active_stage is None, (
            f"'{key}' left an unsettled OBSERVE_ONLY stage -- Pause must auto-settle DISCARD"
        )

    paused_refusals = _drain_quarantines(program)
    assert paused_refusals == {}, f"'{key}' quarantined a Paused bar unexpectedly: {paused_refusals}"
    new_traces = session.traces[traces_before:]
    assert len(new_traces) == len(paused), f"'{key}' did not record a trace for every Paused (OBSERVE_ONLY) bar"
    assert all(t.evaluation_mode is EvaluationMode.OBSERVE_ONLY for t in new_traces), (
        f"'{key}' recorded a Paused bar's trace under the wrong evaluation_mode"
    )
    assert executor.intents == [], f"'{key}' executed a committed intent while Paused: {executor.intents}"

    after_pause = custody_snapshot(strategy, surface)
    assert after_pause == before, (
        f"'{key}' advanced position custody while Paused (OBSERVE_ONLY): "
        f"{ {k: (before[k], after_pause[k]) for k in before if before[k] != after_pause[k]} }"
    )

    # Pause must not corrupt the program: the very next DECIDE bar must still
    # stage cleanly (no leftover UNSETTLED_STAGE from a mishandled auto-settle).
    resumed = sequential_bucket(params.symbol, len(paused), width, "103")
    stage = _stage(program, resumed, mode=EvaluationMode.DECIDE)
    assert not isinstance(stage, StageQuarantine), (
        f"'{key}' quarantined the first DECIDE bar after Resume: "
        f"{stage.reason if isinstance(stage, StageQuarantine) else ''}"
    )


# ---------------------------------------------------------------------------
# 7. Multi-timeframe boundary.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", SEALED_KEYS)
def test_timeframe_mismatched_bucket_is_refused_and_counted(key: str) -> None:
    """Generalizes ``test_signal_program_decision_clock.py``'s single-program
    (``sma_crossover`` only) ``TIMEFRAME_MISMATCH`` counting proof to every
    sealed program, closing the "relevant multi-timeframe boundaries...per
    program" gap that test left open.
    """
    program, params, _executor, _context = _construct_program(key)
    width = program.session.timeframe_ms
    mis_shaped = bucket(params.symbol, 0, width - 60_000, "100")
    refused = _feed_through_program(program, mis_shaped)

    assert refused == {"TIMEFRAME_MISMATCH": 1}, (
        f"'{key}' did not refuse-and-count a bucket narrower than its own {width}ms "
        f"timeframe: {refused}"
    )
    assert program.session.active_stage is None
