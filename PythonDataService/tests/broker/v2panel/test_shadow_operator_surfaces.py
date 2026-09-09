"""Every operator surface reached through a COMPOSED shadow authority (ADR 0059 D2).

The shadow half of slice 4 was unreachable in production: three selectors
upstream of the six gates R14 re-meant still tested ``authority_kind ==
"sqlite"``, so a real shadow boot answered "no authority" and every HTTP
surface 503'd -- the deploy view three lines after it admitted the shadow
world. Nothing caught it because every shadow test called a builder directly
with a hand-made ``ClerkStatus``.

So every test here composes the real thing: ``activate_shadow_clerk_authority``
then ``select_active_clerk_runtime`` against a live broker whose ``submit`` and
``cancel`` raise, installed as the process's active runtime, and then drives the
production caller -- through the ASGI app wherever a route exists.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.broker import ALPACA_LIVE_CAPABILITIES
from app.broker.alpaca.clerk.active_authority import (
    ActiveClerkRuntime,
    activate_shadow_clerk_authority,
    select_active_clerk_runtime,
    set_active_clerk_runtime,
)
from app.broker.contract.capabilities import BrokerCapabilities
from app.broker.contract.models import BrokerAccountSnapshot, BrokerAsset
from app.broker.contract.registry import (
    get_broker_registry,
    reset_broker_registry_for_testing,
)
from app.config import settings
from app.routers.broker_v2_panel import router as panel_router
from app.routers.brokers import router as brokers_router
from app.schemas.run_admission import ProgramBuildAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_account_snapshot import (
    clear_broker_account_snapshot_cache_for_testing,
)
from app.services.broker_v2_panel.panel_data_source import _panel_authority_for_binding
from app.services.broker_v2_panel.panel_projection_service import build_panel
from app.services.broker_v2_panel.sqlite_panel_source import (
    read_sqlite_clerk_status,
    read_sqlite_panel_evidence,
)
from app.services.session_authority import et_minute_of_day_ms
from app.services.sqlite_clerk_compat import (
    active_reconciliation_sweep,
    active_sqlite_facade,
    custody_account_id_for_route,
)
from tests.broker.alpaca.clerk.live_envelope_fixtures import TEST_ENVELOPE_VALUES
from tests.broker.v2panel.conftest import _FakeDeployRegistry
from tests.broker.v2panel.fixtures import fill_entry
from tests.broker.v2panel.test_panel_projection import _MARKET_PULSE

LIVE_ACCT = "9LIVE0001"
SHADOW_ACCT = "shadow:9LIVE0001"
SID = "ema-shadow-1"
# A calendar-derived instant, never a wall-clock read: 10:00 ET on a known
# trading day, so the session window the panel reads is deterministic.
DAY = date(2026, 9, 8)
NOW_MS = et_minute_of_day_ms(DAY, 600)


class _LiveBroker:
    """The live account: real reads, and writes that must never be reached."""

    broker_id = "alpaca"

    def capabilities(self) -> BrokerCapabilities:
        return ALPACA_LIVE_CAPABILITIES

    async def get_account(self) -> BrokerAccountSnapshot:
        return BrokerAccountSnapshot(
            broker="alpaca",
            account_id=LIVE_ACCT,
            account_mode="live",
            account_status="ACTIVE",
            currency="USD",
            cash=100_000.0,
            equity=100_000.0,
            buying_power=200_000.0,
            portfolio_value=100_000.0,
            long_market_value=0.0,
            short_market_value=0.0,
            pattern_day_trader=False,
            trading_blocked=False,
            account_blocked=False,
            created_at_ms=NOW_MS - 1_000,
            observed_at_ms=NOW_MS,
        )

    async def list_orders(self, **_kwargs: Any) -> list:
        return []

    async def list_positions(self) -> list:
        return []

    async def get_asset(self, symbol: str) -> BrokerAsset:
        return BrokerAsset(
            broker="alpaca",
            symbol=symbol,
            asset_class="us_equity",
            tradable=True,
            fractionable=False,
            shortable=False,
            easy_to_borrow=False,
            observed_at_ms=NOW_MS,
        )

    async def submit(self, leg: object, *, client_order_id: str) -> None:
        raise AssertionError("LIVE TRADE PORT WAS REACHED")

    async def cancel(self, order_id: str) -> None:
        raise AssertionError("LIVE CANCEL WAS REACHED")

    async def get_order_by_client_order_id(self, client_order_id: str) -> None:
        return None


def _binding(mode: str = "trade") -> BrokerBotBinding:
    # ``model_construct``: the validating constructor re-runs the sealed
    # program's nested-hash validator, which this projection never reads.
    return BrokerBotBinding.model_construct(
        strategy_instance_id=SID,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol="SPY",
        use_rth=True,
        mode=mode,
        quantity=1,
        action_plan=alpaca_v1_action_plan("SPY"),
        sealed_account_id=SHADOW_ACCT,
        run_id="run-1",
        created_at_ms=NOW_MS - 1_000,
        sealed_program=None,
    )


def _register_instance(facade: object) -> None:
    """Put one registered instance in the shadow authority's own roster."""
    facade.repository.register_strategy_instance(  # type: ignore[attr-defined]
        strategy_instance_id=SID,
        symbol="SPY",
        config_hash="config-1",
        strategy_key="ema_crossover_signal",
        display_name="EMA crossover (shadow)",
        config_json=json.dumps({"mode": "trade", "quantity": 1, "carryover_policy": "FORBID"}),
    )


