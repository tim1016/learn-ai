"""Corrupt vendor minute bars are rejected before lake publication (#2451).

Each corrupt case fails the capture with a ``validation_failed`` failure whose
detail names the offending bars, and publishes nothing — no minute zip, no
staging leftovers, no complete catalog row. On master these captures published
the corrupt bytes.
"""

from __future__ import annotations

import io
import zipfile
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest
import respx

from app.data_lake import catalog_client
from app.data_lake.ensure_data import _minute_trade_dch, ensure_data, minute_bar_identity
from app.data_lake.path_policy import lake_subpath
from app.data_lake.run_materialization import _build_engine_run_spec
from app.engine.data.lean_format import LeanMinuteDataReader
from app.lean_sidecar.trading_calendar import session_windows_ms_utc
from tests._helpers.fake_lake_catalog import (
    FakeCatalog,
    install_fake_catalog,
    mock_launcher,
    point_lake_writer_at_tmp,
)

# 2024-05-20 is a Monday and a full NYSE session; 09:30 ET == 13:30 UTC.
TRADING_DAY = date(2024, 5, 20)
NEXT_DAY_MS_DELTA = 24 * 60 * 60 * 1000
BAR_START_MS = 1716211800000


@pytest.fixture
def fake_catalog(monkeypatch: pytest.MonkeyPatch) -> FakeCatalog:
    return install_fake_catalog(monkeypatch)


@pytest.fixture
def tmp_lake(tmp_path, monkeypatch: pytest.MonkeyPatch):
    return point_lake_writer_at_tmp(tmp_path, monkeypatch)


def _bar(index: int = 0, **overrides: Any) -> dict[str, Any]:
    bar = {
        "v": 1000,
        "vw": 500.0,
        "o": 500.0,
        "c": 500.05,
        "h": 500.10,
        "l": 499.95,
        "t": BAR_START_MS + index * 60_000,
        "n": 10,
    }
    bar.update(overrides)
    return bar


def _payload(results: list[dict[str, Any]]) -> dict[str, Any]:
    return {"ticker": "SPY", "status": "OK", "results": results}


def _mock_polygon_with(payload: dict[str, Any]):
    return respx.get(url__regex=r"https://api\.polygon\.io/v2/aggs/ticker/SPY/range/1/minute/.*").mock(
        return_value=httpx.Response(200, json=payload)
    )


async def _capture_one_day() -> Any:
    spec = _build_engine_run_spec(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, requester="test")
    return await ensure_data(spec)


def _minute_zips(tmp_lake) -> list:
    raw_root = tmp_lake / lake_subpath("raw")
    return list(raw_root.rglob("*_trade.zip")) if raw_root.exists() else []


async def _assert_corrupt_capture_failed(
    payload: dict[str, Any], fake_catalog: FakeCatalog, tmp_lake, *detail_fragments: str
) -> None:
    mock_launcher()
    _mock_polygon_with(payload)

    result = await _capture_one_day()

    # The minute capture fails on validation; derived artifacts that needed
    # it fail in cascade. No minute artifact may complete.
    assert result.overall_status != "complete", result.failures
    assert not any(a.resolution == "minute" for a in result.artifacts)
    validation = [f for f in result.failures if f.reason == "validation_failed"]
    assert len(validation) == 1, result.failures
    failure = validation[0]
    assert failure.trading_date == TRADING_DAY
    assert failure.data_type == "trade"
    for fragment in detail_fragments:
        assert fragment in (failure.detail or ""), failure.detail
    # Nothing is published: no lake bytes, no staging leftovers, no complete minute row.
    assert _minute_zips(tmp_lake) == []
    assert [p for p in (tmp_lake / "staging").rglob("*") if p.is_file()] == []
    assert not any(
        row["artifact_kind"] == "time_series_bars" and row["resolution"] == "minute" and row["status"] == "complete"
        for row in fake_catalog.rows.values()
    )


# ── Capture-level regressions (each published on master) ─────────────


@respx.mock
@pytest.mark.asyncio
async def test_duplicate_timestamp_fails_the_capture(fake_catalog: FakeCatalog, tmp_lake) -> None:
    bars = [_bar(i) for i in range(5)]
    bars.append(_bar(3))  # duplicate of bar[3]'s t
    await _assert_corrupt_capture_failed(
        _payload(bars), fake_catalog, tmp_lake, "duplicate timestamp", "corrupt vendor minute bars"
    )


