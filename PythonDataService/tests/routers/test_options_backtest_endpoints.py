"""The Options Backtest page's setup routes (#3030) over HTTP.

The session clock, the description and every request that is refused before
it reaches the store need no database. The routes that save, list and delete
run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``)
and skip without one, like their neighbours.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from httpx import ASGITransport

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.main import app
from app.routers import options_backtest as options_backtest_router
from app.utils.timestamps import ny_datetime
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import leg, setup_body
from tests.options_backtest.setup_fixtures import tag as tag
from tests.options_backtest.setup_fixtures import unique as unique

PREFIX = "/api/research/options-backtest"
_EXAMPLE_NAMES = ["SPY premium momentum, no re-entry", "SPY premium momentum, one re-entry"]
# 2026-10-12 is a Monday and a full session; with "now" there, the clock is read from it.
_NOW = date(2026, 10, 12)
_FULL_DAY = session_window_for_date(_NOW)
_OPENED_ET = ny_datetime(_FULL_DAY.open_ms_utc)
_CLOSED_ET = ny_datetime(_FULL_DAY.close_ms_utc)


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _now_is_a_monday_session(monkeypatch: pytest.MonkeyPatch) -> None:
    noon = int(datetime(_NOW.year, _NOW.month, _NOW.day, 12, tzinfo=UTC).timestamp() * 1000)
    monkeypatch.setattr(options_backtest_router, "now_ms_utc", lambda: noon)


def _url(name: str) -> str:
    return f"{PREFIX}/setups/{quote(name, safe='')}"


# ── No database ───────────────────────────────────────────────────────────


async def test_the_session_clock_is_a_full_sessions_open_and_close_from_the_calendar(client) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/session-clock")

    assert response.status_code == 200, response.text
    assert response.json() == {
        "open_minute_et": _OPENED_ET.hour * 60 + _OPENED_ET.minute,
        "close_minute_et": _CLOSED_ET.hour * 60 + _CLOSED_ET.minute,
    }


async def test_a_setup_is_described_against_that_clock(client) -> None:
    body = setup_body(
        legs=[
            leg(
                stop_loss={"kind": "percent", "value": "25"},
                momentum={"kind": "percent_up", "value": "25"},
                reentry_on_stop={"kind": "at_once", "count": 1},
            ),
            leg(leg_id="put", right="put"),
        ]
    )

    async with client as c:
        response = await c.post(f"{PREFIX}/setups/describe", json=body)

    assert response.status_code == 200, response.text
    described = response.json()
    entry_clock = (_OPENED_ET + timedelta(minutes=5)).strftime("%H:%M")
    exit_clock = (_CLOSED_ET - timedelta(minutes=46)).strftime("%H:%M")
    assert "SPY" in described["window"] and entry_clock in described["window"] and exit_clock in described["window"]
    assert [part["leg_id"] for part in described["legs"]] == ["call", "put"]
    assert "25%" in described["legs"][0]["text"] and "25%" not in described["legs"][1]["text"]
    assert described["square_off"] and "10,000.00" in described["costs"]
    assert described["notices"] == []


async def test_a_setup_that_does_not_validate_is_a_422_with_the_field_errors(client) -> None:
    stop_over_a_hundred = setup_body(legs=[leg(stop_loss={"kind": "percent", "value": "150"})])
    no_legs = setup_body(legs=[])
    unknown_field = {**setup_body(), "lots": 1}

    async with client as c:
        stopped = await c.post(f"{PREFIX}/setups/describe", json=stop_over_a_hundred)
        empty = await c.post(f"{PREFIX}/setups/describe", json=no_legs)
        extra = await c.post(f"{PREFIX}/setups/describe", json=unknown_field)

    for response in (stopped, empty, extra):
        assert response.status_code == 422
    assert stopped.json()["detail"][0]["loc"] == ["body", "legs", 0, "stop_loss"]
    assert "under 100" in stopped.json()["detail"][0]["msg"]
    assert empty.json()["detail"][0]["loc"] == ["body", "legs"]
    assert extra.json()["detail"][0]["loc"] == ["body", "lots"]


@pytest.mark.parametrize(
    "name",
    ["semi;colon", " leading", "trailing ", "x" * 81, "line\nbreak", "slash\\back", ".hidden", "-dash"],
)
async def test_a_name_outside_the_rule_is_a_422_before_anything_is_saved(client, name: str) -> None:
    async with client as c:
        response = await c.put(_url(name), json=setup_body())

    assert response.status_code == 422, response.text
    assert response.json()["detail"][0]["loc"] == ["path", "name"]


@pytest.mark.parametrize("encoded", ["%2E", "%2E%2E"])
async def test_a_name_of_only_dots_is_a_422_not_a_saved_setup(client, encoded: str) -> None:
    # Percent-encoded so the client keeps the dots in the path, as a server that decodes it would see them.
    async with client as c:
        response = await c.put(f"{PREFIX}/setups/{encoded}", json=setup_body())
        deleted = await c.delete(f"{PREFIX}/setups/{encoded}")

    for refused in (response, deleted):
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"][0]["loc"] == ["path", "name"]


async def test_a_setup_that_does_not_validate_is_a_422_when_saving_too(client) -> None:
    async with client as c:
        response = await c.put(_url("Fine name"), json=setup_body(legs=[]))

    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "legs"]


# ── Postgres ──────────────────────────────────────────────────────────────


async def test_a_saved_setup_is_listed_after_the_examples_and_reads_back_as_it_was_sent(client, tag: str) -> None:
    name = f"Straddle ({tag})"
    body = setup_body(
        no_reentry_after={"anchor": "close", "offset_ms": 60 * 60_000},
        slippage_percent="0.35",
        legs=[leg(stop_loss={"kind": "points", "value": "0.4"}), leg(leg_id="put", right="put")],
    )

    async with client as c:
        saved = await c.put(_url(name), json=body)
        listed = await c.get(f"{PREFIX}/setups")

    assert saved.status_code == 200, saved.text
    assert saved.json()["name"] == name and saved.json()["example"] is False
    assert saved.json()["saved_at_ms"] > 0 and saved.json()["cannot_open_reason"] is None
    rows = listed.json()
    assert [row["name"] for row in rows[:2]] == _EXAMPLE_NAMES
    assert all(
        row["example"] is True and row["saved_at_ms"] is None and row["cannot_open_reason"] is None for row in rows[:2]
    )
    [mine] = [row for row in rows if row["name"] == name]
    assert mine["saved_at_ms"] == saved.json()["saved_at_ms"]
    assert mine["setup"] == saved.json()["setup"]
    assert mine["setup"]["slippage_percent"] == "0.35"
    assert mine["setup"]["legs"][0]["stop_loss"] == {"kind": "points", "value": "0.4"}
    assert mine["setup"]["no_reentry_after"] == {"anchor": "close", "offset_ms": 3_600_000}


async def test_saving_under_a_name_again_replaces_it(client, tag: str) -> None:
    name = f"Again {tag}"

    async with client as c:
        await c.put(_url(name), json=setup_body(underlying="SPY"))
        second = await c.put(_url(name), json=setup_body(underlying="QQQ"))
        listed = (await c.get(f"{PREFIX}/setups")).json()

    assert second.status_code == 200
    assert [row["setup"]["underlying"] for row in listed if row["name"] == name] == ["QQQ"]


async def test_a_stored_setup_that_no_longer_validates_is_listed_with_the_reason_and_the_rest_still_list(
    client, conn, tag: str
) -> None:
    fine, stale = f"Fine {tag}", f"Stale {tag}"
    stale_json = '{"version": 2}'
    await conn.execute(
        "INSERT INTO research_options_backtest_setups (name, setup_json, saved_at_ms) VALUES ($1, $2::jsonb, $3)",
        stale,
        stale_json,
        1_000,
    )

    async with client as c:
        await c.put(_url(fine), json=setup_body())
        listed = await c.get(f"{PREFIX}/setups")

    assert listed.status_code == 200, listed.text
    rows = {row["name"]: row for row in listed.json()}
    assert rows[fine]["setup"] is not None and rows[fine]["cannot_open_reason"] is None
    assert rows[stale]["setup"] is None and rows[stale]["saved_at_ms"] == 1_000
    assert "version" in rows[stale]["cannot_open_reason"]
    assert [name for name in rows][:2] == _EXAMPLE_NAMES


@pytest.mark.parametrize("name", _EXAMPLE_NAMES)
async def test_an_example_cannot_be_saved_over_or_deleted(client, conn, name: str) -> None:
    async with client as c:
        saved = await c.put(_url(name), json=setup_body(underlying="QQQ"))
        deleted = await c.delete(_url(name))
        listed = (await c.get(f"{PREFIX}/setups")).json()

    for refused in (saved, deleted):
        assert refused.status_code == 409, refused.text
        assert "example" in refused.json()["detail"]["message"]
        assert refused.json()["detail"]["code"]
    [example] = [row for row in listed if row["name"] == name]
    assert example["setup"]["underlying"] == "SPY"


async def test_deleting_removes_the_setup_and_a_second_delete_finds_none(client, tag: str) -> None:
    name = f"Gone {tag}"

    async with client as c:
        await c.put(_url(name), json=setup_body())
        first = await c.delete(_url(name))
        again = await c.delete(_url(name))
        listed = (await c.get(f"{PREFIX}/setups")).json()

    assert first.status_code == 204 and first.content == b""
    assert again.status_code == 404
    assert again.json()["detail"]["code"] and name in again.json()["detail"]["message"]
    assert name not in [row["name"] for row in listed]


async def test_a_name_with_spaces_commas_and_brackets_survives_the_url(client, tag: str) -> None:
    name = f"SPY, wide (v2) {tag}"
    body: dict[str, Any] = setup_body()

    async with client as c:
        saved = await c.put(_url(name), json=body)
        deleted = await c.delete(_url(name))

    assert saved.status_code == 200 and saved.json()["name"] == name
    assert deleted.status_code == 204
