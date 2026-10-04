"""The bot panel settles a closing order whose outcome is unknown (#2854).

The panel's action request carries what the operator says Alpaca shows. The
panel source hands it, with the operator the panel route acts as, to the
Clerk's recovery executor, which reads Alpaca itself before it settles
anything. The real Clerk on a real ledger; only Alpaca is faked, and the
panel view and presented action are the stand-ins
``test_sqlite_action_fence`` uses.

The bot's page offers the action from its toolbar and opens a ticket from the
action's ``settlement``: both are read here from the real projection of that
ledger, adapted as the panel serves it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.facts import OrderOutcomeAttestedFacts
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.schemas.alpaca_clerk_sqlite import NOT_AN_ALPACA_ORDER_ID, UnknownOrderAttestationRequest
from app.schemas.bot_page import ToolbarActionView
from app.schemas.broker_v2_panel import (
    BotPanelView,
    PanelActionRequest,
    PanelActionResult,
    PanelQuiesceActionRequest,
)
from app.services.broker_v2_panel.action_execution_service import (
    ActionNotAvailableError,
    DurableIdempotencyStore,
    IdempotencyKeyConflictError,
    IdempotencyStore,
)
from app.services.broker_v2_panel.bot_page_projection import (
    bot_own_status,
    run_facts,
    run_summary_facts,
    run_summary_text,
    toolbar_actions,
)
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    _catalog_row_action,
    _recovery_cure,
    adapt_sqlite_panel,
    exit_in_progress,
    exit_unconfirmed,
    has_bot_scoped_custody_problem,
)
from app.services.broker_v2_panel.sqlite_panel_source import execute_sqlite_panel_action
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_unknown_order_attestation as settling
from tests.broker.alpaca.clerk.sqlite.conftest import FIXTURE_RTH_MS, _clock_at, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import WAIT_MS
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import _context_of, _flatten
from tests.broker.v2panel.test_panel_projection import _clerk_status, _panel, _status

SETTLE = "settle_unknown_exit_order"


@pytest.fixture
def started_bot(tmp_path: Path):
    """A running bot's ledger behind the real Clerk, and an Alpaca that never receives what is sent to it."""
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
    facade = SqliteAlpacaClerkFacade(repo=repo, read=alpaca, trade=alpaca, account_mode="paper")
    try:
        yield repo, alpaca, facade
    finally:
        repo.close()


@pytest.fixture
async def lost_closing_order(started_bot):
    """A stopped bot holding 10 SPY whose closing order's submit was lost and never reached Alpaca."""
    repo, alpaca, facade = started_bot
    await settling._a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    return repo, alpaca, facade


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
    """What a client that does not ask the operator would post: refused in words, nothing changed."""
    repo, alpaca, facade = lost_closing_order
    ledger = repo.custody_transitions()

    with pytest.raises(ActionNotAvailableError, match="Say what Alpaca shows for this order"):
        await _run_from_the_panel(
            repo, facade, IdempotencyStore(wait_timeout_s=0), attestation=None, idempotency_key="settle-2",
        )

    assert repo.custody_transitions() == ledger
    assert alpaca.reads == []
    assert len(alpaca.sent) == 1


def _projection(repo: ClerkSqliteRepository) -> ClerkProjection:
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        projection = reader.bot_snapshot(settling.SID)
    finally:
        reader.close()
    assert projection is not None
    return projection


def _bot_page(repo: ClerkSqliteRepository, *, running: bool) -> tuple[BotPanelView, list[ToolbarActionView]]:
    """The bot's panel as it is served from this ledger, and the toolbar its page draws from it."""
    projection = _projection(repo)
    served = adapt_sqlite_panel(
        _panel(_status(running=running, mode="trade"), _clerk_status(), []), projection, account_mode="paper"
    )
    status = bot_own_status(
        served,
        bot_owns_custody_problem=has_bot_scoped_custody_problem(projection),
        execution_coverage_complete=True,
    )
    return served, toolbar_actions(served, status=status)


def _settle_on_the_page(repo: ClerkSqliteRepository, *, running: bool) -> tuple[str, str, dict | None]:
    """How the page's toolbar offers settling, why, and the settlement its action carries on the wire."""
    served, toolbar = _bot_page(repo, running=running)
    (entry,) = [item for item in toolbar if item.action_id == SETTLE]
    (action,) = [item for item in served.model_dump(mode="json")["actions"] if item["action_id"] == SETTLE]
    assert action["enabled"] is (entry.availability == "available")
    return entry.availability, entry.reason, action["settlement"]


