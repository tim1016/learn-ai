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
from app.research.artifact.root import default_artifacts_root
from app.research.study_inputs import lead_in_sessions
from app.research.study_records.store import StudyRecordStore
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
    monkeypatch.setattr(indicator_reliability_service, "now_ms_utc", lambda: NOW_MS)
    monkeypatch.setattr(dataset_service, "now_ms_utc", lambda: NOW_MS)  # the old provider path, rebuilt for comparison
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
        "building": 0, "corrupt": 0, "captured_before_final": 0, "unavailable_from_provider": 0,
    }
    assert source["coverage"]["warmup"]["expected_sessions"] == 1 and source["coverage"]["warmup"]["added"] == 1
    assert source["warmup"] == {
        "policy": "allow_shortened", "requested_bars": 140, "requested_sessions": 1, "admitted_sessions": 1,
        "admitted_bars": 390, "shortened": False, "shortfall_bars": 0, "cause": "none", "notice": None,
    }
    assert source["bars_used"] == result["bar_count"] and source["minute_bars_read"] == result["bar_count"] + 390
    assert len(source["snapshot_digest"]) == 64 and len(source["identity_digest"]) == 64
    assert len(source["result_identity_digest"]) == 64
    assert source["window_start_ms"] == et_midnight_ms(WINDOW[0]) and source["window_end_ms"] == et_midnight_ms(WINDOW[-1])


@pytest.mark.asyncio
async def test_a_session_the_study_cannot_compute_on_is_refused_not_answered_with_regular_hours(api, world):
    """The study has only ever computed on regular hours (#3523); ``extended`` used to be accepted and ignored."""
    mock_vendor("SPY", tape(WINDOW))

    regular = await post(api, body(session="rth"))
    extended = await post(api, body(session="extended"))

    assert regular.status_code == 200 and regular.json()["data_source"]["session_policy"] == "regular"
    assert extended.status_code == 422
    assert any("session" in str(err["loc"]) for err in extended.json()["detail"])


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

    assert response.status_code == 409
    refused = response.json()
    assert (refused["success"], refused["status"], refused["ticker"]) == (False, "refused", "SPY")
    assert refused["refusal"]["code"] == "resolution_unsupported" and "daily rollup" in refused["refusal"]["reason"]
    assert refused["error"] == refused["refusal"]["reason"]
    assert refused["refusal"]["coverage"] is None and refused["data_source"] is None
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
    refusal = response.json()["refusal"]
    assert refusal["code"] == "data_unavailable_from_provider" and missing_day.isoformat() in refusal["reason"]
    assert [(s["group"], s["outcome"], s["failure_reason"]) for s in refusal["sessions"]] == [
        ("window", "unavailable_from_provider", "provider_no_data")
    ]
    assert refusal["sessions_total"] == 1
    window = refusal["coverage"]["window"]
    assert (window["expected_sessions"], window["added"], window["failed"]) == (len(WINDOW), len(WINDOW) - 1, 1)
    source = response.json()["data_source"]
    assert source["snapshot_digest"] is None and source["identity_digest"] is not None
    assert source["coverage"] == refusal["coverage"], "the source line shows the counts admission reached"


@pytest.mark.asyncio
async def test_a_lake_that_cannot_be_asked_is_unavailable_not_a_server_error(api, world, monkeypatch):
    def catalog_down(spec, **_):
        raise CatalogUnavailableError("down")

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", catalog_down)

    response = await post(api, body())

    assert response.status_code == 503
    assert response.json()["refusal"]["code"] == "lake_unavailable"


@pytest.mark.asyncio
async def test_a_window_ending_on_a_session_that_has_not_finished_is_refused(api, world, monkeypatch):
    mid_session = int(datetime(2024, 3, 15, 12, 0, tzinfo=ET).timestamp() * 1000)
    monkeypatch.setattr(indicator_reliability_service, "now_ms_utc", lambda: mid_session)
    mock_vendor("SPY", tape(WINDOW))

    response = await post(api, body())

    assert response.status_code == 409
    assert response.json()["refusal"]["code"] == "window_not_complete"


