"""An operator settles an EXIT whose closing order's outcome is unknown (#2854).

A closing order whose submit answer was lost leaves its EXIT ``unknown``: the
Clerk keeps custody of the position and can neither prove the order absent
nor take it up. The operator looks at the account at Alpaca and says what it
shows. These tests run the real Clerk on a real temp SQLite ledger against a
faked Alpaca, through the dispatcher the panel and the recovery route use,
and assert what an operator and the ledger see afterwards.

Three things are pinned throughout. The Clerk never takes the operator's
word: it reads Alpaca itself, and anything it reads that does not bear the
attestation out refuses with nothing written. No attestation ever sends an
order. And "no such order" says nothing about Alpaca's position: it is taken
whether or not the position agrees with the Clerk's records, and it leaves
the position to the operator, with the Clerk sending nothing for it by
itself afterwards.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.account_open_work import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import RECOVERY_LEFT_TO_THE_OPERATOR
from app.broker.alpaca.clerk.sqlite.facts import (
    AttributedResidueDischargedFacts,
    EndedForeignOrder,
    OrderOutcomeAttestedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    RecoveryExecutionResult,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    StaleRecoveryTokenError,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import DEFAULT_CLAIM_TTL_MS, ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import ATTRIBUTED_RESIDUE_DISCHARGED
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.scheduled_end import ScheduledEnd
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    EXIT_STUCK_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    ExitStuckCause,
)
from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import (
    MAX_SYMBOL_ORDER_PAGES,
    ORDER_OUTCOME_ATTESTED,
    ORDERS_SINCE_MARGIN_MS,
    OrderOutcomeAttestation,
    UnknownOrderSettlementRefused,
    settle_unknown_exit_order,
)
from app.broker.contract.errors import BrokerError, BrokerRequestInvalid, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent
from app.schemas.bot_end import BotEnd
from app.utils.et_words import et_when_words
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _clock_at,
    _make_held_position,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
    FIVE_MINUTES_OF_PASSES,
    SWEEP_INTERVAL_MS,
    WAIT_MS,
    _Alpaca,
    _AnsweredAs,
    _closing_order,
    _exit_state,
    _passes,
    _Refused,
    _SubmitFate,
    _the_runner_sends_the_exit,
    _the_stream_reports,
    _the_sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    _context_of,
    _ExactLookup,
    _flatten,
    _NoSubmitTrade,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    crashed_with_exposure,  # noqa: F401 -- the repository, in the regular session, these tests reuse
)
from tests.broker.alpaca.clerk.sqlite.test_scheduled_end import (
    _Schedule,
    schedule,  # noqa: F401 -- the runner's schedule of the bots' ends, installed for the tests of a bot's end
)
from tests.broker.alpaca.clerk.sqlite.test_scheduled_end_exit_stuck import (
    _all_end_sales,
    _end_lines,
    _end_on_the_bots_page,
)

SETTLE = "settle_unknown_exit_order"
OPERATOR = "ops-desk"
ABSENT = OrderOutcomeAttestation(kind="absent")
# Where this Alpaca's order ids come from: one UUID per client order id.
_ALPACA_ORDER_IDS = UUID("6f1c2a0e-3b5d-4c7e-8f90-a1b2c3d4e5f6")
# Alpaca order ids of orders that are not the bot's closing order.
NO_SUCH_ORDER_ID = "0d9f3a52-7c1e-4b68-9f0a-2e4c6a8b0d1f"
ANSWERED_ORDER_ID = "5e6f7a8b-9c0d-4e1f-8a3b-4c5d6e7f8091"
# The client order id Alpaca answers a send under when it is not the one the Clerk sent.
ANSWERED_AS = "an-id-the-clerk-never-sent"
UNREADABLE_ORDER_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
BY_HAND_ORDER_ID = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
UNNAMED_ORDER_ID = "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f"
ANOTHER_BOTS_ORDER_ID = "4d5e6f7a-8b9c-4d0e-9f2a-3b4c5d6e7f80"
# A second bot of the same account, in the same symbol.
ANOTHER_BOT = "another-bot"
# Fifteen minutes of the Clerk's passes: several times what the watchdog waits before it sells again.
SIXTY_PASSES = 3 * FIVE_MINUTES_OF_PASSES

type _Context = Callable[[], Awaitable[object]]


class _AlpacaAccount(_Alpaca):
    """Alpaca as the settling Clerk reads it: every read by name, and each one able to fail.

    Beside what :class:`_Alpaca` answers, it lists every order sent since a
    time (open ones and ``ended_orders``), and answers an order by its Alpaca
    order id. ``reads`` names each read in order: ``lookup`` (the closing
    order's exact lookup), ``lookup:other-id`` (the exact lookup of a client
    order id the Clerk holds no order under), ``orders:open``, ``positions``,
    ``orders:all`` (one per page), ``by-id``. ``fails`` makes a named read of
    orders fail; the positions read fails as :class:`_Account` makes it.
    ``meanwhile`` is what happens at Alpaca just before a read of orders
    answers, by the read's name and which one of that name it is since
    ``reads`` was last cleared (the first is 1): an order sent or filled
    while the Clerk is reading.

    An order's Alpaca order id always finds an order Alpaca has, even one
    its exact lookup by client order id does not show -- the state an
    operator who can see the order at Alpaca settles. ``by_broker_id`` states
    what any other id answers, and ``has_no_order_under`` the ids Alpaca
    answers it has no order under, whatever it showed for them before.

    Alpaca names every order with a UUID, and the read by order id takes
    nothing else: an id that is not one is refused before Alpaca is asked,
    exactly as ``AlpacaTradingClient.get_order_by_broker_order_id`` refuses
    it.
    """

    def __init__(
        self, repo: ClerkSqliteRepository, clock: _TestClock, *, submits: tuple[_SubmitFate, ...] = ()
    ) -> None:
        super().__init__(repo, clock, submits=submits)
        self.ended_orders: list[BrokerOrder] = []
        self.fails: dict[str, BrokerError] = {}
        self.asked_since_ms: list[int | None] = []
        # Each page of orders asked for: the symbols, and where the page was to end.
        self.pages_asked: list[tuple[tuple[str, ...] | None, int | None]] = []
        self.meanwhile: dict[tuple[str, int], Callable[[], None]] = {}
        self.by_broker_id: dict[str, BrokerOrder | BrokerError] = {}
        self.has_no_order_under: set[str] = set()

    def _read(self, name: str) -> None:
        self.reads.append(name)
        self.meanwhile.get((name, self.reads.count(name)), lambda: None)()
        if name in self.fails:
            raise self.fails[name]

    async def list_orders(
        self,
        *,
        status: str | None = None,
        limit: int | None = None,
        after_ms: int | None = None,
        symbols: tuple[str, ...] | None = None,
        until_ms: int | None = None,
    ) -> list[BrokerOrder]:
        """Open orders, or -- for ``status="all"`` -- every order as Alpaca lists them.

        Newest first by when they were submitted, narrowed to ``symbols`` and
        to those submitted after ``after_ms`` and before ``until_ms``, and cut
        at ``limit``: one page.
        """
        if status != "all":
            self._read("orders:open")
            return list(self.open_orders)
        self._read("orders:all")
        self.asked_since_ms.append(after_ms)
        self.pages_asked.append((symbols, until_ms))
        wanted = None if symbols is None else {symbol.upper() for symbol in symbols}
        listed = [
            order
            for order in (*self.open_orders, *self.ended_orders)
            # An order whose symbol the Clerk could not read is still one Alpaca files under a symbol.
            if (wanted is None or not order.symbol.strip() or order.symbol.upper() in wanted)
            and order.submitted_at_ms is not None
            and (after_ms is None or order.submitted_at_ms > after_ms)
            and (until_ms is None or order.submitted_at_ms < until_ms)
        ]
        listed.sort(key=lambda order: order.submitted_at_ms or 0, reverse=True)
        return listed[:limit]

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        clerk_order = self._repo.order(client_order_id)
        if clerk_order is None:
            self._read("lookup:other-id")
        elif clerk_order.role == "REDUCING":
            self._read("lookup")
        return await super().get_order_by_client_order_id(client_order_id)

    def has_under_another_id(self, order: BrokerOrder) -> None:
        """Alpaca has ``order`` under a client order id the Clerk never sent, as it stands now: every read shows it.

        Its exact lookup by that id answers, its Alpaca order id finds it,
        and it is listed among the open orders while it works and among the
        ended ones after. Stating it again replaces what Alpaca showed of it.
        """
        assert order.client_order_id is not None
        self._has[order.client_order_id] = order
        shown_before = (self.open_orders, self.ended_orders)
        self.open_orders, self.ended_orders = (
            [listed for listed in orders if listed.order_id != order.order_id] for orders in shown_before
        )
        working = order.status not in {"filled", "canceled", "expired", "rejected", "replaced"}
        (self.open_orders if working else self.ended_orders).append(order)

    def loses_its_next_submit(self) -> None:
        """The next order sent times out and never reaches Alpaca."""
        self._fates.insert(0, "never_arrives")

    def fills_a_buy(self, client_order_id: str) -> None:
        """A buy the account accepted fills whole at $100: the shares join the account, and the order is listed ended."""
        working = self._has[client_order_id]
        assert working.quantity is not None
        filled = working.model_copy(
            update={
                "status": "filled", "filled_quantity": working.quantity, "filled_avg_price": 100.0,
                "submitted_at_ms": self._clock(), "updated_at_ms": self._clock(), "filled_at_ms": self._clock(),
            }
        )
        self._has[client_order_id] = filled
        self.open_orders = [order for order in self.open_orders if order.client_order_id != client_order_id]
        self.ended_orders.append(filled)
        self.holds += working.quantity

    def _alpaca_order_id(self, client_order_id: str) -> str:
        return str(uuid5(_ALPACA_ORDER_IDS, client_order_id))

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        try:
            UUID(order_id)
        except ValueError as exc:
            raise BrokerRequestInvalid(f"{order_id!r} is not an Alpaca order id.") from exc
        self.reads.append("by-id")
        if order_id in self.has_no_order_under:
            return None
        stated = self.by_broker_id.get(order_id)
        if isinstance(stated, BrokerError):
            raise stated
        if stated is not None:
            return stated
        return next((order for order in self._has.values() if order.order_id == order_id), None)

    def order_id_of(self, client_order_id: str) -> str:
        """The Alpaca order id of the order Alpaca has under ``client_order_id``."""
        return self._has[client_order_id].order_id

    def at_alpaca(self, client_order_id: str) -> BrokerOrder:
        return self._has[client_order_id]


def _facade(repo: ClerkSqliteRepository, alpaca: _AlpacaAccount) -> SqliteAlpacaClerkFacade:
    return SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=alpaca, trade=alpaca)


async def _a_stopped_bot_whose_closing_order_was_lost(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount, *, wait_passed: bool = True
) -> str:
    """A bot holding 10 SPY decided to exit, its closing order's submit timed out, and it was stopped.

    Returns the EXIT's effect, ``unknown``. With ``wait_passed`` the
    submit-absence wait since the send is over, and no pass of the Clerk has
    run since: what an operator who acts promptly meets.
    """
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )
    if wait_passed:
        _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _exit_state(repo, exit_id) == "unknown"
    alpaca.reads.clear()
    return exit_id


async def _presented(current_context: _Context) -> RecoveryCapability:
    """The settle action as the bot's page presents it now."""
    return next(
        action for action in build_recovery_catalog(await current_context()) if action.action_id == SETTLE
    )


async def _settle(
    facade: SqliteAlpacaClerkFacade,
    current_context: _Context,
    attestation: OrderOutcomeAttestation | None,
    *,
    presented: RecoveryCapability | None = None,
    operator: str | None = OPERATOR,
    reason: str | None = None,
) -> RecoveryExecutionResult:
    """The operator settles the order, dispatched as the panel and the recovery route dispatch it."""
    capability = presented or await _presented(current_context)
    return await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id=SETTLE,
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason=reason,
            attestation=attestation,
            operator=operator,
        ),
        current_context=current_context,
    )


def _attested(repo: ClerkSqliteRepository) -> list[OrderOutcomeAttestedFacts]:
    return [
        OrderOutcomeAttestedFacts.from_facts_json(row["facts_json"])
        for row in repo.custody_transitions()
        if row["transition_kind"] == ORDER_OUTCOME_ATTESTED
    ]


def _open_episode(repo: ClerkSqliteRepository, reason_code: str) -> dict | None:
    return repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code=reason_code, strategy_instance_id=SID
    )


def _elsewhere(
    order_id: str, *, symbol: str = "SPY", side: str = "sell", status: str = "new",
    client_order_id: str | None = None, filled_quantity: float = 0.0, submitted_at_ms: int = FIXTURE_RTH_MS,
) -> BrokerOrder:
    """An order at Alpaca the Clerk did not place, sent since the closing order was unless dated otherwise."""
    return safe_flatten._broker_order(
        client_order_id or f"elsewhere:{order_id}", order_id=order_id, symbol=symbol, status=status,
        side=side, quantity=10.0, filled_quantity=filled_quantity,
        filled_avg_price=100.0 if filled_quantity else None,
    ).model_copy(update={"submitted_at_ms": submitted_at_ms, "created_at_ms": submitted_at_ms})


# ── Attestation: "Alpaca has no such order" ──────────────────────────────────