def test_settling_is_the_cure_a_row_and_a_page_name_for_the_bot(lost_closing_order) -> None:
    """A roster row's command and a bot page's primary command are the bot's cure.

    The bot's page collects the attestation, so the cure of a bot whose
    closing order is unknown is to settle it: Reconcile now cannot settle an
    order no read shows. The row's command carries the order it is about,
    which is what the page's ticket opens from.
    """
    repo, _alpaca, _facade = lost_closing_order
    projection = _projection(repo)

    row_action = _catalog_row_action(projection, row_needs_attention=True)
    cure = _recovery_cure(projection, bot_owns_problem=True)

    assert row_action is not None and row_action.action_id == SETTLE
    assert row_action.settlement is not None and row_action.confirmation is not None
    assert cure is not None and cure.action_id == SETTLE


def test_the_bot_page_offers_settling_from_its_toolbar_with_the_ticket_it_opens(lost_closing_order) -> None:
    """Fix group, danger, between Cancel open orders and Write off missing shares; the ticket is on the wire."""
    repo, _alpaca, _facade = lost_closing_order

    served, toolbar = _bot_page(repo, running=False)

    (entry,) = [item for item in toolbar if item.action_id == SETTLE]
    assert (entry.label, entry.group, entry.tone, entry.availability) == (
        "Settle the uncertain order", "fix", "danger", "available",
    )
    assert entry.reason == "Say what Alpaca shows for a closing order the Clerk could not confirm."
    fix_group = [item.action_id for item in toolbar if item.group == "fix"]
    assert fix_group[fix_group.index(SETTLE) - 1 : fix_group.index(SETTLE) + 2] == [
        "cancel_verified_working_orders", SETTLE, "discharge_attributed_residue",
    ]
    # A stopped bot still holding shares has no primary command: its page's warning owns that place.
    assert (served.primary_action, entry.primary) == (None, False)

    wire = served.model_dump(mode="json")
    (action,) = [item for item in wire["actions"] if item["action_id"] == SETTLE]
    settlement = action["settlement"]
    (order,) = [order for order in repo.orders_for_strategy(settling.SID) if order.role == "REDUCING"]
    assert (settlement["order_ref"], settlement["symbol"], settlement["side"], settlement["quantity"]) == (
        order.order_ref, "SPY", "sell", 10.0,
    )
    assert settlement["ticket"].startswith("The Clerk sent an order to sell 10 SPY on ")
    assert settlement["ticket"].endswith("Once that session has ended, the Clerk never sends the order again.")
    assert [
        (choice["attestation"], choice["label"], choice["asks_broker_order_id"])
        for choice in settlement["choices"]
    ] == [("absent", "Alpaca has no such order", False), ("exists", "Alpaca shows it", True)]
    assert all(choice["explanation"] for choice in settlement["choices"])
    assert action["confirmation"]["confirm_label"] == "Settle the order"
    # The word the page asks the operator to type is the Clerk's, never the page's own.
    assert action["confirmation"]["required_token"] == "SETTLE"
    assert [item["settlement"] for item in wire["actions"] if item["action_id"] != SETTLE] == [None] * (
        len(wire["actions"]) - 1
    )


def test_sell_and_clear_are_refused_with_the_way_to_settle_while_a_closing_order_is_unconfirmed(
    lost_closing_order,
) -> None:
    """The Clerk holds the position behind the unconfirmed order, so Sell and Clear stay refused (#2854).

    Sell said "Run Reconcile now" and Clear said to sell first; neither is a
    way on. Both now name the action that is: settling the order.
    """
    repo, _alpaca, _facade = lost_closing_order

    _served, toolbar = _bot_page(repo, running=False)

    by_id = {item.action_id: item for item in toolbar}
    sell, clear = by_id["prepare_safe_flatten"], by_id["archive"]
    assert (sell.availability, sell.reason) == (
        "blocked",
        "The Clerk sent a closing order for this bot and could not confirm it, so a flatten could "
        "close the position a second time.",
    )
    assert (clear.availability, clear.reason) == (
        "blocked",
        "The Clerk holds this bot's position behind a closing order it could not confirm. "
        'Use "Settle the uncertain order" first.',
    )
    assert by_id[SETTLE].label == "Settle the uncertain order"