@pytest.mark.asyncio
async def test_require_full_refuses_a_lead_in_the_provider_floor_cut_short_and_the_default_reports_it(api, world, monkeypatch):
    window = expected_sessions(date(2024, 3, 11), date(2024, 3, 15))
    now = et_midnight_ms(date(2029, 3, 6))  # the provider's history begins on Friday Mar 8
    monkeypatch.setattr(indicator_reliability_service, "now_ms_utc", lambda: now)
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
        "notice": allowed.json()["data_source"]["warmup"]["notice"],
    }
    assert "610 bars short of the 1000" in allowed.json()["data_source"]["warmup"]["notice"]
    assert required.status_code == 409
    assert required.json()["refusal"]["code"] == "warmup_shortened"
    assert required.json()["data_source"]["warmup"]["policy"] == "require_full"


@pytest.mark.asyncio
async def test_an_unknown_warmup_policy_is_a_validation_error(api, world):
    response = await post(api, body(warmup_policy="whatever"))

    assert response.status_code == 422
    assert any("warmup_policy" in str(err["loc"]) for err in response.json()["detail"])


@pytest.mark.asyncio
async def test_a_refusal_never_shares_a_status_or_a_body_shape_with_fastapis_validation_error(api, world):
    """A request that fails validation is 422 with ``detail`` a list; a refusal is 409 or 503 with none (#3523)."""
    mock_vendor("SPY", tape(WINDOW))

    refusal = await post(api, body(timespan="day"))
    invalid = await post(api, body(from_date="not-a-date"))

    assert refusal.status_code == 409 and "detail" not in refusal.json()
    assert invalid.status_code == 422 and isinstance(invalid.json()["detail"], list)


def test_the_refusals_are_documented_with_their_body_and_422_stays_fastapis(api):
    responses = api.openapi()["paths"]["/api/research/indicator-reliability"]["post"]["responses"]

    refusal_body = {"$ref": "#/components/schemas/ReliabilityRefusedResult"}
    assert responses["409"]["content"]["application/json"]["schema"] == refusal_body
    assert responses["503"]["content"]["application/json"]["schema"] == refusal_body
    assert responses["422"]["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/HTTPValidationError"}


# ---------------------------------------------------------------------------
# The lead-in through the route (#3523): an empty vendor answer shortens it, a vendor failure refuses
# ---------------------------------------------------------------------------

EMA_200 = {"indicator_name": "ema", "indicator_params": {"length": 200}, "horizons": [1]}  # three lead-in sessions


@pytest.mark.asyncio
async def test_a_symbol_that_lists_inside_the_lead_in_computes_on_what_it_has_and_keeps_the_dropped_session_in_the_counts(api, world):
    vendor = tape(WINDOW)
    del vendor[LEAD[0]]  # the vendor answers with no bars for the oldest lead-in session
    mock_vendor("SPY", vendor)

    response = await post(api, body(**EMA_200))

    assert response.status_code == 200, response.text
    source = response.json()["data_source"]
    assert source["warmup"]["cause"] == "symbol_history_starts"
    assert (source["warmup"]["requested_sessions"], source["warmup"]["admitted_sessions"]) == (3, 2)
    assert source["warmup_sessions"] == 2, "the identity is the lead-in that was read"
    warmup = source["coverage"]["warmup"]
    assert (warmup["expected_sessions"], warmup["added"], warmup["unavailable_from_provider"], warmup["failed"]) == (3, 2, 1, 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["duplicate", "out_of_order"])
async def test_a_tape_the_lake_rejects_on_the_oldest_lead_in_session_is_a_refusal_not_a_shorter_warmup(api, world, fault):
    vendor = tape(WINDOW)
    bars = vendor[LEAD[0]]
    vendor[LEAD[0]] = [*bars[:5], bars[4], *bars[5:]] if fault == "duplicate" else [*bars[:5], bars[6], bars[5], *bars[7:]]
    mock_vendor("SPY", vendor)

    response = await post(api, body(**EMA_200))

    assert response.status_code == 409
    refused = response.json()
    assert refused["refusal"]["code"] == "data_unavailable_from_provider"
    assert [(s["group"], s["failure_reason"]) for s in refused["refusal"]["sessions"]] == [("warmup", "validation_failed")]
    assert refused["data_source"]["coverage"]["warmup"]["unavailable_from_provider"] == 1
    assert refused["data_source"]["warmup"] is None


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


