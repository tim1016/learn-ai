"""``GET /api/broker-clerks/aggregate/bot-history`` — every account's bots, one page (#2574).

The coordinator reads each alpaca lane's ``bot_history_read`` for the
account the lane serves and merges the rows, newest first, keeping each
row's lane and account. An account it could not read -- the lane failed, or
serves no account yet -- and each Dry Run a lane could not read itself are
named gaps, never missing rows.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.broker.alpaca.clerk.fleet_adapter import AlpacaProviderAdapter
from app.broker.fleet.delivery import DeliveryRequest
from app.broker.fleet.errors import ClerkUnreachable
from app.broker.fleet.routing import LaneRouter, RoutedDelivery
from app.broker.fleet.service import FleetControlService
from app.broker.fleet.store import FleetRegistryStore
from app.schemas.bot_history import AccountBotHistory, BotHistoryBot, BotHistoryGap, BotHistoryOrders
from app.security.data_plane_control import CONTROL_SECRET_HEADER
from app.utils.error_handlers import install_fleet_control_error_handler
from tests.broker.fleet.conftest import bind_lane, provision_lane

_ROUTE = "/api/broker-clerks/aggregate/bot-history"
_TEST_SECRET = "test-aggregate-bot-history-secret"


class _ScriptedDelivery:
    """One lane's agent answering a canned history read (or failing)."""

    def __init__(
        self, *, answer: dict | None = None, fail_with: Exception | None = None, status_code: int = 200,
    ) -> None:
        self._answer = answer
        self._fail_with = fail_with
        self._status_code = status_code
        self.requests: list[DeliveryRequest] = []

    async def deliver(self, request: DeliveryRequest) -> RoutedDelivery:
        self.requests.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return RoutedDelivery(
            status_code=self._status_code, headers={}, body=json.dumps(self._answer).encode("utf-8"),
        )


def _bot(sid: str, *, account: str, started_at_ms: int, status: str = "finished", symbol: str = "SPY") -> BotHistoryBot:
    return BotHistoryBot(
        strategy_instance_id=sid, strategy_key="ema_crossover", strategy_label="EMA crossover", symbol=symbol,
        account_id=account, world="paper", world_label="PAPER · practice money", status=status,  # type: ignore[arg-type]
        status_label=status.title(), started_at_ms=started_at_ms, stopped_at_ms=started_at_ms + 60_000,
        outcome=None, transaction_count=2, orders=BotHistoryOrders(sent=1, filled=1, cancelled=0, rejected=0),
        budget_usd="150.00", result_usd="9.99", fees_usd="0.01", money_unavailable_reason=None,
        money_scope_note=None, runs=(), page_unavailable_reason=None,
    )


def _history(account: str, *bots: BotHistoryBot, gaps: tuple[BotHistoryGap, ...] = ()) -> dict:
    return AccountBotHistory(
        account_id=account, observed_at_ms=1_000, bots=bots, gaps=gaps,
    ).model_dump(mode="json")