async def test_the_run_summary_says_a_closing_order_is_unconfirmed_never_that_a_sale_is_queued(
    started_bot,
) -> None:
    """The summary said "a sale is queued" of an order the Clerk could not confirm (#2854).

    None is: the Clerk holds the position and sends nothing more for the
    bot. The line says the order is unconfirmed while it is, and says nothing
    of an exit once the order is settled.
    """
    repo, alpaca, facade = started_bot
    await settling._a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)

    def summary() -> tuple[bool, bool, str]:
        projection = _projection(repo)
        served, _toolbar = _bot_page(repo, running=False)
        facts = run_summary_facts(
            served,
            run_facts(
                None, run_fills=[], activity=None, budget=None,
                exit_in_progress=exit_in_progress(projection, settling.SID),
                exit_unconfirmed=exit_unconfirmed(projection, settling.SID),
            ),
            now_ms=repo.clock(),
        )
        text = run_summary_text(facts.model_copy(update={"ending": "stopped", "started_at_ms": FIXTURE_RTH_MS}))
        return facts.exit_unconfirmed, facts.exit_queued, text

    unconfirmed, queued, text = summary()
    assert (unconfirmed, queued) == (True, False)
    assert text.endswith("holds 10 SPY · a closing order is unconfirmed.")
    assert "a sale is queued" not in text

    settled = await _run_from_the_panel(
        repo, facade, IdempotencyStore(wait_timeout_s=0),
        attestation=UnknownOrderAttestationRequest(kind="absent"), idempotency_key="settle-1",
    )
    assert settled is not None and settled.applied is True
    unconfirmed, queued, text = summary()
    assert (unconfirmed, queued) == (False, False)
    assert text.endswith("holds 10 SPY.")


async def test_the_bot_page_offers_settling_only_while_the_clerks_catalog_does(started_bot) -> None:
    """The toolbar's entry follows the Clerk's catalog through the order's life, and carries a ticket only when offered.

    Nothing uncertain: not needed, so it is off the bar. The closing order is
    lost while the bot runs: blocked, with the Clerk's reason. Stopped, and
    the wait after the send not yet over: blocked. The wait over: offered.
    Settled: not needed again.
    """
    repo, alpaca, facade = started_bot
    assert _settle_on_the_page(repo, running=True) == ("not_needed", "No closing order is uncertain.", None)

    await settling._the_runner_sends_the_exit(repo, alpaca)
    assert _settle_on_the_page(repo, running=True) == (
        "blocked", "Stop the bot before settling its uncertain order.", None,
    )

    submit_stop_run(
        repo, account_id=settling.ACCOUNT_ID, strategy_instance_id=settling.SID,
        lifecycle_run_id=settling.RUN_ID, operator_reason="operator_stop",
    )
    assert _settle_on_the_page(repo, running=False) == (
        "blocked", "The Clerk sent this order moments ago and may still find it at Alpaca by itself.", None,
    )

    _walk_clock_to(repo, repo.clock() + settling.WAIT_MS)
    availability, _reason, settlement = _settle_on_the_page(repo, running=False)
    assert availability == "available" and settlement is not None

    settled = await _run_from_the_panel(
        repo, facade, IdempotencyStore(wait_timeout_s=0),
        attestation=UnknownOrderAttestationRequest(kind="absent"), idempotency_key="settle-1",
    )
    assert settled is not None and settled.applied is True
    assert _settle_on_the_page(repo, running=False) == ("not_needed", "No closing order is uncertain.", None)


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
        not_an_order_id = await client.post(
            route, json={**body, "attestation": {"kind": "exists", "broker_order_id": "learn-ai/spy-bot/v1:close-1"}}
        )

        async def the_key_was_used_for_another_request(*_args: object, **_kwargs: object) -> PanelActionResult:
            raise IdempotencyKeyConflictError("This idempotency key was already used for a different request.")

        monkeypatch.setattr(broker_v2_panel.ds, "run_action", the_key_was_used_for_another_request)
        conflicting = await client.post(route, json={**body, "attestation": {"kind": "absent"}})

    assert settled.status_code == 200, settled.text
    assert (on_another_action.status_code, named.status_code) == (422, 422)
    # An id the operator typed that is no Alpaca order id: refused in words they read, as the Clerk's check refuses it.
    assert not_an_order_id.status_code == 422, not_an_order_id.text
    assert not_an_order_id.json() == {
        "detail": {"reason": "not_an_alpaca_order_id", "message": NOT_AN_ALPACA_ORDER_ID}
    }
    assert conflicting.status_code == 409, conflicting.text
    assert conflicting.json()["detail"]["outcome"] == "conflict"
    assert "already used for a different request" in conflicting.json()["detail"]["message"]
    ((request, operator),) = seen
    assert isinstance(request, PanelQuiesceActionRequest)
    assert request.attestation == UnknownOrderAttestationRequest(**exists)
    assert operator == "desk-operator"
