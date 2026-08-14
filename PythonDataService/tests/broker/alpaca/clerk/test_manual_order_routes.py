"""HTTP contract tests for the SQLite-owned manual market-order tracer."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.models import ChannelHealth
from app.broker.alpaca.clerk.sqlite.custody_subjects import manual_operator_subject_id
from app.broker.alpaca.clerk.sqlite.facts import ExecutionSliceFilledFacts
from app.broker.alpaca.clerk.sqlite.manual_orders import ManualTicketLeg, submit_manual_order
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.stream_health import StreamHealthGate
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAccountSnapshot, BrokerAsset, BrokerOrder, BrokerOrderLeg
from app.config import settings
from app.routers.brokers import router
from app.security.data_plane_control import CONTROL_SECRET_HEADER

ACCOUNT_ID = "PA-MANUAL-ROUTE"
TICKET_ID = "7de3a77c-b698-4e0d-a5d1-2f624574ed35"
LEG_ID = "09d6d63e-6375-4e6d-8d20-3b1bf70c2465"
SECOND_LEG_ID = "5791929d-4a3f-4ffc-a15f-62c34cb6c873"


def _account(*, account_mode: str = "paper") -> BrokerAccountSnapshot:
    return BrokerAccountSnapshot(
        broker="alpaca",
        account_id=ACCOUNT_ID,
        account_mode=account_mode,
        account_status="ACTIVE",
        currency="USD",
        cash=1_000,
        equity=1_000,
        buying_power=2_000,
        portfolio_value=1_000,
        long_market_value=0,
        short_market_value=0,
        pattern_day_trader=False,
        trading_blocked=False,
        account_blocked=False,
        created_at_ms=None,
        observed_at_ms=1_700_000_000_000,
    )


def _asset() -> BrokerAsset:
    return BrokerAsset(
        broker="alpaca",
        asset_id="asset-spy",
        symbol="SPY",
        name="SPDR S&P 500 ETF Trust",
        asset_class="us_equity",
        exchange="NYSE",
        status="active",
        tradable=True,
        fractionable=True,
        shortable=True,
        marginable=True,
    )


class FakeAlpacaPort:
    broker_id = "alpaca"

    def __init__(self, *, repo: ClerkSqliteRepository, account_mode: str = "paper") -> None:
        self.repo = repo
        self.account_mode = account_mode
        self.account_calls = 0
        self.asset_available = True
        self.asset_lookup_calls: list[str] = []
        self.asset_list_calls = 0
        self.submit_calls: list[str] = []
        self.cancel_calls: list[str] = []
        self.orders: dict[str, BrokerOrder] = {}
        self.durable_before_submit = False
        self.cancel_unavailable = False

    async def get_account(self) -> BrokerAccountSnapshot:
        self.account_calls += 1
        return _account(account_mode=self.account_mode)

    async def list_assets(
        self,
        *,
        status: str | None = None,
        limit: int | None = 100,
    ) -> list[BrokerAsset]:
        self.asset_list_calls += 1
        if not self.asset_available:
            return []
        assert status == "active"
        return [_asset()]

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        self.asset_lookup_calls.append(symbol)
        if not self.asset_available:
            return None
        return _asset()

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submit_calls.append(client_order_id)
        ticket = self.repo.manual_order_ticket(TICKET_ID)
        self.durable_before_submit = bool(
            ticket and ticket.legs[0].order_ref == client_order_id and ticket.legs[0].effect_operation_id is not None
        )
        order = BrokerOrder(
            broker="alpaca",
            order_id=f"broker-manual-{len(self.submit_calls)}",
            client_order_id=client_order_id,
            symbol=leg.symbol,
            asset_class="us_equity",
            side=leg.side.value,
            order_type=leg.order_type.value,
            time_in_force=leg.time_in_force.value,
            quantity=leg.quantity,
            filled_quantity=0,
            limit_price=None,
            stop_price=None,
            filled_avg_price=None,
            status="accepted",
            submitted_at_ms=1_700_000_000_100,
            created_at_ms=1_700_000_000_100,
            updated_at_ms=1_700_000_000_100,
            filled_at_ms=None,
            canceled_at_ms=None,
            expired_at_ms=None,
            events=[],
            observed_at_ms=1_700_000_000_100,
        )
        self.orders[client_order_id] = order
        return order

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)
        if self.cancel_unavailable:
            raise BrokerUnavailable("cancel transport unavailable")
        for client_order_id, order in self.orders.items():
            if order.order_id == order_id:
                self.orders[client_order_id] = order.model_copy(
                    update={
                        "status": "canceled",
                        "canceled_at_ms": 1_700_000_000_200,
                        "updated_at_ms": 1_700_000_000_200,
                        "observed_at_ms": 1_700_000_000_200,
                    }
                )
                return
        raise AssertionError(f"unknown broker order {order_id!r}")

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self.orders.get(client_order_id)


@pytest.fixture()
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "ALPACA_SQLITE_MANUAL_TRADING_ENABLED", True)
    monkeypatch.setattr(settings, "DATA_PLANE_CONTROL_SECRET", "manual-route-secret")
    monkeypatch.setattr(settings, "DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL", False)
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path)
    port = FakeAlpacaPort(repo=repo)
    health = {"market_data": True, "execution": True}
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=port,
        trade=port,
        stream_health=StreamHealthGate(
            market_data=lambda: ChannelHealth(
                stream="market_data",
                healthy=health["market_data"],
                reason="" if health["market_data"] else "test market-data outage",
                observed_at_ms=10,
            ),
            execution=lambda: ChannelHealth(
                stream="execution",
                healthy=health["execution"],
                reason="" if health["execution"] else "test execution outage",
                observed_at_ms=11,
            ),
        ),
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app, repo, port, health
    finally:
        set_active_clerk_runtime(None)
        repo.close()


def _headers() -> dict[str, str]:
    return {CONTROL_SECRET_HEADER: "manual-route-secret"}


def _preview_payload(*, quantity: float = 1) -> dict[str, object]:
    return {
        "ticket_id": TICKET_ID,
        "legs": [
            {
                "leg_id": LEG_ID,
                "instruction": {"symbol": "SPY", "side": "buy", "quantity": quantity},
            }
        ],
    }


def _two_leg_preview_payload() -> dict[str, object]:
    return {
        "ticket_id": TICKET_ID,
        "legs": [
            {
                "leg_id": LEG_ID,
                "instruction": {"symbol": "SPY", "side": "buy", "quantity": 1},
            },
            {
                "leg_id": SECOND_LEG_ID,
                "instruction": {
                    "symbol": "SPY",
                    "side": "buy",
                    "quantity": 2,
                    "order_type": "limit",
                    "limit_price": 499.5,
                    "time_in_force": "gtc",
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_disabled_manual_capability_refuses_without_contacting_alpaca(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _repo, port, _health = api
    monkeypatch.setattr(settings, "ALPACA_SQLITE_MANUAL_TRADING_ENABLED", False)
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/capability",
            headers=_headers(),
        )

    assert response.status_code == 200
    assert response.json() == {
        "available": False,
        "unavailable": {
            "code": "MANUAL_TRADING_NOT_QUALIFIED",
            "message": "Manual SQLite trading remains disabled until paper qualification is complete.",
        },
        "supported_order_shape": "BUY or SELL market/limit DAY/GTC equity, one to eight ordered legs",
    }
    assert port.account_calls == 0
    assert port.asset_lookup_calls == []
    assert port.asset_list_calls == 0


@pytest.mark.asyncio
async def test_manual_ticket_preview_submit_replay_and_read_are_durable(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, _repo, port, _health = api
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"
    ticket_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-order-tickets/{TICKET_ID}"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        capability = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/capability",
            headers=_headers(),
        )
        rejected_operator = await client.post(
            preview_path,
            headers=_headers(),
            json={**_preview_payload(), "operator": "browser-spoof"},
        )
        limit_ticket = await client.post(
            preview_path,
            headers=_headers(),
            json={
                **_preview_payload(),
                "legs": [
                    {
                        "leg_id": LEG_ID,
                        "instruction": {
                            "symbol": "SPY",
                            "side": "buy",
                            "quantity": 1,
                            "order_type": "limit",
                            "limit_price": 500,
                            "time_in_force": "gtc",
                        },
                    }
                ],
            },
        )
        preview = await client.post(preview_path, headers=_headers(), json=_preview_payload())
        token = preview.json()["preview_token"]
        submit_body = {"legs": _preview_payload()["legs"], "preview_token": token}
        submitted = await client.put(ticket_path, headers=_headers(), json=submit_body)
        replayed = await client.put(ticket_path, headers=_headers(), json=submit_body)
        restored = await client.get(ticket_path, headers=_headers())
        conflict = await client.put(
            ticket_path,
            headers=_headers(),
            json={"legs": _preview_payload(quantity=2)["legs"], "preview_token": token},
        )
        cancel_path = (
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/"
            f"{submitted.json()['legs'][0]['order']['order_ref']}/cancel"
        )
        cancelled = await client.post(
            cancel_path,
            headers=_headers(),
            json={"cancel_request_id": "7d524bb7-3a8c-4b45-8c8e-e009654d3035"},
        )
        cancel_replay = await client.post(
            cancel_path,
            headers=_headers(),
            json={"cancel_request_id": "7d524bb7-3a8c-4b45-8c8e-e009654d3035"},
        )
        restored_after_cancel = await client.get(ticket_path, headers=_headers())
        generic = await client.post(
            "/api/brokers/alpaca/orders",
            headers=_headers(),
            json={
                "operator": "desk",
                "expected_account_id": ACCOUNT_ID,
                "legs": [{"symbol": "SPY", "side": "buy", "quantity": 1}],
            },
        )

    assert capability.status_code == 200
    assert capability.json()["available"] is True
    assert rejected_operator.status_code == 422
    assert limit_ticket.status_code == 200
    assert limit_ticket.json()["capability"]["available"] is True
    assert preview.status_code == 200
    assert preview.json()["capability"]["available"] is True
    assert submitted.status_code == replayed.status_code == 202
    assert restored.status_code == 200
    assert submitted.json()["subject_id"] == manual_operator_subject_id("operator")
    assert submitted.json()["legs"][0]["state"] == "IN_PROGRESS"
    assert submitted.json()["legs"][0]["order"]["client_order_id"].startswith("manual/operator/v1:")
    assert port.durable_before_submit is True
    assert len(port.submit_calls) == 1
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["reason"] == "manual_ticket_conflict"
    assert generic.status_code == 409
    assert cancelled.status_code == cancel_replay.status_code == 202
    assert cancelled.json()["state"] == "SUCCEEDED"
    assert cancelled.json()["effect"]["kind"] == "CANCEL"
    assert restored_after_cancel.json()["legs"][0]["cancellation"]["state"] == "SUCCEEDED"
    assert len(port.cancel_calls) == 1


@pytest.mark.asyncio
async def test_manual_ticket_continue_requires_a_fresh_preview_and_activates_one_next_leg(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, _repo, port, _health = api
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"
    ticket_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-order-tickets/{TICKET_ID}"
    continuation_path = f"{ticket_path}/continue"
    payload = _two_leg_preview_payload()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        initial_preview = await client.post(preview_path, headers=_headers(), json=payload)
        initial = await client.put(
            ticket_path,
            headers=_headers(),
            json={"legs": payload["legs"], "preview_token": initial_preview.json()["preview_token"]},
        )
        stale_continue = await client.post(
            continuation_path,
            headers=_headers(),
            json={"legs": payload["legs"], "preview_token": initial_preview.json()["preview_token"]},
        )
        refreshed_preview = await client.post(preview_path, headers=_headers(), json=payload)
        continued = await client.post(
            continuation_path,
            headers=_headers(),
            json={"legs": payload["legs"], "preview_token": refreshed_preview.json()["preview_token"]},
        )
        cancelled = await client.post(
            f"{ticket_path}/cancel",
            headers=_headers(),
            json={"cancel_request_id": "d38e6b15-9f4f-47a4-88fd-2e5c91031eb0"},
        )

    assert initial.status_code == 202
    assert [leg["state"] for leg in initial.json()["legs"]] == ["IN_PROGRESS", "RESERVED"]
    assert stale_continue.status_code == 409
    assert stale_continue.json()["detail"]["reason"] == "manual_preview_stale"
    assert continued.status_code == 202
    assert [leg["state"] for leg in continued.json()["legs"]] == ["IN_PROGRESS", "IN_PROGRESS"]
    assert len(port.submit_calls) == 2
    assert cancelled.status_code == 202
    assert [leg["state"] for leg in cancelled.json()["legs"]] == ["CANCELED", "CANCELED"]
    assert len(port.cancel_calls) == 2


@pytest.mark.asyncio
async def test_manual_ticket_continuation_rejects_a_prior_leg_that_stales_after_preview(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, repo, port, _health = api
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"
    ticket_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-order-tickets/{TICKET_ID}"
    continuation_path = f"{ticket_path}/continue"
    payload = _two_leg_preview_payload()

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        initial_preview = await client.post(preview_path, headers=_headers(), json=payload)
        initial = await client.put(
            ticket_path,
            headers=_headers(),
            json={"legs": payload["legs"], "preview_token": initial_preview.json()["preview_token"]},
        )
        refreshed_preview = await client.post(preview_path, headers=_headers(), json=payload)
        repo._conn.execute(
            "UPDATE manual_order_legs SET state = 'ACCEPTED' WHERE ticket_id = ? AND sequence_index = 0",
            (TICKET_ID,),
        )
        repo._conn.commit()
        continued = await client.post(
            continuation_path,
            headers=_headers(),
            json={"legs": payload["legs"], "preview_token": refreshed_preview.json()["preview_token"]},
        )

    assert initial.status_code == 202
    assert refreshed_preview.status_code == 200
    assert refreshed_preview.json()["capability"]["available"] is True
    assert continued.status_code == 409
    assert continued.json()["detail"]["reason"] == "manual_preview_stale"
    assert len(port.submit_calls) == 1


@pytest.mark.asyncio
async def test_manual_ticket_continuation_preview_checks_only_the_next_reserved_leg(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, repo, port, _health = api
    seeded = await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="operator",
        ticket_id="b5d667d3-6820-4c60-927a-2130f3c02aaf",
        leg_id="91cd2b42-12b1-4c04-9caa-ffdfc346fbc2",
        leg=BrokerOrderLeg(symbol="SPY", side="buy", quantity=2),
        trade=port,
    )
    assert seeded.leg.effect_operation_id is not None and seeded.leg.order_ref is not None
    fill = ExecutionSliceFilledFacts(
        execution_id="manual-owned-long-for-preview",
        symbol="SPY",
        side="BUY",
        slice_qty=2,
        slice_price=500,
        fee=None,
        fee_fidelity="not_reported",
        evidence_source="websocket",
        source_event_at_ms=1_700_000_000_200,
    )
    repo.append_transition(
        TransitionInput(
            command_id=seeded.command.command_id,
            effect_operation_id=seeded.leg.effect_operation_id,
            order_ref=seeded.leg.order_ref,
            transition_kind="EXECUTION_SLICE_FILLED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="in_progress",
            source_event_at_ms=fill.source_event_at_ms,
            clerk_observed_at_ms=repo.clock(),
            summary_code="EXECUTION_SLICE_FILLED",
            facts_json=fill.to_facts_json(),
        )
    )
    ticket_id = "c721e10b-9c80-4caf-bacf-c3cd988aa539"
    first_leg = ManualTicketLeg(
        leg_id="3bfe6f0b-7ee2-4a12-ba54-2e3eb2e5522f",
        instruction=BrokerOrderLeg(symbol="SPY", side="sell", quantity=1.5),
    )
    second_leg = ManualTicketLeg(
        leg_id="65a12102-c7c7-4350-8648-7f9dc9377d4a",
        instruction=BrokerOrderLeg(symbol="SPY", side="sell", quantity=0.5),
    )
    legs = (first_leg, second_leg)
    await submit_manual_order(
        repo,
        account_id=ACCOUNT_ID,
        operator_id="operator",
        ticket_id=ticket_id,
        leg_id=first_leg.leg_id,
        leg=first_leg.instruction,
        ticket_legs=legs,
        trade=port,
    )
    assert repo.manual_reduction_available_quantity(
        subject_id=manual_operator_subject_id("operator"), symbol="SPY"
    ) == pytest.approx(0.5, abs=1e-9, rel=0)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview",
            headers=_headers(),
            json={
                "ticket_id": ticket_id,
                "legs": [
                    {"leg_id": leg.leg_id, "instruction": leg.instruction.model_dump(mode="json")} for leg in legs
                ],
            },
        )

    assert preview.status_code == 200
    assert preview.json()["capability"]["available"] is True


@pytest.mark.asyncio
async def test_manual_cancel_unknown_response_uses_server_authored_recovery_copy(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, _repo, port, _health = api
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"
    ticket_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-order-tickets/{TICKET_ID}"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview = await client.post(preview_path, headers=_headers(), json=_preview_payload())
        submitted = await client.put(
            ticket_path,
            headers=_headers(),
            json={"legs": _preview_payload()["legs"], "preview_token": preview.json()["preview_token"]},
        )
        order_ref = submitted.json()["legs"][0]["order"]["order_ref"]
        port.cancel_unavailable = True
        unknown = await client.post(
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/{order_ref}/cancel",
            headers=_headers(),
            json={"cancel_request_id": "d40f1aeb-263c-4a57-8583-0e48f1d9298b"},
        )
        restored = await client.get(ticket_path, headers=_headers())

    assert unknown.status_code == 202
    assert unknown.json()["state"] == "UNKNOWN"
    assert unknown.json()["message"] == "The cancellation outcome is not yet known."
    assert "Refresh this ticket" in unknown.json()["next_action"]
    assert restored.json()["legs"][0]["cancellation"]["message"] == unknown.json()["message"]


@pytest.mark.asyncio
async def test_manual_preview_is_fresh_and_live_accounts_are_refused(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, repo, port, health = api
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"
    ticket_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-order-tickets/{TICKET_ID}"
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview = await client.post(preview_path, headers=_headers(), json=_preview_payload())
        repo.register_strategy_instance(strategy_instance_id="freshness-change", symbol="SPY", config_hash="h")
        stale = await client.put(
            ticket_path,
            headers=_headers(),
            json={"legs": _preview_payload()["legs"], "preview_token": preview.json()["preview_token"]},
        )
        port.account_mode = "live"
        capability = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/capability",
            headers=_headers(),
        )

    assert stale.status_code == 409
    assert stale.json()["detail"]["reason"] == "manual_preview_stale"
    assert port.submit_calls == []
    assert capability.status_code == 200
    assert capability.json()["unavailable"]["code"] == "LIVE_ACCOUNT_REFUSED"
    port.account_mode = "paper"
    health["execution"] = False
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        unhealthy = await client.get(
            f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/capability",
            headers=_headers(),
        )
    assert unhealthy.status_code == 200
    assert unhealthy.json()["unavailable"]["code"] == "BROKER_CHANNEL_UNHEALTHY"


@pytest.mark.asyncio
async def test_manual_preview_refuses_an_unlisted_asset_before_durable_acceptance(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, repo, port, _health = api
    port.asset_available = False
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview = await client.post(preview_path, headers=_headers(), json=_preview_payload())

    assert preview.status_code == 200
    assert preview.json()["capability"]["unavailable"]["code"] == "UNSUPPORTED_MANUAL_ASSET"
    assert repo.manual_order_ticket(TICKET_ID) is None
    assert port.submit_calls == []
    assert port.asset_lookup_calls == ["SPY"]
    assert port.asset_list_calls == 0


@pytest.mark.asyncio
async def test_manual_ticket_reads_and_capability_require_control_authentication(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
) -> None:
    app, _repo, _port, _health = api
    capability_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/capability"
    ticket_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-order-tickets/{TICKET_ID}"

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        capability = await client.get(capability_path)
        ticket = await client.get(ticket_path)

    assert capability.status_code == ticket.status_code == 403


@pytest.mark.asyncio
async def test_invalid_configured_manual_operator_refuses_before_ticket_reservation(
    api: tuple[FastAPI, ClerkSqliteRepository, FakeAlpacaPort, dict[str, bool]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, repo, port, _health = api
    monkeypatch.setattr(settings, "PANEL_OPERATOR_IDENTITY", "invalid operator")
    preview_path = f"/api/brokers/alpaca/accounts/{ACCOUNT_ID}/manual-orders/preview"

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        preview = await client.post(preview_path, headers=_headers(), json=_preview_payload())

    assert preview.status_code == 200
    assert preview.json()["capability"]["unavailable"]["code"] == "INVALID_MANUAL_OPERATOR"
    assert repo.manual_order_ticket(TICKET_ID) is None
    assert port.asset_lookup_calls == []
    assert port.asset_list_calls == 0
