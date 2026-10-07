"""The route that acknowledges an option-event notice, on a Paper account's real Clerk (#2913).

The Clerk, its books, its reconciliation pass and its poll of option events
are the real ones. Only Alpaca is a double. Nothing was sent to Alpaca.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite import option_events
from app.broker.alpaca.clerk.sqlite.facts import UncertaintyResolvedFacts
from app.routers import alpaca_clerk_sqlite
from app.routers.alpaca_clerk_sqlite import router
from app.utils.error_handlers import request_validation_exception_handler
from app.utils.session_anchors import et_date_at_ms
from tests.broker.alpaca.clerk.sqlite.conftest import documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import option_holds
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (  # noqa: F401
    SYMBOL,
    PaperOptions,
    paper,
)

UNREADABLE = "OPTION_EVENT_UNREADABLE"


@pytest.fixture
async def api(paper: PaperOptions) -> AsyncIterator[tuple[FastAPI, PaperOptions]]:  # noqa: F811
    """The Clerk's routes over a Paper account whose bot ``a`` holds one call."""
    order_ref = await paper.resting("a")
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-a-buy")
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=paper.clerk))
    app = FastAPI()
    app.include_router(router)
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    try:
        yield app, paper
    finally:
        set_active_clerk_runtime(None)


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _a_row_the_books_cannot_read(account: PaperOptions, symbol: str = SYMBOL) -> None:
    """Alpaca reports an option activity for half a contract of ``symbol``. A pass reads it and holds for it."""
    account.account.option_events.rows = [documented_option_activity(
        "trading_api", "otm_option_expiry", id="exp-1", symbol=symbol,
        date=et_date_at_ms(account.clock()).isoformat(), qty="-0.5",
    )]
    account.wait(option_events.OPTION_EVENT_POLL_RETRY_MS)
    await account.pass_()


def _as_the_page_read_it(account: PaperOptions, bot: str | None, **changes: Any) -> dict[str, Any]:
    """The acknowledgement a page sends for the open notice: the notice's own id, time and evidence refs."""
    notice = option_holds(account.repo)[(bot, UNREADABLE)]
    return {
        "reason_code": UNREADABLE, "uncertainty_id": notice["uncertainty_id"],
        "observed_at_ms": notice["observed_at_ms"], "item_refs": ["option-activity:exp-1"], **changes,
    }


def _route(account: PaperOptions, bot: str | None) -> str:
    base = f"/api/alpaca-clerk-sqlite/accounts/{account.repo.account_id}"
    return f"{base}/option-event-holds/acknowledge" if bot is None else f"{base}/bots/{bot}/option-event-holds/acknowledge"


