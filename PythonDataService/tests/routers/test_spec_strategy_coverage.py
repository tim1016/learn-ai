"""Strategy Spec refuses a window its data source does not fully cover (#2445).

The Spec endpoint reads the legacy ``LEAN_DATA_ROOT`` / ``LEAN_DATA_CACHE``
folders, and the reader skips a session with no zip without a word. Before
this fix a run on a symbol those folders did not hold — or on a window running
past their last session — reported ``success=True`` with zero trades and the
starting cash untouched: indistinguishable from a strategy that simply never
fired. Codex review V3 reproduced it at the real route boundary.

A zip on disk is not yet a session: one holding no regular-hours bar is
missing to the regular-session reader, and one the reader cannot decode is
refused as unreadable, by path. A window can still be admitted and read
nothing — it holds no trading session at all (a weekend, a holiday). The run
then evaluated zero bars, and says so the way Strategy Lab does instead of
reporting the untouched starting cash as a result.

These tests drive the real default data-source factory (no dependency
override) against a temporary LEAN root, so the refusal is proven where
production meets it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.strategy.spec import StrategySpec
from app.lean_sidecar.trading_calendar import expected_sessions, is_early_close
from app.main import app
from app.routers.spec_strategy import _FIXTURES_DIR, get_data_source_factory
from tests._helpers.lean_store import seed_pre_market_day, seed_store_day

# Thanksgiving fortnight: 2024-11-28 is a closure and 2024-11-29 closes at
# 13:00 ET, so the window carries both calendar cases the check must honour.
WINDOW = (date(2024, 11, 25), date(2024, 12, 6))
THANKSGIVING = date(2024, 11, 28)
BLACK_FRIDAY = date(2024, 11, 29)
# What Strategy Lab and Spec both say about a run that read nothing (#2445).
ZERO_BARS = "missing data: backtest evaluated zero bars for the requested window"


@pytest.fixture
def lean_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the default factory at an empty LEAN root and nothing else."""
    root = tmp_path / "lean-data"
    root.mkdir()
    monkeypatch.setenv("LEAN_DATA_ROOT", str(root))
    monkeypatch.delenv("LEAN_DATA_CACHE", raising=False)
    return root


def _sma_spec(symbol: str = "SPY") -> dict[str, Any]:
    spec = StrategySpec.model_validate_json((_FIXTURES_DIR / "sma_crossover.spec.json").read_text(encoding="utf-8"))
    payload = spec.model_dump(mode="json")
    payload["symbols"] = [symbol]
    return payload


async def _post(spec: dict[str, Any], start: date, end: date) -> httpx.Response:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(
            "/api/spec-strategy/backtest",
            json={"spec": spec, "start_date": start.isoformat(), "end_date": end.isoformat()},
        )


async def _post_backtest(spec: dict[str, Any], start: date, end: date) -> dict[str, Any]:
    resp = await _post(spec, start, end)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _seed(root: Path, symbol: str, days: list[date]) -> None:
    for day in days:
        seed_store_day(root, symbol, day)


async def test_spec_backtest_on_a_symbol_with_no_data_fails_naming_the_gap(lean_root: Path) -> None:
    """The root holds SPY for the whole window; the spec asks for QQQ."""
    sessions = expected_sessions(*WINDOW)
    _seed(lean_root, "SPY", sessions)

    body = await _post_backtest(_sma_spec("QQQ"), *WINDOW)

    assert body["success"] is False, body
    assert body["total_trades"] == 0
    error = body["error"]
    assert "QQQ" in error
    assert f"{len(sessions)} of {len(sessions)}" in error
    assert "missing 2024-11-25..2024-12-06" in error


async def test_spec_backtest_on_a_partially_covered_window_fails_naming_each_gap(lean_root: Path) -> None:
    """Holes inside the window are named as session ranges; a closure is not a hole.

    Missing: 11-27 and the 11-29 half day (one run of sessions, since the
    Thanksgiving closure between them is no session), 12-04 and 12-06.
    """
    missing = {date(2024, 11, 27), BLACK_FRIDAY, date(2024, 12, 4), date(2024, 12, 6)}
    sessions = expected_sessions(*WINDOW)
    assert THANKSGIVING not in sessions
    assert BLACK_FRIDAY in sessions and is_early_close(BLACK_FRIDAY)
    _seed(lean_root, "SPY", [day for day in sessions if day not in missing])

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["total_trades"] == 0
    error = body["error"]
    assert "SPY" in error
    assert f"4 of {len(sessions)}" in error
    assert "missing 2024-11-27..2024-11-29, 2024-12-04, 2024-12-06" in error
    assert THANKSGIVING.isoformat() not in error


