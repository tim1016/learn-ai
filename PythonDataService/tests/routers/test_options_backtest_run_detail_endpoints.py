"""Reading a saved Options Backtest run closely, over HTTP (#3030): its leg trades, one session, the CSV export and the
comparison of two runs.

Every route here reads saved runs, so these run only against a database attested as disposable
(``POSTGRES_URL_IS_EPHEMERAL=1``) and skip without one. The hand-worked figures are in
``tests/options_backtest/_detail_scene.py``.
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.main import app
from app.options_backtest.results import Fill, LegTrade, Mark
from app.options_backtest.run import RunResult, run_sessions
from app.utils.session_anchors import MAX_TIMESTAMP_MS
from tests.options_backtest._a_run import a_busy_run
from tests.options_backtest._detail_scene import (
    CALL_500,
    PUT_500,
    SESSIONS,
    THU_OPEN_MS,
    WED_OPEN_MS,
    minute,
    the_scene,
    the_setup,
)
from tests.options_backtest._run_data import fake_data
from tests.options_backtest._stored_runs import SaveRun
from tests.options_backtest._stored_runs import is_the_job_alive as is_the_job_alive
from tests.options_backtest._stored_runs import save_run as save_run
from tests.options_backtest.setup_fixtures import conn as conn
from tests.options_backtest.setup_fixtures import unique as unique

D = Decimal
PREFIX = "/api/research/options-backtest"
A_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@pytest.fixture
def client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(scope="module")
def scene() -> RunResult:
    return run_sessions(the_setup(), SESSIONS, fake_data(the_scene()))


@pytest.fixture(scope="module")
def busy() -> RunResult:
    return a_busy_run()


def _no_date_in(value: Any, path: str = "body") -> None:
    """Nothing on the wire names a date: a session is its open in ms."""
    if isinstance(value, str):
        assert not A_DATE.search(value), f"{path}: {value!r} names a date"
    elif isinstance(value, dict):
        for key, each in value.items():
            _no_date_in(key, f"{path}.{key}")
            _no_date_in(each, f"{path}.{key}")
    elif isinstance(value, list):
        for at, each in enumerate(value):
            _no_date_in(each, f"{path}[{at}]")


# ── The trades, with the figures of the hand-worked scene ────────────────


def _worked_trades() -> list[dict[str, Any]]:
    """The scene's three trades as the page is sent them, from the hand-worked docstring of ``_detail_scene.py``."""
    open_ms = WED_OPEN_MS
    return [
        {
            "trade_number": 0,
            "leg_id": "put",
            "entry_number": 0,
            "contract_symbol": PUT_500,
            "starting_price_usd": None,
            "momentum_level_usd": None,
            "stop_level_usd": None,
            "entry_decided_at_ms": minute(open_ms, 5),
            "entry": (minute(open_ms, 6), D("2.00"), D("2.00")),
            "exit_reason": "exit_time",
            "exit_group": 2,
            "exit_decided_at_ms": minute(open_ms, 60),
            "sale": (minute(open_ms, 61), D("2.10"), D("2.10")),
            "entry_fee_usd": D("0.65"),
            "exit_fee_usd": D("0.65"),
            "pnl_usd": D("8.70"),
        },
        {
            "trade_number": 1,
            "leg_id": "call",
            "entry_number": 0,
            "contract_symbol": CALL_500,
            "starting_price_usd": D("4.00"),
            "momentum_level_usd": D("5.00"),
            "stop_level_usd": D("4.08"),
            "entry_decided_at_ms": minute(open_ms, 10),
            "entry": (minute(open_ms, 11), D("5.10"), D("5.10")),
            "exit_reason": "stop",
            "exit_group": 1,
            "exit_decided_at_ms": minute(open_ms, 20),
            "sale": (minute(open_ms, 21), D("3.90"), D("3.90")),
            "entry_fee_usd": D("0.65"),
            "exit_fee_usd": D("0.65"),
            "pnl_usd": D("-121.30"),
        },
        {
            "trade_number": 2,
            "leg_id": "call",
            "entry_number": 1,
            "contract_symbol": CALL_500,
            "starting_price_usd": D("3.80"),
            "momentum_level_usd": D("4.75"),
            "stop_level_usd": D("3.88"),
            "entry_decided_at_ms": minute(open_ms, 30),
            "entry": (minute(open_ms, 31), D("4.85"), D("4.85")),
            "exit_reason": "exit_time",
            "exit_group": 2,
            "exit_decided_at_ms": minute(open_ms, 60),
            "sale": (minute(open_ms, 61), D("4.55"), D("4.55")),
            "entry_fee_usd": D("0.65"),
            "exit_fee_usd": D("0.65"),
            "pnl_usd": D("-31.30"),
        },
    ]