@respx.mock
@pytest.mark.asyncio
async def test_wrong_day_bar_fails_the_capture(fake_catalog: FakeCatalog, tmp_lake) -> None:
    bars = [_bar(i) for i in range(5)]
    bars.append(_bar(5, t=BAR_START_MS + NEXT_DAY_MS_DELTA))  # 2024-05-21 ET
    await _assert_corrupt_capture_failed(
        _payload(bars), fake_catalog, tmp_lake,
        "outside requested session 2024-05-20", "2024-05-21",
    )


@respx.mock
@pytest.mark.asyncio
async def test_high_below_low_fails_the_capture(fake_catalog: FakeCatalog, tmp_lake) -> None:
    bars = [_bar(i) for i in range(5)]
    bars.append(_bar(5, o=500.0, h=499.0, l=499.5, c=499.8))  # high < low/open/close
    await _assert_corrupt_capture_failed(
        _payload(bars), fake_catalog, tmp_lake, "high 499.0 < open 500.0", "high 499.0 < close 499.8"
    )


@respx.mock
@pytest.mark.asyncio
async def test_negative_volume_fails_the_capture(fake_catalog: FakeCatalog, tmp_lake) -> None:
    bars = [_bar(i) for i in range(5)]
    bars.append(_bar(5, v=-1))
    await _assert_corrupt_capture_failed(
        _payload(bars), fake_catalog, tmp_lake, "volume=-1 is negative"
    )


@respx.mock
@pytest.mark.asyncio
async def test_valid_capture_is_unchanged(fake_catalog: FakeCatalog, tmp_lake) -> None:
    """A clean 390-bar session still captures and publishes exactly as before."""
    mock_launcher()
    _mock_polygon_with(_payload([_bar(i) for i in range(390)]))

    result = await _capture_one_day()

    assert result.overall_status == "complete", result.failures
    minute = [a for a in result.artifacts if a.resolution == "minute"]
    assert len(minute) == 1
    assert minute[0].row_count == 390
    assert len(_minute_zips(tmp_lake)) == 1


# ── The reader's wrong-width skip is deliberate, not silent ────────


def _minute_zip(rows: list[str]) -> bytes:
    import io
    import zipfile

    csv_name = f"{TRADING_DAY.strftime('%Y%m%d')}_spy_minute_trade.csv"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(csv_name, "\n".join(rows) + "\n")
    return buf.getvalue()


# 09:30 ET in ms-since-midnight is 34200000; prices in deci-cents.
_WELL_FORMED = "34200000,5000000,5001000,4999500,5000500,1000"


def test_reader_skips_a_wrong_width_row_so_the_session_reads_missing() -> None:
    """The skip is the availability calendar's signal (#2451, #2489): a
    session whose rows are all unparseable reads empty — reported missing
    and backfillable — instead of failing the file and marking every
    session in it unreadable. The lake's publication gate is what keeps
    corrupt rows out of managed files; see the parser comment."""
    reader = LeanMinuteDataReader("/nonexistent-lake-root")
    payload = _minute_zip(["34260000,5000000"])  # every row truncated

    assert reader.parse_day_zip(payload, "SPY", TRADING_DAY) == []


def test_reader_parses_well_formed_rows_as_before() -> None:
    reader = LeanMinuteDataReader("/nonexistent-lake-root")
    payload = _minute_zip([_WELL_FORMED, "34260000,5000000,5001000,4999500,5000500,1000"])

    bars = reader.parse_day_zip(payload, "SPY", TRADING_DAY)

    assert len(bars) == 2
    assert bars[0].volume == 1000


# ── #2527 review follow-ups ─────────────────────────────────────────