# ---------------------------------------------------------------------------
# The History record: a lake run is kept with the data source it read, a refused run with its refusal
# ---------------------------------------------------------------------------


def saved_records() -> list[Any]:
    """The runs the route kept, read from the temporary artifact root the test conftest points the default root at."""
    store = StudyRecordStore("indicator-reliability", root=default_artifacts_root())
    return [store.load(record_id) for record_id, _ in store.config_paths()]


@pytest.mark.asyncio
async def test_a_completed_lake_run_is_recorded_with_the_data_source_block_it_returned(api, world):
    mock_vendor("SPY", tape(WINDOW))

    response = await post(api, body())

    assert response.status_code == 200, response.text
    ((config, stored),) = saved_records()
    assert config.status == "completed"
    assert config.inputs["indicator_name"] == "rsi" and config.inputs["horizons"] == [1, 5]
    assert config.inputs["session"] == "rth" and config.inputs["warmup_policy"] == "allow_shortened"
    assert config.data_source == response.json()["data_source"], "the lake block is the record's provenance, field for field"
    assert config.data_source["kind"] == "lake" and len(config.data_source["snapshot_digest"]) == 64
    assert config.headline, "a completed run is listed under a headline made from its result"
    assert stored.result is not None
    assert stored.result["best_horizon"] == response.json()["best_horizon"]
    assert [row["horizon"] for row in stored.result["results"]] == [1, 5]


@pytest.mark.asyncio
async def test_a_window_the_lake_refused_is_recorded_as_a_failed_run_with_its_refusal_and_counts(api, world):
    vendor = tape(WINDOW)
    del vendor[WINDOW[4]]
    mock_vendor("SPY", vendor)

    response = await post(api, body())

    parked = response.json()
    assert response.status_code == 409
    ((config, stored),) = saved_records()
    assert config.status == "failed" and config.status_detail == parked["error"]
    assert config.headline is None
    assert stored.result == parked, "the record holds the refusal the page reads, as the route answered it"
    assert stored.result["refusal"]["code"] == "data_unavailable_from_provider"
    assert config.data_source == parked["data_source"]
    assert config.data_source["coverage"]["window"]["failed"] == 1 and config.data_source["snapshot_digest"] is None


@pytest.mark.asyncio
async def test_a_lake_that_cannot_be_asked_is_recorded_as_a_failed_run_with_its_refusal(api, world, monkeypatch):
    def catalog_down(spec, **_):
        raise CatalogUnavailableError("down")

    monkeypatch.setattr(run_materialization, "_materialize_run_data_sync", catalog_down)

    response = await post(api, body())

    assert response.status_code == 503
    ((config, stored),) = saved_records()
    assert config.status == "failed" and stored.result["refusal"]["code"] == "lake_unavailable"


@pytest.mark.asyncio
async def test_a_run_that_finds_too_little_data_is_recorded_as_failed(api, world):
    mock_vendor("SPY", tape(WINDOW[:1], regular_keep=0.2))

    response = await post(api, body(to_date=WINDOW[0].isoformat(), horizons=[1]))

    assert response.status_code == 400
    ((config, stored),) = saved_records()
    assert config.status == "failed" and (config.status_detail or "").startswith("Insufficient data after warmup trim")
    assert stored.result is None, "the route raised; there is no refusal payload to keep"


@pytest.mark.asyncio
async def test_empty_horizons_are_recorded_as_the_defaults_the_run_used(api, world):
    """The handler fills in the default horizons itself; the record says what ran, not what was posted."""
    mock_vendor("SPY", tape(WINDOW[:1], regular_keep=0.2))

    await post(api, body(to_date=WINDOW[0].isoformat(), horizons=[]))

    ((config, _),) = saved_records()
    assert config.inputs["horizons"] == [1, 5, 10, 15, 30]
