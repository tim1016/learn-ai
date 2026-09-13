"""``POST /api/dataset/plan`` — fetch-free planning receipt contract.

The plan endpoint resolves date intent through the canonical NYSE
calendar (never ``T23:59:59``, never a hard-coded 390-minute session),
projects output columns through the same function the ZIP generation
path uses, and types bar counts as arithmetic estimates with explicit
assumptions and provenance (data-lab workspace redesign PRD §12/§18).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pandas as pd
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.models.requests import DatasetPlanRequest
from app.routers import dataset as dataset_router
from app.services.dataset_plan_service import build_dataset_plan
from app.services.dataset_service import (
    calculate_dynamic_indicators,
    project_output_columns,
)

_ET = ZoneInfo("America/New_York")

_RECIPE: dict[str, Any] = {
    "ticker": "SPY",
    "from_date": "2025-11-26",
    "to_date": "2025-12-01",
    "indicator_entries": [
        {"name": "ema", "params": {"length": 20}},
        {"name": "rsi", "params": {"length": 14}},
        {"name": "bbands", "params": {"length": 20}},
        {"name": "macd", "params": {}},
    ],
}


def _open_ms(year: int, month: int, day: int, hour: int, minute: int) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=_ET).timestamp() * 1000)


@pytest.fixture
def api() -> FastAPI:
    app = FastAPI()
    app.include_router(dataset_router.router, prefix="/api/dataset")
    return app


async def _post_plan(api: FastAPI, payload: dict[str, Any]) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        return await client.post("/api/dataset/plan", json=payload)


# ── Schema and boundary validation ──────────────────────────────


@pytest.mark.asyncio
async def test_plan_missing_ticker_is_422(api: FastAPI) -> None:
    payload = {**_RECIPE}
    del payload["ticker"]
    response = await _post_plan(api, payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_plan_empty_ticker_is_422(api: FastAPI) -> None:
    response = await _post_plan(api, {**_RECIPE, "ticker": ""})
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "from_date,to_date",
    [("2025-12-01", "2025-12-01"), ("2025-12-02", "2025-12-01")],
)
async def test_plan_inverted_or_empty_date_window_is_422(
    api: FastAPI, from_date: str, to_date: str
) -> None:
    response = await _post_plan(api, {**_RECIPE, "from_date": from_date, "to_date": to_date})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_plan_malformed_date_string_is_422(api: FastAPI) -> None:
    response = await _post_plan(api, {**_RECIPE, "from_date": "Nov 26 2025"})
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_plan_inverted_numeric_window_is_422(api: FastAPI) -> None:
    response = await _post_plan(
        api,
        {**_RECIPE, "start_ms_utc": 1_764_000_000_000, "end_ms_utc": 1_763_000_000_000},
    )
    assert response.status_code == 422


# ── Session window resolution ───────────────────────────────────


@pytest.mark.asyncio
async def test_plan_window_spans_thanksgiving_weekend_and_holiday(api: FastAPI) -> None:
    """2025-11-27 is Thanksgiving (NYSE closed), 11-29/30 a weekend.

    Sessions inside [11-26, 12-01]: 11-26, 11-28, 12-01 → 3. The
    half-open window runs 11-26 09:30 ET → 12-02 09:30 ET (the session
    open AFTER the last requested date), never T23:59:59.
    """
    response = await _post_plan(api, _RECIPE)
    assert response.status_code == 200
    body = response.json()

    assert body["window_start_ms_utc"] == _open_ms(2025, 11, 26, 9, 30)
    assert body["window_end_ms_utc"] == _open_ms(2025, 12, 2, 9, 30)
    assert body["window_end_ms_utc"] > body["window_start_ms_utc"]
    assert body["exchange_sessions"] == ["2025-11-26", "2025-11-28", "2025-12-01"]
    assert body["session_count"] == 3
    assert body["exchange"] == "NYSE"
    assert body["calendar_timezone"] == "America/New_York"
    assert body["calendar_version"]


@pytest.mark.asyncio
async def test_plan_window_respects_dst_boundary(api: FastAPI) -> None:
    """US DST starts 2026-03-08: 03-06 opens 09:30 EST (14:30 UTC),
    03-10 opens 09:30 EDT (13:30 UTC). A fixed-offset implementation
    would be off by exactly one hour on one side."""
    payload = {**_RECIPE, "from_date": "2026-03-06", "to_date": "2026-03-09"}
    response = await _post_plan(api, payload)
    assert response.status_code == 200
    body = response.json()

    assert body["window_start_ms_utc"] == _open_ms(2026, 3, 6, 9, 30)
    assert body["window_end_ms_utc"] == _open_ms(2026, 3, 10, 9, 30)
    # Zone-aware proof: the EST-side open lands at 14:30 UTC, the EDT-side
    # open at 13:30 UTC. A fixed-offset implementation would put both at
    # the same UTC wall-clock.
    assert body["window_start_ms_utc"] % 86_400_000 == (14 * 60 + 30) * 60_000
    assert body["window_end_ms_utc"] % 86_400_000 == (13 * 60 + 30) * 60_000


@pytest.mark.asyncio
async def test_plan_numeric_window_overrides_date_strings(api: FastAPI) -> None:
    start = _open_ms(2025, 11, 26, 10, 0)
    end = _open_ms(2025, 11, 28, 15, 0)
    response = await _post_plan(
        api, {**_RECIPE, "start_ms_utc": start, "end_ms_utc": end}
    )
    assert response.status_code == 200
    body = response.json()

    assert body["window_start_ms_utc"] == start
    assert body["window_end_ms_utc"] == end
    # Session enumeration follows the numeric window (11-26 10:00 → 11-28
    # 15:00 ET), not the date strings: 11-27 (Thanksgiving) stays closed.
    assert body["exchange_sessions"] == ["2025-11-26", "2025-11-28"]
    assert body["session_count"] == 2


# ── Column projection parity with the ZIP path ──────────────────


def test_plan_columns_equal_shared_projection_for_representative_recipe() -> None:
    """The receipt's column list must be exactly ``project_output_columns``
    on a frame carrying the same columns — the same function
    ``_build_zip_with_events`` uses for dataset.csv/columns.csv, so plan
    and ZIP parity is structural, not coincidental."""
    plan = build_dataset_plan(DatasetPlanRequest(**_RECIPE))

    # Rebuild the projection frame independently of the service's helper:
    # every column a processed minute-aggregate frame would carry.
    n = 300
    close = pd.Series(100.0 + (pd.Series(range(n)) % 7) * 0.25)
    frame = pd.DataFrame(
        {
            "timestamp": pd.Series(range(n), dtype="int64") * 60_000,
            "open": close - 0.1,
            "high": close + 0.5,
            "low": close - 0.5,
            "close": close,
            "volume": pd.Series([1_000] * n, dtype="int64"),
            "vwap": close,
            "transactions": pd.Series([10] * n, dtype="int64"),
            "session": "rth",
            "PC": close.shift(1).bfill(),
        }
    )
    _, column_meta = calculate_dynamic_indicators(
        frame, _RECIPE["indicator_entries"]
    )
    expected = project_output_columns(frame, column_meta)

    assert plan.output_columns == expected
    assert plan.output_column_count == len(expected)
    # Canonical ordering: PC before open, OHLCV before extras, indicators last.
    assert plan.output_columns[0] == "PC"
    assert plan.output_columns[1:6] == ["open", "high", "low", "close", "volume"]
    assert "macd_12_26_9" in plan.output_columns


# ── Estimate typing, assumptions, provenance ─────────────────────


@pytest.mark.asyncio
async def test_plan_estimate_is_typed_with_assumptions_and_provenance(api: FastAPI) -> None:
    response = await _post_plan(api, _RECIPE)
    assert response.status_code == 200
    body = response.json()

    assert body["estimated_bars"] >= 0
    assert isinstance(body["estimate_assumptions"], list)
    assert body["estimate_assumptions"], "estimate must ship non-empty assumptions"
    assert all(isinstance(a, str) and a for a in body["estimate_assumptions"])
    assert "pandas_market_calendars" in body["estimate_provenance"]
    assert "no network calls" in body["estimate_provenance"]
    # Scheduled RTH minutes of 1-minute bars: 11-26 is a full 390-minute
    # session, 11-28 is the post-Thanksgiving half day (13:00 ET close →
    # 210 minutes), 12-01 is full again. The estimate uses each session's
    # real scheduled span — never a hard-coded 390.
    assert body["estimated_bars"] == 390 + 210 + 390


@pytest.mark.asyncio
async def test_plan_daily_timespan_estimate_counts_sessions(api: FastAPI) -> None:
    response = await _post_plan(
        api, {**_RECIPE, "timespan": "day", "indicator_entries": []}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["estimated_bars"] == body["session_count"] == 3


@pytest.mark.asyncio
async def test_plan_companions_and_warnings_surface(api: FastAPI) -> None:
    payload = {
        **_RECIPE,
        "include_trades": True,
        "options_companion": {"enabled": True, "strikes_each_side": 2},
    }
    response = await _post_plan(api, payload)
    assert response.status_code == 200
    body = response.json()

    assert "trades" in body["companion_dependencies"]
    assert "options_companion" in body["companion_dependencies"]
    assert any("TICK-LEVEL" in w for w in body["warnings"])
    assert any("options_companion" in w for w in body["warnings"])


@pytest.mark.asyncio
async def test_plan_timeframe_advice_present(api: FastAPI) -> None:
    response = await _post_plan(api, _RECIPE)
    assert response.status_code == 200
    body = response.json()
    assert body["allowed_timeframes"]
    assert body["recommended_timeframes"]
    assert set(body["recommended_timeframes"]) <= set(body["allowed_timeframes"])


# ── Review follow-ups (PR #2043) ─────────────────────────────────


@pytest.mark.asyncio
async def test_plan_daily_columns_include_vwap_and_transactions(api: FastAPI) -> None:
    """Polygon grouped-daily aggregates return o/h/l/c/v/vw/n for every
    timespan — the synthetic preview frame must carry vwap/transactions
    for hour/day/week/… recipes too, or the receipt would claim columns
    the ZIP path actually emits are absent."""
    response = await _post_plan(api, {**_RECIPE, "timespan": "day", "indicator_entries": []})
    assert response.status_code == 200
    body = response.json()
    for col in ("open", "high", "low", "close", "volume", "vwap", "transactions", "session"):
        assert col in body["output_columns"], f"{col} missing from daily plan columns"


@pytest.mark.asyncio
async def test_plan_timeframe_advice_uses_resolved_numeric_window(api: FastAPI) -> None:
    """Advice must be derived from the resolved half-open window, not the
    raw date strings: numeric overrides take precedence."""
    from app.services.chart_service import get_allowed_timeframes

    start = _open_ms(2025, 11, 26, 9, 30)
    end = _open_ms(2025, 11, 27, 9, 30)  # one day only (11-27 is Thanksgiving)
    response = await _post_plan(
        api,
        {**_RECIPE, "start_ms_utc": start, "end_ms_utc": end},
    )
    assert response.status_code == 200
    body = response.json()

    expected_allowed, _est, expected_recommended = get_allowed_timeframes(
        "2025-11-26", "2025-11-26", "extended"
    )
    assert body["allowed_timeframes"] == expected_allowed
    assert body["recommended_timeframes"] == [expected_recommended]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["start_ms_utc", "end_ms_utc"])
async def test_plan_out_of_range_ms_window_is_422(api: FastAPI, field: str) -> None:
    """Values beyond MAX_TIMESTAMP_MS must fail validation (422), not 500."""
    from app.utils.session_anchors import MAX_TIMESTAMP_MS

    response = await _post_plan(api, {**_RECIPE, field: MAX_TIMESTAMP_MS + 1})
    assert response.status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["start_ms_utc", "end_ms_utc"])
async def test_generation_out_of_range_ms_window_is_422(api: FastAPI, field: str) -> None:
    from app.utils.session_anchors import MAX_TIMESTAMP_MS

    async with httpx.AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.post(
            "/api/dataset/generate-csv",
            json={**_RECIPE, field: MAX_TIMESTAMP_MS + 1},
        )
    assert response.status_code == 422


def test_generation_numeric_window_overrides_fetch_dates() -> None:
    """Numeric bounds take per-field precedence and convert to the inclusive
    fetch span; a half-open end on the session open of day X excludes day X."""
    from app.models.requests import DatasetGenerationRequest
    from app.services.dataset_plan_service import resolve_generation_window

    base = {**_RECIPE, "indicator_entries": []}
    request = DatasetGenerationRequest(
        **base,
        start_ms_utc=_open_ms(2025, 11, 26, 10, 0),
        end_ms_utc=_open_ms(2025, 12, 2, 9, 30),
    )
    resolved = resolve_generation_window(request)
    assert resolved.from_date == "2025-11-26"
    assert resolved.to_date == "2025-12-01"  # 12-02 session open is EXCLUSIVE


def test_generation_numeric_window_partial_override_and_passthrough() -> None:
    from app.models.requests import DatasetGenerationRequest
    from app.services.dataset_plan_service import resolve_generation_window

    base = {**_RECIPE, "indicator_entries": []}
    # Start-only override wins per-field; to_date is untouched.
    request = DatasetGenerationRequest(**base, start_ms_utc=_open_ms(2025, 11, 26, 10, 0))
    resolved = resolve_generation_window(request)
    assert resolved.from_date == "2025-11-26"
    assert resolved.to_date == "2025-12-01"

    # No numeric bounds → request returned unchanged (dates preserved).
    passthrough = DatasetGenerationRequest(**base)
    assert resolve_generation_window(passthrough) is passthrough


@pytest.mark.asyncio
async def test_plan_include_quality_report_adds_warning_not_bars(api: FastAPI) -> None:
    without = await _post_plan(api, {**_RECIPE, "indicator_entries": []})
    with_report = await _post_plan(
        api, {**_RECIPE, "indicator_entries": [], "include_quality_report": True}
    )
    assert without.status_code == 200 and with_report.status_code == 200
    a, b = without.json(), with_report.json()
    assert b["estimated_bars"] == a["estimated_bars"]
    assert not any("quality" in w for w in a["warnings"])
    assert any("quality_report.md" in w for w in b["warnings"])


@pytest.mark.asyncio
async def test_plan_sessions_expose_ms_open_anchors(api: FastAPI) -> None:
    response = await _post_plan(api, _RECIPE)
    assert response.status_code == 200
    body = response.json()
    anchors = body["exchange_session_opens_ms_utc"]
    assert anchors == [
        _open_ms(2025, 11, 26, 9, 30),
        _open_ms(2025, 11, 28, 9, 30),
        _open_ms(2025, 12, 1, 9, 30),
    ]
    assert len(anchors) == body["session_count"] == len(body["exchange_sessions"])


def test_empty_column_meta_preserves_base_metadata_columns() -> None:
    """Regression: exports with NO indicators must still describe every
    base column (OHLCV, PC, vwap, transactions, session) in the metadata
    and columns.csv — the base-column slice must not truncate when
    column_meta is empty."""
    from app.routers.dataset import _projection_without_indicators
    from app.services.dataset_service import build_metadata_csv, project_output_columns

    n = 5
    close = pd.Series([100.0] * n)
    frame = pd.DataFrame(
        {
            "timestamp": pd.Series(range(n), dtype="int64") * 60_000,
            "open": close,
            "high": close,
            "low": close,
            "close": close,
            "volume": pd.Series([1] * n, dtype="int64"),
            "vwap": close,
            "transactions": pd.Series([1] * n, dtype="int64"),
            "session": "rth",
            "PC": close,
        }
    )
    column_meta: list[dict[str, Any]] = []
    full = project_output_columns(frame, column_meta)
    ohlcv_cols = _projection_without_indicators(frame, column_meta)
    assert ohlcv_cols == full
    assert {
        "PC",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "vwap",
        "transactions",
        "session",
    } <= set(ohlcv_cols)

    csv_text = build_metadata_csv(column_meta, ohlcv_cols).decode("utf-8")
    assert csv_text.splitlines()[0].startswith("column,")
    for col in ohlcv_cols:
        assert any(line.split(",")[0] == col for line in csv_text.splitlines()[1:]), (
            f"{col} missing from columns.csv rows:\n{csv_text}"
        )