def _money(text: str | None) -> Decimal | None:
    return None if text is None else D(text)


def _as_worked(trade: dict[str, Any]) -> dict[str, Any]:
    """A trade of the answer in the shape ``_worked_trades`` is written in: prices as Decimals, fills as tuples."""
    sale = trade["sale"]
    return {
        "trade_number": trade["trade_number"],
        "leg_id": trade["leg_id"],
        "entry_number": trade["entry_number"],
        "contract_symbol": trade["contract"]["symbol"],
        "starting_price_usd": _money(trade["starting_price_usd"]),
        "momentum_level_usd": _money(trade["momentum_level_usd"]),
        "stop_level_usd": _money(trade["stop_level_usd"]),
        "entry_decided_at_ms": trade["entry_decided_at_ms"],
        "entry": (
            trade["entry"]["bar_close_ms"],
            _money(trade["entry"]["seen_usd"]),
            _money(trade["entry"]["filled_usd"]),
        ),
        "exit_reason": trade["exit_reason"],
        "exit_group": trade["exit_group"],
        "exit_decided_at_ms": trade["exit_decided_at_ms"],
        "sale": None if sale is None else (sale["bar_close_ms"], _money(sale["seen_usd"]), _money(sale["filled_usd"])),
        "entry_fee_usd": _money(trade["entry_fee_usd"]),
        "exit_fee_usd": _money(trade["exit_fee_usd"]),
        "pnl_usd": _money(trade["pnl_usd"]),
    }


async def test_the_trades_of_a_run_are_served_in_order_with_every_figure_the_scene_works_out(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "trades")

    async with client as c:
        response = await c.get(f"{PREFIX}/runs/{run_id}/trades")

    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["run_id"], body["total"], body["offset"], body["limit"], body["next_offset"]) == (
        run_id,
        3,
        0,
        1000,
        None,
    )
    assert [_as_worked(trade) for trade in body["trades"]] == _worked_trades()
    for trade in body["trades"]:
        assert trade["session_open_ms"] == WED_OPEN_MS and trade["multiplier"] == 100 and trade["contracts"] == 1
        assert trade["exit_kind"] == "sale" and trade["mark"] is None
        assert trade["contract"]["underlying"] == "SPY" and trade["contract"]["strike_usd"] == "500"
    # A call and a put of the 500 strike expiring on the session's own day: the expiry anchor is 16:00 New York.
    sixteen_hundred_eastern = WED_OPEN_MS + 390 * 60_000
    assert {(t["contract"]["right"], t["contract"]["expiry_ms"]) for t in body["trades"]} == {
        ("put", sixteen_hundred_eastern),
        ("call", sixteen_hundred_eastern),
    }
    _no_date_in(body)