@pytest.fixture()
async def shadow_app(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[FastAPI, ActiveClerkRuntime]]:
    """One composed shadow authority, installed as the process's active runtime."""
    await activate_shadow_clerk_authority(
        live_account_id=LIVE_ACCT, artifacts_root=tmp_path
    )
    broker = _LiveBroker()
    runtime = await select_active_clerk_runtime(
        read=broker,
        trade=broker,
        artifacts_root=tmp_path,
        live_envelope_values=TEST_ENVELOPE_VALUES,
    )
    assert runtime.authority_kind == "shadow", runtime.startup_failure
    set_active_clerk_runtime(runtime)
    clear_broker_account_snapshot_cache_for_testing()
    reset_broker_registry_for_testing()
    get_broker_registry().register(broker)  # type: ignore[arg-type]
    set_bot_task_registry(_FakeDeployRegistry())  # type: ignore[arg-type]
    app = FastAPI()
    app.include_router(panel_router)
    app.include_router(brokers_router)
    try:
        yield app, runtime
    finally:
        set_bot_task_registry(None)
        set_active_clerk_runtime(None)
        clear_broker_account_snapshot_cache_for_testing()
        reset_broker_registry_for_testing()
        await runtime.close()


async def test_the_deploy_view_is_reachable_over_http_and_offers_shadow(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(a) The Task-6 deploy wire and the Task-10 Shadow card, in production shape."""
    app, _runtime = shadow_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots/deploy"
        )

    assert response.status_code == 200, response.json()
    view = response.json()
    assert view["account_mode"] == "live"
    offered = {mode["mode"]: mode["availability"] for mode in view["execution_modes"]}
    assert offered == {"dry_run": "available", "shadow": "available", "live": "planned"}


async def test_clerk_status_is_reachable_and_reads_the_shadow_custody_identity(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(b) Also I3's proof: the broker answers 9LIVE0001, custody is shadow:9LIVE0001.

    Comparing those two raw ids made the terminal identity-mismatch posture
    fire on every correct shadow boot, which in turn made the custody-world
    relaxation below it unreachable.
    """
    app, _runtime = shadow_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/brokers/alpaca/clerk/status")

    assert response.status_code == 200, response.json()
    body = response.json()
    assert body["account_id"] == SHADOW_ACCT
    # The wire cannot say ``real_paper`` beside a ``shadow:`` custody id.
    assert body["authority_kind"] == "shadow"
    condition = body["operator_posture"]["condition"]
    condition_id = None if condition is None else condition["id"]
    assert condition_id != "alpaca_account_identity_mismatch"
    assert condition_id != "alpaca_account_wrong_execution_mode"


async def test_a_shadow_binding_reads_its_own_authority_and_renders_simulated_fills(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(c) The panel's authority id is the facade's, and its fills say simulated.

    Left as the route account id, the evidence read raises a bare ValueError
    (500); passed through naively, `authority_kind_for_account` answers
    ``real_paper`` and every synthesized shadow fill renders as a real fill on
    a real-money account -- R8's exact prohibition.
    """
    _app, runtime = shadow_app
    facade = active_sqlite_facade("alpaca")
    assert facade is not None
    _register_instance(facade)
    binding = _binding()

    async with _panel_authority_for_binding(_FakeDeployRegistry(), binding) as selected:
        assert selected is not None
        assert selected.account_id == SHADOW_ACCT
        evidence = await read_sqlite_panel_evidence(
            "alpaca", selected.account_id, SID, now_ms=NOW_MS, facade=selected
        )
        assert evidence is not None
        clerk = await read_sqlite_clerk_status("alpaca")
        assert clerk is not None
        panel = build_panel(
            evidence.status,
            clerk,
            [fill_entry(sid=SID, intent="a", ts_ms=NOW_MS, account_id=SHADOW_ACCT)],
            account_id=LIVE_ACCT,
            authority_account_id=selected.account_id,
            exposure={},
            fills_today=1,
            realized_pnl_today=0.0,
            open_pnl=None,
            latest_decision=None,
            last_bar_at_ms=None,
            journal_tail_ref=f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots/{SID}/decisions",
            journal_tail_seq=None,
            flatten_supported=False,
            now_ms=NOW_MS,
            program_build=ProgramBuildAdmissionFact(
                state="UNPROVEN",
                program_key="ema_crossover_signal",
                verified_at_ms=NOW_MS,
                explanation="This instance has no complete Signal Program seal.",
            ),
            market_pulse=_MARKET_PULSE,
        )

    [fill] = panel.recent_fills
    assert fill.simulated is True
    assert fill.authority_account_id == SHADOW_ACCT
    assert fill.authority_kind == "shadow"
    assert runtime.authority_kind == "shadow"


async def test_a_manual_order_post_is_refused_with_a_typed_reason(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """(d) Widening the selector must not open manual orders on real money.

    The qualification gate is lifted here on purpose: without it the refusal
    would prove only that manual trading is globally off, not that the shadow
    authority itself refuses.
    """
    app, _runtime = shadow_app
    monkeypatch.setattr(settings, "ALPACA_SQLITE_MANUAL_TRADING_ENABLED", True)
    body = {
        "ticket_id": "11111111-1111-4111-8111-111111111111",
        "legs": [
            {
                "leg_id": "22222222-2222-4222-8222-222222222222",
                "instruction": {"symbol": "SPY", "side": "buy", "quantity": 1},
            }
        ],
    }

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        at_route_account = await client.post(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/manual-orders/preview", json=body
        )
        at_custody_account = await client.post(
            f"/api/brokers/alpaca/accounts/{SHADOW_ACCT}/manual-orders/preview",
            json=body,
        )

    assert at_route_account.status_code == 404
    assert at_route_account.json()["detail"]["reason"] == "sqlite_account_not_selected"
    assert at_custody_account.status_code == 200
    capability = at_custody_account.json()["capability"]
    assert capability["available"] is False
    assert capability["unavailable"]["code"] in {
        "BROKER_ACCOUNT_MISMATCH",
        "LIVE_ACCOUNT_REFUSED",
    }


async def test_the_running_sweep_is_reachable_for_supervised_lease_revival(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(e) ADR 0050's revive_now() must find the sweep already running the lease."""
    _app, runtime = shadow_app

    sweep = active_reconciliation_sweep("alpaca")

    assert sweep is not None
    assert sweep is runtime.sweep


async def test_the_bots_catalog_is_reachable_over_http_on_a_shadow_authority(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(f) The roster read translates the route id to the custody id.

    Every route resolves the *live* account id; under shadow the authority
    custodies ``shadow:<live id>``. Handing the route id straight to
    ``read_sqlite_catalog`` made its account guard raise a bare ``ValueError``
    that nothing translates -- a 500 on the bots list of every shadow account.
    """
    app, _runtime = shadow_app
    facade = active_sqlite_facade("alpaca")
    assert facade is not None
    _register_instance(facade)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots/catalog"
        )

    assert response.status_code == 200, response.text
    rows = response.json()
    assert [row["strategy_instance_id"] for row in rows] == [SID]
    # The row names the authority that custodies it. A roster row saying
    # ``9LIVE0001`` would present shadow bots as bots on the real-money
    # account -- R8's prohibition, one surface over from the panel's fills.
    assert rows[0]["account_id"] == SHADOW_ACCT


async def test_both_chart_reads_answer_their_typed_state_on_a_shadow_authority(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(g) The two ``panel_chart_data_source`` readers translate the same way.

    No instance is registered, so each endpoint answers the same typed state a
    real-paper authority answers for a bot it does not carry. Before the
    translation both raised the readers' bare ``ValueError`` -- a 500 -- from
    the account guard, which sits above every "no such bot" branch.
    """
    app, _runtime = shadow_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        live = await client.get(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots/{SID}/chart/live",
            params={"resolution": "1m"},
        )
        history = await client.get(
            f"/api/brokers/alpaca/accounts/{LIVE_ACCT}/bots/{SID}/chart/history",
            params={"timeframe": "1d"},
        )

    assert live.status_code == 404, live.text
    assert history.status_code == 503, history.text


async def test_the_custody_diagnosis_reads_the_shadow_authoritys_own_identity(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(h) The brokers-router projection surface, which reads the facade's own id.

    ``_read_sqlite_account_projection`` already passes ``facade.account_id``,
    never the route id, so this endpoint was never part of the defect. It is
    pinned here so a later edit that "simplifies" it to the route id is caught
    by a test rather than by a real-money operator.
    """
    app, _runtime = shadow_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/brokers/alpaca/clerk/custody-diagnosis")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == SHADOW_ACCT
    assert body["authority_kind"] == "shadow"


async def test_a_foreign_route_account_is_still_refused_under_shadow(
    shadow_app: tuple[FastAPI, ActiveClerkRuntime],
) -> None:
    """(i) The translation must not launder a foreign account into the authority.

    Two independent proofs: the route refuses an id that is not the broker's
    account, and the translation itself maps a foreign id to a foreign custody
    id -- so the readers' own guard still refuses it if a future caller reaches
    them without the route check.
    """
    app, _runtime = shadow_app

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        foreign = await client.get(
            "/api/brokers/alpaca/accounts/9LIVE9999/bots/catalog"
        )
        # The custody id is an authority key, never an addressable route id.
        custody = await client.get(
            f"/api/brokers/alpaca/accounts/{SHADOW_ACCT}/bots/catalog"
        )

    assert foreign.status_code == 404, foreign.text
    assert custody.status_code == 404, custody.text

    facade = active_sqlite_facade("alpaca")
    assert facade is not None
    assert custody_account_id_for_route("alpaca", LIVE_ACCT) == facade.account_id
    assert custody_account_id_for_route("alpaca", "9LIVE9999") != facade.account_id