@pytest.fixture
def secret(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", _TEST_SECRET)
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    return {CONTROL_SECRET_HEADER: _TEST_SECRET}


def _app(service: FleetControlService, deliveries: dict[str, _ScriptedDelivery]) -> FastAPI:
    from app.routers import broker_clerks

    app = FastAPI()
    app.state.fleet_service = service
    install_fleet_control_error_handler(app)
    app.state.fleet_lane_router = LaneRouter(
        service=service, delivery_for=lambda broker, session: deliveries[session.clerk_id],
    )
    app.include_router(broker_clerks.router)
    return app


@pytest.fixture
def fleet(tmp_path: Path):
    """Three alpaca lanes: Paper answers, Live's read fails, a third has no account."""
    service = FleetControlService(
        store=FleetRegistryStore.open(control_dir=tmp_path / "control"),
        provider_adapters={"alpaca": AlpacaProviderAdapter()},
    )
    paper = provision_lane(service, broker="alpaca", label="history-paper", tmp_path=tmp_path)
    bind_lane(service, paper, account="acct-paper")
    live = provision_lane(service, broker="alpaca", label="history-live", tmp_path=tmp_path)
    bind_lane(service, live, account="acct-live")
    unbound = provision_lane(service, broker="alpaca", label="history-unbound", tmp_path=tmp_path)
    deliveries = {
        paper.clerk_id: _ScriptedDelivery(answer=_history(
            "acct-paper",
            _bot("older", account="acct-paper", started_at_ms=10_000, status="cleared", symbol="QQQ"),
            _bot("newer", account="acct-paper", started_at_ms=20_000, status="running"),
            gaps=(BotHistoryGap(strategy_instance_id="spy-dry-1", reason="This Dry Run's own records could not be read, so it is not listed."),),
        )),
        live.clerk_id: _ScriptedDelivery(fail_with=ClerkUnreachable("agent timed out reading history")),
    }
    return service, deliveries, paper, live, unbound


@pytest.mark.asyncio
async def test_every_account_is_listed_newest_first_and_each_unread_one_is_named(fleet, secret) -> None:
    service, deliveries, paper, live, unbound = fleet

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        response = await client.get(_ROUTE, headers=secret)

    assert response.status_code == 200
    body = response.json()
    assert [row["strategy_instance_id"] for row in body["rows"]] == ["newer", "older"]
    assert {row["clerk_id"] for row in body["rows"]} == {paper.clerk_id}
    assert body["rows"][0]["result_usd"] == "9.99"  # the lane's own dollars, untouched
    gaps = {(gap["clerk_id"], gap["strategy_instance_id"]): gap for gap in body["gaps"]}
    assert gaps[(live.clerk_id, None)]["reason_code"] == "clerk_unreachable"
    assert gaps[(live.clerk_id, None)]["account_id"] == "acct-live"
    assert gaps[(unbound.clerk_id, None)]["reason_code"] == "no_confirmed_account"
    assert gaps[(paper.clerk_id, "spy-dry-1")]["reason"].startswith("This Dry Run's own records")
    assert body["total"] == 2 and body["symbols"] == ["QQQ", "SPY"]
    # The Paper lane was asked for the account it serves.
    (request,) = deliveries[paper.clerk_id].requests
    assert request.agent_path() == "/api/brokers/alpaca/accounts/acct-paper/bot-history"


@pytest.mark.asyncio
async def test_filters_and_pages_narrow_rows_but_never_hide_a_gap(fleet, secret) -> None:
    service, deliveries, _paper, live, _unbound = fleet
    app = _app(service, deliveries)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        cleared = (await client.get(_ROUTE, params={"status": "cleared"}, headers=secret)).json()
        second = (await client.get(_ROUTE, params={"page": 2, "page_size": 1}, headers=secret)).json()

    assert [row["strategy_instance_id"] for row in cleared["rows"]] == ["older"]
    assert cleared["total"] == 1 and cleared["symbols"] == ["QQQ", "SPY"]
    assert live.clerk_id in {gap["clerk_id"] for gap in cleared["gaps"]}
    assert [row["strategy_instance_id"] for row in second["rows"]] == ["older"]
    assert second["total"] == 2


@pytest.mark.asyncio
async def test_one_account_filter_reads_only_that_account(fleet, secret) -> None:
    service, deliveries, paper, live, _unbound = fleet

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        body = (await client.get(_ROUTE, params={"clerk_id": paper.clerk_id}, headers=secret)).json()

    assert deliveries[live.clerk_id].requests == []
    assert {gap["clerk_id"] for gap in body["gaps"]} == {paper.clerk_id}
    assert body["total"] == 2


@pytest.mark.asyncio
async def test_one_bot_is_asked_of_each_lane_and_held_here_too(fleet, secret) -> None:
    """A bot's own page opens History on that bot: each lane is asked for it
    alone, and a lane that answers with more (an older build) is narrowed here."""
    service, deliveries, paper, _live, _unbound = fleet

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        body = (await client.get(_ROUTE, params={"strategy_instance_id": "older"}, headers=secret)).json()

    (request,) = deliveries[paper.clerk_id].requests
    assert dict(request.query) == {"strategy_instance_id": "older"}
    assert [row["strategy_instance_id"] for row in body["rows"]] == ["older"]
    assert body["total"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "body", "reason"),
    [
        (
            404,
            {"detail": {"message": "Account 'acct-live' is not this Clerk's account.", "why": "It serves another.",
                        "next_action": None}},
            "Account 'acct-live' is not this Clerk's account. It serves another.",
        ),
        (404, {"detail": "Bot history is available on Alpaca accounts."}, "Bot history is available on Alpaca accounts."),
        (422, {"detail": [{"loc": ["query", "strategy_instance_id"], "msg": "too long"}]},
         "This account's Clerk refused the read (422)."),
    ],
)
async def test_a_lane_that_refuses_its_read_is_named_in_its_clerks_own_words(
    fleet, secret, status_code: int, body: dict, reason: str,
) -> None:
    """The lane answered, so it is not unreachable: its gap carries the
    Clerk's own words and a code of its own."""
    service, deliveries, _paper, live, _unbound = fleet
    deliveries[live.clerk_id] = _ScriptedDelivery(answer=body, status_code=status_code)

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        page = (await client.get(_ROUTE, headers=secret)).json()

    (gap,) = (gap for gap in page["gaps"] if gap["clerk_id"] == live.clerk_id)
    assert (gap["reason_code"], gap["reason"], gap["account_id"]) == ("lane_refused_read", reason, "acct-live")