async def test_every_price_is_text_in_plain_digits_and_every_instant_a_whole_number_under_the_ceiling(
    client: httpx.AsyncClient, save_run: SaveRun, busy: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(busy, "typed")

    async with client as c:
        body = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"limit": 5000})).json()

    assert body["total"] == len(body["trades"]) >= 10
    plain = re.compile(r"-?\d+(\.\d+)?")
    for trade in body["trades"]:
        for name in (
            "entry_fee_usd",
            "exit_fee_usd",
            "pnl_usd",
            "starting_price_usd",
            "momentum_level_usd",
            "stop_level_usd",
        ):
            assert trade[name] is None or plain.fullmatch(trade[name]), (name, trade[name])
        for name, value in trade.items():
            if name.endswith("_ms"):
                assert type(value) is int and 0 <= value <= MAX_TIMESTAMP_MS, (name, value)
        for part in (trade["entry"], trade["sale"], trade["mark"]):
            for name, value in (part or {}).items():
                assert not name.endswith("_usd") or plain.fullmatch(value), (name, value)
                assert not name.endswith("_ms") or (type(value) is int and 0 <= value <= MAX_TIMESTAMP_MS)
    _no_date_in(body)


async def test_a_leg_marked_and_not_sold_is_told_apart_from_one_that_was_sold(
    client: httpx.AsyncClient, save_run: SaveRun, busy: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(busy, "marks")
    saved: list[LegTrade] = [trade for session in busy.sessions for trade in session.outcome.trades]
    assert any(isinstance(t.exit, Mark) for t in saved) and any(isinstance(t.exit, Fill) for t in saved)

    async with client as c:
        body = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"limit": 5000})).json()

    assert len(body["trades"]) == len(saved)
    for wire, trade in zip(body["trades"], saved, strict=True):
        assert wire["exit_reason"] == trade.exit_reason.value
        if isinstance(trade.exit, Mark):
            assert wire["exit_kind"] == "mark" and wire["sale"] is None
            assert wire["mark"] == {"bar_close_ms": trade.exit.bar_close_ms, "price_usd": wire["mark"]["price_usd"]}
            assert D(wire["mark"]["price_usd"]) == trade.exit.price
            assert D(wire["exit_fee_usd"]) == 0
        else:
            assert wire["exit_kind"] == "sale" and wire["mark"] is None
            assert wire["sale"]["bar_close_ms"] == trade.exit.bar_close_ms
            assert (D(wire["sale"]["seen_usd"]), D(wire["sale"]["filled_usd"])) == (trade.exit.seen, trade.exit.filled)
        assert (D(wire["entry"]["seen_usd"]), D(wire["entry"]["filled_usd"])) == (trade.entry.seen, trade.entry.filled)
        assert D(wire["pnl_usd"]) == trade.pnl_usd
        assert wire["contract"]["symbol"] == trade.contract.occ_symbol


# ── Paging and filters ───────────────────────────────────────────────────


async def test_a_run_can_be_read_a_page_at_a_time_and_the_pages_add_up_to_every_trade_once(
    client: httpx.AsyncClient, save_run: SaveRun, busy: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(busy, "pages")

    async with client as c:
        whole = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"limit": 5000})).json()
        pages: list[dict[str, Any]] = []
        offset: int | None = 0
        while offset is not None:
            page = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"limit": 7, "offset": offset})).json()
            assert page["total"] == whole["total"] and page["limit"] == 7 and page["offset"] == offset
            pages.append(page)
            offset = page["next_offset"]

    total = whole["total"]
    assert [len(page["trades"]) for page in pages] == [7] * (total // 7) + ([total % 7] if total % 7 else [])
    assert [trade for page in pages for trade in page["trades"]] == whole["trades"]


async def test_the_trades_can_be_narrowed_to_a_session_and_to_a_leg_and_the_total_follows(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "narrow")

    async with client as c:
        calls = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "call"})).json()
        wednesday = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"session_open_ms": WED_OPEN_MS})).json()
        thursday = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"session_open_ms": THU_OPEN_MS})).json()
        lag = (await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "lag"})).json()
        one = (
            await c.get(
                f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "call", "session_open_ms": WED_OPEN_MS, "limit": 1}
            )
        ).json()

    assert (calls["total"], [t["trade_number"] for t in calls["trades"]]) == (2, [1, 2])
    assert (wednesday["total"], len(wednesday["trades"])) == (3, 3)
    # A session that was tested and made no trade, and a leg that made none, answer with nothing: those are results.
    assert (thursday["total"], thursday["trades"], thursday["next_offset"]) == (0, [], None)
    assert (lag["total"], lag["trades"]) == (0, [])
    assert (one["total"], [t["trade_number"] for t in one["trades"]], one["next_offset"]) == (2, [1], 1)


