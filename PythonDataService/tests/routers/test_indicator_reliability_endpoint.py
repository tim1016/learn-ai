"""``POST /api/research/indicator-reliability`` over admitted lake inputs (#3523; PRD #3490 T05, T08, T09; #3527).

The route runs under ASGI with the real lake path beneath it — pinned vendor bars served at the vendor's HTTP seam,
``ensure_data``, the lake writer, the in-memory catalog stand-in, committed-byte admission — and the real statistics on
top. The Polygon SDK is made unreachable while a request runs: a response proves no bar came from the provider path.

The random-shuffle baseline is an unseeded Monte Carlo draw in production; where a test compares numbers it seeds the
draw (``seeded_baseline``) so the comparison is between inputs, not between random streams.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import pytest
import respx
from fastapi import FastAPI
from httpx import ASGITransport

from app.data_lake import run_materialization
from app.data_lake.catalog_client import CatalogUnavailableError
from app.lean_sidecar.trading_calendar import expected_sessions, session_close_ms_utc, session_open_ms_utc
from app.research import indicator_reliability as reliability_math
from app.research.study_inputs import lead_in_sessions
from app.routers import indicator_reliability as router
from app.services import dataset_service, indicator_reliability_service
from app.services.polygon_client import PolygonClientService
from app.utils.session_anchors import et_midnight_ms
from tests._helpers.fake_lake_catalog import install_fake_catalog, mock_launcher, point_lake_writer_at_tmp
from tests._helpers.lake_study import commit_bars, corrected_day
from tests._helpers.pinned_vendor import VendorBar, mock_vendor, vendor_history
from tests._helpers.reliability_provider_path import provider_frame, seeded_baseline

ET = ZoneInfo("America/New_York")
NOW_MS = 1_735_689_600_000  # 2025-01-01
WINDOW = expected_sessions(date(2024, 3, 4), date(2024, 3, 15))  # ten sessions across the spring-forward Sunday
LEAD = lead_in_sessions(WINDOW[0], 3)  # more sessions than any test's warmup asks for
#: Jun 19 and Jul 4 are holidays and Jul 3 closes at 13:00: 18 sessions, 123 hourly bars, past the study's 100-bar guard.
HOURLY_WINDOW = expected_sessions(date(2024, 6, 17), date(2024, 7, 12))
HOURLY_INDICATOR = {"indicator_name": "rsi", "indicator_params": {"length": 5}}  # 50 warmup bars: nine hourly sessions

#: Dense enough that every minute of the regular session trades, so bar counts are exact.
DENSE = {"regular_keep": 1.0, "extended_keep": 0.9}


@pytest.fixture
def api() -> FastAPI:
    app = FastAPI()
    app.include_router(router.router, prefix="/api/research")
    return app


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    catalog = install_fake_catalog(monkeypatch)
    lake = point_lake_writer_at_tmp(tmp_path, monkeypatch)
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: NOW_MS)
    monkeypatch.setattr(indicator_reliability_service, "now_ms_utc", lambda: NOW_MS)
    monkeypatch.setattr(router, "RANDOM_SIMULATIONS", 20)
    with respx.mock:
        mock_launcher()
        yield catalog, lake


def tape(days: list[date], **options: Any) -> dict[date, list[VendorBar]]:
    return vendor_history([*LEAD, *days], seed=11, start_price=150.0, **{**DENSE, **options})


def body(**overrides: Any) -> dict[str, Any]:
    base = {
        "symbol": "SPY",
        "from_date": WINDOW[0].isoformat(),
        "to_date": WINDOW[-1].isoformat(),
        "timespan": "minute",
        "multiplier": 1,
        "session": "rth",
        "indicator_name": "rsi",
        "indicator_params": {"length": 14},
        "horizons": [1, 5],
        "include_slope": False,
    }
    return {**base, **overrides}


async def post(api: FastAPI, payload: dict[str, Any]) -> httpx.Response:
    """One request, with the Polygon SDK made unreachable for its duration: a response proves no bar came from it."""
    unreachable = AssertionError("the reliability study asked the Polygon SDK for bars")
    with patch.object(PolygonClientService, "fetch_aggregates", side_effect=unreachable):
        async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test", timeout=120) as client:
            return await client.post("/api/research/indicator-reliability", json=payload)


# ---------------------------------------------------------------------------
# A completed study says where its data came from
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_completed_study_carries_a_lake_data_source_with_coverage_warmup_and_identity(api, world):
    mock_vendor("SPY", tape(WINDOW))

    response = await post(api, body())

    assert response.status_code == 200, response.text
    result = response.json()
    source = result["data_source"]
    assert result["success"] is True and source["kind"] == "lake"
    assert (source["session_policy"], source["bar_minutes"], source["price_adjustment_mode"]) == ("regular", 1, "polygon_split_adjusted")
    assert source["coverage"]["window"] == {
        "expected_sessions": len(WINDOW), "reused": 0, "added": len(WINDOW), "missing": 0, "failed": 0,
        "building": 0, "corrupt": 0, "unavailable_from_provider": 0,
    }
    assert source["coverage"]["warmup"]["expected_sessions"] == 1 and source["coverage"]["warmup"]["added"] == 1
    assert source["warmup"] == {
        "policy": "allow_shortened", "requested_bars": 140, "requested_sessions": 1, "admitted_sessions": 1,
        "admitted_bars": 390, "shortened": False, "shortfall_bars": 0, "cause": "none",
    }
    assert source["bars_used"] == result["bar_count"] and source["minute_bars_read"] == result["bar_count"] + 390
    assert len(source["snapshot_digest"]) == 64 and len(source["identity_digest"]) == 64
    assert len(source["result_identity_digest"]) == 64
    assert source["window_start_ms"] == et_midnight_ms(WINDOW[0]) and source["window_end_ms"] == et_midnight_ms(WINDOW[-1])


@pytest.mark.asyncio
async def test_the_legacy_session_label_stays_inert_and_the_sample_is_always_regular_hours(api, world):
    mock_vendor("SPY", tape(WINDOW))

    regular = (await post(api, body(session="rth"))).json()
    extended = (await post(api, body(session="extended"))).json()

    assert regular["data_source"]["session_policy"] == extended["data_source"]["session_policy"] == "regular"
    assert regular["bar_count"] == extended["bar_count"]
    assert regular["data_source"]["identity_digest"] == extended["data_source"]["identity_digest"]


# ---------------------------------------------------------------------------
# Same canonical input, same outputs (T08)
# ---------------------------------------------------------------------------


KEYS_NOT_IN_THE_MATH = {"annualized_ir", "sharpe_estimate", "breadth_per_year", "random_baseline_distribution"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("timespan", "multiplier"), [("minute", 1), ("minute", 5)])
async def test_the_endpoint_gives_the_statistics_the_provider_path_gave_on_the_same_bars(
    api, world, monkeypatch, timespan, multiplier
):
    """On a price grid the lake holds exactly, the lake's bars are the vendor's bars and so are every statistic."""
    vendor = tape(WINDOW)
    mock_vendor("SPY", vendor)
    seeded_baseline(monkeypatch)

    response = await post(api, body(timespan=timespan, multiplier=multiplier))

    assert response.status_code == 200, response.text
    old = provider_frame(vendor, from_date=WINDOW[0], to_date=WINDOW[-1], timespan=timespan, multiplier=multiplier)
    raw_results, _, metadata = reliability_math.compute_indicator_reliability_with_oos(
        df=old.frame, indicator_column=old.indicator_column, horizons=[1, 5], include_slope=False,
        train_ratio=reliability_math.TRAIN_RATIO, random_simulations=20,
    )
    result = response.json()
    assert result["bar_count"] == len(old.frame)
    assert [r["horizon"] for r in result["results"]] == [a.horizon for a in raw_results]
    compared = 0
    for row, analysis in zip(result["results"], raw_results, strict=True):
        for key, expected in asdict(analysis).items():
            if key in KEYS_NOT_IN_THE_MATH or key not in row:
                continue
            compared += 1
            if isinstance(expected, float) or isinstance(row[key], float):
                assert np.isclose(row[key], expected, atol=1e-9, rtol=0, equal_nan=True), (analysis.horizon, key)
            else:
                assert row[key] == expected, (analysis.horizon, key)
    assert compared >= 2 * 20, "every statistic of every horizon was compared, not a handful"
    for key in ("train_start", "train_end", "test_start", "test_end", "train_bars", "test_bars"):
        assert result[key] == metadata[key], key


# ---------------------------------------------------------------------------
# #3527: the session filter used to be blind to the bar length
# ---------------------------------------------------------------------------


def hourly_buckets(day: date) -> int:
    """How many wall-clock hours the canonical calendar's session touches: the bars an hourly study should have."""
    return len({t // 3_600_000 for t in range(session_open_ms_utc(day), session_close_ms_utc(day), 60_000)})


@pytest.mark.asyncio
async def test_an_hourly_request_keeps_the_bar_that_holds_the_open_on_every_session(api, world):
    """Seven bars on a regular day, four on a 13:00 close. The old call dropped the 09:00 bar: six and three."""
    lead = lead_in_sessions(HOURLY_WINDOW[0], 9)
    vendor = vendor_history([*lead, *HOURLY_WINDOW], seed=11, start_price=150.0, regular_keep=1.0, extended_keep=0.05)
    mock_vendor("SPY", vendor)
    wanted = sum(hourly_buckets(day) for day in HOURLY_WINDOW)
    assert wanted == 7 * (len(HOURLY_WINDOW) - 1) + 4, "the calendar: one half day among regular sessions"

    response = await post(
        api,
        body(
            from_date=HOURLY_WINDOW[0].isoformat(), to_date=HOURLY_WINDOW[-1].isoformat(),
            timespan="hour", multiplier=1, horizons=[1], **HOURLY_INDICATOR,
        ),
    )

    assert response.status_code == 200, response.text
    assert response.json()["bar_count"] == wanted
    assert response.json()["data_source"]["bar_minutes"] == 60
    old_call = provider_frame(
        vendor, from_date=HOURLY_WINDOW[0], to_date=HOURLY_WINDOW[-1], timespan="hour", indicator_params={"length": 5}
    )
    assert len(old_call.frame) == wanted - len(HOURLY_WINDOW), "the old call dropped the 09:00 bar of every session"


@pytest.mark.asyncio
async def test_a_daily_request_is_refused_with_a_reason_not_answered_with_zero_bars(api, world):
    """The old call dropped every daily bar (they start at midnight, before the open) and failed on 'no data'."""
    mock_vendor("SPY", tape(WINDOW))

    response = await post(api, body(timespan="day", multiplier=1))

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "resolution_unsupported" and "daily rollup" in detail["message"]
    assert detail["coverage"] is None and detail["data_source"] is None
    assert world[0].rows == {}, "refused before the lake was touched"


# ---------------------------------------------------------------------------
# T05: a failed admission refuses, with the counts and reasons; nothing is computed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_window_the_lake_cannot_admit_is_refused_with_the_session_and_the_counts(api, world):
    vendor = tape(WINDOW)
    missing_day = WINDOW[4]
    del vendor[missing_day]
    mock_vendor("SPY", vendor)

    response = await post(api, body())

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "data_unavailable_from_provider" and missing_day.isoformat() in detail["message"]
    assert [(s["group"], s["outcome"]) for s in detail["sessions"]] == [("window", "unavailable_from_provider")]
    assert detail["sessions_total"] == 1
    window = detail["coverage"]["window"]
    assert (window["expected_sessions"], window["added"], window["failed"]) == (len(WINDOW), len(WINDOW) - 1, 1)
    assert detail["data_source"]["snapshot_digest"] is None and detail["data_source"]["identity_digest"] is not None


@pytest.mark.asyncio
async def test_a_lake_that_cannot_be_asked_is_unavailable_not_a_server_error(api, world, monkeypatch):
    def catalog_down(spec, **_):
        raise CatalogUnavailableError("down")

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", catalog_down)

    response = await post(api, body())

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "lake_unavailable"


@pytest.mark.asyncio
async def test_a_window_ending_on_a_session_that_has_not_finished_is_refused(api, world, monkeypatch):
    mid_session = int(datetime(2024, 3, 15, 12, 0, tzinfo=ET).timestamp() * 1000)
    monkeypatch.setattr(indicator_reliability_service, "now_ms_utc", lambda: mid_session)
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: mid_session)
    mock_vendor("SPY", tape(WINDOW))

    response = await post(api, body())

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "window_not_complete"


@pytest.mark.asyncio
async def test_require_full_refuses_a_lead_in_the_provider_floor_cut_short_and_the_default_reports_it(api, world, monkeypatch):
    window = expected_sessions(date(2024, 3, 11), date(2024, 3, 15))
    now = et_midnight_ms(date(2029, 3, 6))  # the provider's history begins on Friday Mar 8
    monkeypatch.setattr(indicator_reliability_service, "now_ms_utc", lambda: now)
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: now)
    mock_vendor("SPY", vendor_history(expected_sessions(date(2024, 3, 8), window[-1]), seed=11, start_price=150.0, **DENSE))
    payload = body(
        from_date=window[0].isoformat(), to_date=window[-1].isoformat(), indicator_name="ema", indicator_params={"length": 200},
        horizons=[1],
    )

    allowed = await post(api, payload)
    required = await post(api, {**payload, "warmup_policy": "require_full"})

    assert allowed.status_code == 200, allowed.text
    assert allowed.json()["data_source"]["warmup"] == {
        "policy": "allow_shortened", "requested_bars": 1000, "requested_sessions": 1, "admitted_sessions": 1,
        "admitted_bars": 390, "shortened": True, "shortfall_bars": 610, "cause": "provider_history_floor",
    }
    assert required.status_code == 409
    assert required.json()["detail"]["code"] == "warmup_shortened"
    assert required.json()["detail"]["data_source"]["warmup"]["policy"] == "require_full"


