"""The bot panel settles a closing order whose outcome is unknown (#2854).

The panel's action request carries what the operator says Alpaca shows. The
panel source hands it, with the operator the panel route acts as, to the
Clerk's recovery executor, which reads Alpaca itself before it settles
anything. The real Clerk on a real ledger; only Alpaca is faked, and the
panel view and presented action are the stand-ins
``test_sqlite_action_fence`` uses.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.facts import OrderOutcomeAttestedFacts
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.alpaca_clerk_sqlite import UnknownOrderAttestationRequest
from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult, PanelQuiesceActionRequest
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    IdempotencyStore,
)
from app.services.broker_v2_panel.sqlite_panel_source import execute_sqlite_panel_action
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_unknown_order_attestation as settling
from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _clock_at
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import _context_of

SETTLE = "settle_unknown_exit_order"


@pytest.fixture
async def lost_closing_order(tmp_path: Path):
    """A stopped bot holding 10 SPY whose closing order's submit was lost and never reached Alpaca."""
    clock = _clock_at(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=settling.ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=settling.SID, symbol="SPY", config_hash="h1"
    )
    submit_start_run(
        repo, account_id=settling.ACCOUNT_ID, strategy_instance_id=settling.SID,
        lifecycle_run_id=settling.RUN_ID,
    )
    alpaca = settling._AlpacaAccount(repo, clock, submits=("never_arrives",))
    await settling._a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade = SqliteAlpacaClerkFacade(repo=repo, read=alpaca, trade=alpaca, account_mode="paper")
    try:
        yield repo, alpaca, facade
    finally:
        repo.close()


async def _run_from_the_panel(
    repo: ClerkSqliteRepository,
    facade: SqliteAlpacaClerkFacade,
    store: IdempotencyStore,
    *,
    attestation: UnknownOrderAttestationRequest | None,
    idempotency_key: str,
) -> PanelActionResult | None:
    """The panel source runs the presented settle action for the operator ``desk-operator``."""
    presented = next(
        action
        for action in build_recovery_catalog(await _context_of(repo, settling.SID)())
        if action.action_id == SETTLE
    )
    return await execute_sqlite_panel_action(
        "alpaca",
        repo.account_id,
        settling.SID,
        request=PanelActionRequest(
            action_id=SETTLE,
            revision=1,
            concurrency_token=presented.concurrency_token,
            idempotency_key=idempotency_key,
            reason="Alpaca's order list shows no sell of SPY.",
            attestation=attestation,
        ),
        panel=SimpleNamespace(revision=1),
        action=SimpleNamespace(
            action_id=SETTLE, label=presented.label, concurrency_token=presented.concurrency_token
        ),
        availability_error=None,
        facade=facade,
        store=store,
        operator_identity="desk-operator",
    )


async def test_the_panel_settles_the_order_as_its_operator_and_a_repost_does_nothing_twice(
    lost_closing_order,
) -> None:
    """The attestation and the operator reach the Clerk; the same key posted again replays the receipt."""
    repo, alpaca, facade = lost_closing_order
    store = IdempotencyStore(wait_timeout_s=0)
    absent = UnknownOrderAttestationRequest(kind="absent")

    first = await _run_from_the_panel(repo, facade, store, attestation=absent, idempotency_key="settle-1")
    ledger = repo.custody_transitions()
    alpaca.reads.clear()
    again = await _run_from_the_panel(repo, facade, store, attestation=absent, idempotency_key="settle-1")

    assert first is not None and again is not None
    assert (first.applied, again.applied) == (True, False)
    assert again.receipt_id == first.receipt_id
    assert repo.custody_transitions() == ledger and alpaca.reads == []
    (attested,) = [
        OrderOutcomeAttestedFacts.from_facts_json(row["facts_json"])
        for row in ledger
        if row["transition_kind"] == "ORDER_OUTCOME_ATTESTED"
    ]
    assert (attested.attestation, attested.operator) == ("absent", "desk-operator")
    assert attested.operator_reason == "Alpaca's order list shows no sell of SPY."
    assert len(alpaca.sent) == 1, "settling sends nothing"


async def test_a_settle_posted_without_saying_what_alpaca_shows_is_refused_and_writes_nothing(
    lost_closing_order,
) -> None:
    """What a page that cannot ask the operator yet would post: refused in words, nothing changed."""
    repo, alpaca, facade = lost_closing_order
    ledger = repo.custody_transitions()

    with pytest.raises(ActionNotAvailableError, match="Say what Alpaca shows for this order"):
        await _run_from_the_panel(
            repo, facade, IdempotencyStore(wait_timeout_s=0), attestation=None, idempotency_key="settle-2",
        )

    assert repo.custody_transitions() == ledger
    assert alpaca.reads == []
    assert len(alpaca.sent) == 1


async def test_the_quiesce_route_carries_the_attestation_and_acts_as_the_configured_operator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A draining lane routes this operation (ADR 0063 §2): the attestation must travel on it.

    Who settles is the route's configured operator identity, never a field of
    the request.
    """
    from app.routers import broker_v2_panel

    seen: list[tuple[PanelActionRequest, str]] = []

    async def run_action(broker, account_id, sid, request, *, operator_identity):
        seen.append((request, operator_identity))
        return PanelActionResult(
            action_id=request.action_id, receipt_id="transition:7", recorded_at_ms=1, applied=True,
            revision=2, concurrency_token="tok-2", message="done",
        )

    monkeypatch.setattr(broker_v2_panel.ds, "run_action", run_action)
    monkeypatch.setattr(broker_v2_panel, "schedule_live_projection_refresh", lambda *_args: None)
    monkeypatch.setattr(broker_v2_panel.settings, "PANEL_OPERATOR_IDENTITY", "desk-operator")
    app = FastAPI()
    app.include_router(broker_v2_panel.router)
    body = {"action_id": SETTLE, "revision": 1, "concurrency_token": "tok", "idempotency_key": "key"}
    exists = {"kind": "exists", "broker_order_id": "alpaca-order-1"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        route = "/api/brokers/alpaca/accounts/PA-1/bots/sid-1/actions/quiesce"
        settled = await client.post(route, json={**body, "attestation": exists})
        on_another_action = await client.post(
            route, json={**body, "action_id": "reconcile_now", "attestation": exists}
        )
        named = await client.post(route, json={**body, "attestation": exists, "operator_identity": "x"})

    assert settled.status_code == 200, settled.text
    assert (on_another_action.status_code, named.status_code) == (422, 422)
    ((request, operator),) = seen
    assert isinstance(request, PanelQuiesceActionRequest)
    assert request.attestation == UnknownOrderAttestationRequest(**exists)
    assert operator == "desk-operator"