async def test_an_absent_order_ends_its_exit_as_not_sent_and_leaves_its_position_to_the_operator(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost closing order never reached Alpaca, and the operator says so.

    The Clerk reads the order's exact lookup, the open orders, the positions
    and then every order in the symbol since, finds no such order and none
    working, and ends the EXIT as not sent. The unknown outcome closes and
    the entry is free to be exited again. The bot still holds its 10 SPY,
    and they are the operator's now: ``EXIT_NOT_FLAT`` says the position is
    still open, and ``EXIT_STUCK`` is open with it, so the Clerk sends
    nothing for them by itself. Who settled it, and what the Clerk read, is
    on the record. No order left the Clerk.

    All of that is one write to the ledger: the attestation's own row ends
    the EXIT and opens both episodes, so there is no part of it to be
    missing, and no pass can meet the notice without the hold.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    (entry,) = repo.entry_orders_for_strategy(SID)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    ledger = repo.custody_transitions()

    result = await _settle(
        facade, current_context, ABSENT, reason="Checked Alpaca's order list: no sell of SPY today."
    )

    assert result.applied is True
    assert len(alpaca.sent) == 1, "settling never sends an order"
    assert alpaca.reads == ["lookup", "orders:open", "positions", "orders:all"]
    assert alpaca.asked_since_ms == [closing.updated_at_ms - ORDERS_SINCE_MARGIN_MS]

    (ended,) = repo.custody_transitions()[len(ledger):]
    assert (ended["transition_kind"], ended["summary_code"]) == (ORDER_OUTCOME_ATTESTED, "ORDER_ATTESTED_ABSENT")
    _the_position_is_left_to_the_operator(repo, exit_id)
    assert repo.active_exit_for_order(entry.order_ref) is None, "the entry can be exited again"

    (recorded,) = _attested(repo)
    assert (recorded.attestation, recorded.operator) == ("absent", OPERATOR)
    assert recorded.operator_reason == "Checked Alpaca's order list: no sell of SPY today."
    assert (recorded.client_order_id, recorded.symbol, recorded.side, recorded.quantity) == (
        closing.order_ref, "SPY", "SELL", 10.0,
    )
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (10.0, 10.0)
    assert result.receipt_id == f"transition:{_attestation_row(repo)['sequence']}"

    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    stuck = _open_episode(repo, EXIT_STUCK_REASON_CODE)
    assert notice is not None and stuck is not None
    assert notice["headline"] == "A closing order was settled as not sent; the position is still open"
    assert notice["explanation"] == (
        "The Clerk could not confirm the order to sell 10 SPY. An operator said Alpaca has no such "
        "order, the Clerk's own reads of Alpaca found no order under its client order id and none "
        "working on SPY, and the exit was ended as not sent; 10 SPY is still held. When the Clerk "
        "read the account, Alpaca held 10 SPY and the Clerk's records said 10 SPY; that does not "
        "show whether the order traded. The Clerk sends no order for them by itself."
    )
    assert notice["next_step"] == (
        "Look at this account's position at Alpaca. If Alpaca does not hold these shares, write off "
        "the missing shares; otherwise sell what is held. Both are on the bot's page."
    )
    assert stuck["headline"] == (
        "A closing order was settled as not sent; the Clerk sends no order for the position by itself"
    )
    assert stuck["explanation"] == (
        "An operator said Alpaca does not have this bot's order to sell 10 SPY, and the Clerk ended "
        "this exit as not sent. It holds 10 SPY for the bot and sends no order for them by itself."
    )
    assert stuck["operator_impact"] == (
        "New exposure stays on hold for this strategy, and the Clerk sends no order for this "
        "position by itself. Exact operator reduction and the write-off remain available."
    )
    assert stuck["next_step"] == "Sell them or write them off from the bot's page."
    assert ExitStuckCause.from_mapping(
        UncertaintyRaisedFacts.from_facts_json(stuck["facts_json"]).cause_facts
    ) == ExitStuckCause(
        symbol="SPY", attributed_qty=10.0, redrive_count=0,
        first_observed_at_ms=recorded.broker_observed_at_ms,
        attested_absent_order_ref=closing.order_ref,
    )


def _attestation_row(repo: ClerkSqliteRepository) -> dict:
    (row,) = [
        row for row in repo.custody_transitions() if row["transition_kind"] == ORDER_OUTCOME_ATTESTED
    ]
    return row


def _the_position_is_left_to_the_operator(
    repo: ClerkSqliteRepository, exit_id: str, *, held: float = 10.0
) -> None:
    """What every accepted "no such order" leaves: the EXIT ended as not sent, and its position held for the operator.

    The bot holds what it held, the notice that the position is open is up,
    and so is the episode under which the Clerk sends nothing for it.
    """
    assert _exit_state(repo, exit_id) == "failed"
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is not None
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is not None
    assert repo.position(SID, "SPY") == held, "an attestation never changes what the bot holds"


async def _the_operators_cure(
    facade: SqliteAlpacaClerkFacade, current_context: _Context, action_id: str
) -> RecoveryExecutionResult:
    """The operator runs ``action_id`` from the bot's page, as it is presented there now."""
    capability = next(
        action for action in build_recovery_catalog(await current_context()) if action.action_id == action_id
    )
    assert capability.available, capability.unavailable_reason
    return await execute_recovery_action(
        facade,
        request=RecoveryExecutionRequest(
            action_id=action_id,
            concurrency_token=capability.concurrency_token,
            execution_ref=capability.execution_ref,
            reason="The shares are not at Alpaca.",
        ),
        current_context=current_context,
    )


async def test_a_settled_exit_is_told_on_the_bots_page_as_left_to_the_operator_never_as_failed_retries(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """What the readers of a stuck exit show for one an operator settled as not sent.

    The bot's page shows recovery as stopped, in words that say why: an
    operator said Alpaca has no such order. It does not say the Clerk gave
    up after repeated failures, which never happened, and the notice keeps
    its own next step. The watchdog's own record of its pass says the same.
    The attestation's row is on the timeline of both episodes it opened.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    attested = _attestation_row(repo)
    stuck = _open_episode(repo, EXIT_STUCK_REASON_CODE)
    assert stuck is not None
    assert stuck["uncertainty_id"] == f"uncertainty:{attested['sequence']}:EXIT_STUCK"
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        snapshot = reader.bot_snapshot(SID)
        timelines = {
            reason_code: [
                entry.transition_kind
                for entry in reader.timeline_page(uncertainty_id=episode["uncertainty_id"]).entries
            ]
            for reason_code in (EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE)
            if (episode := _open_episode(repo, reason_code)) is not None
        }
    finally:
        reader.close()
    assert snapshot is not None
    (notice,) = [item for item in snapshot.uncertainties if item.reason_code == EXIT_NOT_FLAT_REASON_CODE]
    status = notice.recovery_status
    assert status is not None
    assert (status.kind, status.reason_code) == ("stuck", "EXIT_STUCK")
    assert status.explanation == RECOVERY_LEFT_TO_THE_OPERATOR == (
        "An operator said Alpaca has no such closing order, and the Clerk ended that exit as not "
        "sent. The Clerk sends no order for this position by itself."
    )
    assert status.stuck_since_ms == attested["clerk_observed_at_ms"]
    assert notice.next_step.endswith("otherwise sell what is held. Both are on the bot's page.")
    evaluated = repo.last_strategy_transition(
        strategy_instance_id=SID, transition_kind="EXIT_RECOVERY_EVALUATED"
    )
    assert evaluated is not None
    assert json.loads(evaluated["facts_json"])["explanation"] == RECOVERY_LEFT_TO_THE_OPERATOR
    assert ORDER_OUTCOME_ATTESTED in timelines[EXIT_NOT_FLAT_REASON_CODE]
    assert timelines[EXIT_STUCK_REASON_CODE] == [ORDER_OUTCOME_ATTESTED]


async def test_an_order_that_did_not_trade_is_never_sold_again_by_the_clerk_and_the_operators_sale_closes_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order never traded: Alpaca holds the bot's 10 SPY, as the Clerk's records say.

    The check says so, and says that it shows nothing about the order. "No
    such order" is taken, and for fifteen minutes of passes in the regular
    session the Clerk sends nothing: an account that agrees with its records
    is exactly where the watchdog used to sell again by itself.

    The operator then sells from the bot's page. The Flatten reads the
    account before it sends, sells the 10 once, and its fill leaves the bot
    flat: both episodes resolve, as after any stuck exit.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    (order,) = repo.unknown_exit_orders(SID)

    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, current_context, ABSENT)

    assert findings.would_settle is True
    assert findings.lines == (
        "Alpaca shows no order under this order's id.",
        "No order is working on SPY.",
        "Since this one was sent, Alpaca shows no order for SPY that the Clerk did not place.",
        "Alpaca holds 10 SPY; the Clerk's records say 10 SPY.",
        "None of this shows whether this order traded. Once settled, the exit ends as not sent; the "
        "Clerk holds 10 SPY for the bot and sends no order for them by itself. You then sell them "
        "or write them off from the bot's page.",
    )
    _the_position_is_left_to_the_operator(repo, exit_id)

    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == 1, "the Clerk sends nothing by itself, though Alpaca agrees with its records"
    _the_position_is_left_to_the_operator(repo, exit_id)

    assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
    alpaca.reads.clear()
    await _flatten(facade, current_context)

    assert alpaca.reads[:3] == ["orders:open", "positions", "submit"], "the Flatten checked the account first"
    assert [(sent.leg.side, sent.leg.quantity) for sent in alpaca.sent[1:]] == [("sell", 10)]
    alpaca.fills(alpaca.sent[1].client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 2)

    assert repo.position(SID, "SPY") == 0.0
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is None
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is None
    assert len(alpaca.sent) == 2, "the 10 SPY were sold once, by the operator"


async def test_a_position_closed_by_hand_at_alpaca_is_settled_never_sold_again_and_written_off(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The operator closed the stuck position by hand: a sell the Clerk did not place sold the bot's 10 SPY.

    Alpaca holds none of them now, against the Clerk's 10. The check says
    what the sale sold and what each side holds, and that none of it shows
    whether the lost order traded. "No such order" is taken at once -- no
    pass need have recorded the difference first -- with the sale and both
    quantities on the record. In the fifteen minutes of passes that follow
    the Clerk sends nothing.

    The operator then writes the shares off from the bot's page: Alpaca is
    flat for them, so the write-off goes through, the bot holds nothing, and
    both episodes resolve.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    alpaca.ended_orders = [_elsewhere(BY_HAND_ORDER_ID, status="filled", filled_quantity=10.0)]
    alpaca.holds = 0.0
    assert _open_position_drift(repo) is None, "no pass has recorded the difference"
    (order,) = repo.unknown_exit_orders(SID)

    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, current_context, ABSENT)

    assert findings.would_settle is True
    assert findings.lines == (
        "Alpaca shows no order under this order's id.",
        "No order is working on SPY.",
        "Since this one was sent, an order the Clerk did not place sold 10 SPY at Alpaca (Alpaca "
        f"order id {BY_HAND_ORDER_ID}, filled).",
        "Alpaca holds no SPY; the Clerk's records say 10 SPY.",
        "None of this shows whether this order traded. Once settled, the exit ends as not sent; the "
        "Clerk holds 10 SPY for the bot and sends no order for them by itself. You then sell them "
        "or write them off from the bot's page.",
    )
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (EndedForeignOrder(BY_HAND_ORDER_ID, "filled", "sell", 10.0),)
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (0.0, 10.0)
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None
    assert "Alpaca held no SPY and the Clerk's records said 10 SPY;" in notice["explanation"]
    _the_position_is_left_to_the_operator(repo, exit_id)

    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == 1, "the shares sold by hand are never sold again"
    _the_position_is_left_to_the_operator(repo, exit_id)

    written_off = await _the_operators_cure(facade, current_context, "discharge_attributed_residue")

    assert written_off.applied is True
    assert repo.position(SID, "SPY") == 0.0
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is None
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is None
    assert len(alpaca.sent) == 1


async def _shares_outside_the_clerks_records(repo: ClerkSqliteRepository, alpaca: _AlpacaAccount) -> str:
    """Alpaca held 20 SPY against the Clerk's 10 before the lost sale sold 10 unseen: each side reads 10 after."""
    alpaca.holds = 20.0
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.fills(_closing_order(repo).client_order_id)
    return exit_id


async def _another_bots_buy_the_clerk_has_not_folded(repo: ClerkSqliteRepository, alpaca: _AlpacaAccount) -> str:
    """Another bot of the account bought 10 SPY; Alpaca filled it, and the Clerk has not folded that fill yet.

    The lost sale sold this bot's 10 unseen. Alpaca holds the other bot's
    10, and the Clerk's records still say this bot's 10 and none for the
    other.
    """
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca, wait_passed=False)
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=ANOTHER_BOT, symbol="SPY", config_hash="h2"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=ANOTHER_BOT, lifecycle_run_id="run-2")
    bought = await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=ANOTHER_BOT, decision_id="enter-2",
        lifecycle_run_id="run-2", leg=safe_flatten._leg(quantity=10, side="buy"), trade=alpaca,
    )
    assert bought.order_ref is not None
    alpaca.fills(_closing_order(repo).client_order_id)
    alpaca.fills_a_buy(bought.order_ref)
    assert repo.position(ANOTHER_BOT, "SPY") == 0.0, "the Clerk has not folded the other bot's fill"
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    alpaca.reads.clear()
    return exit_id


async def _a_buy_resting_since_before_the_order_was_sent(repo: ClerkSqliteRepository, alpaca: _AlpacaAccount) -> str:
    """A buy the Clerk did not place rested at Alpaca since the day before, and filled after the lost sale sold.

    It was sent before the closing order was created, so it is not among the
    symbol's orders since; and it has ended, so it is not among the open
    orders either.
    """
    resting = _elsewhere(BY_HAND_ORDER_ID, side="buy", submitted_at_ms=FIXTURE_RTH_MS - 86_400_000)
    alpaca.open_orders = [resting]
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.fills(_closing_order(repo).client_order_id)
    alpaca.open_orders = []
    alpaca.ended_orders = [
        resting.model_copy(update={"status": "filled", "filled_quantity": 10.0, "filled_avg_price": 100.0})
    ]
    alpaca.holds = 10.0
    return exit_id


async def _a_buy_made_by_hand_since_the_order_was_sent(repo: ClerkSqliteRepository, alpaca: _AlpacaAccount) -> str:
    """A buy of 10 SPY made by hand filled after the lost sale sold the bot's 10 unseen."""
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.fills(_closing_order(repo).client_order_id)
    alpaca.ended_orders = [_elsewhere(BY_HAND_ORDER_ID, side="buy", status="filled", filled_quantity=10.0)]
    alpaca.holds = 10.0
    return exit_id