async def test_spec_backtest_on_a_fully_covered_window_is_unchanged(lean_root: Path) -> None:
    """Admission adds nothing to a covered run: same response as a bare reader over the same root."""
    _seed(lean_root, "SPY", expected_sessions(*WINDOW))

    admitted = await _post_backtest(_sma_spec(), *WINDOW)

    app.dependency_overrides[get_data_source_factory] = lambda: (
        lambda symbol, start, end: LeanMinuteDataReader([lean_root])
    )
    try:
        bare = await _post_backtest(_sma_spec(), *WINDOW)
    finally:
        app.dependency_overrides.pop(get_data_source_factory, None)

    assert admitted["success"] is True, admitted
    assert admitted["total_trades"] > 0, "the covered window must exercise a real run, not an empty one"
    assert admitted == bare


@pytest.mark.parametrize(
    "window",
    [(date(2024, 11, 30), date(2024, 12, 1)), (THANKSGIVING, THANKSGIVING)],
    ids=["weekend", "holiday"],
)
async def test_spec_backtest_on_a_window_with_no_session_fails_instead_of_reporting_the_starting_cash(
    lean_root: Path, window: tuple[date, date]
) -> None:
    """No session is missing from a window that holds none, so admission passes; the run read nothing."""
    assert expected_sessions(*window) == []

    body = await _post_backtest(_sma_spec("ZZZZ"), *window)

    assert body["success"] is False, body
    assert body["error"] == ZERO_BARS
    assert body["final_equity"] == 0.0


async def test_spec_backtest_on_zips_with_no_regular_hours_bar_is_refused_as_missing(lean_root: Path) -> None:
    """Every session's zip is on disk, but the regular-session reader reads no bar from any of them."""
    sessions = expected_sessions(*WINDOW)
    for day in sessions:
        seed_pre_market_day(lean_root, "SPY", day)

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["error"] == (
        f"missing data: SPY has no minute bars for {len(sessions)} of {len(sessions)} trading sessions "
        "in 2024-11-25..2024-12-06 — missing 2024-11-25..2024-12-06"
    )


async def test_spec_backtest_with_one_session_holding_no_regular_hours_bar_fails_naming_it(lean_root: Path) -> None:
    """The other sessions produce bars, so the zero-bar backstop cannot catch the hole (#2475 review, Codex P1).

    Before the fix the zip's mere presence admitted the session, and the run
    completed on the eight sessions the reader did read.
    """
    sessions = expected_sessions(*WINDOW)
    hole = date(2024, 12, 3)
    _seed(lean_root, "SPY", [day for day in sessions if day != hole])
    seed_pre_market_day(lean_root, "SPY", hole)

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["error"] == (
        f"missing data: SPY has no minute bars for 1 of {len(sessions)} trading sessions "
        "in 2024-11-25..2024-12-06 — missing 2024-12-03"
    )


async def test_spec_backtest_on_a_zip_the_reader_cannot_decode_fails_naming_the_file(lean_root: Path) -> None:
    """A damaged zip is reported as unreadable, by path — not as a missing session a backfill would fill."""
    sessions = expected_sessions(*WINDOW)
    _seed(lean_root, "SPY", sessions)
    damaged = lean_root / "equity" / "usa" / "minute" / "spy" / "20241203_trade.zip"
    damaged.write_bytes(b"not a zip")

    body = await _post_backtest(_sma_spec(), *WINDOW)

    assert body["success"] is False, body
    assert body["error"] == (
        f"unreadable data: SPY minute data for 1 of {len(sessions)} trading sessions in 2024-11-25..2024-12-06 "
        f"is on disk but cannot be read — {damaged} (BadZipFile: File is not a zip file)"
    )


async def test_spec_backtest_with_an_end_before_its_start_is_a_bad_request(lean_root: Path) -> None:
    resp = await _post(_sma_spec(), WINDOW[1], WINDOW[0])

    assert resp.status_code == 400, resp.text
    assert resp.json()["detail"] == "end_date must not precede start_date (got start=2024-12-06, end=2024-11-25)"
