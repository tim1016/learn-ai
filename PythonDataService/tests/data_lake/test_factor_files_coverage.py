"""Factor-file coverage (#2452): what a file built over captured sessions holds,
and the one check every adjusted reader runs.

The coverage claim (``app/data_lake/factor_files.py`` module docstring): a
covered span holds every corporate action with ``first < ex-date <= last``,
priced against the prior session's close, so every ratio *inside* a span
is exact on the latest-known basis; actions outside every span scale a
whole span uniformly. The tests below pin that claim: a single span's
bytes against a literal the pre-#2452 window builder produced, and
in-span ratios against narrow builds at ``atol=1e-9, rtol=0`` (the only
difference is the builder's 10-dp factor quantization, ~1e-10 relative
per row).
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from itertools import pairwise
from pathlib import Path

import pytest

from app.data_lake.factor_files import (
    FactorFileBuildError,
    FactorFileNotCoveringError,
    FactorFileReferenceError,
    SessionRun,
    build_factor_file_bytes,
    factor_coverage_record_bytes,
    factor_multiplier_as_of,
    parse_factor_file,
    plan_factor_file,
    read_covering_factor_rows,
)
from app.data_lake.path_policy import LeanFactorFilePath
from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent
from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc

SYMBOL = "SPY"
# 2024-07-04 is a holiday and 07-06/07 a weekend; 07-09 is left uncaptured.
CAPTURED = [date(2024, 7, d) for d in (1, 2, 3, 5, 8, 10, 11)]
SPAN_1 = SessionRun(date(2024, 7, 1), date(2024, 7, 8))
SPAN_2 = SessionRun(date(2024, 7, 10), date(2024, 7, 11))


def _split(ex: date, split_from: float = 1, split_to: float = 2) -> SplitEvent:
    return SplitEvent(execution_date=ex.isoformat(), split_from=split_from, split_to=split_to)


def _dividend(ex: date, cash: float) -> DividendEvent:
    return DividendEvent(ex_dividend_date=ex.isoformat(), cash_amount=cash)


def _flat_closes(sessions: list[date]) -> dict[date, Decimal]:
    return {d: Decimal(100) for d in sessions}


# ---------------------------------------------------------------------------
# Planning: spans, kept actions, reference sessions
# ---------------------------------------------------------------------------


def test_spans_are_runs_of_scheduled_sessions_across_weekends_and_holidays() -> None:
    plan = plan_factor_file(CAPTURED, [], [])

    assert plan.spans == (SPAN_1, SPAN_2)
    assert plan.anchor_sessions == (date(2024, 7, 1), date(2024, 7, 11))


def test_only_actions_inside_a_span_are_kept_each_priced_on_its_prior_session() -> None:
    inside = _split(date(2024, 7, 5))  # prior session 07-03, across the holiday
    on_span_start = _split(date(2024, 7, 1))
    in_the_gap = _split(date(2024, 7, 9))
    on_second_span_start = _split(date(2024, 7, 10))
    after_capture = _split(date(2024, 7, 15))
    dividend = _dividend(date(2024, 7, 11), 1.5)

    plan = plan_factor_file(
        CAPTURED,
        [on_span_start, inside, in_the_gap, on_second_span_start, after_capture],
        [dividend],
    )

    assert plan.splits == (inside,)
    assert plan.dividends == (dividend,)
    assert plan.reference_sessions == (date(2024, 7, 3), date(2024, 7, 10))


def test_a_weekend_ex_date_inside_a_span_is_kept() -> None:
    weekend = _dividend(date(2024, 7, 6), 1.0)

    plan = plan_factor_file(CAPTURED, [], [weekend])

    assert plan.dividends == (weekend,)
    assert plan.reference_sessions == (date(2024, 7, 5),)


@pytest.mark.parametrize(
    ("captured", "reason"),
    [([], "at least one captured session"), ([date(2024, 7, 6)], "scheduled NYSE session")],
    ids=["nothing-captured", "only-a-weekend"],
)
def test_a_plan_without_a_captured_session_is_a_typed_refusal(captured: list[date], reason: str) -> None:
    with pytest.raises(FactorFileBuildError, match=reason):
        plan_factor_file(captured, [], [])


def test_a_missing_reference_close_refuses_the_build() -> None:
    plan = plan_factor_file(CAPTURED, [], [_dividend(date(2024, 7, 11), 1.5)])
    closes = _flat_closes(CAPTURED)
    del closes[date(2024, 7, 10)]

    with pytest.raises(FactorFileReferenceError, match="2024-07-10"):
        build_factor_file_bytes(SYMBOL, plan, closes)


# ---------------------------------------------------------------------------
# Unpriced sessions: one unreadable close breaks one span, not the symbol
# ---------------------------------------------------------------------------


def test_an_unpriced_reference_session_ends_its_span_and_drops_its_action() -> None:
    """07-03 prices the 07-05 split (across the holiday). Unpriced, it ends
    the span there: the split lands on the next span's first session, outside
    every span, and the other span's dividend is untouched."""
    split = _split(date(2024, 7, 5))
    dividend = _dividend(date(2024, 7, 11), 1.5)

    plan = plan_factor_file(CAPTURED, [split], [dividend], unpriced_sessions=[date(2024, 7, 3)])

    assert plan.spans == (
        SessionRun(date(2024, 7, 1), date(2024, 7, 3)),
        SessionRun(date(2024, 7, 5), date(2024, 7, 8)),
        SPAN_2,
    )
    assert plan.splits == ()
    assert plan.dividends == (dividend,)
    assert plan.reference_sessions == (date(2024, 7, 10),)
    assert plan.anchor_sessions == (date(2024, 7, 1), date(2024, 7, 11))