async def test_a_bots_notice_is_acknowledged_as_the_configured_operator_once_the_books_agree_with_the_broker(
    api: tuple[FastAPI, PaperOptions], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Who acknowledged is the installation's operator identity, never a request field."""
    app, account = api
    monkeypatch.setattr(alpaca_clerk_sqlite.settings, "PANEL_OPERATOR_IDENTITY", "desk-operator")
    await _a_row_the_books_cannot_read(account)
    request = _as_the_page_read_it(account, "a")
    written = account.repo.last_custody_sequence()

    async with _client(app) as client:
        named = await client.post(_route(account, "a"), json={**request, "operator": "someone-else"})
        assert account.repo.last_custody_sequence() == written
        answered = await client.post(_route(account, "a"), json=request)
        recorded_at = account.repo.last_custody_sequence()
        again = await client.post(_route(account, "a"), json=request)

    assert named.status_code == 422, "the operator is never a request field"
    assert answered.status_code == 200, answered.text
    receipt = answered.json()
    assert (receipt["strategy_instance_id"], receipt["reason_code"], receipt["operator"]) == (
        "a", UNREADABLE, "desk-operator"
    )
    assert receipt["item_refs"] == ["option-activity:exp-1"]
    assert receipt["sequence"] == recorded_at
    assert option_holds(account.repo) == {}
    [resolved] = [
        UncertaintyResolvedFacts.from_facts_json(row[2]) for row in account.log()
        if row[1] == "OPTION_EVENT_HOLD_ACKNOWLEDGED"
    ]
    assert "operator:desk-operator" in resolved.evidence_refs
    # The same request once more finds no notice open: nothing is acknowledged twice.
    assert again.status_code == 409 and again.json()["detail"]["reason_code"] == "OPTION_EVENT_HOLD_NOT_OPEN"


async def test_the_route_refuses_while_the_broker_holds_something_else_and_names_the_symbol(
    api: tuple[FastAPI, PaperOptions],
) -> None:
    """The broker no longer shows the contract and the books still hold it. The pass the route makes sees that."""
    app, account = api
    await _a_row_the_books_cannot_read(account)
    account.account.settled.add(SYMBOL)
    request = _as_the_page_read_it(account, "a")
    written = account.repo.last_custody_sequence()

    async with _client(app) as client:
        refused = await client.post(_route(account, "a"), json=request)

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert (detail["reason"], detail["reason_code"]) == (
        "option_event_hold_not_acknowledged", "OPTION_EVENT_HOLD_POSITIONS_DIFFER"
    )
    assert detail["symbols"] == [SYMBOL] and SYMBOL in detail["message"]
    assert ("a", UNREADABLE) in option_holds(account.repo)
    # The pass the route made wrote what it found. The acknowledgement itself wrote nothing.
    assert "OPTION_EVENT_HOLD_ACKNOWLEDGED" not in {row[1] for row in account.log()}
    assert account.repo.last_custody_sequence() >= written


async def test_a_notice_of_the_accounts_own_is_acknowledged_on_the_accounts_route(
    api: tuple[FastAPI, PaperOptions],
) -> None:
    """The row names a contract no bot holds, on an underlying no bot holds a contract of: the account is held."""
    app, account = api
    await _a_row_the_books_cannot_read(account, symbol="MSFT260918C00300000")
    assert (None, UNREADABLE) in option_holds(account.repo)

    async with _client(app) as client:
        on_the_bots_route = await client.post(_route(account, "a"), json=_as_the_page_read_it(account, None))
        answered = await client.post(_route(account, None), json=_as_the_page_read_it(account, None))

    assert on_the_bots_route.status_code == 409
    assert on_the_bots_route.json()["detail"]["reason_code"] == "OPTION_EVENT_HOLD_NOT_OPEN"
    assert answered.status_code == 200, answered.text
    assert answered.json()["strategy_instance_id"] is None and option_holds(account.repo) == {}


@pytest.mark.parametrize(
    ("changes", "reason_code", "names_now"),
    [
        pytest.param(
            {"item_refs": ["option-activity:another"]}, "OPTION_EVENT_HOLD_NAMES_OTHER_ITEMS", ["option-activity:exp-1"],
            id="the request names another row",
        ),
        pytest.param({"observed_at_ms": 1}, "OPTION_EVENT_HOLD_CHANGED_SINCE_READ", ["option-activity:exp-1"], id="the notice changed since it was read"),
        pytest.param({"reason_code": "OPTION_EXPIRED_NOT_SETTLED"}, "OPTION_EVENT_HOLD_ENDS_ANOTHER_WAY", [], id="a hold that ends another way"),
        pytest.param({"reason_code": "OPTION_ORDER_NOT_OURS"}, "OPTION_EVENT_HOLD_ENDS_ANOTHER_WAY", [], id="the hold for an outside order"),
    ],
)
async def test_the_route_refuses_by_name_and_writes_nothing(
    api: tuple[FastAPI, PaperOptions], changes: dict[str, Any], reason_code: str, names_now: list[str]
) -> None:
    app, account = api
    await _a_row_the_books_cannot_read(account)
    request = _as_the_page_read_it(account, "a", **changes)

    async with _client(app) as client:
        refused = await client.post(_route(account, "a"), json=request)

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert (detail["reason_code"], detail["item_refs"]) == (reason_code, names_now)
    assert ("a", UNREADABLE) in option_holds(account.repo)
    assert "OPTION_EVENT_HOLD_ACKNOWLEDGED" not in {row[1] for row in account.log()}


async def test_a_reason_that_is_no_option_event_hold_is_refused_before_any_route_runs(
    api: tuple[FastAPI, PaperOptions],
) -> None:
    app, account = api
    await _a_row_the_books_cannot_read(account)

    async with _client(app) as client:
        refused = await client.post(
            _route(account, "a"), json=_as_the_page_read_it(account, "a", reason_code="EXIT_NOT_FLAT")
        )

    assert refused.status_code == 422
    assert ("a", UNREADABLE) in option_holds(account.repo)
