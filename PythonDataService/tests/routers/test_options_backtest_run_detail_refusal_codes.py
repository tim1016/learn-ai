"""The refusals of a saved run's routes that carry a code of their own, with no database (#3221).

Every assertion of these codes was in a test that needs a database attested as disposable
(``test_options_backtest_run_detail_{compare,session,}_endpoints.py`` and
``test_options_backtest_run_detail_hostile_requests.py``), so a pull request never fired them and only the daily run did. The real routes, ``run_detail`` and ``saved_runs`` run here over a fake connection
(``tests/options_backtest/_fake_connection.py``), which answers the few statements they send and refuses any other.

Hand-worked: run 41 and run 42 are complete runs of one session each, stored with the setup of
``tests/options_backtest/setup_fixtures.py``: one leg, ``call``. Their one session opens at ``FIRST_OPEN_MS``.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.options.contract import OptionContract
from app.options_backtest import run_detail
from tests.options_backtest._fake_connection import (
    FIRST_OPEN_MS,
    FakeConnection,
    Pool,
    a_run_row,
    a_skipped_session_row,
)
from tests.options_backtest._stored_runs import PREFIX
from tests.options_backtest._stored_runs import client as client
from tests.options_backtest.setup_fixtures import setup

ANOTHER_SESSION_MS = FIRST_OPEN_MS + 60_000


def _stored(
    monkeypatch: pytest.MonkeyPatch, *runs: dict[str, object], sessions: tuple[dict[str, object], ...] = ()
) -> None:
    monkeypatch.setattr(run_detail, "with_connection", Pool(FakeConnection(runs, sessions=sessions)))  # type: ignore[arg-type]


def _a_setup_that_can_no_longer_be_read() -> str:
    """The stored setup of a run with a field the setup no longer has: it does not validate."""
    return json.dumps({**json.loads(setup().model_dump_json()), "lots": 1})


async def test_runs_one_of_which_has_a_stored_setup_that_can_no_longer_be_read_are_a_409_that_names_that_run(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored(monkeypatch, a_run_row(41), a_run_row(42, setup_json=_a_setup_that_can_no_longer_be_read()))

    response = await client.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 42})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "OPTIONS_BACKTEST_RUN_SETUP_CANNOT_BE_READ"
    # The place and the reason beside the run: a field the setup no longer has. The run that reads is not named.
    assert "the stored setup of run 42 (b) can no longer be read: lots: " in detail["message"]
    assert "run 41" not in detail["message"]


async def test_runs_of_different_underlyings_are_a_409_naming_both_runs_and_their_underlyings(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored(monkeypatch, a_run_row(41, underlying="SPY"), a_run_row(42, underlying="QQQ"))

    response = await client.get(f"{PREFIX}/runs/compare", params={"a": 41, "b": 42})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "OPTIONS_BACKTEST_RUNS_OF_DIFFERENT_UNDERLYINGS"
    for fragment in ("run 41 (a) trades SPY", "run 42 (b) trades QQQ"):
        assert fragment in detail["message"]


async def test_a_session_the_run_did_not_test_is_a_404_with_its_code_on_both_routes_that_name_one(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored(monkeypatch, a_run_row(41), sessions=(a_skipped_session_row(),))

    tested = await client.get(f"{PREFIX}/runs/41/trades", params={"session_open_ms": FIRST_OPEN_MS})
    trades = await client.get(f"{PREFIX}/runs/41/trades", params={"session_open_ms": ANOTHER_SESSION_MS})
    session = await client.get(f"{PREFIX}/runs/41/sessions/{ANOTHER_SESSION_MS}")

    # The session it did test is an answer, so the 404s are about the session and not about the run.
    assert tested.status_code == 200, tested.text
    for refused in (trades, session):
        assert refused.status_code == 404, refused.text
        assert refused.json()["detail"]["code"] == "OPTIONS_BACKTEST_SESSION_NOT_IN_RUN"
        assert str(ANOTHER_SESSION_MS) in refused.json()["detail"]["message"]


async def test_a_leg_the_setup_does_not_have_is_a_404_with_its_code_and_a_leg_it_has_is_a_page(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored(monkeypatch, a_run_row(41))

    absent = await client.get(f"{PREFIX}/runs/41/trades", params={"leg_id": "nope"})
    present = await client.get(f"{PREFIX}/runs/41/trades", params={"leg_id": "call"})

    assert absent.status_code == 404, absent.text
    assert absent.json()["detail"]["code"] == "OPTIONS_BACKTEST_LEG_NOT_IN_RUN"
    assert "'nope'" in absent.json()["detail"]["message"]
    assert present.status_code == 200, present.text
    assert (present.json()["total"], present.json()["trades"]) == (0, [])


async def test_a_leg_id_is_answered_from_the_stored_trades_alone_when_the_stored_setup_can_no_longer_be_read(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored(monkeypatch, a_run_row(41, setup_json=_a_setup_that_can_no_longer_be_read()))

    response = await client.get(f"{PREFIX}/runs/41/trades", params={"leg_id": "nope"})

    # With the setup gone the run cannot say which legs it had: nothing stored is nothing stored, and no 404.
    assert response.status_code == 200, response.text
    assert (response.json()["total"], response.json()["trades"]) == (0, [])


async def test_a_stored_session_whose_events_disagree_with_its_trades_is_a_500_with_a_code_and_another_session_opens(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    contract = OptionContract.of(root="SPY", expiry_date=date(2026, 10, 9), right="call", strike=Decimal("500"))
    # The call leg took a starting price and has no trade, no cancel and no refusal: it waited and never ended, which the
    # replay cannot make.
    starting_price = {
        "kind": "starting_price_taken",
        "at_ms": FIRST_OPEN_MS + 5 * 60_000,
        "leg_id": "call",
        "entry_number": 0,
        "contract": contract.occ_symbol,
        "price": "4.00",
        "reason": None,
        "detail": None,
    }
    broken = {**a_skipped_session_row(FIRST_OPEN_MS), "events_json": json.dumps([starting_price])}
    _stored(monkeypatch, a_run_row(41), sessions=(broken, a_skipped_session_row(ANOTHER_SESSION_MS)))

    response = await client.get(f"{PREFIX}/runs/41/sessions/{FIRST_OPEN_MS}")
    other = await client.get(f"{PREFIX}/runs/41/sessions/{ANOTHER_SESSION_MS}")

    assert response.status_code == 500, response.text
    assert response.json()["detail"]["code"] == "OPTIONS_BACKTEST_STORED_SESSION_INCONSISTENT"
    assert "leg 'call', entry 0" in response.json()["detail"]["message"]
    assert other.status_code == 200, other.text
