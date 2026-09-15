"""`/api/research/return-distribution` router contract.

The endpoint reads the lake only (no provider fallback), so every test
seeds a byte-accurate lake in tmp_path through the canonical zip writer
and pins the service's root resolution to it. The window travels as int64
ms UTC (UTC-midnight start / final-instant end, the Data Lab convention)
and Python resolves the calendar dates. Assertions cover the happy path,
the two typed coverage errors, the request-validation surface, and the
capture-on-demand boundary (probe → receipt → typed errors).
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.data_lake.path_policy import LeanFactorFilePath
from app.engine.data.lean_format import write_lean_day_zip
from app.engine.data.trade_bar import TradeBar
from app.lean_sidecar.trading_calendar import expected_sessions
from app.routers import return_distribution as return_distribution_router
from app.services import return_distribution_service

ET = ZoneInfo("America/New_York")
N_SESSIONS = 40
SYMBOL = "SPY"
FROM_MS = int(datetime(2024, 7, 1, tzinfo=UTC).timestamp() * 1000)
TO_MS = int(datetime(2025, 6, 30, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000)


def _et_ms(d: date, h: int, m: int) -> int:
    return int(datetime(d.year, d.month, d.day, h, m, tzinfo=ET).timestamp() * 1000)


def _utc_day_end_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, 23, 59, 59, 999000, tzinfo=UTC).timestamp() * 1000)


def _bar(d: date, h: int, m: int, open_: float, close: float) -> TradeBar:
    o, c = Decimal(str(open_)), Decimal(str(close))
    return TradeBar(
        symbol=SYMBOL,
        open=o,
        high=max(o, c),
        low=min(o, c),
        close=c,
        volume=100,
        start_ms=_et_ms(d, h, m),
        end_ms=_et_ms(d, h, m) + 60_000,
    )


def _seed_lake(root: Path) -> list[date]:
    """40 real sessions of extended-hours bars plus one dividend factor row."""
    sessions = expected_sessions(date(2024, 7, 1), date(2025, 6, 30))[:N_SESSIONS]
    rng = np.random.default_rng(20260914)
    price = 100.0
    for i, d in enumerate(sessions):
        bars: list[TradeBar] = []
        pre_open = price * (1.0 + float(rng.normal(0.0, 0.003)))
        if i % 2 == 0:
            bars.append(_bar(d, 8, 0, pre_open, pre_open * 1.001))
        open_ = pre_open * (1.0 + float(rng.normal(0.0, 0.003)))
        noon = open_ * (1.0 + float(rng.normal(0.0, 0.006)))
        close = noon * (1.0 + float(rng.normal(0.0, 0.006)))
        bars.extend(
            [
                _bar(d, 9, 30, open_, open_ * 1.0005),
                _bar(d, 11, 59, noon * 0.9995, noon),
                _bar(d, 12, 0, noon, noon * 1.0005),
                _bar(d, 15, 59, close * 0.9995, close),
            ]
        )
        if i % 3 == 0:
            bars.append(_bar(d, 16, 30, close, close * 1.0005))
        write_lean_day_zip(root, SYMBOL, d, bars)
        price = close

    factor_rel = LeanFactorFilePath(market="usa", symbol=SYMBOL).relative_path()
    factor_path = root.joinpath(*factor_rel.parts)
    factor_path.parent.mkdir(parents=True, exist_ok=True)
    # A dividend turning over mid-window (row dated session 5 covers data
    # through it; later data uses the terminal factor-1 row).
    factor_path.write_text(
        f"{sessions[5].strftime('%Y%m%d')},0.99,1,100\n"
        f"{sessions[-1].strftime('%Y%m%d')},1,1,100\n",
        encoding="ascii",
    )
    return sessions


def _seed_flat_lake(root: Path) -> list[date]:
    """40 sessions at an unchanging price — zero variance, defined stats."""
    sessions = expected_sessions(date(2024, 7, 1), date(2025, 6, 30))[:N_SESSIONS]
    for d in sessions:
        write_lean_day_zip(
            root,
            SYMBOL,
            d,
            [
                _bar(d, 9, 30, 100.0, 100.0),
                _bar(d, 11, 59, 100.0, 100.0),
                _bar(d, 12, 0, 100.0, 100.0),
                _bar(d, 15, 59, 100.0, 100.0),
            ],
        )
    return sessions


@pytest.fixture
def seeded_lake(tmp_path: Path) -> Path:
    root = tmp_path / "lake"
    _seed_lake(root)
    return root


def _complete_capture_receipt() -> return_distribution_service.CaptureReceipt:
    return return_distribution_service.CaptureReceipt(status="complete", fetched_artifact_count=0)


def _app_with(
    root: Path, monkeypatch: pytest.MonkeyPatch, capture=None
) -> FastAPI:
    monkeypatch.setattr(
        return_distribution_service, "resolve_lake_root", lambda _mode: root
    )
    if capture is not None:
        monkeypatch.setattr(return_distribution_service, "_capture_missing_sessions", capture)
    app = FastAPI()
    app.include_router(return_distribution_router.router, prefix="/api/research")
    return app


@pytest.fixture
async def api(seeded_lake: Path, monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    # The real capture boundary needs the catalog + provider; the seeded lake
    # needs capture for its lead-in, so the stub records a completed capture.
    async def _stub_capture(**kwargs: object) -> return_distribution_service.CaptureReceipt:
        return _complete_capture_receipt()

    return _app_with(seeded_lake, monkeypatch, capture=_stub_capture)


def _request_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "symbol": SYMBOL,
        "from_ms_utc": FROM_MS,
        "to_ms_utc": TO_MS,
    }
    body.update(overrides)
    return body


async def _post(api: FastAPI, body: dict[str, object]) -> object:
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        return await client.post("/api/research/return-distribution", json=body)


@pytest.mark.asyncio
async def test_return_distribution_happy_path(api: FastAPI, seeded_lake: Path) -> None:
    response = await _post(api, _request_body())
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["meta"]["symbol"] == SYMBOL
    assert body["meta"]["adjustment"] == "split_and_dividend"
    assert body["meta"]["resolution"] == "1m"
    # The numeric window echoes verbatim — Python resolved the dates, the
    # wire never carried a date string.
    assert body["meta"]["from_ms_utc"] == FROM_MS
    assert body["meta"]["to_ms_utc"] == TO_MS
    # The requested calendar window is wider than the seeded lake (40
    # sessions inside 2024-07-01..2025-06-30), so coverage reports the gap
    # honestly instead of implying the window is complete.
    requested = len(expected_sessions(date(2024, 7, 1), date(2025, 6, 30)))
    assert body["coverage"]["requested_sessions"] == requested
    assert body["coverage"]["missing_sessions"] == requested - N_SESSIONS
    assert any("not captured" in w for w in body["meta"]["warnings"])
    assert not any("unadjusted" in w for w in body["meta"]["warnings"])

    kinds = {k["kind"]: k for k in body["kinds"]}
    assert set(kinds) == {"close_to_close", "session", "overnight"}
    for kind, dist in kinds.items():
        total = sum(b["count"] for b in dist["bins"])
        assert total == dist["stats"]["n_days"], kind
        assert len(dist["normal_expected_counts"]) == len(dist["bins"])
        # Open edge bins are present and last/first.
        assert kinds[kind]["bins"][0]["is_edge"] is True
        assert kinds[kind]["bins"][-1]["is_edge"] is True

    assert kinds["session"]["stats"]["n_days"] == N_SESSIONS
    assert kinds["close_to_close"]["stats"]["n_days"] == N_SESSIONS - 1

    days = body["days"]
    assert len(days) == N_SESSIONS
    assert all(d["session_pct"] is not None for d in days)
    assert days[0]["close_to_close_pct"] is None  # no prior session in the lake
    assert days[1]["close_to_close_pct"] is not None
    # Every day carries its per-kind bin identity — the stamped membership
    # the drill-down selects by, never re-derived client-side.
    assert set(days[0]["bin_indices"]) == {"close_to_close", "session", "overnight"}
    assert days[0]["bin_indices"]["close_to_close"] is None
    assert isinstance(days[0]["bin_indices"]["session"], int)
    bin_indices = [d["bin_indices"]["session"] for d in days]
    for bin_index, bin_model in enumerate(kinds["session"]["bins"]):
        assert bin_indices.count(bin_index) == bin_model["count"]
    # Extended segments alternate in the seed: even sessions have pre-market,
    # every third has after-hours.
    assert any(d["pre_market_pct"] is not None for d in days)
    assert any(d["after_hours_pct"] is not None for d in days)
    assert any(d["pre_market_pct"] is None for d in days)

    assert body["coverage"]["returned_sessions"] == N_SESSIONS
    assert body["coverage"]["first_session_open_ms_utc"] == days[0]["session_open_ms_utc"]
    assert body["meta"]["capture"]["status"] == "complete"


@pytest.mark.asyncio
async def test_return_distribution_inverted_window_rejected(api: FastAPI) -> None:
    response = await _post(api, _request_body(to_ms_utc=FROM_MS - 1))
    assert response.status_code == 422
    assert "to_ms_utc" in response.text


@pytest.mark.asyncio
async def test_return_distribution_constant_series_has_defined_stats(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A flat price series is valid input: zero variance, None standardized
    moments, a zero overlay — never a server error."""
    root = tmp_path / "lake"
    _seed_flat_lake(root)
    app = _app_with(root, monkeypatch, capture=None)
    # The flat lake starts at the window's first date, so the lead-in probe
    # wants capture; a no-op completed receipt keeps the read synchronous.
    async def _noop_capture(**kwargs: object) -> return_distribution_service.CaptureReceipt:
        return _complete_capture_receipt()

    monkeypatch.setattr(return_distribution_service, "_capture_missing_sessions", _noop_capture)

    response = await _post(app, _request_body())
    assert response.status_code == 200, response.text
    body = response.json()
    for kind in body["kinds"]:
        stats = kind["stats"]
        assert stats["std_pct"] == 0.0
        assert stats["annualized_vol_pct"] == 0.0
        assert stats["skewness"] is None
        assert stats["excess_kurtosis"] is None
        assert stats["var_95_pct"] == 0.0
        assert kind["normal_expected_counts"] == [0.0] * len(kind["bins"])