_NO_ORDER_THE_CLERK_DID_NOT_PLACE = (
    "Since this one was sent, Alpaca shows no order for SPY that the Clerk did not place."
)


@pytest.mark.parametrize(
    ("the_sale_is_hidden_by", "the_clerk_saw"),
    [
        pytest.param(
            _shares_outside_the_clerks_records, _NO_ORDER_THE_CLERK_DID_NOT_PLACE,
            id="shares-the-account-held-outside-the-records",
        ),
        pytest.param(
            _another_bots_buy_the_clerk_has_not_folded, _NO_ORDER_THE_CLERK_DID_NOT_PLACE,
            id="another-bots-buy-not-folded-yet",
        ),
        pytest.param(
            _a_buy_resting_since_before_the_order_was_sent, _NO_ORDER_THE_CLERK_DID_NOT_PLACE,
            id="a-buy-resting-since-before-the-order-was-sent",
        ),
        pytest.param(
            _a_buy_made_by_hand_since_the_order_was_sent,
            "Since this one was sent, an order the Clerk did not place bought 10 SPY at Alpaca (Alpaca "
            f"order id {BY_HAND_ORDER_ID}, filled).",
            id="a-buy-made-by-hand-since",
        ),
    ],
)
async def test_a_sale_that_traded_unseen_behind_a_position_that_still_agrees_is_never_made_a_second_time(
    crashed_with_exposure,  # noqa: F811
    the_sale_is_hidden_by: Callable[[ClerkSqliteRepository, _AlpacaAccount], Awaitable[str]],
    the_clerk_saw: str,
) -> None:
    """The regressions (#2854 review): the lost sale did trade, and Alpaca's position agrees with the Clerk's records all the same.

    Four ways it can: shares the account held outside the Clerk's records
    before the order was sent; another bot's buy that Alpaca filled and the
    Clerk has not folded; a buy resting since before the order was sent,
    which the symbol's orders since leave out; a buy made by hand since.
    While the Clerk took an agreeing position to show the order had not
    traded, each of them ended the EXIT as not sent and let the watchdog
    sell the bot's 10 SPY a second time.

    "No such order" is taken in all four: no order carries this order's id
    and none is working on the symbol, and that is all it says. The check
    says what the Clerk saw, and that it does not show whether the order
    traded. The position is left to the operator in the same write, and
    fifteen minutes of passes in the regular session send nothing.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id = await the_sale_is_hidden_by(repo, alpaca)
    facade = _facade(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)
    sent = len(alpaca.sent)

    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, _context_of(repo, SID), ABSENT)

    assert findings.would_settle is True
    assert findings.lines[2:] == (
        the_clerk_saw,
        "Alpaca holds 10 SPY; the Clerk's records say 10 SPY.",
        "None of this shows whether this order traded. Once settled, the exit ends as not sent; the "
        "Clerk holds 10 SPY for the bot and sends no order for them by itself. You then sell them "
        "or write them off from the bot's page.",
    )
    (recorded,) = _attested(repo)
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (10.0, 10.0)
    _the_position_is_left_to_the_operator(repo, exit_id)

    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == sent, "the 10 SPY the lost order sold are never sold a second time"
    _the_position_is_left_to_the_operator(repo, exit_id)


def _the_lookup_fails(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.fails["lookup"] = BrokerUnavailable("alpaca timed out")


def _the_open_orders_cannot_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.fails["orders:open"] = BrokerUnavailable("alpaca timed out")


def _the_positions_cannot_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.positions_unreadable = BrokerUnavailable("alpaca timed out")


def _the_position_is_not_a_number(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.holds = math.nan


def _the_orders_since_cannot_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.fails["orders:all"] = BrokerUnavailable("alpaca timed out")


def _the_open_order_page_is_full(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.open_orders = [
        _elsewhere(f"qqq-{index}", symbol="QQQ", side="buy") for index in range(MAX_OPEN_ORDER_SNAPSHOT)
    ]


def _the_symbol_has_more_orders_since_than_the_clerk_reads(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [
        _elsewhere(f"spy-{index}", side="buy", status="canceled", submitted_at_ms=FIXTURE_RTH_MS + index)
        for index in range(MAX_SYMBOL_ORDER_PAGES * MAX_OPEN_ORDER_SNAPSHOT + 1)
    ]


def _the_exact_lookup_answers(alpaca: _AlpacaAccount, closing: str) -> None:
    alpaca.now_has(closing)


def _the_open_orders_list_it(alpaca: _AlpacaAccount, closing: str) -> None:
    alpaca.open_orders = [_elsewhere("alpaca-closing", client_order_id=closing)]


def _the_orders_since_list_it_filled(alpaca: _AlpacaAccount, closing: str) -> None:
    alpaca.ended_orders = [_elsewhere("alpaca-closing", client_order_id=closing, status="filled")]


def _it_fills_after_the_open_orders_were_read(alpaca: _AlpacaAccount, closing: str) -> None:
    alpaca.meanwhile[("orders:all", 1)] = lambda: _the_orders_since_list_it_filled(alpaca, closing)


def _a_buy_is_working_on_the_symbol(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.open_orders = [_elsewhere("someone-buys", side="buy")]


def _a_buy_sent_the_day_before_is_still_working(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.open_orders = [_elsewhere("resting-buy", side="buy", submitted_at_ms=FIXTURE_RTH_MS - 86_400_000)]


def _a_buy_is_sent_after_the_open_orders_were_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.meanwhile[("orders:all", 1)] = lambda: _a_buy_is_working_on_the_symbol(alpaca, _closing)


def _an_open_order_names_no_symbol(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.open_orders = [_elsewhere("no-symbol", symbol="", side="buy")]


def _a_sell_is_working_that_the_open_orders_do_not_list(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [_elsewhere("another-sell", status="partially_filled", filled_quantity=4.0)]


def _an_ended_order_could_not_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [
        _elsewhere("garbled", status="canceled").model_copy(update={"unreadable_fields": ("filled_qty",)})
    ]


def _a_replaced_sell_whose_replacement_was_not_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [
        _elsewhere("replaced-sell", status="replaced").model_copy(update={"replaced_by": "its-replacement"})
    ]


def _a_replaced_buy_whose_replacement_was_not_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [
        _elsewhere("replaced-buy", side="buy", status="replaced").model_copy(
            update={"replaced_by": "its-replacement"}
        )
    ]


@pytest.mark.parametrize(
    ("alpaca_shows", "reason_code"),
    [
        pytest.param(_the_lookup_fails, "BROKER_UNREADABLE", id="the-exact-lookup-fails"),
        pytest.param(_the_open_orders_cannot_be_read, "BROKER_UNREADABLE", id="open-orders-unreadable"),
        pytest.param(_the_positions_cannot_be_read, "BROKER_UNREADABLE", id="positions-unreadable"),
        pytest.param(_the_position_is_not_a_number, "BROKER_UNREADABLE", id="position-not-a-number"),
        pytest.param(_the_orders_since_cannot_be_read, "BROKER_UNREADABLE", id="orders-since-unreadable"),
        pytest.param(_the_open_order_page_is_full, "ORDER_LIST_INCOMPLETE", id="open-order-page-full"),
        pytest.param(
            _the_symbol_has_more_orders_since_than_the_clerk_reads, "ORDER_LIST_INCOMPLETE",
            id="more-pages-of-the-symbols-orders-than-the-clerk-reads",
        ),
        pytest.param(_the_exact_lookup_answers, "ORDER_EXISTS_AT_BROKER", id="the-exact-lookup-answers"),
        pytest.param(_the_open_orders_list_it, "ORDER_EXISTS_AT_BROKER", id="listed-open-under-our-id"),
        pytest.param(_the_orders_since_list_it_filled, "ORDER_EXISTS_AT_BROKER", id="listed-filled-under-our-id"),
        pytest.param(
            _it_fills_after_the_open_orders_were_read, "ORDER_EXISTS_AT_BROKER",
            id="filled-under-our-id-after-the-open-orders-were-read",
        ),
        pytest.param(_a_buy_is_working_on_the_symbol, "BROKER_ORDER_WORKING", id="an-order-working-on-the-symbol"),
        pytest.param(
            _a_buy_sent_the_day_before_is_still_working, "BROKER_ORDER_WORKING",
            id="a-working-buy-only-the-open-orders-list",
        ),
        pytest.param(
            _a_buy_is_sent_after_the_open_orders_were_read, "BROKER_ORDER_WORKING",
            id="a-buy-sent-after-the-open-orders-were-read",
        ),
        pytest.param(_an_open_order_names_no_symbol, "BROKER_ORDER_WORKING", id="a-working-order-names-no-symbol"),
        pytest.param(
            _a_sell_is_working_that_the_open_orders_do_not_list, "BROKER_ORDER_WORKING",
            id="a-working-sell-only-the-orders-since-list",
        ),
        pytest.param(_an_ended_order_could_not_be_read, "UNKNOWN_ORDER_ON_SYMBOL", id="an-unreadable-order-since"),
        pytest.param(
            _a_replaced_sell_whose_replacement_was_not_read, "UNKNOWN_ORDER_ON_SYMBOL",
            id="a-replaced-sell-whose-replacement-was-not-read",
        ),
        pytest.param(
            _a_replaced_buy_whose_replacement_was_not_read, "UNKNOWN_ORDER_ON_SYMBOL",
            id="a-replaced-buy-whose-replacement-was-not-read",
        ),
    ],
)
async def test_an_absent_attestation_alpaca_does_not_bear_out_is_refused_and_nothing_is_written(
    crashed_with_exposure,  # noqa: F811
    alpaca_shows: Callable[[_AlpacaAccount, str], None],
    reason_code: str,
) -> None:
    """Whatever the operator says, the Clerk ends the order as not sent only on its own reads.

    A read that fails or answers with what the Clerk cannot record, a list
    that may be incomplete, the order itself in any read, any order working
    on the symbol, an ended order the Clerk could not read or whose
    replacement it did not see: each refuses. The ledger is exactly as it
    was, the EXIT still holds custody, and nothing is sent.

    The symbol's orders are read after the open orders, so neither list
    alone is enough and no working order falls between them: one resting
    since before this order was sent is only among the open orders, and one
    sent after the open orders were read -- this order itself included,
    should it surface and fill by then -- is only among the symbol's orders.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    (order,) = repo.unknown_exit_orders(SID)
    alpaca_shows(alpaca, closing.order_ref)
    ledger = repo.custody_transitions()
    facade = _facade(repo, alpaca)

    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await facade.settle_unknown_exit_order(
            strategy_instance_id=SID, presented=order, attestation=ABSENT, operator=OPERATOR, reason=None,
        )

    assert refused.value.reason_code == reason_code
    assert "Nothing was changed." in str(refused.value)
    assert repo.custody_transitions() == ledger
    assert _exit_state(repo, exit_id) == "unknown"
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is not None
    assert len(alpaca.sent) == 1
    # The dispatcher tells the operator the same thing, and still writes nothing.
    alpaca.reads.clear()
    with pytest.raises(RecoveryExecutionError, match="Nothing was changed"):
        await _settle(facade, _context_of(repo, SID), ABSENT)
    assert repo.custody_transitions() == ledger


async def test_an_order_settled_as_absent_that_had_filled_unseen_is_credited_once_alpaca_shows_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order did fill, and no read showed it: Alpaca holds none of the bot's 10 SPY.

    Settled as absent, the EXIT ends with the 10 SPY still attributed and
    left to the operator. The Clerk goes on looking the closing order of a
    failed EXIT up while its symbol is drifted: when Alpaca shows it filled,
    the fill is credited once and the bot holds nothing, as Alpaca says.
    That is the flat every stuck exit is resolved by, so both episodes
    close. Nothing is sold twice.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    alpaca.fills(closing.client_order_id)
    assert alpaca.holds == 0.0

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)
    _the_position_is_left_to_the_operator(repo, exit_id)

    alpaca.answers_its_lookup(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 3)

    assert repo.position(SID, "SPY") == 0.0
    assert repo.effective_fill_totals_for_order(closing.order_ref)[0] == 10.0
    assert _exit_state(repo, exit_id) == "failed"
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is None
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is None
    assert len(alpaca.sent) == 1


async def test_an_order_sent_moments_ago_is_not_ended_as_absent_until_alpaca_would_show_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Alpaca answered the send under another client order id, so the action is offered at once.

    An order sent moments ago may not show in a read yet, so "no such order"
    waits out the submit-absence wait like the Clerk's own re-send does. It
    is refused on the Clerk's own records, before Alpaca is asked anything.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs("an-id-the-clerk-never-sent"),))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca, wait_passed=False)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    ledger = repo.custody_transitions()
    assert (await _presented(current_context)).available

    with pytest.raises(RecoveryExecutionError, match="Alpaca may not show it yet"):
        await _settle(facade, current_context, ABSENT)
    (order,) = repo.unknown_exit_orders(SID)
    checked = await facade.check_unknown_exit_order(
        strategy_instance_id=SID, presented=order, attestation=ABSENT
    )

    assert (checked.would_settle, checked.refusal_reason_code) == (False, "ORDER_LOOKUP_STILL_SETTLING")
    assert alpaca.reads == [], "the wait is refused on the Clerk's own records, before Alpaca is asked anything"
    assert repo.custody_transitions() == ledger
    assert _exit_state(repo, exit_id) == "unknown"
    assert len(alpaca.sent) == 1


def _answered_with(**as_it_stands: object) -> BrokerOrder:
    """The order Alpaca answered the closing order's send with, under the id the Clerk never sent."""
    return _elsewhere(ANSWERED_ORDER_ID, client_order_id=ANSWERED_AS, **as_it_stands)  # type: ignore[arg-type]


