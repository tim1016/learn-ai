"""``GET /api/edge/realized-vs-iv/window``: the window a run reads when its request names none (#3525).

The page shows this window and counts what the lake lacks over it, so the route must say exactly what a run would read.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.lean_sidecar.trading_calendar import expected_sessions
from app.research.lake_study import SESSION_POLICY_FOR_REQUEST
from app.research.study_inputs import StudyInputRefusal
from app.routers.jobs import RealizedVsIvJobRequest
from app.routers.realized_vs_iv import _request_session
from app.services.realized_vs_iv_study_service import RealizedVsIvParams, realized_vs_iv_identity
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms

URL = "/api/edge/realized-vs-iv/window"
NOW_MS = 1_735_689_600_000  # 2025-01-01 00:00 UTC, 19:00 ET on 2024-12-31


@pytest.fixture(autouse=True)
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.routers.realized_vs_iv.now_ms_utc", lambda: NOW_MS)


async def test_the_daily_window_holds_the_273_sessions_a_z_score_needs_and_ends_on_the_last_readable_session(client):
    response = await client.get(URL, params={"bar_size": "1d"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["bar_size"] == "1d" and body["session"] == "rth"
    assert et_date_at_ms(body["window_start_ms"]) == date(2023, 11, 29)
    assert et_date_at_ms(body["window_end_ms"]) == date(2024, 12, 30), "2024-12-31's extended tape is final at 20:20 ET"
    assert body["window_start_ms"] == et_midnight_ms(date(2023, 11, 29)) and body["window_end_ms"] == et_midnight_ms(date(2024, 12, 30))
    assert body["sessions"] == 273 == len(expected_sessions(date(2023, 11, 29), date(2024, 12, 30)))


@pytest.mark.parametrize(
    ("params", "session", "sessions"),
    [
        pytest.param({"bar_size": "15m"}, "extended", 25, id="15m reads extended hours unless told otherwise"),
        pytest.param({"bar_size": "15m", "session": "extended"}, "extended", 25, id="15m extended"),
        pytest.param({"bar_size": "15m", "session": "rth"}, "rth", 31, id="15m regular hours holds fewer bars a session, so more sessions"),
    ],
)
async def test_the_intraday_window_depends_on_the_session_the_run_reads(client, params, session, sessions):
    body = (await client.get(URL, params=params)).json()

    assert body["session"] == session and body["sessions"] == sessions


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"bar_size": "1d"}, id="daily"),
        pytest.param({"bar_size": "15m"}, id="15m default"),
        pytest.param({"bar_size": "15m", "session": "rth"}, id="15m regular"),
    ],
)
async def test_the_window_is_exactly_what_a_run_that_names_none_reads(client, params):
    body = (await client.get(URL, params=params)).json()
    policy = {"rth": "regular", "extended": "extended"}[body["session"]]

    identity = realized_vs_iv_identity(
        RealizedVsIvParams("SPY", params["bar_size"], session_policy=policy if "session" in params else None), now_ms=NOW_MS  # type: ignore[arg-type]
    )

    assert (identity.inputs.window_start, identity.inputs.window_end) == (
        et_date_at_ms(body["window_start_ms"]),
        et_date_at_ms(body["window_end_ms"]),
    )
    assert identity.inputs.session_policy == policy


async def test_a_page_can_send_the_window_back_unchanged_in_the_job_request(client):
    body = (await client.get(URL, params={"bar_size": "1d"})).json()

    request = RealizedVsIvJobRequest(
        job_id="x", symbol="SPY", window_start_ms=body["window_start_ms"], window_end_ms=body["window_end_ms"]
    )

    assert (request.window_start_ms, request.window_end_ms) == (body["window_start_ms"], body["window_end_ms"])


@pytest.mark.parametrize("params", [{"bar_size": "5m"}, {"bar_size": "1h"}, {}, {"bar_size": "1d", "session": "all"}])
async def test_a_bar_size_or_session_the_study_does_not_define_is_a_422(client, params):
    assert (await client.get(URL, params=params)).status_code == 422


async def test_a_daily_window_for_the_regular_session_the_page_names_is_the_default_one(client):
    named = (await client.get(URL, params={"bar_size": "1d", "session": "rth"})).json()
    default = (await client.get(URL, params={"bar_size": "1d"})).json()

    assert named == default and named["session"] == "rth"


async def test_extended_hours_daily_bars_are_a_422_that_carries_the_refusal_a_run_would_give(client):
    """Daily bars are regular hours only (the owner's ruling, #3525); the server holds it, not only the page."""
    response = await client.get(URL, params={"bar_size": "1d", "session": "extended"})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "resolution_unsupported" and "regular hours only" in detail["reason"]
    with pytest.raises(StudyInputRefusal) as run_refusal:
        realized_vs_iv_identity(RealizedVsIvParams("SPY", "1d", session_policy="extended"), now_ms=NOW_MS)
    assert (run_refusal.value.code, run_refusal.value.reason) == (detail["code"], detail["reason"])


def test_the_routes_spelling_of_a_session_is_the_inverse_of_the_one_requests_use():
    assert {_request_session(policy): policy for policy in ("regular", "extended")} == SESSION_POLICY_FOR_REQUEST