@pytest.mark.asyncio
async def test_return_distribution_skips_capture_when_lead_in_covered(
    seeded_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lake that holds the window *and its lead-in* is not re-captured."""
    sessions = expected_sessions(date(2024, 7, 1), date(2025, 6, 30))[:N_SESSIONS]
    from_ms = int(
        datetime(sessions[10].year, sessions[10].month, sessions[10].day, tzinfo=UTC).timestamp() * 1000
    )
    to_ms = _utc_day_end_ms(sessions[-1])

    calls: list[object] = []

    async def _recording_capture(**kwargs: object) -> return_distribution_service.CaptureReceipt:
        calls.append(kwargs)
        return _complete_capture_receipt()

    app = _app_with(seeded_lake, monkeypatch, capture=_recording_capture)
    response = await _post(app, _request_body(from_ms_utc=from_ms, to_ms_utc=to_ms))
    assert response.status_code == 200, response.text
    assert calls == []
    assert response.json()["meta"]["capture"]["status"] == "not_attempted"


@pytest.mark.asyncio
async def test_return_distribution_probe_captures_the_lead_in_before_the_window(
    seeded_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lake holding the requested dates but not the fortnight before them
    still triggers capture, and the capture span reaches into the lead-in —
    the first session's close-to-close return needs that previous close."""
    sessions = expected_sessions(date(2024, 7, 1), date(2025, 6, 30))[:N_SESSIONS]
    spans: list[tuple[date, date]] = []

    async def _recording_capture(
        *, symbol: str, start: date, end: date
    ) -> return_distribution_service.CaptureReceipt:
        spans.append((start, end))
        return _complete_capture_receipt()

    app = _app_with(seeded_lake, monkeypatch, capture=_recording_capture)
    # Request from the lake's first captured session: the window itself is
    # fully held, only the lead-in is missing.
    response = await _post(app, _request_body())
    assert response.status_code == 200, response.text
    assert len(spans) == 1
    captured_start, captured_end = spans[0]
    assert captured_start < sessions[0], "capture must reach before the window"
    assert sessions[0] <= captured_end <= date(2025, 6, 30)


@pytest.mark.asyncio
async def test_return_distribution_unknown_symbol_is_typed_not_found(api: FastAPI) -> None:
    response = await _post(api, _request_body(symbol="MSFT"))
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "NOT_CAPTURED"
    assert SYMBOL in detail["captured_symbols"]


@pytest.mark.asyncio
async def test_return_distribution_path_unsafe_symbol_is_typed_not_found(api: FastAPI) -> None:
    response = await _post(api, _request_body(symbol="../../etc/passwd"))
    assert response.status_code == 404
    assert response.json()["detail"]["error_code"] == "NOT_CAPTURED"


@pytest.mark.asyncio
async def test_return_distribution_thin_window_is_typed_bad_request(api: FastAPI) -> None:
    response = await _post(
        api, _request_body(to_ms_utc=_utc_day_end_ms(date(2024, 7, 5)))
    )
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert detail["error_code"] == "INSUFFICIENT_COVERAGE"
    # All four scheduled sessions are captured — the window is simply too
    # thin for statistics, which is a different failure from missing data.
    assert detail["available_sessions"] == 4
    assert detail["available_sessions"] < return_distribution_service.MIN_SESSIONS


@pytest.mark.asyncio
async def test_return_distribution_bad_geometry_is_bad_request(api: FastAPI) -> None:
    response = await _post(api, _request_body(span_pct=5.2))
    assert response.status_code == 400
    assert "integer multiple" in response.json()["detail"]


@pytest.mark.asyncio
async def test_return_distribution_rejects_unknown_fields(api: FastAPI) -> None:
    response = await _post(api, _request_body(ticker="SPY"))
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_return_distribution_missing_factor_file_warns_unadjusted(
    seeded_lake: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    factor_rel = LeanFactorFilePath(market="usa", symbol=SYMBOL).relative_path()
    (seeded_lake.joinpath(*factor_rel.parts)).unlink()

    async def _stub_capture(**kwargs: object) -> return_distribution_service.CaptureReceipt:
        return _complete_capture_receipt()

    app = _app_with(seeded_lake, monkeypatch, capture=_stub_capture)
    response = await _post(app, _request_body())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["meta"]["adjustment"] == "raw"
    assert any("unadjusted" in w for w in body["meta"]["warnings"])


@pytest.mark.asyncio
async def test_return_distribution_captures_missing_symbol_into_lake_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symbol the lake does not hold is populated first, then studied:
    the capture stub seeds the lake, and the endpoint's 200 over the seeded
    bytes proves the read happened after the capture."""
    root = tmp_path / "lake"
    root.mkdir()
    capture_calls: list[tuple[str, date, date]] = []

    async def _seeding_capture(
        *, symbol: str, start: date, end: date
    ) -> return_distribution_service.CaptureReceipt:
        capture_calls.append((symbol, start, end))
        _seed_lake(root)
        return return_distribution_service.CaptureReceipt(
            status="complete", fetched_artifact_count=N_SESSIONS
        )

    app = _app_with(root, monkeypatch, capture=_seeding_capture)

    response = await _post(app, _request_body())
    assert response.status_code == 200, response.text
    body = response.json()

    assert len(capture_calls) == 1
    captured_symbol, captured_start, captured_end = capture_calls[0]
    assert captured_symbol == SYMBOL
    # The capture span covers the window (lead-in included, never past it).
    assert captured_start <= date(2024, 7, 1)
    assert date(2024, 7, 1) <= captured_end <= date(2025, 6, 30)
    # The study read what the capture wrote.
    assert len(body["days"]) == N_SESSIONS
    assert body["meta"]["capture"] == {
        "status": "complete",
        "fetched_artifact_count": N_SESSIONS,
        "detail": None,
    }


@pytest.mark.asyncio
async def test_return_distribution_capture_failure_is_typed_not_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A capture that still leaves the lake empty is a typed 404 carrying
    why the capture could not populate the symbol."""
    root = tmp_path / "lake"
    root.mkdir()

    async def _failing_capture(**kwargs: object) -> return_distribution_service.CaptureReceipt:
        return return_distribution_service.CaptureReceipt(
            status="failed",
            fetched_artifact_count=0,
            detail="CatalogUnavailableError: pool not initialized",
        )

    app = _app_with(root, monkeypatch, capture=_failing_capture)

    response = await _post(app, _request_body())
    assert response.status_code == 404
    detail = response.json()["detail"]
    assert detail["error_code"] == "NOT_CAPTURED"
    assert "could not populate" in detail["message"]
    assert "CatalogUnavailableError" in detail["capture_note"]