async def test_an_order_alpaca_answered_under_another_id_is_settled_as_absent_once_that_order_has_ended(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The order Alpaca answered the send with is under an id the Clerk never sent.

    While it works, neither choice settles: it may be this order, so "no
    such order" is refused, and the Clerk cannot take up an order under
    another client order id yet, so "it is this Alpaca order" is refused
    too. Both refusals say so, and say what would settle it. Once the
    Clerk's own read of that order shows it ended with nothing filled, it
    sold nothing and can sell nothing more: "no such order" ends the EXIT as
    not sent, and that read is on the record.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs(ANSWERED_AS),))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    alpaca.has_under_another_id(_answered_with())
    ledger = repo.custody_transitions()

    with pytest.raises(RecoveryExecutionError) as absent_refused:
        await _settle(facade, current_context, ABSENT)
    with pytest.raises(RecoveryExecutionError) as exists_refused:
        await _settle(
            facade, current_context, OrderOutcomeAttestation(kind="exists", broker_order_id=ANSWERED_ORDER_ID)
        )

    assert "that order is still working at Alpaca (new, 0 filled)" in str(absent_refused.value)
    assert "The Clerk cannot take up an order under another id yet" in str(absent_refused.value)
    assert (
        'Once that order has ended without selling anything, "Alpaca has no such order" settles this one.'
        in str(absent_refused.value)
    )
    assert "is the order Alpaca answered this send with" in str(exists_refused.value)
    assert (
        "While that order is working, this one cannot be settled either way; once it has ended "
        'without selling anything, "Alpaca has no such order" settles it.' in str(exists_refused.value)
    )
    assert repo.custody_transitions() == ledger and _exit_state(repo, exit_id) == "unknown"

    alpaca.has_under_another_id(_answered_with(status="canceled"))
    alpaca.reads.clear()
    (order,) = repo.unknown_exit_orders(SID)
    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, current_context, ABSENT)

    assert findings.would_settle is True
    assert findings.lines[1] == (
        f"The order Alpaca answered this order's send with (Alpaca order {ANSWERED_ORDER_ID}) has "
        "ended, canceled, without selling anything."
    )
    assert findings.lines[3] == (
        "Since this one was sent, Alpaca shows no other order for SPY that the Clerk did not place."
    )
    # The order Alpaca answered with is read by its own id: once for the check, once to settle.
    assert alpaca.reads == ["lookup", "lookup:other-id", "orders:open", "positions", "orders:all"] * 2
    _the_position_is_left_to_the_operator(repo, exit_id)
    (recorded,) = _attested(repo)
    assert recorded.answered_as_order == EndedForeignOrder(ANSWERED_ORDER_ID, "canceled", "sell", 0.0)
    assert recorded.ended_foreign_orders is None, "the order Alpaca answered with is not one more ended order"
    assert len(alpaca.sent) == 1


async def test_an_order_alpaca_answered_under_another_id_that_sold_is_never_settled_as_absent_or_sold_twice(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The regression (#2854 review): the order Alpaca answered the send with filled, and stray shares hid it.

    Alpaca answered the closing order's send under another client order id,
    and that order sold the bot's 10 SPY. The account also holds 10 SPY that
    are in none of the Clerk's records: Alpaca held 20 against the Clerk's
    10 before the sale and holds 10 against 10 after it, so the position
    agrees and the drift a pass had recorded clears by itself. "No such
    order" was then taken: the check said it would settle, the EXIT ended as
    not sent and released a position already sold, and eight passes later
    the watchdog sold 10 SPY a second time.

    The Clerk reads that order itself. It sold, so the check and the
    settlement refuse in the same words, which name the order and what it
    did; nothing is written, the EXIT keeps custody, and no pass in the next
    fifteen minutes sends a second sale.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs(ANSWERED_AS),))
    alpaca.holds = 20.0
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    await _passes(repo, _the_sweep(repo, alpaca), 2)
    assert _open_position_drift(repo) is not None, "20 SPY at Alpaca against the Clerk's 10"
    alpaca.has_under_another_id(_answered_with(status="filled", filled_quantity=10.0))
    alpaca.holds = 10.0
    await _passes(repo, _the_sweep(repo, alpaca), 2)
    assert _open_position_drift(repo) is None, "10 against 10: the drift cleared by itself"
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()

    checked = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await facade.settle_unknown_exit_order(
            strategy_instance_id=SID, presented=order, attestation=ABSENT, operator=OPERATOR, reason=None,
        )
    with pytest.raises(RecoveryExecutionError) as dispatched:
        await _settle(facade, current_context, ABSENT)

    assert (checked.would_settle, checked.refusal_reason_code) == (False, "ANSWERED_ORDER_FILLED")
    assert refused.value.reason_code == "ANSWERED_ORDER_FILLED"
    assert checked.lines == (str(refused.value),) == (str(dispatched.value),)
    assert str(refused.value) == (
        f"Alpaca answered this order's send with another order (client order id {ANSWERED_AS!r}, "
        f"Alpaca order id {ANSWERED_ORDER_ID!r}), and that order sold 10 SPY under that id (filled). "
        "This order cannot be settled as absent. The Clerk cannot take up an order under another id "
        "yet, so it keeps the position held. Nothing was changed."
    )
    assert repo.custody_transitions() == ledger
    assert _attested(repo) == []

    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == 1, "the 10 SPY Alpaca sold are never sold a second time"
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None, "the EXIT keeps custody of the position"
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is None
    assert repo.position(SID, "SPY") == 10.0


def _open_position_drift(repo: ClerkSqliteRepository) -> dict | None:
    return repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=POSITION_DRIFT_REASON_CODE, strategy_instance_id=None
    )


def _alpaca_shows_no_order_under_that_id(_alpaca: _AlpacaAccount) -> None:
    """Nothing at Alpaca answers to the id it answered the send under."""


def _its_lookup_fails(alpaca: _AlpacaAccount) -> None:
    alpaca.fails["lookup:other-id"] = BrokerUnavailable("alpaca timed out")


def _it_could_not_be_read_in_full(alpaca: _AlpacaAccount) -> None:
    alpaca.has_under_another_id(
        _answered_with(status="canceled").model_copy(update={"unreadable_fields": ("filled_qty",)})
    )


def _it_is_still_working(alpaca: _AlpacaAccount) -> None:
    alpaca.has_under_another_id(_answered_with(status="partially_filled", filled_quantity=4.0))


def _alpaca_replaced_it(alpaca: _AlpacaAccount) -> None:
    alpaca.has_under_another_id(
        _answered_with(status="replaced").model_copy(update={"replaced_by": BY_HAND_ORDER_ID})
    )


def _it_was_canceled_after_selling_some(alpaca: _AlpacaAccount) -> None:
    alpaca.has_under_another_id(_answered_with(status="canceled", filled_quantity=4.0))


def _only_the_symbols_orders_list_it_filled(alpaca: _AlpacaAccount) -> None:
    alpaca.ended_orders = [_answered_with(status="filled", filled_quantity=10.0)]


@pytest.mark.parametrize(
    ("answered_as", "alpaca_shows", "reason_code", "says"),
    [
        pytest.param(
            ANSWERED_AS, _alpaca_shows_no_order_under_that_id, "ANSWERED_ORDER_NOT_READ",
            "the Clerk found no order under that id at Alpaca", id="no-order-under-that-id",
        ),
        pytest.param(
            ANSWERED_AS, _it_could_not_be_read_in_full, "ANSWERED_ORDER_NOT_READ",
            "the Clerk could not read all of that order", id="not-read-in-full",
        ),
        pytest.param(
            None, _alpaca_shows_no_order_under_that_id, "ANSWERED_ORDER_NOT_READ",
            "an order that names no client order id, so the Clerk has no id to read that order by",
            id="answered-with-no-id-to-read-it-by",
        ),
        pytest.param(
            ANSWERED_AS, _its_lookup_fails, "BROKER_UNREADABLE",
            "The Clerk could not read the account at Alpaca (alpaca timed out)", id="its-lookup-fails",
        ),
        pytest.param(
            ANSWERED_AS, _it_is_still_working, "ANSWERED_ORDER_WORKING",
            "that order is still working at Alpaca (partially_filled, 4 filled)", id="still-working",
        ),
        pytest.param(
            ANSWERED_AS, _alpaca_replaced_it, "ANSWERED_ORDER_REPLACED",
            f"Alpaca has since replaced that order with Alpaca order {BY_HAND_ORDER_ID!r}", id="replaced",
        ),
        pytest.param(
            ANSWERED_AS, _it_was_canceled_after_selling_some, "ANSWERED_ORDER_FILLED",
            "that order sold 4 SPY under that id (canceled)", id="canceled-after-selling-some",
        ),
        pytest.param(
            ANSWERED_AS, _only_the_symbols_orders_list_it_filled, "ANSWERED_ORDER_FILLED",
            "that order sold 10 SPY under that id (filled)", id="filled-and-only-the-symbols-orders-list-it",
        ),
    ],
)
async def test_no_such_order_is_refused_unless_the_order_alpaca_answered_with_was_read_ended_and_unfilled(
    crashed_with_exposure,  # noqa: F811
    answered_as: str | None,
    alpaca_shows: Callable[[_AlpacaAccount], None],
    reason_code: str,
    says: str,
) -> None:
    """ "No such order" for a send Alpaca answered under another id rests on the Clerk's read of that order.

    Not readable -- no order under that id, one the Clerk could not read in
    full, an answer that named no id to read it by, a lookup that fails --
    still working, replaced, or ended after filling anything: each refuses
    in its own words, the check and the settlement alike, and nothing is
    written or sent. The account's position agrees with the Clerk's records
    throughout, and answers for nothing.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs(answered_as),))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca_shows(alpaca)
    facade = _facade(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()

    checked = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await facade.settle_unknown_exit_order(
            strategy_instance_id=SID, presented=order, attestation=ABSENT, operator=OPERATOR, reason=None,
        )

    assert (checked.would_settle, checked.refusal_reason_code) == (False, reason_code)
    assert refused.value.reason_code == reason_code
    assert checked.lines == (str(refused.value),)
    assert says in str(refused.value) and str(refused.value).endswith("Nothing was changed.")
    assert repo.custody_transitions() == ledger
    assert _exit_state(repo, exit_id) == "unknown"
    assert len(alpaca.sent) == 1


async def test_orders_the_clerk_did_not_place_are_shown_and_recorded_each_by_its_own_side(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Since the closing sale was sent, the symbol saw orders the Clerk did not place, on both sides.

    A buy filled 4 before it was canceled, a sale tried by hand was canceled
    with nothing filled, and a buy expired unfilled. None of them is
    working, so none refuses. The check says what each did by its own side,
    one line for each that traded and one for those that did not, and the
    attestation records them all, with their sides.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.ended_orders = [
        _elsewhere("another-buy", side="buy", status="canceled", filled_quantity=4.0),
        _elsewhere("by-hand", status="canceled", submitted_at_ms=FIXTURE_RTH_MS - 1),
        _elsewhere("a-buy", side="buy", status="expired", submitted_at_ms=FIXTURE_RTH_MS - 2),
        # Not this order's symbol, and from before it was sent: neither is asked about, filled as they are.
        _elsewhere("qqq", symbol="QQQ", status="filled", filled_quantity=10.0),
        _elsewhere("long-ago", status="filled", filled_quantity=10.0, submitted_at_ms=FIXTURE_RTH_MS - 86_400_000),
    ]
    alpaca.holds = 14.0
    facade = _facade(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)

    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, _context_of(repo, SID), ABSENT)

    assert findings.would_settle is True
    assert findings.lines[2:5] == (
        "Since this one was sent, an order the Clerk did not place bought 4 SPY at Alpaca (Alpaca "
        "order id another-buy, canceled).",
        "Since this one was sent, 2 orders for SPY that the Clerk did not place ended at Alpaca with "
        "nothing filled: Alpaca order by-hand, to sell, canceled; Alpaca order a-buy, to buy, expired.",
        "Alpaca holds 14 SPY; the Clerk's records say 10 SPY.",
    )
    assert {symbols for symbols, _until in alpaca.pages_asked} == {("SPY",)}
    _the_position_is_left_to_the_operator(repo, exit_id)
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (
        EndedForeignOrder("another-buy", "canceled", "buy", 4.0),
        EndedForeignOrder("by-hand", "canceled", "sell", 0.0),
        EndedForeignOrder("a-buy", "expired", "buy", 0.0),
    )
    assert recorded.orders_since_read == 3, "the symbol's orders since: the buy that filled some and the two that did not"
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (14.0, 10.0)
    assert len(alpaca.sent) == 1


async def test_the_check_speaks_only_of_orders_the_clerk_did_not_place_when_its_own_sales_are_listed(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A sell the Clerk placed itself, for another bot or by hand, is listed since this one was sent.

    No order the Clerk did not place is, and the check says exactly that. It
    said "Alpaca shows no other order to sell SPY since this one was sent",
    which the listed sell made false.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    (in_the_clerks_records,) = repo.entry_orders_for_strategy(SID)
    # Listed as a sell under a client order id the Clerk's own records hold: an order it placed.
    alpaca.ended_orders = [
        _elsewhere("a-sell-the-clerk-placed", client_order_id=in_the_clerks_records.order_ref, status="canceled")
    ]
    (order,) = repo.unknown_exit_orders(SID)

    findings = await _facade(repo, alpaca).check_unknown_exit_order(
        strategy_instance_id=SID, presented=order, attestation=ABSENT
    )

    assert findings.would_settle is True
    assert findings.lines[2] == _NO_ORDER_THE_CLERK_DID_NOT_PLACE


def _the_bots_shares_were_sold_by_hand(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [_elsewhere(BY_HAND_ORDER_ID, status="filled", filled_quantity=10.0)]
    alpaca.holds -= 10.0


def _the_lost_sale_sold_unseen(alpaca: _AlpacaAccount, closing: str) -> None:
    alpaca.fills(closing)


def _the_lost_sale_never_arrived(_alpaca: _AlpacaAccount, _closing: str) -> None:
    """Alpaca holds the bot's 10 SPY, as the Clerk's records say."""


@pytest.mark.parametrize(
    ("submit", "alpaca_holds", "another_bot_holds_the_symbol", "at_alpaca"),
    [
        pytest.param(
            "never_arrives", 10.0, False, _the_bots_shares_were_sold_by_hand,
            id="closed-by-hand-the-bot-alone-in-the-symbol",
        ),
        pytest.param(
            "never_arrives", 20.0, True, _the_bots_shares_were_sold_by_hand,
            id="closed-by-hand-another-bot-holds-the-symbol",
        ),
        pytest.param(
            "never_arrives", 10.0, False, _the_lost_sale_never_arrived,
            id="the-order-did-not-trade",
        ),
        pytest.param(
            "arrives_unanswered", 20.0, False, _the_lost_sale_sold_unseen,
            id="the-order-traded-unseen-behind-shares-outside-the-records",
        ),
    ],
)
async def test_a_bots_scheduled_end_sells_nothing_once_its_closing_order_is_settled_as_not_sent(
    crashed_with_exposure,  # noqa: F811
    schedule: _Schedule,  # noqa: F811
    submit: _SubmitFate,
    alpaca_holds: float,
    another_bot_holds_the_symbol: bool,
    at_alpaca: Callable[[_AlpacaAccount, str], None],
) -> None:
    """The bot's owner set it to sell at its end, and its closing order is lost.

    The end comes: the Clerk stops the run, and the sale the end owes waits
    for the EXIT that holds the position. "No such order" ends that EXIT and
    leaves the position to the operator, so the end's sale must not go out:
    the Clerk cannot tell whether the bot's shares were sold already, and
    whatever Alpaca holds in the symbol may be another's.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(submit,))
    alpaca.holds = alpaca_holds
    if another_bot_holds_the_symbol:
        repo.register_strategy_instance(
            exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=ANOTHER_BOT, symbol="SPY", config_hash="h2"
        )
        submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=ANOTHER_BOT, lifecycle_run_id="run-2")
        await _make_held_position(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=ANOTHER_BOT, run_id="run-2",
            decision_id="enter-2", execution_id="exec-2",
        )
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    schedule.ends[SID] = ScheduledEnd(
        strategy_instance_id=SID, end=BotEnd(end_at_ms=repo.clock(), end_action="SELL")
    )
    at_alpaca(alpaca, _closing_order(repo).client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert repo.active_run(SID) is None, "the Clerk stopped the run at its end"
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _exit_state(repo, exit_id) == "unknown" and len(alpaca.sent) == 1

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)
    _the_position_is_left_to_the_operator(repo, exit_id)
    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == 1, "the end's sale is not sent for a position left to the operator"
    assert repo.position(SID, "SPY") == 10.0
    assert schedule.carried_out == [] and SID in schedule.ends, "the end was recorded carried out with the bot still holding"


