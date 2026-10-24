"""Admitted lake inputs for a research study (#3521; PRD #3490 R12-R14, T05, T06, T08).

Every test here crosses the real seams: pinned vendor bars are served at the
vendor's HTTP boundary, the real ``ensure_data`` fetches and validates them,
the real lake writer encodes them, the catalog is the in-memory stand-in the
lake suites share, and admission reads them back through committed-byte
admission and the corporate-action receipt check into a temporary lake root.
No test hands the study the bars it expects.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest
import respx

from app.data_lake import run_materialization
from app.data_lake.adjustment_versions import companion_path
from app.data_lake.catalog_client import CatalogUnavailableError
from app.data_lake.lean_writer import build_minute_trade_zip_bytes
from app.data_lake.types import ArtifactFailure
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research.study_inputs import (
    MinuteWindowIdentity,
    StudyInputRefusal,
    admit_minute_inputs,
    bars_as_rows,
    fold_minute_bars,
    lead_in_sessions,
)
from tests._helpers.fake_lake_catalog import FakeCatalog, install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, commit_payload, lake_file, minute_bar
from tests._helpers.pinned_vendor import VendorCalls, mock_vendor, scaled, vendor_history

ET = ZoneInfo("America/New_York")

#: 2025-01-01 00:00 UTC: after every window used here, so each session is complete and in the provider's history.
NOW_MS = 1_735_689_600_000
#: Five years on: the 2024 windows below are older than the provider's history, so only held days can be read.
LATER_MS = 1_893_456_000_000

MARCH = expected_sessions(date(2024, 3, 7), date(2024, 3, 13))  # Mar 10 is the spring-forward Sunday
JULY = expected_sessions(date(2024, 7, 1), date(2024, 7, 5))  # Jul 3 is an early close; Jul 4 a holiday


@pytest.fixture
def catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def lake(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def identity(
    days: list[date],
    *,
    mode: str = "raw",
    session: str = "extended",
    warmup: int = 0,
    symbol: str = "SPY",
) -> MinuteWindowIdentity:
    return MinuteWindowIdentity(symbol, mode, session, days[0], days[-1], warmup)  # type: ignore[arg-type]


def admit(ident: MinuteWindowIdentity, *, now_ms: int = NOW_MS):
    return admit_minute_inputs(ident, requester="test", now_ms=now_ms)


def refusal_of(ident: MinuteWindowIdentity, *, now_ms: int = NOW_MS) -> StudyInputRefusal:
    with pytest.raises(StudyInputRefusal) as raised:
        admit(ident, now_ms=now_ms)
    return raised.value


def counts(coverage_group) -> tuple[int, int, int, int]:
    """(reused, added, missing, failed)."""
    return coverage_group.reused, coverage_group.added, coverage_group.missing, coverage_group.failed


# ---------------------------------------------------------------------------
# T05: held, one gap, fully cold, corrupt adjustment, daily rollup
# ---------------------------------------------------------------------------


@respx.mock
def test_a_cold_window_is_fetched_and_each_session_counts_as_added(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    tape = vendor_history(MARCH)
    mock_vendor("SPY", tape, calls=calls)

    admitted = admit(identity(MARCH))

    assert counts(admitted.coverage.window) == (0, len(MARCH), 0, 0)
    assert calls.days() == MARCH, "each missing session costs exactly one vendor call"
    assert admitted.coverage.artifacts_fetched >= len(MARCH)
    expected = [b["t"] for day in MARCH for b in tape[day]]
    assert [bar.start_ms for bar in admitted.window_bars] == expected, "the lake hands back every bar the vendor sent"


@respx.mock
def test_a_held_window_reuses_every_session_without_asking_the_vendor(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), calls=calls)
    first = admit(identity(MARCH))
    calls_after_first = calls.count

    second = admit(identity(MARCH))

    assert counts(second.coverage.window) == (len(MARCH), 0, 0, 0)
    assert calls.count == calls_after_first, "a held window must cost the vendor nothing"
    assert second.coverage.artifacts_fetched == 0
    assert second.snapshot_digest == first.snapshot_digest, "the same bytes are the same revision"


@respx.mock
def test_one_gap_is_filled_and_counted_apart_from_the_sessions_already_held(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), calls=calls)
    admit(identity(MARCH[:2]))
    admit(identity(MARCH[3:]))
    calls.requests.clear()

    admitted = admit(identity(MARCH))

    assert counts(admitted.coverage.window) == (len(MARCH) - 1, 1, 0, 0)
    assert calls.days() == [MARCH[2]], "only the gap is fetched"


@respx.mock
def test_a_gap_the_vendor_cannot_fill_refuses_and_names_the_session(catalog, lake):
    mock_launcher()
    tape = vendor_history(MARCH)
    mock_vendor("SPY", tape)
    admit(identity(MARCH[:2]))
    admit(identity(MARCH[3:]))
    respx.mock.reset()
    mock_launcher()
    mock_vendor("SPY", {day: bars for day, bars in tape.items() if day != MARCH[2]})

    refusal = refusal_of(identity(MARCH))

    assert refusal.code == "data_unavailable_from_provider"
    assert counts(refusal.coverage.window) == (len(MARCH) - 1, 0, 0, 1)
    assert [(i.trading_date, i.outcome) for i in refusal.coverage.issues] == [(MARCH[2], "unavailable_from_provider")]
    assert MARCH[2].isoformat() in refusal.reason


@respx.mock
def test_a_retained_day_older_than_the_provider_floor_stays_readable_and_a_missing_one_refuses(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), calls=calls)
    admit(identity(MARCH[:3]))
    calls.requests.clear()

    held = admit(identity(MARCH[:3]), now_ms=LATER_MS)
    refusal = refusal_of(identity(MARCH), now_ms=LATER_MS)

    assert counts(held.coverage.window) == (3, 0, 0, 0), "retained history outlives the provider's window"
    assert refusal.code == "data_unavailable_from_provider"
    assert counts(refusal.coverage.window) == (3, 0, 0, len(MARCH) - 3)
    assert calls.count == 0, "a day the provider no longer serves is never asked of it"
    assert "starts" in refusal.coverage.issues[0].reason


def test_a_session_that_has_not_finished_refuses_instead_of_reading_a_partial_day(catalog, lake):
    mid_session = int(datetime(2024, 3, 13, 12, 0, tzinfo=ET).timestamp() * 1000)

    refusal = refusal_of(identity(MARCH), now_ms=mid_session)

    assert refusal.code == "window_not_complete"
    assert "2024-03-13" in refusal.reason


@pytest.mark.parametrize(
    ("symbol", "window", "code"),
    [
        pytest.param("BRK-B", MARCH, "symbol_not_addressable", id="a_ticker_the_lake_cannot_hold"),
        pytest.param("SPY", [date(2024, 3, 9), date(2024, 3, 10)], "no_sessions_in_window", id="a_weekend"),
    ],
)
def test_a_window_the_lake_could_never_hold_refuses_before_any_work(catalog, lake, symbol, window, code):
    refusal = refusal_of(MinuteWindowIdentity(symbol, "raw", "extended", window[0], window[-1]))

    assert refusal.code == code
    assert refusal.coverage is None


def _result_with_failure(spec, failure: ArtifactFailure):
    from app.data_lake.types import DataAvailabilityResult

    return DataAvailabilityResult(
        request_id=spec.request_id,
        overall_status="partial",
        lean_data_root_path="/unused",
        data_availability_hash="h",
        failures=[failure],
        completed_at_ms=0,
        duration_ms=0,
    )


@respx.mock
def test_a_session_another_run_is_still_capturing_is_building_not_missing(catalog, lake, monkeypatch):
    """A lease the other writer has not released is a wait, never a gap (R13).

    The seam is the materializer's sync bridge, which returns what ``ensure_data`` reported once its wait ran out;
    the probe, the read pass and the classification are real.
    """
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    admit(identity(MARCH[:1]))
    admit(identity(MARCH[2:3]))
    contended = ArtifactFailure(
        artifact_kind="time_series_bars", symbol="SPY", trading_date=MARCH[1], data_type="trade", reason="lease_timeout"
    )
    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", lambda spec, **_: _result_with_failure(spec, contended))

    refusal = refusal_of(identity(MARCH[:3]))

    assert refusal.code == "data_building"
    assert [(i.trading_date, i.outcome) for i in refusal.coverage.issues] == [(MARCH[1], "building")]
    assert counts(refusal.coverage.window) == (2, 0, 0, 1)


@pytest.mark.parametrize("error", [CatalogUnavailableError("down"), TimeoutError("late")])
def test_a_capture_that_cannot_run_or_finish_refuses_with_its_own_code(catalog, lake, monkeypatch, error):
    def _raise(spec, **_):
        raise error

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", _raise)

    refusal = refusal_of(identity(MARCH[:2]))

    if isinstance(error, CatalogUnavailableError):
        assert refusal.code == "lake_unavailable"
    else:
        assert refusal.code == "data_building", "a capture that ran out of time is still in flight, not lost"
        assert refusal.coverage.window.building == 2


@respx.mock
def test_a_minute_study_neither_waits_on_nor_builds_the_daily_rollup(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    # A backtest over a narrower window builds the symbol's daily rollup; the wider study window makes it stale.
    narrow = run_materialization._build_engine_run_spec(symbol="SPY", start=MARCH[0], end=MARCH[1], requester="test")
    import asyncio

    asyncio.run(run_materialization._materialize_run_data(narrow, resolution="minute"))
    rollups = lambda: [r for r in catalog.rows.values() if r["resolution"] == "daily"]  # noqa: E731
    assert len(rollups()) == 1
    rollup_before = dict(rollups()[0])

    admitted = admit(identity(MARCH))

    assert counts(admitted.coverage.window) == (2, len(MARCH) - 2, 0, 0)
    assert rollups() == [rollup_before], "the study left the daily rollup exactly as it found it"


# ---------------------------------------------------------------------------
# Corrupt adjustment: a rebuildable record is repaired; one the provider can no longer serve refuses
# ---------------------------------------------------------------------------


def _corrupt_adjustment_record(lake: Path, day: date) -> None:
    record = companion_path(lake_file(lake, "polygon_split_adjusted", day))
    record.write_text(record.read_text().replace('"corporate_action_version": "', '"corporate_action_version": "0'))


@respx.mock
def test_an_adjusted_day_with_a_bad_adjustment_record_is_rebuilt_and_counted_as_added(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), adjusted=vendor_history(MARCH), calls=calls)
    admit(identity(MARCH, mode="polygon_split_adjusted"))
    _corrupt_adjustment_record(lake, MARCH[2])
    calls.requests.clear()

    admitted = admit(identity(MARCH, mode="polygon_split_adjusted"))

    assert counts(admitted.coverage.window) == (len(MARCH) - 1, 1, 0, 0)
    assert calls.days() == [MARCH[2]], "only the day whose record failed is fetched again"
    assert admitted.coverage.artifacts_refreshed + admitted.coverage.artifacts_fetched >= 1


@respx.mock
def test_a_bad_adjustment_record_on_a_day_the_provider_no_longer_serves_refuses_as_corrupt(catalog, lake):
    mock_launcher()
    calls = VendorCalls()
    mock_vendor("SPY", vendor_history(MARCH), adjusted=vendor_history(MARCH), calls=calls)
    admit(identity(MARCH, mode="polygon_split_adjusted"))
    _corrupt_adjustment_record(lake, MARCH[2])
    calls.requests.clear()

    refusal = refusal_of(identity(MARCH, mode="polygon_split_adjusted"), now_ms=LATER_MS)

    assert refusal.code == "data_corrupt"
    assert counts(refusal.coverage.window) == (len(MARCH) - 1, 0, 0, 1)
    assert "AdjustmentVersionError" in refusal.coverage.issues[0].reason
    assert calls.count == 0


# ---------------------------------------------------------------------------
# Data the lake must not accept, and data it must keep exactly
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_vendor_bars_with_a_duplicate_or_out_of_order_timestamp_are_never_published(catalog, lake, fault):
    mock_launcher()
    tape = vendor_history(MARCH)
    bars = tape[MARCH[1]]
    tape[MARCH[1]] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]
    mock_vendor("SPY", tape)

    refusal = refusal_of(identity(MARCH[:3]))

    assert refusal.code == "data_unavailable_from_provider"
    assert [(i.trading_date, i.outcome) for i in refusal.coverage.issues] == [(MARCH[1], "unavailable_from_provider")]
    assert "validation_failed" in refusal.coverage.issues[0].reason
    assert not lake_file(lake, "raw", MARCH[1]).exists(), "a corrupt tape never reaches the lake"


@respx.mock
@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
def test_a_committed_file_with_duplicate_or_out_of_order_rows_refuses_as_corrupt(catalog, lake, fault):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    admit(identity(MARCH[:3]))
    day = MARCH[1]
    rows = [minute_bar(day, 10, 0), minute_bar(day, 10, 1), minute_bar(day, 10, 2)]
    rows = [*rows, rows[2]] if fault == "duplicate" else [rows[0], rows[2], rows[1]]
    commit_bars(catalog, lake, day, rows)

    refusal = refusal_of(identity(MARCH[:3]))

    assert refusal.code == "data_corrupt"
    assert [(i.trading_date, i.outcome) for i in refusal.coverage.issues] == [(day, "corrupt")]
    assert "strictly increasing" in refusal.coverage.issues[0].reason


@respx.mock
def test_a_committed_file_holding_bars_of_another_date_refuses_as_corrupt(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    admit(identity(MARCH[:3]))
    day = MARCH[1]
    name = f"{day:%Y%m%d}_spy_minute_trade.csv"
    written = zipfile.ZipFile(io.BytesIO(build_minute_trade_zip_bytes("SPY", f"{day:%Y%m%d}", [minute_bar(day, 10, 0)])))
    # 10:00 ET is 36,000,000 ms after midnight; 25:00 is the next ET day.
    stray = io.BytesIO()
    with zipfile.ZipFile(stray, "w") as zf:
        zf.writestr(name, written.read(name).decode().replace("36000000", "90000000", 1))
    commit_payload(catalog, lake, day, stray.getvalue())

    refusal = refusal_of(identity(MARCH[:3]))

    assert refusal.code == "data_corrupt"
    assert "outside the session date" in refusal.coverage.issues[0].reason


@respx.mock
def test_prices_finer_than_the_lake_grid_round_half_up_to_the_ten_thousandth(catalog, lake):
    """The lake's price encoding has a precision boundary (lean_writer.to_deci_cent): 1/10,000, rounded half-up."""
    mock_launcher()
    tape = vendor_history(JULY[:1], decimals=5, start_price=12.0)
    mock_vendor("SPY", tape)

    admitted = admit(identity(JULY[:1]))

    expected = [
        (Decimal(repr(b["o"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP), Decimal(repr(b["c"])).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
        for b in tape[JULY[0]]
    ]
    assert [(bar.open, bar.close) for bar in admitted.window_bars] == expected
    drift = max(abs(float(bar.close) - b["c"]) for bar, b in zip(admitted.window_bars, tape[JULY[0]], strict=True))
    # Half a ten-thousandth is the most a price can move; a tie moves by exactly that, so allow float noise only.
    assert 0 < drift <= 5e-5 + 1e-9, "finer prices move by at most half a ten-thousandth, and some of them do move"


@respx.mock
def test_fractional_volume_is_kept_exactly(catalog, lake):
    mock_launcher()
    tape = vendor_history(JULY[:1], fractional_volume=True)
    mock_vendor("SPY", tape)

    admitted = admit(identity(JULY[:1]))

    assert [bar.volume for bar in admitted.window_bars] == [Decimal(repr(b["v"])).normalize() for b in tape[JULY[0]]]
    assert any(bar.volume != bar.volume.to_integral_value() for bar in admitted.window_bars)


# ---------------------------------------------------------------------------
# Sessions: early close, DST, raw versus split-adjusted, warmup
# ---------------------------------------------------------------------------


@respx.mock
@pytest.mark.parametrize("policy", ["regular", "extended"])
def test_the_session_policy_decides_which_bars_an_early_close_day_contributes(catalog, lake, policy):
    mock_launcher()
    tape = vendor_history(JULY)
    mock_vendor("SPY", tape)
    early = date(2024, 7, 3)

    admitted = admit(identity(JULY, session=policy))

    by_day = {day: [b for b in admitted.window_bars if datetime.fromtimestamp(b.start_ms / 1000, ET).date() == day] for day in JULY}
    open_ms, close_ms = session_open_ms_utc(early), session_close_ms_utc(early)
    assert datetime.fromtimestamp(close_ms / 1000, ET).hour == 13, "the calendar, not this test, says it closes at 13:00"
    if policy == "regular":
        assert all(open_ms <= b.start_ms < close_ms for b in by_day[early])
        assert by_day[early][-1].start_ms < close_ms
    else:
        assert any(b.start_ms >= close_ms for b in by_day[early]), "the after-hours tape of a half day is part of the sample"
        assert any(b.start_ms < open_ms for b in by_day[early]), "so is the pre-market"
    assert date(2024, 7, 4) not in by_day or not by_day[date(2024, 7, 4)], "the holiday contributes nothing"
    assert set(admitted.coverage.window.as_dict().values()) >= {len(JULY)}


@respx.mock
def test_sessions_across_the_spring_forward_boundary_open_at_the_calendars_instant(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))

    admitted = admit(identity(MARCH, session="regular"))

    first_bar_by_day: dict[date, int] = {}
    for bar in admitted.window_bars:
        first_bar_by_day.setdefault(datetime.fromtimestamp(bar.start_ms / 1000, ET).date(), bar.start_ms)
    opens_utc = {session_open_ms_utc(day) for day in MARCH}
    assert len(opens_utc) == len(MARCH)
    # 2024-03-08 is EST (open 14:30 UTC) and 2024-03-11 is EDT (open 13:30 UTC): the UTC instants differ by an hour,
    # the wall-clock open does not.
    assert {(session_open_ms_utc(d) % 86_400_000) // 3_600_000 for d in MARCH} == {13, 14}
    for day, first_ms in first_bar_by_day.items():
        assert first_ms >= session_open_ms_utc(day)
        assert datetime.fromtimestamp(first_ms / 1000, ET).strftime("%H") in {"09", "10"}


@respx.mock
def test_raw_and_split_adjusted_windows_are_different_identities_with_different_bytes(catalog, lake):
    """A 10-for-1 split lands on Mar 11: the adjusted tape divides the earlier days' prices by ten."""
    mock_launcher()
    raw = vendor_history(MARCH, start_price=1000.0)
    split_day = MARCH[3]
    adjusted = {day: (scaled({day: bars}, 0.1)[day] if day < split_day else bars) for day, bars in raw.items()}
    split = {"ticker": "SPY", "execution_date": split_day.isoformat(), "split_from": 1, "split_to": 10}
    mock_vendor("SPY", raw, adjusted=adjusted, splits=[split])

    raw_in = admit(identity(MARCH, mode="raw"))
    adjusted_in = admit(identity(MARCH, mode="polygon_split_adjusted"))

    before = [i for i, b in enumerate(raw_in.window_bars) if datetime.fromtimestamp(b.start_ms / 1000, ET).date() < split_day]
    after = [i for i in range(len(raw_in.window_bars)) if i not in set(before)]
    assert [adjusted_in.window_bars[i].close for i in before] != [raw_in.window_bars[i].close for i in before]
    assert float(adjusted_in.window_bars[before[0]].close) == pytest.approx(float(raw_in.window_bars[before[0]].close) / 10, abs=0.01)
    assert [adjusted_in.window_bars[i].close for i in after] == [raw_in.window_bars[i].close for i in after]
    assert raw_in.snapshot_digest != adjusted_in.snapshot_digest
    assert raw_in.snapshot.corporate_action_versions == {}
    assert set(adjusted_in.snapshot.corporate_action_versions) == {"SPY"}
    assert raw_in.data_root_id == adjusted_in.data_root_id


@respx.mock
def test_warmup_sessions_are_the_calendars_sessions_before_the_window_and_counted_apart(catalog, lake):
    mock_launcher()
    window = expected_sessions(date(2024, 3, 11), date(2024, 3, 13))
    lead = lead_in_sessions(window[0], 3)
    assert lead == [date(2024, 3, 6), date(2024, 3, 7), date(2024, 3, 8)], "the weekend is not a session"
    mock_vendor("SPY", vendor_history([*lead, *window]))
    admit(identity(lead[1:]))

    admitted = admit(identity(window, warmup=3))

    assert counts(admitted.coverage.warmup) == (2, 1, 0, 0)
    assert counts(admitted.coverage.window) == (0, 3, 0, 0)
    assert admitted.coverage.warmup.expected == 3
    first_window_ms = session_open_ms_utc(window[0]) - 6 * 3_600_000
    assert all(bar.start_ms < first_window_ms for bar in admitted.warmup_bars)
    assert all(bar.start_ms >= first_window_ms for bar in admitted.window_bars)


@respx.mock
def test_repairing_a_held_day_gives_the_window_a_new_snapshot(catalog, lake):
    mock_launcher()
    mock_vendor("SPY", vendor_history(MARCH))
    before = admit(identity(MARCH[:3]))
    commit_bars(catalog, lake, MARCH[1], [minute_bar(MARCH[1], 10, 0), minute_bar(MARCH[1], 10, 1, "101.00")])

    after = admit(identity(MARCH[:3]))

    assert after.snapshot_digest != before.snapshot_digest
    assert counts(after.coverage.window) == (3, 0, 0, 0), "a repaired day is still a held day"
    assert after.snapshot.sessions == before.snapshot.sessions


# ---------------------------------------------------------------------------
# Fold: coarser bars from minutes, never filled
# ---------------------------------------------------------------------------


def _pandas_fold(bars: list[TradeBar], minutes: int) -> pd.DataFrame:
    """An independent reference: group minutes by the ET wall-clock bucket they fall in."""
    frame = pd.DataFrame(
        {
            "t": [b.start_ms for b in bars],
            "open": [float(b.open) for b in bars],
            "high": [float(b.high) for b in bars],
            "low": [float(b.low) for b in bars],
            "close": [float(b.close) for b in bars],
            "volume": [float(b.volume) for b in bars],
        }
    )
    local = pd.to_datetime(frame["t"], unit="ms", utc=True).dt.tz_convert("America/New_York")
    floored = local.dt.floor(f"{minutes}min").dt.tz_convert("UTC")
    frame["bucket"] = (floored - pd.Timestamp("1970-01-01", tz="UTC")) // pd.Timedelta(milliseconds=1)
    grouped = frame.groupby("bucket", sort=True).agg(
        open=("open", "first"), high=("high", "max"), low=("low", "min"), close=("close", "last"), volume=("volume", "sum")
    )
    return grouped.reset_index()


@respx.mock
@pytest.mark.parametrize("minutes", [3, 5, 15])
def test_fold_matches_an_independent_pandas_reference(catalog, lake, minutes):
    mock_launcher()
    mock_vendor("SPY", vendor_history(JULY))
    bars = list(admit(identity(JULY)).window_bars)

    folded = fold_minute_bars(bars, minutes)
    reference = _pandas_fold(bars, minutes)

    assert [b.start_ms for b in folded] == reference["bucket"].tolist()
    for column in ("open", "high", "low", "close"):
        np.testing.assert_allclose([float(getattr(b, column)) for b in folded], reference[column].to_numpy(), atol=1e-12, rtol=0)
    np.testing.assert_allclose([float(b.volume) for b in folded], reference["volume"].to_numpy(), atol=1e-9, rtol=0)
    bucket_ms = minutes * 60_000
    assert all(b.start_ms % bucket_ms == 0 for b in folded), "every bar starts on a wall-clock boundary"
    assert len({b.start_ms for b in folded}) == len(folded)


def test_fold_fills_nothing_and_keeps_a_partial_trailing_bucket():
    day = date(2024, 3, 11)
    open_ms = session_open_ms_utc(day)
    price = Decimal("10")
    # minutes 0, 1 and 2 of the first 5-minute bucket, nothing in the second, one lone minute in the third
    starts = [open_ms, open_ms + 60_000, open_ms + 120_000, open_ms + 10 * 60_000]
    minutes = [TradeBar(symbol="SPY", start_ms=s, end_ms=s + 60_000, open=price, high=price + i, low=price - i, close=price + i, volume=Decimal(i + 1)) for i, s in enumerate(starts)]

    folded = fold_minute_bars(minutes, 5)

    assert [b.start_ms for b in folded] == [open_ms, open_ms + 10 * 60_000], "the empty bucket yields no bar"
    assert folded[0].open == price and folded[0].close == price + 2 and folded[0].high == price + 2 and folded[0].low == price - 2
    assert folded[0].volume == Decimal(1 + 2 + 3)
    assert folded[1].volume == Decimal(4), "the trailing partial bucket is emitted, as a vendor aggregate would"


def test_fold_of_one_minute_bars_is_the_input_and_bad_lengths_are_refused():
    assert fold_minute_bars([], 1) == []
    with pytest.raises(ValueError, match="positive"):
        fold_minute_bars([], 0)


def test_rows_carry_the_bar_start_and_float_prices_exactly_on_the_grid():
    bar = TradeBar(symbol="SPY", start_ms=1_700_000_000_000, end_ms=1_700_000_060_000, open=Decimal("12.3457"), high=Decimal("12.3457"), low=Decimal("12.3457"), close=Decimal("12.3457"), volume=Decimal("0.333333"))

    (row,) = bars_as_rows([bar])

    assert row == {"timestamp": 1_700_000_000_000, "open": 12.3457, "high": 12.3457, "low": 12.3457, "close": 12.3457, "volume": 0.333333}