@pytest.mark.asyncio
async def test_an_unknown_warmup_policy_is_a_validation_error(api, world):
    response = await post(api, body(warmup_policy="whatever"))

    assert response.status_code == 422
    assert any("warmup_policy" in str(err["loc"]) for err in response.json()["detail"])


# ---------------------------------------------------------------------------
# The guards that were always there
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_largest_horizon_is_still_a_hundred_bars(api, world):
    response = await post(api, body(horizons=[1, 101]))

    assert response.status_code == 400 and response.json()["detail"] == "Maximum horizon is 100 bars"


def regular_bars(vendor: dict[date, list[VendorBar]], *days: date) -> int:
    return sum(1 for day in days for b in vendor[day] if session_open_ms_utc(day) <= b["t"] < session_close_ms_utc(day))


@pytest.mark.asyncio
async def test_too_few_bars_for_the_lookback_and_the_horizon_is_still_insufficient_data(api, world):
    vendor = tape(WINDOW[:1], regular_keep=0.1)
    mock_vendor("SPY", vendor)

    response = await post(
        api, body(to_date=WINDOW[0].isoformat(), indicator_name="ema", indicator_params={"length": 200}, horizons=[100])
    )

    got = regular_bars(vendor, *LEAD, WINDOW[0])  # EMA(200) warms up on three sessions
    assert response.status_code == 400
    assert response.json()["detail"] == f"Insufficient data: got {got} bars, need at least 300"