@respx.mock
@pytest.mark.asyncio
async def test_fractional_share_volume_publishes_exactly(fake_catalog: FakeCatalog, tmp_lake) -> None:
    """Since 2026-02-23 Polygon's ``v`` carries fractional shares (SPY
    2026-09-21 04:00 ET: ``9238.22128``). That is real volume, not
    corruption: the capture publishes it to the last vendor digit, and a
    whole volume still writes without a decimal point."""
    vendor_volumes = [9238.22128, 5244.999999, 0.5, 1000, 34307.0]
    mock_launcher()
    _mock_polygon_with(_payload([_bar(i, v=v) for i, v in enumerate(vendor_volumes)]))

    result = await _capture_one_day()

    assert result.overall_status == "complete", result.failures
    [zip_path] = _minute_zips(tmp_lake)
    payload = zip_path.read_bytes()
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        rows = zf.read(zf.namelist()[0]).decode().splitlines()
    assert [row.split(",")[5] for row in rows] == ["9238.22128", "5244.999999", "0.5", "1000", "34307"]
    stored = LeanMinuteDataReader("/nonexistent-lake-root").parse_day_zip(payload, "SPY", TRADING_DAY)
    assert [bar.volume for bar in stored] == [
        Decimal("9238.22128"), Decimal("5244.999999"), Decimal("0.5"), Decimal("1000"), Decimal("34307"),
    ]


@respx.mock
@pytest.mark.asyncio
async def test_an_unrepresentable_timestamp_is_a_validation_failure(
    fake_catalog: FakeCatalog, tmp_lake
) -> None:
    """A t_ms outside datetime's range used to raise ValueError inside the
    validator before CorruptVendorBarsError could be built, aborting the
    capture with the claim stranded; it is now an offending bar (#2527
    review)."""
    bars = [_bar(i) for i in range(3)]
    raw = [{**b, "t": 9_223_372_036_854_775_807} for b in bars[:1]] + bars[1:]
    await _assert_corrupt_capture_failed(
        _payload(raw), fake_catalog, tmp_lake, "is not a representable instant"
    )


@respx.mock
@pytest.mark.asyncio
async def test_a_legacy_corrupt_cache_hit_is_revalidated_and_rebuilt(
    fake_catalog: FakeCatalog, tmp_lake
) -> None:
    """A pre-validation artifact (complete row, honest sha, corrupt bytes)
    must not stay reusable behind its matching contract: the cache hit
    re-validates the stored bars and the corrupt day rebuilds from the
    provider (#2527 review)."""
    import hashlib

    from app.engine.data.lean_format import write_lean_day_zip
    from app.engine.data.trade_bar import TradeBar

    window = session_windows_ms_utc(TRADING_DAY, TRADING_DAY)[0]
    price = Decimal("100")
    write_lean_day_zip(
        tmp_lake / lake_subpath("raw"),
        "SPY",
        TRADING_DAY,
        [
            TradeBar(symbol="SPY", open=price, high=price, low=price, close=price, volume=10,
                     start_ms=window.open_ms_utc, end_ms=window.open_ms_utc + 60_000),
            TradeBar(symbol="SPY", open=price, high=price, low=price, close=price, volume=10,
                     start_ms=window.open_ms_utc, end_ms=window.open_ms_utc + 60_000),  # duplicate
        ],
    )
    zip_path = tmp_lake / lake_subpath("raw") / "equity/usa/minute/spy" / f"{TRADING_DAY:%Y%m%d}_trade.zip"
    payload = zip_path.read_bytes()
    spec = _build_engine_run_spec(symbol="SPY", start=TRADING_DAY, end=TRADING_DAY, requester="seed")
    identity = minute_bar_identity(spec, symbol="SPY", trading_date=TRADING_DAY, data_type="trade")
    artifact_id = await fake_catalog.claim_minute_bar(
        identity=identity,
        worker_id="legacy-writer",
        lease_ttl_ms=60_000,
        data_contract_hash=_minute_trade_dch(spec.price_adjustment_mode, trading_date=TRADING_DAY),
        file_path=f"equity/usa/minute/spy/{TRADING_DAY:%Y%m%d}_trade.zip",
    )
    assert artifact_id is not None
    assert await fake_catalog.complete_artifact(
        artifact_id,
        row_count=2,
        first_bar_start_ms=window.open_ms_utc,
        last_bar_start_ms=window.open_ms_utc,
        file_size_bytes=len(payload),
        file_sha256=hashlib.sha256(payload).hexdigest(),
        lease_generation=catalog_client.INITIAL_LEASE_GENERATION,
    )

    mock_launcher()
    polygon = _mock_polygon_with(_payload([_bar(i) for i in range(390)]))
    result = await _capture_one_day()

    assert result.overall_status == "complete", result.failures
    minute = [a for a in result.artifacts if a.resolution == "minute"]
    assert len(minute) == 1
    assert minute[0].row_count == 390, "the corrupt legacy cache hit must be rebuilt, not reused"
    assert polygon.call_count == 1