# ── What it refuses ──────────────────────────────────────────────────────


async def test_a_run_that_does_not_exist_is_a_404_and_one_that_is_not_complete_is_a_409_naming_its_status(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    running = await save_run(scene, "running", end="running")
    failed = await save_run(scene, "failed", end="failed")
    cancelled = await save_run(scene, "cancelled", end="cancelled")

    async with client as c:
        missing = await c.get(f"{PREFIX}/runs/{2**52}/trades")
        is_the_job_alive["live"] = True
        busy_run = await c.get(f"{PREFIX}/runs/{running}/trades")
        is_the_job_alive["live"] = False
        gone_run = await c.get(f"{PREFIX}/runs/{running}/trades")
        failed_run = await c.get(f"{PREFIX}/runs/{failed}/trades")
        cancelled_run = await c.get(f"{PREFIX}/runs/{cancelled}/trades")

    assert missing.status_code == 404 and missing.json()["detail"]["code"] == "OPTIONS_BACKTEST_RUN_NOT_FOUND"
    for response, status in (
        (busy_run, "running"),
        (gone_run, "interrupted"),
        (failed_run, "failed"),
        (cancelled_run, "cancelled"),
    ):
        assert response.status_code == 409, response.text
        detail = response.json()["detail"]
        assert detail["code"] == "OPTIONS_BACKTEST_RUN_NOT_COMPLETE"
        assert status in detail["message"]


async def test_a_session_the_run_did_not_test_and_a_leg_it_does_not_have_are_404s_with_their_own_codes(
    client: httpx.AsyncClient, save_run: SaveRun, scene: RunResult, is_the_job_alive: Any
) -> None:
    run_id = await save_run(scene, "unknown")

    async with client as c:
        session = await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"session_open_ms": WED_OPEN_MS + 60_000})
        leg = await c.get(f"{PREFIX}/runs/{run_id}/trades", params={"leg_id": "nope"})

    assert session.status_code == 404
    assert session.json()["detail"]["code"] == "OPTIONS_BACKTEST_SESSION_NOT_IN_RUN"
    assert str(WED_OPEN_MS + 60_000) in session.json()["detail"]["message"]
    assert leg.status_code == 404 and leg.json()["detail"]["code"] == "OPTIONS_BACKTEST_LEG_NOT_IN_RUN"


@pytest.mark.parametrize(
    "path, params",
    [
        ("/runs/0/trades", {}),
        ("/runs/-3/trades", {}),
        ("/runs/abc/trades", {}),
        (f"/runs/{2**53}/trades", {}),
        ("/runs/1/trades", {"limit": 0}),
        ("/runs/1/trades", {"limit": 5001}),
        ("/runs/1/trades", {"offset": -1}),
        ("/runs/1/trades", {"session_open_ms": -1}),
        ("/runs/1/trades", {"session_open_ms": MAX_TIMESTAMP_MS + 1}),
        ("/runs/1/trades", {"leg_id": ""}),
        ("/runs/1/trades", {"leg_id": "x" * 33}),
        ("/runs/1/trades", {"limit": "many"}),
    ],
)
async def test_a_request_that_cannot_be_one_is_a_422_before_any_run_is_looked_for(
    client: httpx.AsyncClient, path: str, params: dict[str, Any]
) -> None:
    async with client as c:
        response = await c.get(f"{PREFIX}{path}", params=params)

    assert response.status_code == 422, response.text