@pytest.mark.asyncio
async def test_a_short_window_is_still_insufficient_after_the_warmup_is_trimmed(api, world):
    vendor = tape(WINDOW[:1], regular_keep=0.2)
    mock_vendor("SPY", vendor)

    response = await post(api, body(to_date=WINDOW[0].isoformat(), horizons=[1]))

    assert response.status_code == 400
    assert response.json()["detail"] == f"Insufficient data after warmup trim: {regular_bars(vendor, WINDOW[0])} bars"


# ---------------------------------------------------------------------------
# T09 at the route: a repaired lake is a new identity for the same request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repairing_a_session_changes_the_snapshot_and_result_identity_but_not_the_request_identity(api, world):
    catalog, lake = world
    vendor = tape(WINDOW)
    mock_vendor("SPY", vendor)
    before = (await post(api, body())).json()
    commit_bars(catalog, lake, WINDOW[3], corrected_day(WINDOW[3], vendor[WINDOW[3]]), mode="polygon_split_adjusted")

    after = (await post(api, body())).json()
    repeat = (await post(api, body())).json()

    b, a, r = before["data_source"], after["data_source"], repeat["data_source"]
    assert a["identity_digest"] == b["identity_digest"]
    assert a["snapshot_digest"] != b["snapshot_digest"] and a["result_identity_digest"] != b["result_identity_digest"]
    assert (r["snapshot_digest"], r["result_identity_digest"]) == (a["snapshot_digest"], a["result_identity_digest"])
    assert a["coverage"]["window"]["reused"] == len(WINDOW) and a["coverage"]["window"]["added"] == 0
