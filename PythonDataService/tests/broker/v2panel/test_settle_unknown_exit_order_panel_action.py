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
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.alpaca_clerk_sqlite import UnknownOrderAttestationRequest
from app.schemas.broker_v2_panel import PanelActionRequest, PanelActionResult, PanelQuiesceActionRequest
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    DurableIdempotencyStore,
    IdempotencyKeyConflictError,
    IdempotencyStore,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import _catalog_row_action, _recovery_cure
from app.services.broker_v2_panel.sqlite_panel_source import execute_sqlite_panel_action
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_unknown_order_attestation as settling
from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _clock_at, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import WAIT_MS
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import _context_of, _flatten

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
    posted_again_with_token: str | None = None,
) -> PanelActionResult | None:
    """The panel source runs the presented settle action for the operator ``desk-operator``.

    ``posted_again_with_token`` is the token of a request posted again as it
    was first sent, whatever the action presents now.
    """
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
            concurrency_token=posted_again_with_token or presented.concurrency_token,
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


async def test_a_key_sent_again_with_another_attestation_is_a_conflict_not_the_first_receipt(
    lost_closing_order, tmp_path: Path
) -> None:
    """The key that settled "no such order" is posted again saying "it is this Alpaca order".

    That is not a replay of the first request. It is refused as a conflict:
    the first receipt is not handed back for something the operator did not
    say, Alpaca is not read and nothing is written. The same attestation
    under the same key still replays -- from the ledger file too, as after a
    restart.
    """
    repo, alpaca, facade = lost_closing_order
    receipts = tmp_path / "panel_action_receipts.json"
    store = DurableIdempotencyStore(receipts, wait_timeout_s=0)
    absent = UnknownOrderAttestationRequest(kind="absent")
    exists = UnknownOrderAttestationRequest(kind="exists", broker_order_id=settling.NO_SUCH_ORDER_ID)
    first = await _run_from_the_panel(repo, facade, store, attestation=absent, idempotency_key="settle-1")
    ledger = repo.custody_transitions()
    alpaca.reads.clear()

    for ledger_as_read in (store, DurableIdempotencyStore(receipts, wait_timeout_s=0)):
        with pytest.raises(IdempotencyKeyConflictError, match="already used for a different request"):
            await _run_from_the_panel(
                repo, facade, ledger_as_read, attestation=exists, idempotency_key="settle-1"
            )
        again = await _run_from_the_panel(
            repo, facade, ledger_as_read, attestation=absent, idempotency_key="settle-1"
        )
        assert first is not None and again is not None
        assert (again.applied, again.receipt_id) == (False, first.receipt_id)

    assert repo.custody_transitions() == ledger and alpaca.reads == []
    assert len(alpaca.sent) == 1


class _ProcessDied(Exception):
    """The process is gone: the Clerk's ledger holds the settlement, and the panel's receipt ledger does not."""


class _DiesBeforeItWritesTheReceipt(DurableIdempotencyStore):
    async def complete(self, *_args: object, **_kwargs: object) -> None:
        raise _ProcessDied


async def test_a_settlement_the_process_died_after_is_answered_with_its_receipt_when_posted_again(
    lost_closing_order, tmp_path: Path
) -> None:
    """The Clerk recorded the settlement, and the process stopped before the panel's receipt ledger did.

    After the restart the key reloads with no receipt, and the settled order
    is no longer presented. The same request posted again -- its key, its
    token, its attestation -- was answered 409, "previously failed". It is
    answered now with the settlement's own record: nothing is read at Alpaca
    and nothing is written, and the key keeps that receipt from then on.

    A key whose request the Clerk refused is still a failed key while no
    settlement is on the ledger for it to be answered with.
    """
    repo, alpaca, facade = lost_closing_order
    receipts = tmp_path / "panel_action_receipts.json"
    absent = UnknownOrderAttestationRequest(kind="absent")
    presented = next(
        action
        for action in build_recovery_catalog(await _context_of(repo, settling.SID)())
        if action.action_id == SETTLE
    )
    alpaca.open_orders = [settling._elsewhere("someone-buys", side="buy")]
    with pytest.raises(ActionNotAvailableError, match="An order for SPY is working at Alpaca"):
        await _run_from_the_panel(
            repo, facade, DurableIdempotencyStore(receipts, wait_timeout_s=0),
            attestation=absent, idempotency_key="refused",
        )
    alpaca.open_orders = []
    with pytest.raises(ActionNotAvailableError, match="previously failed"):
        await _run_from_the_panel(
            repo, facade, DurableIdempotencyStore(receipts, wait_timeout_s=0),
            attestation=absent, idempotency_key="refused",
        )

    with pytest.raises(_ProcessDied):
        await _run_from_the_panel(
            repo, facade, _DiesBeforeItWritesTheReceipt(receipts, wait_timeout_s=0),
            attestation=absent, idempotency_key="settle-1",
        )
    ledger = repo.custody_transitions()
    (settlement,) = [row for row in ledger if row["transition_kind"] == "ORDER_OUTCOME_ATTESTED"]
    alpaca.reads.clear()

    for after_a_restart in (
        DurableIdempotencyStore(receipts, wait_timeout_s=0),
        DurableIdempotencyStore(receipts, wait_timeout_s=0),
    ):
        again = await _run_from_the_panel(
            repo, facade, after_a_restart, attestation=absent, idempotency_key="settle-1",
            posted_again_with_token=presented.concurrency_token,
        )
        assert again is not None
        assert (again.applied, again.receipt_id) == (False, f"transition:{settlement['sequence']}")
        assert again.recorded_at_ms == settlement["recorded_at_ms"]

    assert repo.custody_transitions() == ledger and alpaca.reads == []
    assert len(alpaca.sent) == 1