async def _the_operator_writes_the_position_off(
    alpaca: _AlpacaAccount, facade: SqliteAlpacaClerkFacade, current_context: _Context
) -> None:
    """The bot's shares were sold by hand at Alpaca, and the operator writes the Clerk's record of them off."""
    alpaca.holds = 0.0
    written_off = await _the_operators_cure(facade, current_context, "discharge_attributed_residue")
    assert written_off.applied is True


async def _the_operator_sells_the_position(
    alpaca: _AlpacaAccount, facade: SqliteAlpacaClerkFacade, current_context: _Context
) -> None:
    """The operator flattens the position from the bot's page, and that sale fills."""
    assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
    await _flatten(facade, current_context)
    alpaca.fills(alpaca.sent[-1].client_order_id)


@pytest.mark.parametrize(
    ("the_operator_closes_it", "orders_sent_in_all"),
    [
        pytest.param(_the_operator_writes_the_position_off, 1, id="written-off"),
        pytest.param(_the_operator_sells_the_position, 2, id="sold-by-the-operator"),
    ],
)
async def test_an_end_still_pending_whose_own_lost_sale_is_settled_as_not_sent_waits_for_the_operator(
    crashed_with_exposure,  # noqa: F811
    schedule: _Schedule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
    the_operator_closes_it: Callable[[_AlpacaAccount, SqliteAlpacaClerkFacade, _Context], Awaitable[None]],
    orders_sent_in_all: int,
) -> None:
    """The lost closing order is the sale at the bot's scheduled end itself.

    The end comes: the Clerk stops the run and sends the end's sale, the
    answer is lost, and the record that the end was carried out does not
    land. "No such order" ends that sale as not sent and leaves the position
    to the operator, in one write. The failed sale is not the close of the
    holding: the end stays pending, the bell and the bot's page say it waits
    for the operator, and the Clerk sends nothing, pass after pass. Once the
    operator's write-off or sale leaves the bot flat, the end is carried out
    with no second sale of its own.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await safe_flatten._held_position(repo)
    the_end = ScheduledEnd(strategy_instance_id=SID, end=BotEnd(end_at_ms=repo.clock(), end_action="SELL"))
    schedule.ends[SID] = the_end
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    [end_sale] = _all_end_sales(repo)
    assert repo.active_run(SID) is None, "the Clerk stopped the run at its end"
    assert _exit_state(repo, end_sale) == "unknown" and len(alpaca.sent) == 1
    schedule.carried_out.clear()
    schedule.ends[SID] = the_end  # as if the carried-out record had not landed
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)

    await _settle(facade, current_context, ABSENT)
    _the_position_is_left_to_the_operator(repo, end_sale)
    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == 1, "the Clerk sold again for a position left to the operator"
    assert schedule.carried_out == [] and schedule.ends[SID] == the_end, (
        "the failed sale completed the end with the bot still holding"
    )
    page = _end_on_the_bots_page(repo, the_end)
    assert (page.status, page.waits_for_operator) == ("ending", True)
    [line] = await _end_lines(repo, monkeypatch)
    assert (line.reason_code, line.strategy_instance_id) == ("SCHEDULED_END_WAITS_FOR_OPERATOR", SID)

    await the_operator_closes_it(alpaca, facade, current_context)
    await _passes(repo, _the_sweep(repo, alpaca), 2)

    assert repo.position(SID, "SPY") == 0.0
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is None
    assert [end for end, _at_ms in schedule.carried_out] == [the_end]
    assert _all_end_sales(repo) == [end_sale] and len(alpaca.sent) == orders_sent_in_all, "the end sold again"
    assert await _end_lines(repo, monkeypatch) == []


async def test_an_end_recorded_carried_out_whose_own_lost_sale_is_settled_as_not_sent_is_told_as_no_order_sent(
    crashed_with_exposure,  # noqa: F811
    schedule: _Schedule,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lost closing order is the sale at the bot's scheduled end itself, and the record that
    the end was carried out stands, as it lands the moment the sale is accepted.

    "No such order" ends that sale as not sent and leaves the position to the
    operator. The end's card used to keep saying "sale put in" while the bot
    showed as stuck (#2854); it now says the sale was never at Alpaca, and
    the end's completion is the operator's.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await safe_flatten._held_position(repo)
    the_end = ScheduledEnd(strategy_instance_id=SID, end=BotEnd(end_at_ms=repo.clock(), end_action="SELL"))
    schedule.ends[SID] = the_end
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    [end_sale] = _all_end_sales(repo)
    assert repo.active_run(SID) is None, "the Clerk stopped the run at its end"
    assert _exit_state(repo, end_sale) == "unknown" and len(alpaca.sent) == 1
    [(carried, carried_at_ms)] = schedule.carried_out
    assert carried == the_end, "the end was recorded carried out the moment its sale was accepted"
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)

    await _settle(facade, current_context, ABSENT)
    _the_position_is_left_to_the_operator(repo, end_sale)
    await _passes(repo, _the_sweep(repo, alpaca), 2)

    assert len(alpaca.sent) == 1, "the Clerk sold again for a position left to the operator"
    assert repo.position(SID, "SPY") == 10.0
    page = _end_on_the_bots_page(repo, the_end, carried_out_at_ms=carried_at_ms)
    assert (page.status, page.waits_for_operator) == ("ended", True)
    assert page.headline == f"Ended {et_when_words(carried_at_ms, now_ms=repo.clock())} · no order sent"
    assert page.notice == (
        "The end's sale of 10 SPY was sent, its answer was lost, and an operator settled that "
        "Alpaca has no such order; the exit was ended as not sent. "
        "Sell the position or write it off from the bot's page."
    )
    assert page.explanation == (
        "The Clerk stopped the bot at its end and sent the sale of its shares, but the sale's "
        "answer was lost. An operator settled that Alpaca has no such order, so the exit was "
        "ended as not sent. The Clerk sends no order for the position by itself."
    )
    assert await _end_lines(repo, monkeypatch) == [], "the stuck exit bells; the end itself no longer asks"


async def test_a_replaced_sale_is_read_through_to_the_order_that_replaced_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A sell the Clerk did not place was replaced at Alpaca, and its replacement ended too.

    Both are among the orders read, so nothing of that sale can still be
    working: the order is settled, and both are on the record.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.ended_orders = [
        _elsewhere("replaced-sell", status="replaced").model_copy(update={"replaced_by": "its-replacement"}),
        _elsewhere("its-replacement", status="canceled", submitted_at_ms=FIXTURE_RTH_MS + 1),
    ]

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (
        EndedForeignOrder("its-replacement", "canceled", "sell", 0.0),
        EndedForeignOrder("replaced-sell", "replaced", "sell", 0.0),
    )


async def test_the_symbols_orders_since_are_read_a_page_at_a_time_to_their_end(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """More orders in the symbol since the closing order was sent than one read returns.

    The Clerk reads on from where each full page ended until a page comes
    back short, so a busy symbol no longer refuses for good. Two orders
    submitted in the same millisecond straddle the page's end: the second
    page asks for that millisecond again, and each order is counted once.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    newest_ms = FIXTURE_RTH_MS + 10_000
    page = MAX_OPEN_ORDER_SNAPSHOT
    alpaca.ended_orders = [
        # Newest first as listed: orders 499 and 500, either side of the page's end, share a millisecond.
        _elsewhere(
            f"spy-{index}", side="buy", status="canceled",
            submitted_at_ms=newest_ms - (index if index < page else index - 1),
        )
        for index in range(page + 200)
    ]

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    oldest_of_the_first_page_ms = newest_ms - (page - 1)
    assert alpaca.pages_asked == [(("SPY",), None), (("SPY",), oldest_of_the_first_page_ms + 1)]
    assert alpaca.reads == ["lookup", "orders:open", "positions", "orders:all", "orders:all"]
    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert recorded.orders_since_read == page + 200
    assert len(alpaca.sent) == 1


# ── A buy that covers a short: the same attestation, in words about a short ──


async def _a_stopped_bot_whose_cover_was_lost(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount, *, wait_passed: bool = True
) -> str:
    """A bot short 10 SPY decided to exit, its buy to cover timed out, and it was stopped.

    Returns the EXIT's effect, ``unknown``. Alpaca is short the same 10.
    """
    alpaca.holds = -10.0
    entry_ref = await safe_flatten._held_position(repo, side="sell")
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="program-exit-1",
        lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref,
    )
    exit_id = accepted.effect_operation_id
    assert exit_id is not None
    await resolve_exit(
        repo, effect_operation_id=exit_id, trade=alpaca, pricing=UNPRICEABLE_RECOVERY, read=None
    )
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )
    if wait_passed:
        _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _exit_state(repo, exit_id) == "unknown"
    assert [(sent.leg.side, sent.leg.quantity) for sent in alpaca.sent] == [("buy", 10)]
    assert repo.position(SID, "SPY") == -10.0
    alpaca.reads.clear()
    return exit_id