def test_an_unpriced_last_session_of_a_span_changes_nothing() -> None:
    """Nothing in a span follows its last session, so there is no span to end."""
    assert plan_factor_file(CAPTURED, [], [], unpriced_sessions=[date(2024, 7, 8)]).spans == (SPAN_1, SPAN_2)


def test_only_reads_crossing_an_unpriced_session_are_refused(tmp_path: Path) -> None:
    """The built file (07-03's close unreadable) still covers every read
    that stays on one side of 07-03 → 07-05; only a read crossing it is refused."""
    closes = _flat_closes(CAPTURED)
    del closes[date(2024, 7, 3)]
    split, dividend = _split(date(2024, 7, 5)), _dividend(date(2024, 7, 11), 1.5)
    plan = plan_factor_file(CAPTURED, [split], [dividend], unpriced_sessions=[date(2024, 7, 3)])
    _write(tmp_path, build_factor_file_bytes(SYMBOL, plan, closes), list(plan.spans))

    assert _read(tmp_path, [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3)])
    assert _read(tmp_path, [date(2024, 7, 5), date(2024, 7, 8)])
    assert _read(tmp_path, [date(2024, 7, 10), date(2024, 7, 11)])
    with pytest.raises(FactorFileNotCoveringError, match=r"2024-07-03\.\.2024-07-05"):
        _read(tmp_path, [date(2024, 7, 3), date(2024, 7, 5)])


# ---------------------------------------------------------------------------
# Numerical basis
# ---------------------------------------------------------------------------


def test_a_single_span_builds_the_pinned_bytes() -> None:
    """No numerical change for a contiguous capture. The expected bytes are
    what the pre-#2452 window builder produced over [2024-06-03, 2026-04-30]
    for these inputs (captured at abe8ac95), and they check by hand: the
    2025-12-19 dividend is 1 - 1.81 / 503.88 = 0.9964078749; the 2024-09-20
    one multiplies in 1 - 1.75 / 500.75, giving 0.9929256706; the 4:1 split
    is 0.25 through its prior session, 2024-08-29."""
    sessions = expected_sessions(date(2024, 6, 3), date(2026, 4, 30))
    closes = {d: Decimal(500) + Decimal(i) / 100 for i, d in enumerate(sessions)}
    splits = [_split(date(2024, 8, 30), 1, 4)]
    dividends = [_dividend(date(2024, 9, 20), 1.75), _dividend(date(2025, 12, 19), 1.81)]

    planned = build_factor_file_bytes(SYMBOL, plan_factor_file(sessions, splits, dividends), closes)

    assert planned == (
        b"20240603,0.9929256706,0.25,500\n"
        b"20240829,0.9929256706,0.25,500.61\n"
        b"20240919,0.9929256706,1,500.75\n"
        b"20251218,0.9964078749,1,503.88\n"
        b"20260430,1,1,504.78\n"
    )


