"""The Options Backtest page's setup routes (#3030) over HTTP.

The session clock, the description and every request that is refused before
it reaches the store need no database. The routes that save, list and delete
run only against a database attested as disposable (``POSTGRES_URL_IS_EPHEMERAL=1``)
and skip without one, like their neighbours.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
import pytest
from httpx import ASGITransport

from app.lean_sidecar.trading_calendar import session_window_for_date
from app.main import app
from app.routers import options_backtest as options_backtest_router
from app.utils.session_anchors import MAX_TIMESTAMP_MS
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


_NOON_MS = int(datetime(_NOW.year, _NOW.month, _NOW.day, 12, tzinfo=UTC).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _now_is_a_monday_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(options_backtest_router, "now_ms_utc", lambda: _NOON_MS)


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
    window = " ".join(described["window"])
    assert "SPY" in window and entry_clock in window and exit_clock in window
    assert [part["leg_id"] for part in described["legs"]] == ["call", "put"]
    assert "25%" in " ".join(described["legs"][0]["sentences"])
    assert "25%" not in " ".join(described["legs"][1]["sentences"])
    assert described["square_off"] and "10,000.00" in " ".join(described["costs"])
    assert described["notices"] == []
    # Every block is a list of strings, not one string.
    for block in ("window", "square_off", "costs", "notices"):
        assert isinstance(described[block], list) and all(isinstance(each, str) for each in described[block])
    assert all(isinstance(each, list) for each in (part["sentences"] for part in described["legs"]))


async def test_the_description_carries_the_setup_as_the_server_read_it_with_its_defaults_filled(client) -> None:
    sent = setup_body()
    assert "slippage_percent" not in sent and "price_step_usd" not in sent and "version" not in sent

    async with client as c:
        response = await c.post(f"{PREFIX}/setups/describe", json=sent)

    read = response.json()["setup"]
    assert (read["version"], read["slippage_percent"], read["price_step_usd"]) == (1, "0.3", "0.01")
    assert read["underlying"] == "SPY" and [each["leg_id"] for each in read["legs"]] == ["call"]
    # What the page sends back to run is this, not what it sent: the same setup, in full.
    assert read["fees"] == {"kind": "not_charged", "per_contract_usd": None}
    assert read["starting_cash_usd"] == "10000"


async def test_a_setup_using_every_rule_is_described_in_the_order_the_owner_reads_it(client) -> None:
    # One setup that switches on every rule: an entry and an exit, the re-entry cutoff, partial square-off, slippage and a
    # price step, estimated fees, and two legs that between them use each way to pick, wait, stop and come back.
    body = setup_body(
        entry={"anchor": "open", "offset_ms": 15 * 60_000},
        exit={"anchor": "close", "offset_ms": 30 * 60_000},
        no_reentry_after={"anchor": "close", "offset_ms": 90 * 60_000},
        square_off="partial",
        slippage_percent="0.45",
        price_step_usd="0.05",
        fees={"kind": "estimated"},
        starting_cash_usd="25000",
        legs=[
            leg(
                leg_id="call",
                contracts=2,
                expiry={"kind": "same_day"},
                strike={"kind": "atm_offset", "steps": 1},
                stop_loss={"kind": "percent", "value": "20"},
                momentum={"kind": "percent_up", "value": "10"},
                reentry_on_stop={"kind": "at_once", "count": 2},
            ),
            leg(
                leg_id="put",
                right="put",
                expiry={"kind": "min_days", "days": 7},
                strike={"kind": "atm_offset", "steps": -2},
                stop_loss={"kind": "points", "value": "0.5"},
                momentum={"kind": "points_down", "value": "0.25"},
                reentry_on_stop={"kind": "on_momentum", "count": 1},
            ),
        ],
    )

    async with client as c:
        described = (await c.post(f"{PREFIX}/setups/describe", json=body)).json()

    def starts(sentences: list[str]) -> list[str]:
        return [" ".join(sentence.split()[:4]) for sentence in sentences]

    # The order is the order of the setup: the window opens with the market, then the entry, the exit, how an order
    # fills, what happens at the exit, and last the re-entry cutoff with the sentence that spares first entries.
    assert starts(described["window"]) == [
        "Trades SPY.",
        "The entry is read",
        "The exit is read",
        "On a day that",
        "An order fills at",
        "A buy is dropped",
        "At the exit, whatever",
        "A stop hit later",
        "First entries are not",
    ]
    # A leg: what it buys, when it buys, when it sells, and what it does after a sell.
    for leg_description in described["legs"]:
        first, *rest = leg_description["sentences"]
        assert first.startswith(f"Leg {leg_description['leg_id']}: buys")
        assert starts(rest) == ["The buy waits until", "Sells once a minute", "After a stop, picks"]
    # Costs: the fills first, then the fees (two sentences for estimated fees), the starting cash last.
    assert starts(described["costs"]) == [
        "Each fill is moved",
        "Fees are estimated from",
        "A session from before",
        "Starting cash is $25,000.00.",
    ]
    assert [leg_description["leg_id"] for leg_description in described["legs"]] == ["call", "put"]


@pytest.mark.parametrize("spelled", ["1e5", "100000", "1.0E+5", "100000.00"])
async def test_a_setups_money_is_served_in_plain_digits_however_it_was_spelled(client, tag: str, spelled: str) -> None:
    body = setup_body(starting_cash_usd=spelled)
    # 1e5 and 1.0E+5 are a hundred thousand; the two-place spelling keeps its places, which are the owner's own.
    plain = "100000.00" if spelled == "100000.00" else "100000"

    async with client as c:
        described = (await c.post(f"{PREFIX}/setups/describe", json=body)).json()
        saved = (await c.put(_url(f"Plain {tag}"), json=body)).json()
        listed = (await c.get(f"{PREFIX}/setups")).json()

    [mine] = [row for row in listed if row["name"] == f"Plain {tag}"]
    assert described["setup"]["starting_cash_usd"] == plain
    assert saved["setup"]["starting_cash_usd"] == plain
    assert mine["setup"]["starting_cash_usd"] == plain


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


async def test_a_setup_that_sells_is_described_with_each_leg_said_bought_or_sold_and_is_read_back_with_its_side(
    client,
) -> None:
    body = setup_body(
        legs=[
            leg(leg_id="long_call"),
            leg(
                leg_id="short_call",
                side="sell",
                strike={"kind": "atm_offset", "steps": 2},
                stop_loss={"kind": "percent", "value": "150"},
            ),
        ]
    )

    async with client as c:
        response = await c.post(f"{PREFIX}/setups/describe", json=body)

    assert response.status_code == 200, response.text
    described = response.json()
    bought, sold = (" ".join(part["sentences"]) for part in described["legs"])
    assert "Leg long_call: buys 1 at-the-money call" in bought
    assert "Leg short_call: sells to open 1 call 2 strikes above the money" in sold
    assert "bought back" in sold and "150% above what it was sold for" in sold
    # The setup as the server read it carries a sold leg's side, and a bought leg's carries none.
    assert [each.get("side") for each in described["setup"]["legs"]] == [None, "sell"]


@pytest.mark.parametrize(
    ("legs", "said"),
    [
        ([leg(leg_id="short_call", side="sell")], "sell more call contracts than they buy"),
        ([leg(leg_id="long_call"), leg(leg_id="short_put", right="put", side="sell")], "sell more put contracts"),
        (
            [leg(leg_id="long_call"), leg(leg_id="short_call", side="sell", expiry={"kind": "same_day"})],
            "needs every leg on one expiry rule",
        ),
    ],
)
async def test_a_setup_that_sells_without_cover_or_on_two_expiry_rules_is_a_422_in_the_servers_words(
    client, legs: list[dict[str, Any]], said: str
) -> None:
    async with client as c:
        described = await c.post(f"{PREFIX}/setups/describe", json=setup_body(legs=legs))
        saved = await c.put(_url("Never saved"), json=setup_body(legs=legs))

    for response in (described, saved):
        assert response.status_code == 422, response.text
        [problem] = response.json()["detail"]
        assert problem["loc"] == ["body"]
        assert said in problem["msg"]


async def test_a_percent_stop_of_a_hundred_or_more_is_a_422_on_a_bought_leg_at_its_stop_loss_and_is_taken_on_a_sold_leg(
    client,
) -> None:
    stop = {"kind": "percent", "value": "150"}

    async with client as c:
        bought = await c.post(f"{PREFIX}/setups/describe", json=setup_body(legs=[leg(stop_loss=stop)]))
        sold = await c.post(
            f"{PREFIX}/setups/describe",
            json=setup_body(legs=[leg(leg_id="long_call"), leg(leg_id="short_call", side="sell", stop_loss=stop)]),
        )

    assert bought.status_code == 422
    assert bought.json()["detail"][0]["loc"] == ["body", "legs", 0, "stop_loss"]
    assert sold.status_code == 200, sold.text


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
    assert saved.json()["cannot_open_reason"] is None
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


async def test_a_setup_is_saved_at_the_moment_the_clock_says_in_the_answer_and_in_the_store(
    client, conn, tag: str
) -> None:
    name = f"On the clock {tag}"

    async with client as c:
        saved = await c.put(_url(name), json=setup_body())
        listed = (await c.get(f"{PREFIX}/setups")).json()

    stored = await conn.fetchval("SELECT saved_at_ms FROM research_options_backtest_setups WHERE name = $1", name)
    [row] = [row for row in listed if row["name"] == name]
    assert saved.json()["saved_at_ms"] == _NOON_MS
    assert stored == _NOON_MS
    assert row["saved_at_ms"] == _NOON_MS


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


async def test_no_stored_row_can_make_the_list_fail_whatever_its_json_or_its_stamp_holds(
    client, conn, tag: str
) -> None:
    fine = f"Fine {tag}"
    insert = "INSERT INTO research_options_backtest_setups (name, setup_json, saved_at_ms) VALUES ($1, $2::jsonb, $3)"
    odd_json = {
        f"A list {tag}": "[1, 2]",
        f"A string {tag}": '"hello"',
        f"A number {tag}": "42",
        f"A null {tag}": "null",
        f"An unknown field {tag}": '{"lots": 1}',
        f"An empty object {tag}": "{}",
        f"Deep {tag}": "[" * 2000 + "]" * 2000,
    }
    for name, stored in odd_json.items():
        await conn.execute(insert, name, stored, 1_000)
    # The smallest and the largest stamp the table admits are served as they are.
    await conn.execute(insert, f"Earliest {tag}", "{}", 0)
    await conn.execute(insert, f"Latest {tag}", "{}", MAX_TIMESTAMP_MS)

    async with client as c:
        await c.put(_url(fine), json=setup_body())
        listed = await c.get(f"{PREFIX}/setups")

    assert listed.status_code == 200, listed.text
    rows = {row["name"]: row for row in listed.json() if row["name"].endswith(tag)}
    assert rows[fine]["setup"] is not None
    for name in (*odd_json, f"Earliest {tag}", f"Latest {tag}"):
        assert rows[name]["setup"] is None and rows[name]["cannot_open_reason"], name
    assert (rows[f"Earliest {tag}"]["saved_at_ms"], rows[f"Latest {tag}"]["saved_at_ms"]) == (0, MAX_TIMESTAMP_MS)


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


# ── The limits ────────────────────────────────────────────────────────────

_LIMIT_OF_A_FIELD = {
    "max_legs": lambda n: {"legs": [leg(leg_id=f"l{i}") for i in range(n)]},
    "max_contracts_per_leg": lambda n: {"legs": [leg(contracts=n)]},
    "max_reentries_per_leg": lambda n: {
        "legs": [leg(stop_loss={"kind": "percent", "value": "25"}, reentry_on_stop={"kind": "at_once", "count": n})]
    },
    "max_strike_steps": lambda n: {"legs": [leg(strike={"kind": "atm_offset", "steps": n})]},
    "max_expiry_days": lambda n: {"legs": [leg(expiry={"kind": "min_days", "days": n})]},
}


async def test_the_limits_are_served_with_the_names_a_page_reads(client) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}/limits")

    assert response.status_code == 200, response.text
    limits = response.json()
    chain_limits = ("max_strikes_each_side", "max_contracts_per_download")
    assert set(limits) == {*_LIMIT_OF_A_FIELD, "max_range_sessions", *chain_limits, "setup_name"}
    assert all(
        isinstance(limits[name], int) and limits[name] > 0
        for name in (*_LIMIT_OF_A_FIELD, "max_range_sessions", *chain_limits)
    )
    assert set(limits["setup_name"]) == {"pattern", "sentence"} and limits["setup_name"]["sentence"]


def test_the_setup_routes_read_the_range_limit_without_importing_the_run_service() -> None:
    # A fresh interpreter: this file imports the whole app, and the app imports the service.
    probe = "import sys, app.routers.options_backtest; print('app.options_backtest.service' in sys.modules)"

    done = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )

    assert done.stdout.split() == ["False"], done.stderr


@pytest.mark.parametrize("name", list(_LIMIT_OF_A_FIELD))
async def test_each_limit_served_is_the_one_the_server_enforces_it_admits_the_limit_and_refuses_one_more(
    client, name: str
) -> None:
    async with client as c:
        limit = (await c.get(f"{PREFIX}/limits")).json()[name]
        at_the_limit = await c.post(f"{PREFIX}/setups/describe", json=setup_body(**_LIMIT_OF_A_FIELD[name](limit)))
        one_more = await c.post(f"{PREFIX}/setups/describe", json=setup_body(**_LIMIT_OF_A_FIELD[name](limit + 1)))

    assert at_the_limit.status_code == 200, at_the_limit.text
    assert one_more.status_code == 422


async def test_the_name_rule_served_is_the_one_a_save_is_checked_against(client) -> None:
    import re

    async with client as c:
        rule = (await c.get(f"{PREFIX}/limits")).json()["setup_name"]
        refused = await c.put(_url("semi;colon"), json=setup_body())

    assert re.fullmatch(rule["pattern"], "A good name (1)") and not re.fullmatch(rule["pattern"], "semi;colon")
    assert refused.status_code == 422