async def test_a_settlement_the_process_died_after_is_still_answered_once_the_bot_has_settled_another_order(
    lost_closing_order, tmp_path: Path
) -> None:
    """The regression (#2854 review): the bot settled a second order before the first one's key was posted again.

    The Clerk recorded the first settlement and the process stopped before
    the panel's receipt ledger did. The operator then flattened the bot,
    that closing order's submit was lost too, and they settled it under a
    key of its own. The first request posted again -- its key, its token,
    its attestation -- was looked for only in the bot's latest settlement,
    which is the second order's, and was answered 409, "previously failed".

    It is found among the bot's settlements by its own token now, and
    answered with the first settlement's record. The second key still
    replays its own. Nothing is read at Alpaca and nothing is written.
    """
    repo, alpaca, facade = lost_closing_order
    receipts = tmp_path / "panel_action_receipts.json"
    absent = UnknownOrderAttestationRequest(kind="absent")
    current_context = _context_of(repo, settling.SID)
    presented_first = next(
        action for action in build_recovery_catalog(await current_context()) if action.action_id == SETTLE
    )
    with pytest.raises(_ProcessDied):
        await _run_from_the_panel(
            repo, facade, _DiesBeforeItWritesTheReceipt(receipts, wait_timeout_s=0),
            attestation=absent, idempotency_key="settle-first",
        )
    (first_settlement,) = [
        row for row in repo.custody_transitions() if row["transition_kind"] == "ORDER_OUTCOME_ATTESTED"
    ]

    alpaca.loses_its_next_submit()
    assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
    await _flatten(facade, current_context)
    (flatten_order,) = repo.unknown_exit_orders(settling.SID)
    assert flatten_order.order_ref != first_settlement["order_ref"] and len(alpaca.sent) == 2
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    second = await _run_from_the_panel(
        repo, facade, DurableIdempotencyStore(receipts, wait_timeout_s=0),
        attestation=absent, idempotency_key="settle-second",
    )
    ledger = repo.custody_transitions()
    alpaca.reads.clear()

    after_a_restart = DurableIdempotencyStore(receipts, wait_timeout_s=0)
    first_again = await _run_from_the_panel(
        repo, facade, after_a_restart, attestation=absent, idempotency_key="settle-first",
        posted_again_with_token=presented_first.concurrency_token,
    )
    second_again = await _run_from_the_panel(
        repo, facade, after_a_restart, attestation=absent, idempotency_key="settle-second",
    )

    assert first_again is not None and second is not None and second_again is not None
    assert (first_again.applied, first_again.receipt_id) == (False, f"transition:{first_settlement['sequence']}")
    assert first_again.recorded_at_ms == first_settlement["recorded_at_ms"]
    assert second.applied is True and second.receipt_id != first_again.receipt_id
    assert (second_again.applied, second_again.receipt_id) == (False, second.receipt_id)
    assert repo.custody_transitions() == ledger and alpaca.reads == []
    assert len(alpaca.sent) == 2, "settling sends nothing"


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


def test_no_row_and_no_page_names_settling_as_the_bots_cure_while_no_page_can_run_it(
    lost_closing_order,
) -> None:
    """A roster row's command and a bot page's primary command are the bot's cure.

    No page can collect an attestation yet, so the cure of a bot whose
    closing order is unknown is still the one a row and a page can run:
    Reconcile now. Settling is offered by the Clerk's catalog all the same.
    """
    repo, _alpaca, _facade = lost_closing_order
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        projection = reader.bot_snapshot(settling.SID)
    finally:
        reader.close()
    assert projection is not None

    row_action = _catalog_row_action(projection, row_needs_attention=True)
    cure = _recovery_cure(projection, bot_owns_problem=True)

    assert row_action is not None and row_action.action_id == "reconcile_now"
    assert cure is not None and cure.action_id == "reconcile_now"
    settle = next(action for action in projection.recovery_actions if action.action_id == SETTLE)
    assert (settle.available, settle.primary) == (True, False)


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
    exists = {"kind": "exists", "broker_order_id": "61e69015-8549-4bfd-b9c3-01e75843f47d"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        route = "/api/brokers/alpaca/accounts/PA-1/bots/sid-1/actions/quiesce"
        settled = await client.post(route, json={**body, "attestation": exists})
        on_another_action = await client.post(
            route, json={**body, "action_id": "reconcile_now", "attestation": exists}
        )
        named = await client.post(route, json={**body, "attestation": exists, "operator_identity": "x"})

        async def the_key_was_used_for_another_request(*_args: object, **_kwargs: object) -> PanelActionResult:
            raise IdempotencyKeyConflictError("This idempotency key was already used for a different request.")

        monkeypatch.setattr(broker_v2_panel.ds, "run_action", the_key_was_used_for_another_request)
        conflicting = await client.post(route, json={**body, "attestation": {"kind": "absent"}})

    assert settled.status_code == 200, settled.text
    assert (on_another_action.status_code, named.status_code) == (422, 422)
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["detail"]["outcome"] == "conflict"
    assert "already used for a different request" in conflicting.json()["detail"]["message"]
    ((request, operator),) = seen
    assert isinstance(request, PanelQuiesceActionRequest)
    assert request.attestation == UnknownOrderAttestationRequest(**exists)
    assert operator == "desk-operator"