_NARROW_SESSIONS = expected_sessions(date(2024, 7, 1), date(2024, 9, 30))
_WIDE_SESSIONS = expected_sessions(date(2024, 3, 1), date(2025, 12, 31))
_IN_WINDOW_SPLITS = [_split(date(2024, 8, 30), 1, 4)]
_IN_WINDOW_DIVIDENDS = [_dividend(date(2024, 7, 19), 1.2), _dividend(date(2024, 9, 20), 1.75)]


def _day_over_day_ratios(splits: list[SplitEvent], dividends: list[DividendEvent], sessions: list[date]) -> list[float]:
    rows = parse_factor_file(
        build_factor_file_bytes(
            SYMBOL, plan_factor_file(sessions, splits, dividends), _flat_closes(_WIDE_SESSIONS)
        ).decode("ascii")
    )
    return [
        float(factor_multiplier_as_of(rows, previous) / factor_multiplier_as_of(rows, current))
        for previous, current in pairwise(_NARROW_SESSIONS)
    ]


def test_widening_leaves_every_in_window_ratio_unchanged() -> None:
    """Actions before a window never touch its bars, and later dividends scale
    every bar in it by one factor, so each day-over-day multiplier ratio
    inside the window is the narrow build's (the 10-dp quantization is the
    only difference, far inside ``atol=1e-9``)."""
    earlier_splits = [_split(date(2024, 4, 15), 1, 3)]
    earlier_dividends = [_dividend(date(2024, 3, 22), 1.4)]
    later_dividends = [_dividend(date(2025, 3, 21), 1.6), _dividend(date(2025, 6, 20), 1.7)]

    narrow = _day_over_day_ratios(_IN_WINDOW_SPLITS, _IN_WINDOW_DIVIDENDS, _NARROW_SESSIONS)
    wide = _day_over_day_ratios(
        earlier_splits + _IN_WINDOW_SPLITS,
        earlier_dividends + _IN_WINDOW_DIVIDENDS + later_dividends,
        _WIDE_SESSIONS,
    )

    assert wide == pytest.approx(narrow, abs=1e-9, rel=0)


def test_actions_in_a_capture_gap_leave_each_spans_ratios_as_that_span_alone() -> None:
    """A split and a dividend in the uncaptured gap between two spans are in
    no span, so the file omits them; each span's day-over-day multiplier
    ratios must then equal a build over that span alone.

    Tolerance ``atol=1e-9, rtol=0`` (the strict-float default): the builds
    differ only in the 10-dp quantization of cumulative factors. Each factor
    here is >= ~0.32, so it carries <= ~1.6e-10 relative rounding error and
    a ratio of two <= ~3.2e-10 — review measured <= 8e-11.

    Span B holds no split on purpose: under the ported dividend formula a
    later split rescales an earlier dividend's ratio (pinned below as an open
    question), which is not what this test is about.
    """
    span_a = expected_sessions(date(2024, 3, 1), date(2024, 6, 28))
    span_b = expected_sessions(date(2024, 8, 1), date(2024, 12, 31))
    closes = {
        d: Decimal(100) + Decimal(i) / 100
        for i, d in enumerate(expected_sessions(date(2024, 3, 1), date(2024, 12, 31)))
    }
    in_a = [_split(date(2024, 4, 15), 1, 3), _dividend(date(2024, 3, 22), 1.4)]
    in_gap = [_split(date(2024, 7, 15), 1, 2), _dividend(date(2024, 7, 19), 1.1)]
    in_b = [_dividend(date(2024, 9, 20), 1.75), _dividend(date(2024, 12, 20), 1.9)]
    splits = [a for a in (*in_a, *in_gap, *in_b) if isinstance(a, SplitEvent)]
    dividends = [a for a in (*in_a, *in_gap, *in_b) if isinstance(a, DividendEvent)]
    both = [*span_a, *span_b]

    def ratios(captured: list[date], span: list[date]) -> list[float]:
        rows = parse_factor_file(
            build_factor_file_bytes(SYMBOL, plan_factor_file(captured, splits, dividends), closes).decode("ascii")
        )
        return [
            float(factor_multiplier_as_of(rows, previous) / factor_multiplier_as_of(rows, current))
            for previous, current in pairwise(span)
        ]

    plan = plan_factor_file(both, splits, dividends)
    assert plan.spans == (SessionRun(span_a[0], span_a[-1]), SessionRun(span_b[0], span_b[-1]))
    assert not {*in_gap} & {*plan.splits, *plan.dividends}
    for span in (span_a, span_b):
        alone = ratios(span, span)
        assert any(r != pytest.approx(1.0, abs=1e-6, rel=0) for r in alone), "the span must hold an action"
        assert ratios(both, span) == pytest.approx(alone, abs=1e-9, rel=0)