async def test_a_lost_buy_that_covers_a_short_is_offered_checked_and_settled_in_words_about_a_short(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The regression (#2854 review): the uncertain closing order is a buy that covers a short of 10 SPY.

    The choice said the shares stay with the bot and the Clerk may sell
    again, the check said Alpaca holds -10 SPY, and the notice said -10 SPY
    is still held. The choice, the check's lines and both episodes the exit
    ends under speak of a short, and of buying it back or writing it off.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_cover_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    (order,) = repo.unknown_exit_orders(SID)

    settlement = (await _presented(current_context)).settlement
    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, current_context, ABSENT)

    assert settlement is not None and settlement.side == "buy"
    assert settlement.ticket.startswith("The Clerk sent an order to buy 10 SPY on ")
    assert settlement.choices[0].explanation == (
        "The Clerk checks once more that this order is not at Alpaca and that nothing is working on "
        "SPY. If so, it ends this exit as not sent. The short position stays with the bot, and the "
        "Clerk sends no order for it by itself: you then buy it back or write it off from this "
        "page. This cannot be undone."
    )
    assert findings.lines == (
        "Alpaca shows no order under this order's id.",
        "No order is working on SPY.",
        "Since this one was sent, Alpaca shows no order for SPY that the Clerk did not place.",
        "Alpaca holds a short of 10 SPY; the Clerk's records say a short of 10 SPY.",
        "None of this shows whether this order traded. Once settled, the exit ends as not sent; the "
        "Clerk holds a short of 10 SPY for the bot and sends no order for it by itself. You then "
        "buy it back or write it off from the bot's page.",
    )
    _the_position_is_left_to_the_operator(repo, exit_id, held=-10.0)
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    stuck = _open_episode(repo, EXIT_STUCK_REASON_CODE)
    assert notice is not None and stuck is not None
    assert notice["explanation"] == (
        "The Clerk could not confirm the order to buy 10 SPY. An operator said Alpaca has no such "
        "order, the Clerk's own reads of Alpaca found no order under its client order id and none "
        "working on SPY, and the exit was ended as not sent; a short of 10 SPY is still open. When "
        "the Clerk read the account, Alpaca held a short of 10 SPY and the Clerk's records said a "
        "short of 10 SPY; that does not show whether the order traded. The Clerk sends no order "
        "for it by itself."
    )
    assert notice["next_step"] == (
        "Look at this account's position at Alpaca. If Alpaca is not short these shares, write off "
        "the short the Clerk records; otherwise buy back what is short. Both are on the bot's page."
    )
    assert stuck["explanation"] == (
        "An operator said Alpaca does not have this bot's order to buy 10 SPY, and the Clerk ended "
        "this exit as not sent. It holds a short of 10 SPY for the bot and sends no order for it by "
        "itself."
    )
    assert stuck["next_step"] == "Buy it back or write it off from the bot's page."
    assert len(alpaca.sent) == 1


async def test_a_short_bought_back_by_hand_settles_a_lost_cover_and_each_order_is_said_by_its_own_side(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order is a buy that covers a short of 10 SPY, and the operator bought the 10 back by hand.

    Alpaca is short none of the bot's 10 now, against the short the Clerk
    records. The check says that buy bought, that a sale the Clerk did not
    place sold 3 before it was canceled, and what each side holds, a short
    as a short. "No such order" is taken with those orders and both
    quantities on the record. A sale tried by hand that expired with nothing
    filled is listed beside them, by what it did.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_cover_was_lost(repo, alpaca)
    alpaca.ended_orders = [
        _elsewhere(BY_HAND_ORDER_ID, side="buy", status="filled", filled_quantity=10.0),
        _elsewhere("a-sale", side="sell", status="canceled", filled_quantity=3.0, submitted_at_ms=FIXTURE_RTH_MS - 1),
        _elsewhere("a-sell", side="sell", status="expired", submitted_at_ms=FIXTURE_RTH_MS - 2),
    ]
    alpaca.holds = -3.0
    facade = _facade(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)

    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, _context_of(repo, SID), ABSENT)

    assert findings.would_settle is True
    assert findings.lines[2:6] == (
        "Since this one was sent, an order the Clerk did not place bought 10 SPY at Alpaca (Alpaca "
        f"order id {BY_HAND_ORDER_ID}, filled).",
        "Since this one was sent, an order the Clerk did not place sold 3 SPY at Alpaca (Alpaca "
        "order id a-sale, canceled).",
        "Since this one was sent, 1 order for SPY that the Clerk did not place ended at Alpaca with "
        "nothing filled: Alpaca order a-sell, to sell, expired.",
        "Alpaca holds a short of 3 SPY; the Clerk's records say a short of 10 SPY.",
    )
    _the_position_is_left_to_the_operator(repo, exit_id, held=-10.0)
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (
        EndedForeignOrder(BY_HAND_ORDER_ID, "filled", "buy", 10.0),
        EndedForeignOrder("a-sale", "canceled", "sell", 3.0),
        EndedForeignOrder("a-sell", "expired", "sell", 0.0),
    )
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (-3.0, -10.0)
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None
    assert (
        "When the Clerk read the account, Alpaca held a short of 3 SPY and the Clerk's records said "
        "a short of 10 SPY;" in notice["explanation"]
    )
    assert len(alpaca.sent) == 1


# ── Attestation: "Alpaca shows it, under this order id" ──────────────────────


async def _a_stopped_bot_whose_closing_order_alpaca_has_unseen(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    """The lost closing order reached Alpaca and works there; its exact lookup still reads absent.

    Returns the EXIT's effect and the attestation an operator who found the
    order at Alpaca makes: its Alpaca order id.
    """
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    return exit_id, OrderOutcomeAttestation(
        kind="exists", broker_order_id=alpaca.order_id_of(closing.client_order_id)
    )


async def test_an_order_alpaca_filled_is_taken_up_its_fill_credited_once_and_the_exit_ends_flat(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost closing order filled at Alpaca, and no lookup of the Clerk's own shows it.

    The operator gives its Alpaca order id. The Clerk reads that order, finds
    it is its own, and records the fill: the bot is flat, the EXIT succeeds
    and the unknown outcome closes. The fill is credited once -- not again
    when the stream later delivers its execution, and not by the passes that
    follow. Nothing is sent.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    closing = _closing_order(repo)
    alpaca.fills(closing.client_order_id)

    result = await _settle(_facade(repo, alpaca), _context_of(repo, SID), it_is_this_order)

    assert result.applied is True
    assert alpaca.reads == ["by-id"], "one read of the order named, and no other"
    assert len(alpaca.sent) == 1
    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0.0
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    taken_up = repo.order(closing.order_ref)
    assert taken_up is not None
    assert (taken_up.broker_order_id, taken_up.broker_state) == (it_is_this_order.broker_order_id, "filled")
    (recorded,) = _attested(repo)
    assert (recorded.attestation, recorded.operator, recorded.broker_order_id) == (
        "exists", OPERATOR, it_is_this_order.broker_order_id,
    )
    assert (recorded.broker_state, recorded.broker_filled_quantity) == ("filled", 10.0)

    await _the_stream_reports(
        repo,
        alpaca.at_alpaca(closing.client_order_id),
        BrokerOrderEvent(
            event_type="fill", occurred_at_ms=clock.value, price=100.0, quantity=10.0,
            execution_id="exec-closing-1",
        ),
    )
    await _passes(repo, _the_sweep(repo, alpaca), 3)

    assert repo.position(SID, "SPY") == 0.0
    assert repo.effective_fill_totals_for_order(closing.order_ref)[0] == 10.0
    assert len(alpaca.sent) == 1


async def test_a_working_order_taken_up_by_its_alpaca_id_stays_followed_while_its_own_lookup_reads_absent(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Taken up while it works, the order must not fall back to ``unknown`` on the next pass.

    Its exact lookup by client order id still reads absent on every pass.
    The Clerk holds the order's Alpaca order id now, and reads it by that:
    three passes leave the EXIT in progress and send nothing. When the order
    fills, the same read finds the fill and the EXIT ends flat.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    closing = _closing_order(repo)

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), it_is_this_order)

    assert _exit_state(repo, exit_id) == "in_progress"
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    for _ in range(3):
        await _passes(repo, _the_sweep(repo, alpaca), 1)
        assert await alpaca.get_order_by_client_order_id(closing.client_order_id) is None
        assert _exit_state(repo, exit_id) == "in_progress"
        assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert len(alpaca.sent) == 1
    assert repo.position(SID, "SPY") == 10.0

    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0.0
    assert len(alpaca.sent) == 1


async def test_an_order_that_ended_unfilled_at_alpaca_fails_its_exit_and_the_position_stays(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The order reached Alpaca and was canceled there with nothing filled: the bot still holds its shares."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    closing = _closing_order(repo)
    assert it_is_this_order.broker_order_id is not None
    alpaca.by_broker_id[it_is_this_order.broker_order_id] = alpaca.at_alpaca(
        closing.client_order_id
    ).model_copy(update={"status": "canceled", "canceled_at_ms": clock.value, "updated_at_ms": clock.value})

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), it_is_this_order)

    assert _exit_state(repo, exit_id) == "failed"
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is not None
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


async def test_an_order_alpaca_replaced_is_not_taken_up_and_the_refusal_names_what_replaced_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The closing order reached Alpaca and was replaced there, with nothing filled.

    Taken up, it left the EXIT ``unknown`` for good: a replaced order does
    nothing more under its own id, and the order was no longer offered for
    settling. It is refused instead, by the check and the settlement alike,
    in words that name the order that replaced it. Nothing is written, and
    the order is still offered.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    closing = _closing_order(repo)
    assert it_is_this_order.broker_order_id is not None
    alpaca.by_broker_id[it_is_this_order.broker_order_id] = alpaca.at_alpaca(
        closing.client_order_id
    ).model_copy(update={"status": "replaced", "replaced_by": BY_HAND_ORDER_ID})
    facade = _facade(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()

    checked = await facade.check_unknown_exit_order(
        strategy_instance_id=SID, presented=order, attestation=it_is_this_order
    )
    with pytest.raises(RecoveryExecutionError) as refused:
        await _settle(facade, _context_of(repo, SID), it_is_this_order)

    assert (checked.would_settle, checked.refusal_reason_code) == (False, "ORDER_REPLACED")
    assert checked.lines == (str(refused.value),)
    assert str(refused.value) == (
        f"Alpaca replaced order {it_is_this_order.broker_order_id!r} with Alpaca order "
        f"{BY_HAND_ORDER_ID!r}. A replaced order does nothing more under its own id, and the Clerk "
        "cannot take up the order that replaced it yet, so it keeps the position held. Nothing was "
        "changed."
    )
    assert repo.custody_transitions() == ledger
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.unknown_exit_orders(SID) == (order,), "the order is still offered for settling"
    assert len(alpaca.sent) == 1


def _no_order_has_that_id(_alpaca: _AlpacaAccount, _ours: BrokerOrder) -> str:
    return NO_SUCH_ORDER_ID


def _the_read_fails(alpaca: _AlpacaAccount, _ours: BrokerOrder) -> str:
    alpaca.by_broker_id[UNREADABLE_ORDER_ID] = BrokerUnavailable("alpaca timed out")
    return UNREADABLE_ORDER_ID


def _alpaca_cannot_take_the_id(alpaca: _AlpacaAccount, _ours: BrokerOrder) -> str:
    alpaca.by_broker_id[NO_SUCH_ORDER_ID] = BrokerRequestInvalid("order_id is invalid")
    return NO_SUCH_ORDER_ID


def _a_sell_placed_at_alpaca_by_hand(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[BY_HAND_ORDER_ID] = ours.model_copy(
        update={"order_id": BY_HAND_ORDER_ID, "client_order_id": "f3b1c2d4-alpaca-generated"}
    )
    return BY_HAND_ORDER_ID


def _an_order_that_names_no_client_order_id(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[UNNAMED_ORDER_ID] = ours.model_copy(
        update={"order_id": UNNAMED_ORDER_ID, "client_order_id": None}
    )
    return UNNAMED_ORDER_ID


def _another_bots_order(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[ANOTHER_BOTS_ORDER_ID] = ours.model_copy(
        update={"order_id": ANOTHER_BOTS_ORDER_ID, "client_order_id": "learn-ai/another-bot/v1:close"}
    )
    return ANOTHER_BOTS_ORDER_ID


def _our_id_on_another_quantity(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[ours.order_id] = ours.model_copy(update={"quantity": 4.0})
    return ours.order_id


def _our_id_on_the_other_side(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[ours.order_id] = ours.model_copy(update={"side": "buy"})
    return ours.order_id


def _our_id_on_another_symbol(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[ours.order_id] = ours.model_copy(update={"symbol": "QQQ"})
    return ours.order_id


def _our_order_with_a_value_the_clerk_could_not_read(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[ours.order_id] = ours.model_copy(update={"unreadable_fields": ("filled_qty",)})
    return ours.order_id


def _our_order_filled_at_no_price(alpaca: _AlpacaAccount, ours: BrokerOrder) -> str:
    alpaca.by_broker_id[ours.order_id] = ours.model_copy(
        update={"status": "filled", "filled_quantity": 10.0, "filled_avg_price": None}
    )
    return ours.order_id


@pytest.mark.parametrize(
    ("alpaca_answers", "reason_code"),
    [
        pytest.param(_no_order_has_that_id, "NO_SUCH_BROKER_ORDER", id="no-order-under-that-id"),
        pytest.param(_alpaca_cannot_take_the_id, "NO_SUCH_BROKER_ORDER", id="alpaca-cannot-take-the-id"),
        pytest.param(_the_read_fails, "BROKER_UNREADABLE", id="the-read-fails"),
        pytest.param(_a_sell_placed_at_alpaca_by_hand, "NOT_THIS_ORDER", id="a-sell-placed-by-hand"),
        pytest.param(_an_order_that_names_no_client_order_id, "NOT_THIS_ORDER", id="no-client-order-id"),
        pytest.param(_another_bots_order, "NOT_THIS_ORDER", id="another-bots-order"),
        pytest.param(_our_id_on_another_quantity, "NOT_THIS_ORDER", id="another-quantity"),
        pytest.param(_our_id_on_the_other_side, "NOT_THIS_ORDER", id="the-other-side"),
        pytest.param(_our_id_on_another_symbol, "NOT_THIS_ORDER", id="another-symbol"),
        pytest.param(_our_order_with_a_value_the_clerk_could_not_read, "ORDER_UNREADABLE", id="unreadable"),
        pytest.param(_our_order_filled_at_no_price, "ORDER_UNREADABLE", id="filled-at-no-price"),
    ],
)
async def test_an_alpaca_order_that_is_not_provably_this_order_is_refused_and_nothing_is_written(
    crashed_with_exposure,  # noqa: F811
    alpaca_answers: Callable[[_AlpacaAccount, BrokerOrder], str],
    reason_code: str,
) -> None:
    """The order id the operator gives is taken only for the order the Clerk sent.

    It must carry exactly this order's client order id, and its symbol, side
    and quantity. A sell someone placed by hand for the same shares, another
    bot's order, an id Alpaca does not know, an order the Clerk cannot read in
    full: each refuses, the ledger is exactly as it was, and nothing is sent.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    (order,) = repo.unknown_exit_orders(SID)
    named = OrderOutcomeAttestation(
        kind="exists", broker_order_id=alpaca_answers(alpaca, alpaca.at_alpaca(closing.client_order_id))
    )
    ledger = repo.custody_transitions()
    facade = _facade(repo, alpaca)

    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await facade.settle_unknown_exit_order(
            strategy_instance_id=SID, presented=order, attestation=named, operator=OPERATOR, reason=None,
        )

    assert refused.value.reason_code == reason_code
    assert "Nothing was changed." in str(refused.value)
    with pytest.raises(RecoveryExecutionError, match="Nothing was changed"):
        await _settle(facade, _context_of(repo, SID), named)
    assert repo.custody_transitions() == ledger
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


@pytest.mark.parametrize(
    "not_an_order_id",
    [
        "learn-ai/spy-bot/v1:close-1",
        "0D9F3A52-7C1E-4B68-9F0A-2E4C6A8B0D1F",
        "0d9f3a527c1e4b689f0a2e4c6a8b0d1f",
        " 0d9f3a52-7c1e-4b68-9f0a-2e4c6a8b0d1f",
        "",
    ],
    ids=["a-client-order-id", "upper-case", "no-hyphens", "a-space-before-it", "empty"],
)
def test_an_attestation_names_an_alpaca_order_id_only_as_alpaca_spells_it(not_an_order_id: str) -> None:
    """An Alpaca order id is a UUID, lower case with hyphens; the request boundary brings any other spelling to it.

    The Clerk checks the order Alpaca returns carries the id it asked for,
    and records that id on the order, so no other spelling is ever carried
    past the boundary.
    """
    with pytest.raises(ValueError, match="names its Alpaca order id"):
        OrderOutcomeAttestation(kind="exists", broker_order_id=not_an_order_id)

    assert OrderOutcomeAttestation(kind="exists", broker_order_id=NO_SUCH_ORDER_ID).broker_order_id == (
        NO_SUCH_ORDER_ID
    )


# ── The claim, the commit's own re-read, and replay ──────────────────────────


async def test_a_pass_that_meets_a_settlement_in_progress_defers_the_exit_and_sends_nothing(
    crashed_with_exposure,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The settlement holds the EXIT's claim from before its first read to its commit.

    The wait since the lost send is over, so a pass that drove this EXIT now
    would send its order again. One runs while the settlement is reading
    Alpaca's positions: it finds the EXIT claimed, defers it, and sends
    nothing. The settlement then commits against the order as it was.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    during_the_pass: list[tuple[str, int]] = []

    async def a_pass_runs() -> None:
        await reconcile_account(
            repo, read=alpaca, trade=alpaca, trigger="AUTOMATIC", pricing=UNPRICEABLE_RECOVERY
        )
        during_the_pass.append((_exit_state(repo, exit_id), len(alpaca.sent)))

    alpaca.while_positions_are_read = a_pass_runs

    with caplog.at_level(logging.INFO, logger="app.broker.alpaca.clerk.sqlite.reconcile"):
        result = await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    assert during_the_pass == [("unknown", 1)], "the pass left the claimed EXIT alone"
    assert [
        record.effect_operation_id
        for record in caplog.records
        if getattr(record, "action", None) == "reconcile_effect_deferred"
    ] == [exit_id]
    assert result.applied is True
    assert _exit_state(repo, exit_id) == "failed"
    assert len(alpaca.sent) == 1


async def test_a_settlement_that_meets_a_pass_driving_the_exit_is_refused_before_it_reads_anything(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The other way round: a pass holds the EXIT's claim, and may be sending its order again right now."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    ledger = repo.custody_transitions()
    held_by_a_pass = repo.claim_before_broker_contact(exit_id)

    with pytest.raises(RecoveryExecutionError, match="working on this exit right now"):
        await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    assert repo.custody_transitions() == ledger
    assert alpaca.reads == []
    assert repo.release_operation_claim(effect_operation_id=exit_id, token=held_by_a_pass.token), (
        "the pass still holds its claim"
    )


class _IntakeWaitedFor(ReentrantAsyncLock):
    """An intake fence a settlement waits for: ``meanwhile`` happens before its fenced run starts."""

    def __init__(self, meanwhile: Callable[[], None]) -> None:
        super().__init__()
        self._meanwhile = meanwhile

    async def off_loop[LocalResult](
        self, operation: Callable[..., LocalResult], *args: object, **kwargs: object
    ) -> LocalResult:
        self._meanwhile()
        return await super().off_loop(operation, *args, **kwargs)


@pytest.mark.parametrize(
    "attested",
    [
        pytest.param(lambda _alpaca, _closing: ABSENT, id="absent"),
        pytest.param(
            lambda alpaca, closing: OrderOutcomeAttestation(
                kind="exists", broker_order_id=alpaca.order_id_of(closing)
            ),
            id="exists",
        ),
    ],
)
async def test_a_settlement_whose_claim_a_pass_took_over_before_its_commit_is_refused_and_writes_nothing(
    crashed_with_exposure,  # noqa: F811
    attested: Callable[[_AlpacaAccount, str], OrderOutcomeAttestation],
) -> None:
    """The regression (#2854 review): the settlement read Alpaca, then waited for intake longer than its claim lives.

    Its claim on the EXIT expired in that wait and a pass took it: a pass
    that finds the order absent sends it again. The commit rechecked the
    order, which the pass had not written to yet, and not the claim, so it
    settled an order the pass was about to send.

    The commit now renews its own claim inside its fenced run, before it
    writes. The claim is the pass's, so the settlement is refused in words
    that say so, nothing is written, and the pass keeps its claim.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()
    taken_over: list[str] = []

    def the_claim_expires_and_a_pass_takes_it() -> None:
        _walk_clock_to(repo, repo.clock() + DEFAULT_CLAIM_TTL_MS + 1)
        taken_over.append(repo.claim_before_broker_contact(exit_id).token)

    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await settle_unknown_exit_order(
            repo, read=alpaca, trade=alpaca, intake=_IntakeWaitedFor(the_claim_expires_and_a_pass_takes_it),
            strategy_instance_id=SID, presented=order, attestation=attested(alpaca, closing.client_order_id),
            operator=OPERATOR, operator_reason=None,
        )

    assert refused.value.reason_code == "EXIT_CLAIM_LOST"
    assert str(refused.value) == (
        "This settlement took too long, and another check of this exit may have started "
        "meanwhile. Nothing was changed. Try again in a few seconds."
    )
    assert repo.custody_transitions() == ledger
    assert _attested(repo) == []
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.unknown_exit_orders(SID) == (order,), "the order is still offered for settling"
    (the_pass,) = taken_over
    assert repo.release_operation_claim(effect_operation_id=exit_id, token=the_pass), (
        "the pass still holds its claim"
    )
    assert len(alpaca.sent) == 1


async def test_a_check_takes_no_claim_so_it_answers_while_a_pass_holds_the_exit(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A check is a read: a pass that holds the EXIT's claim does not make it answer "busy".

    It reads Alpaca and says what it found, writes nothing, and leaves the
    pass its claim.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()
    held_by_a_pass = repo.claim_before_broker_contact(exit_id)

    findings = await _facade(repo, alpaca).check_unknown_exit_order(
        strategy_instance_id=SID, presented=order, attestation=ABSENT
    )

    assert (findings.would_settle, findings.refusal_reason_code) == (True, None)
    assert alpaca.reads == ["lookup", "orders:open", "positions", "orders:all"]
    assert repo.custody_transitions() == ledger
    assert repo.release_operation_claim(effect_operation_id=exit_id, token=held_by_a_pass.token), (
        "the pass still holds its claim"
    )


async def test_a_check_in_flight_never_makes_a_pass_defer_the_exit_or_a_settlement_find_it_busy(
    crashed_with_exposure,  # noqa: F811
    caplog: pytest.LogCaptureFixture,
) -> None:
    """While a check reads Alpaca's positions, a pass runs and an operator settles.

    The check holds nothing: the pass drives the EXIT as it would have, and
    the settlement that follows it goes through. The check, which read the
    order before either, then answers that the order is no longer the one it
    was asked about.

    The order was sent twice, so a pass half a minute after its second send
    looks it up and does not send it again; "no such order" may be settled
    by then.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives", "never_arrives"))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert len(alpaca.sent) == 2
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    (order,) = repo.unknown_exit_orders(SID)
    meanwhile: list[object] = []

    async def a_pass_runs_and_the_operator_settles() -> None:
        await reconcile_account(
            repo, read=alpaca, trade=alpaca, trigger="AUTOMATIC", pricing=UNPRICEABLE_RECOVERY
        )
        meanwhile.append(await _settle(facade, current_context, ABSENT))

    alpaca.while_positions_are_read = a_pass_runs_and_the_operator_settles

    with caplog.at_level(logging.INFO, logger="app.broker.alpaca.clerk.sqlite.reconcile"):
        findings = await facade.check_unknown_exit_order(
            strategy_instance_id=SID, presented=order, attestation=ABSENT
        )

    assert not [
        record for record in caplog.records if getattr(record, "action", None) == "reconcile_effect_deferred"
    ], "the pass did not find the EXIT claimed"
    (settled,) = meanwhile
    assert isinstance(settled, RecoveryExecutionResult) and settled.applied is True
    assert _exit_state(repo, exit_id) == "failed"
    assert (findings.would_settle, findings.refusal_reason_code) == (False, "ORDER_NO_LONGER_UNKNOWN")
    assert len(_attested(repo)) == 1
    assert len(alpaca.sent) == 2


async def test_an_order_the_clerk_sent_again_since_it_was_presented_is_not_settled(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The operator looked at Alpaca before the Clerk's latest send; what they saw is out of date.

    A pass sends the lost order again after the action was presented. The
    presented token no longer matches, and the settlement itself refuses the
    order as it was presented. Nothing is written by either.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives", "never_arrives"))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    presented = await _presented(current_context)
    (order_as_presented,) = repo.unknown_exit_orders(SID)

    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert len(alpaca.sent) == 2
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    ledger = repo.custody_transitions()

    with pytest.raises(StaleRecoveryTokenError):
        await _settle(facade, current_context, ABSENT, presented=presented)
    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await facade.settle_unknown_exit_order(
            strategy_instance_id=SID, presented=order_as_presented, attestation=ABSENT,
            operator=OPERATOR, reason=None,
        )

    assert refused.value.reason_code == "ORDER_SENT_AGAIN"
    assert repo.custody_transitions() == ledger
    assert _exit_state(repo, exit_id) == "unknown"
    assert len(alpaca.sent) == 2


async def test_an_order_alpaca_acknowledges_while_the_clerk_reads_is_not_ended_as_absent(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The reads found nothing, and the stream acknowledges the order before the commit.

    The commit reads the order again inside its one fenced run: it is no
    longer an order the Clerk cannot confirm, so the attestation writes
    nothing and the EXIT carries on with the order the stream reported.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    working = safe_flatten._broker_order(
        closing.order_ref, order_id="alpaca-closing", status="new", side="sell", quantity=10.0,
    )

    async def the_stream_acknowledges_it_once_the_last_read_has_answered() -> None:
        await _the_stream_reports(
            repo, working,
            BrokerOrderEvent(event_type="new", occurred_at_ms=clock.value, price=None, quantity=None),
        )

    original = alpaca.list_orders

    async def list_orders(**kwargs: object) -> list[BrokerOrder]:
        answer = await original(**kwargs)  # type: ignore[arg-type]
        if kwargs.get("status") == "all":
            await the_stream_acknowledges_it_once_the_last_read_has_answered()
        return answer

    alpaca.list_orders = list_orders  # type: ignore[method-assign]

    with pytest.raises(RecoveryExecutionError, match="no longer holds this order as one it cannot confirm"):
        await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    assert _attested(repo) == []
    assert repo.first_effect_transition(effect_operation_id=exit_id, transition_kind="EXIT_NOT_FLAT") is None
    assert _exit_state(repo, exit_id) == "in_progress"
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


async def test_the_same_settlement_asked_for_again_returns_its_receipt_and_does_nothing_twice(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A retry of a settled request -- same order, same attestation, the token it was presented under."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    presented = await _presented(current_context)
    first = await _settle(facade, current_context, ABSENT, presented=presented)
    ledger = repo.custody_transitions()
    alpaca.reads.clear()

    again = await _settle(facade, current_context, ABSENT, presented=presented)

    assert again.applied is False
    assert (again.receipt_id, again.recorded_at_ms) == (first.receipt_id, _attestation_row(repo)["recorded_at_ms"])
    assert repo.custody_transitions() == ledger
    assert alpaca.reads == [], "a replay reads nothing and settles nothing"
    assert len(alpaca.sent) == 1
    # Another attestation of the same order is not that settlement: its token is spent.
    with pytest.raises(StaleRecoveryTokenError):
        await _settle(
            facade, current_context, OrderOutcomeAttestation(kind="exists", broker_order_id=NO_SUCH_ORDER_ID),
            presented=presented,
        )
    assert repo.custody_transitions() == ledger


@pytest.mark.parametrize("alpaca_filled_it", [False, True], ids=["absent", "exists-filled"])
async def test_a_settlements_first_answer_carries_the_time_its_row_was_recorded_as_every_replay_does(
    crashed_with_exposure,  # noqa: F811
    alpaca_filled_it: bool,
) -> None:
    """The clock moves on while a settlement is written; its receipt is dated by its own row.

    The first answer read the clock again once everything was written, so it
    was dated later than the row, and the same request asked again was
    answered with another time. Both now carry the row's.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered" if alpaca_filled_it else "never_arrives",))
    attestation = ABSENT
    if alpaca_filled_it:
        _exit_id, attestation = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
        alpaca.fills(_closing_order(repo).client_order_id)
    else:
        await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    presented = await _presented(current_context)
    append = repo.append_transition

    def append_transition(transition: TransitionInput, **kwargs: object) -> object:
        committed = append(transition, **kwargs)  # type: ignore[arg-type]
        clock.advance(7)
        return committed

    repo.append_transition = append_transition  # type: ignore[method-assign]

    first = await _settle(facade, current_context, attestation, presented=presented)
    again = await _settle(facade, current_context, attestation, presented=presented)

    recorded_at_ms = _attestation_row(repo)["recorded_at_ms"]
    assert (first.applied, again.applied) == (True, False)
    assert first.receipt_id == again.receipt_id
    assert first.recorded_at_ms == again.recorded_at_ms == recorded_at_ms
    assert repo.clock() > recorded_at_ms, "the clock had moved on by the time the first answer was made"


async def test_a_settlement_asked_again_by_a_page_that_names_no_order_is_found_by_its_token(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A bot's page sends the token it was presented, and names no order.

    Asked again, that request is answered with its settlement's own record:
    the spent token names the order, its latest send and the other id Alpaca
    answered that send under, all still on the ledger. A request under any
    other token is not that settlement, and gets no receipt.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs(ANSWERED_AS),))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.has_under_another_id(_answered_with(status="canceled"))
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    presented = await _presented(current_context)
    first = await _settle(facade, current_context, ABSENT, presented=presented)
    ledger = repo.custody_transitions()
    alpaca.reads.clear()

    def naming_no_order(concurrency_token: str) -> RecoveryExecutionRequest:
        return RecoveryExecutionRequest(
            action_id=SETTLE, concurrency_token=concurrency_token, execution_ref=None, reason=None,
            attestation=ABSENT, operator=OPERATOR,
        )

    again = await execute_recovery_action(
        facade, request=naming_no_order(presented.concurrency_token), current_context=current_context
    )
    with pytest.raises(StaleRecoveryTokenError):
        await execute_recovery_action(
            facade, request=naming_no_order("0" * 64), current_context=current_context
        )
    with pytest.raises(StaleRecoveryTokenError):
        await execute_recovery_action(
            facade, request=naming_no_order("é" * 64), current_context=current_context
        )

    assert (again.applied, again.receipt_id) == (False, first.receipt_id)
    assert repo.custody_transitions() == ledger and alpaca.reads == []


async def test_the_presented_token_outlives_the_clerks_passes_until_the_order_is_sent_again(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Each pass records that the order is still absent; none of that is in the token.

    The pass one sweep interval after the lost send finds the order absent
    and its wait not over: the token presented before it still stands. The
    next pass sends the order again, and the token is a new one.
    """
    repo, clock = crashed_with_exposure
    assert SWEEP_INTERVAL_MS < WAIT_MS
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives", "never_arrives"))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca, wait_passed=False)
    current_context = _context_of(repo, SID)
    token = (await _presented(current_context)).concurrency_token
    uncertain_rows = repo.count_order_transitions(
        order_ref=_closing_order(repo).order_ref, transition_kind="ORDER_SUBMIT_UNCERTAIN", at_most=99,
    )

    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert len(alpaca.sent) == 1
    assert repo.count_order_transitions(
        order_ref=_closing_order(repo).order_ref, transition_kind="ORDER_SUBMIT_UNCERTAIN", at_most=99,
    ) > uncertain_rows, "the pass recorded the order still absent"
    assert (await _presented(current_context)).concurrency_token == token

    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert len(alpaca.sent) == 2
    assert (await _presented(current_context)).concurrency_token != token


# ── Who may be offered it, and what the record keeps ─────────────────────────


async def test_a_running_bots_unknown_exit_is_not_settled(crashed_with_exposure) -> None:  # noqa: F811
    """A running bot's own EXIT is sent with no check of the account, so its order is settled only once it is stopped."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _the_runner_sends_the_exit(repo, alpaca)
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()

    presented = await _presented(current_context)
    assert (presented.available, presented.unavailable_reason_code) == (False, "RUN_STILL_ACTIVE")
    with pytest.raises(RecoveryActionUnavailableError):
        await _settle(facade, current_context, ABSENT, presented=presented)
    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await facade.settle_unknown_exit_order(
            strategy_instance_id=SID, presented=order, attestation=ABSENT, operator=OPERATOR, reason=None,
        )

    assert refused.value.reason_code == "RUN_STILL_ACTIVE"
    assert repo.custody_transitions() == ledger


async def test_a_simulations_order_is_never_settled_against_alpaca(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A Dry Run's or a Shadow's port fills inside the send and reaches no account an operator could look at."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)
    ledger = repo.custody_transitions()

    with pytest.raises(UnknownOrderSettlementRefused) as refused:
        await settle_unknown_exit_order(
            repo, read=alpaca, trade=_NoSubmitTrade(), intake=ReentrantAsyncLock(),
            strategy_instance_id=SID, presented=order, attestation=ABSENT,
            operator=OPERATOR, operator_reason=None,
        )

    assert refused.value.reason_code == "NOT_A_BROKER_ACCOUNT"
    assert repo.custody_transitions() == ledger
    assert alpaca.reads == []


async def test_an_entry_whose_outcome_is_unknown_is_never_offered_for_settling(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Exits only (owner decision on #2854): a lost entry is voided by the Clerk itself."""
    repo, _clock = crashed_with_exposure
    await submit_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-lost",
        lifecycle_run_id=RUN_ID, leg=safe_flatten._leg(quantity=10, side="buy"),
        trade=_ExactLookup(submit_error=BrokerUnavailable("the submit timed out")),
    )
    submit_stop_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is not None

    presented = await _presented(_context_of(repo, SID))

    assert repo.unknown_exit_orders(SID) == ()
    assert (presented.available, presented.needed) == (False, False)
    assert presented.unavailable_reason_code == "NO_UNKNOWN_EXIT_ORDER"


async def test_an_order_alpaca_answered_with_no_client_order_id_is_not_offered_and_neither_choice_runs(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Alpaca answered the send with an order that names no client order id: the Clerk has no id to read it by.

    Neither attestation could settle it, so the action is not offered, and a
    request for either is refused before Alpaca is asked anything. Nothing
    is written and the EXIT keeps custody.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs(None),))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    ledger = repo.custody_transitions()

    presented = await _presented(current_context)
    assert (presented.available, presented.unavailable_reason_code) == (False, "ANSWERED_ORDER_NAMES_NO_ID")
    for attestation in (ABSENT, OrderOutcomeAttestation(kind="exists", broker_order_id=NO_SUCH_ORDER_ID)):
        with pytest.raises(RecoveryActionUnavailableError):
            await _settle(facade, current_context, attestation, presented=presented)

    assert repo.custody_transitions() == ledger
    assert alpaca.reads == []
    assert _exit_state(repo, exit_id) == "unknown"
    assert len(alpaca.sent) == 1


async def test_a_settlement_names_its_operator_and_the_dispatcher_refuses_one_that_names_none(
    crashed_with_exposure,  # noqa: F811
) -> None:
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    ledger = repo.custody_transitions()

    with pytest.raises(RecoveryExecutionError, match="names no operator"):
        await _settle(facade, current_context, ABSENT, operator=None)
    with pytest.raises(RecoveryExecutionError, match="Say what Alpaca shows"):
        await _settle(facade, current_context, None)

    assert repo.custody_transitions() == ledger
    assert alpaca.reads == []


async def test_an_attestation_records_only_what_its_own_reads_found(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The store is hash-chained: a fact gains no key it does not use.

    The attestation's own row leaves out everything the other attestation
    records, and the reason when none was given. The two episodes its EXIT
    ended under are recorded whole, each as the envelope every raised
    episode records, and each cites the order.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    recorded = json.loads(_attestation_row(repo)["facts_json"])
    assert set(recorded) == {
        "attestation", "operator", "client_order_id", "symbol", "side", "quantity",
        "latest_send_sequence", "uncertainty_id", "broker_observed_at_ms",
        "open_orders_read", "orders_since_read", "orders_read_since_ms",
        "broker_qty", "account_attributed_qty", "exit_not_flat", "exit_stuck",
    }
    for reason_code, envelope in (
        (EXIT_NOT_FLAT_REASON_CODE, "exit_not_flat"), (EXIT_STUCK_REASON_CODE, "exit_stuck"),
    ):
        episode = _open_episode(repo, reason_code)
        assert episode is not None
        assert json.loads(episode["facts_json"]) == recorded[envelope]
        assert UncertaintyRaisedFacts.from_facts_json(episode["facts_json"]).evidence_refs == [
            _closing_order(repo).order_ref
        ]


async def test_an_attestation_of_an_order_that_filled_nothing_records_no_fill_price(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The price of a fill is a fact only of an order that had filled anything: a working, unfilled one records none."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    _exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), it_is_this_order)

    assert set(json.loads(_attestation_row(repo)["facts_json"])) == {
        "attestation", "operator", "client_order_id", "symbol", "side", "quantity",
        "latest_send_sequence", "uncertainty_id", "broker_observed_at_ms",
        "broker_order_id", "broker_state", "broker_filled_quantity", "broker_updated_at_ms",
    }


def _the_ledger_records_the_bots_shares_written_off(repo: ClerkSqliteRepository) -> None:
    """A write-off of the bot's 10 SPY, put on the ledger as its own transition.

    The Clerk's discharge refuses while an EXIT of the symbol is unfinished,
    so no action of its own leaves a bot holding nothing under an unknown
    EXIT; the row is written here to stand for any way the ledger came to say
    so.
    """
    repo.append_transition(
        TransitionInput(
            strategy_instance_id=SID,
            transition_kind=ATTRIBUTED_RESIDUE_DISCHARGED,
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code=ATTRIBUTED_RESIDUE_DISCHARGED,
            facts_json=AttributedResidueDischargedFacts(
                symbol="SPY", discharged_qty=10.0, broker_qty=0.0, account_attributed_qty=10.0,
                broker_observed_at_ms=repo.clock(), uncertainty_ids=[], operator_reason=None,
            ).to_facts_json(),
        )
    )


async def test_an_absent_order_of_a_bot_that_holds_nothing_ends_its_exit_flat(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The bot holds nothing, and its closing order's outcome is still unknown.

    It has nothing left to exit, so "no such order" ends the EXIT as flat and
    raises no notice that a position is still open.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.holds = 0.0
    _the_ledger_records_the_bots_shares_written_off(repo)
    assert repo.position(SID, "SPY") == 0.0 and _exit_state(repo, exit_id) == "unknown"

    result = await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    assert result.applied is True
    assert _exit_state(repo, exit_id) == "succeeded"
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is None
    (recorded,) = _attested(repo)
    assert recorded.exit_not_flat is None
    assert len(alpaca.sent) == 1


async def test_an_absent_order_ends_its_exit_under_the_notice_its_bot_already_carries(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The bot's own exit was refused and left its notice; the operator's Flatten for the same shares was then lost.

    Settling the Flatten's order as absent ends that EXIT under the notice
    the bot already carries -- the same notice, now about this order -- and
    opens the hold beside it, in the same one write. The watchdog had been
    going to try again for that notice; it no longer does.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(
        repo, clock, submits=(_Refused(422, "extended hours order must be DAY limit orders"), "never_arrives")
    )
    refused_exit = await _the_runner_sends_the_exit(repo, alpaca)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )
    carried = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert _exit_state(repo, refused_exit) == "failed" and carried is not None
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
    await _flatten(facade, current_context)
    (flatten_order,) = repo.unknown_exit_orders(SID)
    assert len(alpaca.sent) == 2
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    ledger = repo.custody_transitions()

    await _settle(facade, current_context, ABSENT)

    assert _exit_state(repo, flatten_order.effect_operation_id) == "failed"
    (ended,) = repo.custody_transitions()[len(ledger):]
    assert ended["proof_reference"] == carried["uncertainty_id"], "the refresh is on the notice's own timeline"
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None and notice["uncertainty_id"] == carried["uncertainty_id"]
    assert notice["headline"] == "A closing order was settled as not sent; the position is still open"
    assert UncertaintyRaisedFacts.from_facts_json(notice["facts_json"]).evidence_refs == [
        flatten_order.order_ref
    ]
    stuck = _open_episode(repo, EXIT_STUCK_REASON_CODE)
    assert stuck is not None and stuck["uncertainty_id"] == f"uncertainty:{ended['sequence']}:EXIT_STUCK"
    assert ExitStuckCause.from_mapping(
        UncertaintyRaisedFacts.from_facts_json(stuck["facts_json"]).cause_facts
    ).first_observed_at_ms == carried["observed_at_ms"]
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert len(alpaca.sent) == 2

    await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)

    assert len(alpaca.sent) == 2, "the watchdog no longer sells for the notice the bot carried"


async def test_a_mirror_rebuild_replays_an_order_taken_up_by_its_alpaca_id(tmp_path: Path) -> None:
    """The mirror alone rebuilds the order's Alpaca order id, its fill and the flat EXIT, row for row.

    The attestation's own row carries what the Clerk read of the order,
    with the price of its fill and Alpaca's time for it, and is rebuilt the
    same.
    """
    clock = _clock_at(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, symbol="SPY", config_hash="h1"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    closing = _closing_order(repo)
    alpaca.fills(closing.client_order_id)
    await _settle(_facade(repo, alpaca), _context_of(repo, SID), it_is_this_order)
    transitions = repo.custody_transitions()
    (attested,) = _attested(repo)
    assert (
        attested.broker_state, attested.broker_filled_quantity,
        attested.broker_filled_avg_price, attested.broker_updated_at_ms,
    ) == ("filled", 10.0, 100.0, clock.value)
    db_path = repo.db_path
    repo.close()
    db_path.rename(db_path.with_suffix(".db.corrupt"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == transitions
        assert _attested(rebuilt) == [attested]
        assert _exit_state(rebuilt, exit_id) == "succeeded"
        assert rebuilt.position(SID, "SPY") == 0.0
        taken_up = rebuilt.order(closing.order_ref)
        assert taken_up is not None
        assert (taken_up.broker_order_id, taken_up.broker_state) == (it_is_this_order.broker_order_id, "filled")
        assert _open_episode(rebuilt, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    finally:
        rebuilt.close()


async def test_a_mirror_rebuild_replays_a_settled_order(tmp_path: Path) -> None:
    """The ledger's mirror alone rebuilds the settled EXIT, both episodes it ended under and the position, row for row."""
    clock = _clock_at(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, symbol="SPY", config_hash="h1"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)
    transitions = repo.custody_transitions()
    episodes = [_open_episode(repo, EXIT_NOT_FLAT_REASON_CODE), _open_episode(repo, EXIT_STUCK_REASON_CODE)]
    assert None not in episodes
    db_path = repo.db_path
    repo.close()
    db_path.rename(db_path.with_suffix(".db.corrupt"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == transitions
        assert _exit_state(rebuilt, exit_id) == "failed"
        assert rebuilt.position(SID, "SPY") == 10.0
        assert _open_episode(rebuilt, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
        assert [
            _open_episode(rebuilt, EXIT_NOT_FLAT_REASON_CODE), _open_episode(rebuilt, EXIT_STUCK_REASON_CODE)
        ] == episodes
    finally:
        rebuilt.close()