@pytest.mark.asyncio
async def test_a_lane_server_error_keeps_its_body_in_the_log_and_is_named_unreachable(fleet, secret) -> None:
    """A lane's 5xx body may carry internal detail, so the lane router keeps
    it in the coordinator log (#2164); the account is still named."""
    service, deliveries, _paper, live, _unbound = fleet
    deliveries[live.clerk_id] = _ScriptedDelivery(answer={"detail": {"message": "/srv/secret/path"}}, status_code=503)

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        page = (await client.get(_ROUTE, headers=secret)).json()

    (gap,) = (gap for gap in page["gaps"] if gap["clerk_id"] == live.clerk_id)
    assert gap["reason_code"] == "clerk_unreachable"
    assert "/srv/secret/path" not in gap["reason"]


@pytest.mark.asyncio
async def test_a_lane_answering_something_that_is_not_a_bot_history_is_named_unreadable(fleet, secret) -> None:
    service, deliveries, _paper, live, _unbound = fleet
    deliveries[live.clerk_id] = _ScriptedDelivery(answer={"bots": "not a list"})

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        page = (await client.get(_ROUTE, headers=secret)).json()

    (gap,) = (gap for gap in page["gaps"] if gap["clerk_id"] == live.clerk_id)
    assert (gap["reason_code"], gap["account_id"]) == ("unreadable_answer", "acct-live")
    assert gap["reason"] == "This account's bots could not be read right now. Refresh to try again."


@pytest.mark.asyncio
async def test_an_account_filter_naming_no_lane_is_a_named_gap_never_an_empty_list(fleet, secret) -> None:
    """#2615: an unknown account answered an empty page with no gap, which
    reads as an account with no bots."""
    service, deliveries, paper, live, _unbound = fleet

    async with AsyncClient(transport=ASGITransport(app=_app(service, deliveries)), base_url="http://test") as client:
        page = (await client.get(_ROUTE, params={"clerk_id": "clrk_nobody"}, headers=secret)).json()

    assert page["rows"] == [] and page["total"] == 0
    assert [(gap["clerk_id"], gap["account_id"], gap["reason_code"]) for gap in page["gaps"]] == [
        ("clrk_nobody", None, "unknown_account"),
    ]
    assert deliveries[paper.clerk_id].requests == [] and deliveries[live.clerk_id].requests == []


def test_the_history_read_outlasts_the_fleet_default_and_the_fan_out_outlasts_it() -> None:
    """One account's history reads every Dry Run's own database; the
    coordinator waits a little longer than the lane read, so a slow lane's own
    named failure arrives before the fan-out gives up on it."""
    from app.broker.alpaca.clerk.fleet_adapter import ALPACA_OPERATIONS
    from app.broker.fleet.internal_http import BOT_HISTORY_READ_TIMEOUT_S, DEFAULT_INTERNAL_TIMEOUT_S

    (operation,) = (op for op in ALPACA_OPERATIONS if op.operation_id == "bot_history_read")
    assert operation.read_timeout_s == BOT_HISTORY_READ_TIMEOUT_S > DEFAULT_INTERNAL_TIMEOUT_S
    assert operation.requires_effective_account