def test_a_later_split_rescales_an_earlier_dividend_under_the_ported_formula() -> None:
    """Pins a known, reported discrepancy — not an endorsement (#2452 report).

    ``build_factor_file_bytes`` prices a dividend as ``1 - cash * split_factor
    / reference_close`` with the cumulative split factor of the splits *after*
    it, exactly as LEAN's ToolBox ``FactorFileGenerator.CalculateNextDividendFactor``
    does at the pinned commit. Our reference close is raw, and QuantConnect's
    own factor file for AAPL at that commit encodes the raw/raw ratio instead
    (the 2020-08-07 $0.82 dividend before the 2020-08-31 4:1 split:
    0.9949942 / 0.9967882 = 0.998200 = 1 - 0.82 / 455.61, not 0.999550).
    So a split captured after a window rescales that window's dividend-day
    ratio. Changing the formula is an owner decision; until then this test
    makes the dependence explicit.
    """
    narrow = _day_over_day_ratios(_IN_WINDOW_SPLITS, _IN_WINDOW_DIVIDENDS, _NARROW_SESSIONS)
    with_later_split = _day_over_day_ratios(
        [*_IN_WINDOW_SPLITS, _split(date(2025, 6, 10), 1, 2)], _IN_WINDOW_DIVIDENDS, _WIDE_SESSIONS
    )
    dividend_day = _NARROW_SESSIONS.index(date(2024, 9, 20)) - 1

    assert narrow[dividend_day] == pytest.approx(1 - 1.75 / 100, abs=1e-9, rel=0)
    assert with_later_split[dividend_day] == pytest.approx(1 - 1.75 * 0.5 / 100, abs=1e-9, rel=0)


# ---------------------------------------------------------------------------
# The coverage record and the one check
# ---------------------------------------------------------------------------


def _write(lake_root: Path, body: bytes, spans: list[SessionRun], *, record_symbol: str = SYMBOL) -> None:
    paths = LeanFactorFilePath(market="usa", symbol=SYMBOL)
    csv_path = lake_root.joinpath(*paths.relative_path().parts)
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    csv_path.write_bytes(body)
    lake_root.joinpath(*paths.coverage_record_path().parts).write_bytes(
        factor_coverage_record_bytes(record_symbol, body, spans)
    )


_BODY = b"20240703,1,0.5,100\n20240711,1,1,50\n"


def _read(lake_root: Path, sessions: list[date]) -> object:
    return read_covering_factor_rows(lake_root, market="usa", symbol=SYMBOL, sessions=sessions)


def test_the_record_stores_session_open_anchors_as_int64_ms_utc(tmp_path: Path) -> None:
    _write(tmp_path, _BODY, [SPAN_1, SPAN_2])

    record = json.loads(
        tmp_path.joinpath(*LeanFactorFilePath(market="usa", symbol=SYMBOL).coverage_record_path().parts).read_text()
    )

    assert record["covered_spans"][0] == {
        "first_session_open_ms_utc": session_open_ms_utc(date(2024, 7, 1)),
        "last_session_open_ms_utc": session_open_ms_utc(date(2024, 7, 8)),
    }
    assert isinstance(record["covered_spans"][0]["first_session_open_ms_utc"], int)


def test_runs_inside_the_covered_spans_are_covered(tmp_path: Path) -> None:
    _write(tmp_path, _BODY, [SPAN_1, SPAN_2])

    assert _read(tmp_path, [date(2024, 7, 1), date(2024, 7, 2), date(2024, 7, 3)]) == parse_factor_file(
        _BODY.decode("ascii")
    )
    # 07-09 is not read, so 07-08 and 07-10 are separate runs, one per span.
    assert _read(tmp_path, [date(2024, 7, 5), date(2024, 7, 8), date(2024, 7, 10)])
    assert _read(tmp_path, []) == parse_factor_file(_BODY.decode("ascii"))


def test_a_run_bridging_two_spans_is_not_covered(tmp_path: Path) -> None:
    """07-09 captured after the build joins the runs: the 07-08 → 07-09 → 07-10
    returns cross actions the file never priced."""
    _write(tmp_path, _BODY, [SPAN_1, SPAN_2])

    with pytest.raises(FactorFileNotCoveringError, match=r"2024-07-08\.\.2024-07-10"):
        _read(tmp_path, [date(2024, 7, 8), date(2024, 7, 9), date(2024, 7, 10)])


def test_sessions_past_the_last_span_are_not_covered(tmp_path: Path) -> None:
    _write(tmp_path, _BODY, [SPAN_1])

    with pytest.raises(FactorFileNotCoveringError, match=r"2024-07-01\.\.2024-07-08"):
        _read(tmp_path, [date(2024, 7, 10), date(2024, 7, 11)])


def test_no_factor_file_covers_nothing(tmp_path: Path) -> None:
    with pytest.raises(FactorFileNotCoveringError, match="no factor file"):
        _read(tmp_path, [date(2024, 7, 1)])


def test_a_factor_file_without_a_record_covers_nothing(tmp_path: Path) -> None:
    csv_path = tmp_path.joinpath(*LeanFactorFilePath(market="usa", symbol=SYMBOL).relative_path().parts)
    csv_path.parent.mkdir(parents=True)
    csv_path.write_bytes(_BODY)

    with pytest.raises(FactorFileNotCoveringError, match="no coverage record"):
        _read(tmp_path, [date(2024, 7, 1)])


def test_a_record_bound_to_other_bytes_covers_nothing(tmp_path: Path) -> None:
    _write(tmp_path, _BODY, [SPAN_1, SPAN_2])
    csv_path = tmp_path.joinpath(*LeanFactorFilePath(market="usa", symbol=SYMBOL).relative_path().parts)
    csv_path.write_bytes(b"20240711,1,1,50\n")  # a narrower rebuild without its record

    with pytest.raises(FactorFileNotCoveringError, match="different bytes"):
        _read(tmp_path, [date(2024, 7, 1)])


def test_a_record_for_another_symbol_covers_nothing(tmp_path: Path) -> None:
    _write(tmp_path, _BODY, [SPAN_1], record_symbol="QQQ")

    with pytest.raises(FactorFileNotCoveringError, match="different bytes"):
        _read(tmp_path, [date(2024, 7, 1)])


@pytest.mark.parametrize(
    "record",
    [
        b"not json",
        b'{"schema_version": 2, "symbol": "SPY", "factor_file_sha256": "' + b"0" * 64 + b'", "covered_spans": []}',
        b'{"schema_version": 1, "symbol": "SPY", "factor_file_sha256": "' + b"0" * 64 + b'", "covered_spans": ['
        b'{"first_session_open_ms_utc": 2, "last_session_open_ms_utc": 1}]}',
        b'{"schema_version": 1, "symbol": "SPY", "factor_file_sha256": "' + b"0" * 64 + b'", "covered_spans": ['
        b'{"first_session_open_ms_utc": 1, "last_session_open_ms_utc": 5}, '
        b'{"first_session_open_ms_utc": 5, "last_session_open_ms_utc": 9}]}',
    ],
    ids=["malformed-json", "unknown-schema", "inverted-span", "overlapping-spans"],
)
def test_an_unreadable_record_covers_nothing(tmp_path: Path, record: bytes) -> None:
    _write(tmp_path, _BODY, [SPAN_1])
    tmp_path.joinpath(*LeanFactorFilePath(market="usa", symbol=SYMBOL).coverage_record_path().parts).write_bytes(
        record
    )

    with pytest.raises(FactorFileNotCoveringError, match="unreadable"):
        _read(tmp_path, [date(2024, 7, 1)])
