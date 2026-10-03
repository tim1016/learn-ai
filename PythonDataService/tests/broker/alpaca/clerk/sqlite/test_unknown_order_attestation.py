"""An operator settles an EXIT whose closing order's outcome is unknown (#2854).

A closing order whose submit answer was lost leaves its EXIT ``unknown``: the
Clerk keeps custody of the position and can neither prove the order absent
nor take it up. The operator looks at the account at Alpaca and says what it
shows. These tests run the real Clerk on a real temp SQLite ledger against a
faked Alpaca, through the dispatcher the panel and the recovery route use,
and assert what an operator and the ledger see afterwards.

Two things are pinned throughout. The Clerk never takes the operator's word:
it reads Alpaca itself, and anything it reads that does not bear the
attestation out refuses with nothing written. And no attestation ever sends
an order.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from uuid import UUID, uuid5

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.account_open_work import MAX_OPEN_ORDER_SNAPSHOT
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.enter import submit_enter
from app.broker.alpaca.clerk.sqlite.facts import (
    AttributedResidueDischargedFacts,
    EndedForeignOrder,
    OrderOutcomeAttestedFacts,
    UncertaintyRaisedFacts,
)
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
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
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.residue_discharge import ATTRIBUTED_RESIDUE_DISCHARGED
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
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
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerPosition
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _clock_at,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
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

SETTLE = "settle_unknown_exit_order"
OPERATOR = "ops-desk"
ABSENT = OrderOutcomeAttestation(kind="absent")
# Where this Alpaca's order ids come from: one UUID per client order id.
_ALPACA_ORDER_IDS = UUID("6f1c2a0e-3b5d-4c7e-8f90-a1b2c3d4e5f6")
# Alpaca order ids of orders that are not the bot's closing order.
NO_SUCH_ORDER_ID = "0d9f3a52-7c1e-4b68-9f0a-2e4c6a8b0d1f"
ANSWERED_ORDER_ID = "5e6f7a8b-9c0d-4e1f-8a3b-4c5d6e7f8091"
UNREADABLE_ORDER_ID = "1a2b3c4d-5e6f-4a7b-8c9d-0e1f2a3b4c5d"
BY_HAND_ORDER_ID = "2b3c4d5e-6f7a-4b8c-9d0e-1f2a3b4c5d6e"
UNNAMED_ORDER_ID = "3c4d5e6f-7a8b-4c9d-8e1f-2a3b4c5d6e7f"
ANOTHER_BOTS_ORDER_ID = "4d5e6f7a-8b9c-4d0e-9f2a-3b4c5d6e7f80"

type _Context = Callable[[], Awaitable[object]]


class _AlpacaAccount(_Alpaca):
    """Alpaca as the settling Clerk reads it: every read by name, and each one able to fail.

    Beside what :class:`_Alpaca` answers, it lists every order sent since a
    time (open ones and ``ended_orders``), and answers an order by its Alpaca
    order id. ``reads`` names each read in order: ``lookup`` (the closing
    order's exact lookup), ``orders:open``, ``positions``, ``orders:all``
    (one per page), ``by-id``. ``fails`` makes a named read fail.
    ``holds_per_read`` is what the account holds at each successive positions
    read.

    An order's Alpaca order id always finds an order Alpaca has, even one
    its exact lookup by client order id does not show -- the state an
    operator who can see the order at Alpaca settles. ``by_broker_id`` states
    what any other id answers.

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
        self.holds_per_read: list[float] = []
        self.by_broker_id: dict[str, BrokerOrder | BrokerError] = {}

    def _read(self, name: str) -> None:
        self.reads.append(name)
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

    async def list_positions(self) -> list[BrokerPosition]:
        if "positions" in self.fails:
            self._read("positions")
        if self.holds_per_read:
            self.holds = self.holds_per_read.pop(0)
        return await super().list_positions()

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        clerk_order = self._repo.order(client_order_id)
        if clerk_order is not None and clerk_order.role == "REDUCING":
            self._read("lookup")
        return await super().get_order_by_client_order_id(client_order_id)


    def _alpaca_order_id(self, client_order_id: str) -> str:
        return str(uuid5(_ALPACA_ORDER_IDS, client_order_id))

    async def get_order_by_broker_order_id(self, order_id: str) -> BrokerOrder | None:
        try:
            UUID(order_id)
        except ValueError as exc:
            raise BrokerRequestInvalid(f"{order_id!r} is not an Alpaca order id.") from exc
        self.reads.append("by-id")
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


async def test_an_absent_order_ends_its_exit_as_not_sent_and_nothing_is_sent(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost closing order never reached Alpaca, and the operator says so.

    The Clerk reads the account twice -- the order's exact lookup, the open
    orders, the positions, every order since -- finds nothing, and ends the
    EXIT as not sent. The unknown outcome closes, ``EXIT_NOT_FLAT`` says the
    position is still open, the entry is free to be exited again, and the
    bot still holds its 10 SPY. Who settled it, and what the Clerk read, is
    on the record. No order left the Clerk.

    All of that is one write to the ledger: the attestation's own row ends
    the EXIT and opens its notice, so there is no part of it to be missing.
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
    assert alpaca.reads == ["lookup", "orders:open", "positions", "orders:all"] * 2
    assert alpaca.asked_since_ms == [closing.updated_at_ms - ORDERS_SINCE_MARGIN_MS] * 2

    assert _exit_state(repo, exit_id) == "failed"
    (ended,) = repo.custody_transitions()[len(ledger):]
    assert (ended["transition_kind"], ended["summary_code"]) == (ORDER_OUTCOME_ATTESTED, "ORDER_ATTESTED_ABSENT")
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None
    assert notice["headline"] == "A closing order was settled as not sent; the position is still open"
    assert repo.position(SID, "SPY") == 10.0, "an attestation never changes what the bot holds"
    assert repo.active_exit_for_order(entry.order_ref) is None, "the entry can be exited again"

    (recorded,) = _attested(repo)
    assert (recorded.attestation, recorded.operator) == ("absent", OPERATOR)
    assert recorded.operator_reason == "Checked Alpaca's order list: no sell of SPY today."
    assert (recorded.client_order_id, recorded.symbol, recorded.side, recorded.quantity) == (
        closing.order_ref, "SPY", "SELL", 10.0,
    )
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (10.0, 10.0)
    assert result.receipt_id == f"transition:{_attestation_row(repo)['sequence']}"


def _attestation_row(repo: ClerkSqliteRepository) -> dict:
    (row,) = [
        row for row in repo.custody_transitions() if row["transition_kind"] == ORDER_OUTCOME_ATTESTED
    ]
    return row


def _the_lookup_fails(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.fails["lookup"] = BrokerUnavailable("alpaca timed out")


def _the_open_orders_cannot_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.fails["orders:open"] = BrokerUnavailable("alpaca timed out")


def _the_positions_cannot_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.positions_unreadable = BrokerUnavailable("alpaca timed out")


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


def _a_buy_is_working_on_the_symbol(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.open_orders = [_elsewhere("someone-buys", side="buy")]


def _an_open_order_names_no_symbol(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.open_orders = [_elsewhere("no-symbol", symbol="", side="buy")]


def _a_sell_is_working_that_the_open_orders_do_not_list(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [_elsewhere("another-sell", status="partially_filled", filled_quantity=4.0)]


def _someone_elses_sell_filled_and_the_difference_is_not_recorded(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [_elsewhere("another-sell", status="filled", filled_quantity=10.0)]
    alpaca.holds = 0.0


def _an_ended_order_could_not_be_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [
        _elsewhere("garbled", status="canceled").model_copy(update={"unreadable_fields": ("filled_qty",)})
    ]


def _a_replaced_sell_whose_replacement_was_not_read(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.ended_orders = [
        _elsewhere("replaced-sell", status="replaced").model_copy(update={"replaced_by": "its-replacement"})
    ]


def _alpaca_holds_no_shares(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.holds = 0.0


def _the_position_moves_between_the_two_reads(alpaca: _AlpacaAccount, _closing: str) -> None:
    alpaca.holds_per_read = [10.0, 4.0]


@pytest.mark.parametrize(
    ("alpaca_shows", "reason_code"),
    [
        pytest.param(_the_lookup_fails, "BROKER_UNREADABLE", id="the-exact-lookup-fails"),
        pytest.param(_the_open_orders_cannot_be_read, "BROKER_UNREADABLE", id="open-orders-unreadable"),
        pytest.param(_the_positions_cannot_be_read, "BROKER_UNREADABLE", id="positions-unreadable"),
        pytest.param(_the_orders_since_cannot_be_read, "BROKER_UNREADABLE", id="orders-since-unreadable"),
        pytest.param(_the_open_order_page_is_full, "ORDER_LIST_INCOMPLETE", id="open-order-page-full"),
        pytest.param(
            _the_symbol_has_more_orders_since_than_the_clerk_reads, "ORDER_LIST_INCOMPLETE",
            id="more-pages-of-the-symbols-orders-than-the-clerk-reads",
        ),
        pytest.param(_the_exact_lookup_answers, "ORDER_EXISTS_AT_BROKER", id="the-exact-lookup-answers"),
        pytest.param(_the_open_orders_list_it, "ORDER_EXISTS_AT_BROKER", id="listed-open-under-our-id"),
        pytest.param(_the_orders_since_list_it_filled, "ORDER_EXISTS_AT_BROKER", id="listed-filled-under-our-id"),
        pytest.param(_a_buy_is_working_on_the_symbol, "BROKER_ORDER_WORKING", id="an-order-working-on-the-symbol"),
        pytest.param(_an_open_order_names_no_symbol, "BROKER_ORDER_WORKING", id="a-working-order-names-no-symbol"),
        pytest.param(
            _a_sell_is_working_that_the_open_orders_do_not_list, "BROKER_ORDER_WORKING",
            id="a-working-sell-only-the-orders-since-list",
        ),
        pytest.param(
            _someone_elses_sell_filled_and_the_difference_is_not_recorded, "POSITION_DIFFERENCE_NOT_RECORDED",
            id="a-foreign-sell-filled-and-no-drift-is-recorded",
        ),
        pytest.param(_an_ended_order_could_not_be_read, "UNKNOWN_ORDER_ON_SYMBOL", id="an-unreadable-order-since"),
        pytest.param(
            _a_replaced_sell_whose_replacement_was_not_read, "UNKNOWN_ORDER_ON_SYMBOL",
            id="a-replaced-sell-whose-replacement-was-not-read",
        ),
        pytest.param(_alpaca_holds_no_shares, "POSITION_DIFFERENCE_NOT_RECORDED", id="position-differs-unrecorded"),
        pytest.param(_the_position_moves_between_the_two_reads, "BROKER_SNAPSHOT_UNSETTLED", id="position-moves"),
    ],
)
async def test_an_absent_attestation_alpaca_does_not_bear_out_is_refused_and_nothing_is_written(
    crashed_with_exposure,  # noqa: F811
    alpaca_shows: Callable[[_AlpacaAccount, str], None],
    reason_code: str,
) -> None:
    """Whatever the operator says, the Clerk ends the order as not sent only on its own reads.

    A read that fails, a list that may be incomplete, the order itself in any
    read, any order working on the symbol, an ended order the Clerk could not
    read or whose replacement it did not see, a position that differs from
    the Clerk's records or moves between the reads: each refuses. The ledger
    is exactly as it was, the EXIT still holds custody, and nothing is sent.
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
    with pytest.raises(RecoveryExecutionError, match="Nothing was changed"):
        await _settle(facade, _context_of(repo, SID), ABSENT)
    assert repo.custody_transitions() == ledger


async def test_a_position_alpaca_no_longer_holds_is_settled_only_under_a_recorded_drift(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Alpaca holds no SPY while the Clerk's records say 10: the order may have filled unseen.

    Before the Clerk has recorded that difference the attestation is refused.
    Once a pass has raised ``POSITION_DRIFT`` naming the symbol -- the episode
    that keeps the Clerk from selling shares Alpaca does not hold -- it is
    taken: the EXIT ends, both quantities are on the record and in the
    notice, and the bot's 10 SPY are still attributed for the operator to
    write off.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca, wait_passed=False)
    alpaca.holds = 0.0
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _exit_state(repo, exit_id) == "unknown" and len(alpaca.sent) == 1

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)

    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (0.0, 10.0)
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None
    assert "Alpaca holds 0 SPY; the Clerk's records say 10." in notice["explanation"]
    assert "write off the missing shares" in notice["next_step"]
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


async def test_an_order_settled_as_absent_that_had_filled_unseen_is_credited_once_alpaca_shows_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order did fill, and no read showed it: Alpaca holds none of the bot's 10 SPY.

    Settled as absent under the recorded drift, the EXIT ends with the 10 SPY
    still attributed. The Clerk goes on looking the closing order of a failed
    EXIT up while its symbol is drifted: when Alpaca shows it filled, the
    fill is credited once and the bot holds nothing, as Alpaca says. Nothing
    is sold twice.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca, wait_passed=False)
    closing = _closing_order(repo)
    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _exit_state(repo, exit_id) == "unknown" and alpaca.holds == 0.0

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)
    assert _exit_state(repo, exit_id) == "failed" and repo.position(SID, "SPY") == 10.0

    alpaca.answers_its_lookup(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 3)

    assert repo.position(SID, "SPY") == 0.0
    assert repo.effective_fill_totals_for_order(closing.order_ref)[0] == 10.0
    assert _exit_state(repo, exit_id) == "failed"
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


async def test_an_order_alpaca_answered_under_another_id_is_settled_as_absent_once_that_order_has_ended(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The order Alpaca answered the send with is under an id the Clerk never sent.

    While it works, neither choice settles: it may be this order, so "no
    such order" is refused, and the Clerk cannot take up an order under
    another client order id yet, so "it is this Alpaca order" is refused
    too. Both refusals say so. Once that order has ended with nothing filled
    it can sell nothing more: "no such order" ends the EXIT as not sent, and
    the order the operator and the Clerk were looking at is on the record.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(_AnsweredAs("an-id-the-clerk-never-sent"),))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    answered_with = _elsewhere(ANSWERED_ORDER_ID, client_order_id="an-id-the-clerk-never-sent")
    alpaca.open_orders = [answered_with]
    alpaca.by_broker_id[ANSWERED_ORDER_ID] = answered_with
    ledger = repo.custody_transitions()

    with pytest.raises(RecoveryExecutionError) as absent_refused:
        await _settle(facade, current_context, ABSENT)
    with pytest.raises(RecoveryExecutionError) as exists_refused:
        await _settle(
            facade, current_context, OrderOutcomeAttestation(kind="exists", broker_order_id=ANSWERED_ORDER_ID)
        )

    assert "cannot be settled as absent while an order is working on its symbol" in str(absent_refused.value)
    assert "the Clerk cannot take up an order under another id yet" in str(absent_refused.value)
    assert "is the order Alpaca answered this send with" in str(exists_refused.value)
    assert "While that order is working, this one cannot be settled either way" in str(exists_refused.value)
    assert repo.custody_transitions() == ledger and _exit_state(repo, exit_id) == "unknown"

    alpaca.open_orders = []
    alpaca.ended_orders = [answered_with.model_copy(update={"status": "canceled"})]
    await _settle(facade, current_context, ABSENT)

    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (EndedForeignOrder(ANSWERED_ORDER_ID, "canceled", 0.0),)
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


async def test_a_sale_tried_by_hand_and_canceled_does_not_stand_in_the_way_of_no_such_order(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """An operator tried the sale by hand at Alpaca, and canceled it with nothing filled.

    That order has ended and can sell nothing more. The check says what the
    Clerk saw of it, the settlement goes through, and the attestation
    records it. Before, it refused this order for good.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.ended_orders = [
        _elsewhere("by-hand", status="canceled"),
        # Not this order's side, not its symbol, and from before it was sent: none is asked about.
        _elsewhere("a-buy", side="buy", status="filled", filled_quantity=10.0),
        _elsewhere("qqq", symbol="QQQ", status="filled", filled_quantity=10.0),
        _elsewhere("long-ago", status="filled", filled_quantity=10.0, submitted_at_ms=FIXTURE_RTH_MS - 86_400_000),
    ]
    facade = _facade(repo, alpaca)
    (order,) = repo.unknown_exit_orders(SID)

    findings = await facade.check_unknown_exit_order(strategy_instance_id=SID, presented=order, attestation=ABSENT)
    await _settle(facade, _context_of(repo, SID), ABSENT)

    assert findings.would_settle is True
    assert findings.lines[2] == (
        "Since this one was sent, Alpaca shows 1 order to sell SPY that the Clerk did not place, "
        "all ended: Alpaca order by-hand, canceled, 0 filled."
    )
    assert {symbols for symbols, _until in alpaca.pages_asked} == {("SPY",)}
    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (EndedForeignOrder("by-hand", "canceled", 0.0),)
    assert recorded.orders_since_read == 2, "the canceled sell and the buy: the symbol's orders since"
    assert len(alpaca.sent) == 1


async def test_a_foreign_sale_that_filled_is_settled_only_under_a_recorded_drift_and_is_on_the_record(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A sell the Clerk did not place filled for the bot's 10 SPY: Alpaca holds none of them now.

    The order has ended, so it does not refuse by itself; what it sold is in
    Alpaca's position, and the position is what answers for it. Until a pass
    has recorded that difference the attestation is refused. Under the
    recorded drift it is taken, the filled order is on the record, and the
    notice tells the operator to write the shares off.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca, wait_passed=False)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    alpaca.ended_orders = [_elsewhere("another-sell", status="filled", filled_quantity=10.0)]
    alpaca.holds = 0.0
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    _walk_clock_to(repo, repo.clock() + WAIT_MS)
    assert _exit_state(repo, exit_id) == "unknown" and len(alpaca.sent) == 1

    await _settle(facade, current_context, ABSENT)

    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert recorded.ended_foreign_orders == (EndedForeignOrder("another-sell", "filled", 10.0),)
    assert (recorded.broker_qty, recorded.account_attributed_qty) == (0.0, 10.0)
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None and "write off the missing shares" in notice["next_step"]
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


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
        EndedForeignOrder("its-replacement", "canceled", 0.0),
        EndedForeignOrder("replaced-sell", "replaced", 0.0),
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
    assert alpaca.pages_asked == [(("SPY",), None), (("SPY",), oldest_of_the_first_page_ms + 1)] * 2
    assert alpaca.reads == ["lookup", "orders:open", "positions", "orders:all", "orders:all"] * 2
    assert _exit_state(repo, exit_id) == "failed"
    (recorded,) = _attested(repo)
    assert recorded.orders_since_read == page + 200
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
    assert alpaca.reads == ["lookup", "orders:open", "positions", "orders:all"] * 2
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
    rounds_read = 0

    async def the_stream_acknowledges_it_after_the_second_round() -> None:
        await _the_stream_reports(
            repo, working,
            BrokerOrderEvent(event_type="new", occurred_at_ms=clock.value, price=None, quantity=None),
        )

    original = alpaca.list_orders

    async def list_orders(**kwargs: object) -> list[BrokerOrder]:
        nonlocal rounds_read
        answer = await original(**kwargs)  # type: ignore[arg-type]
        if kwargs.get("status") == "all":
            rounds_read += 1
            if rounds_read == 2:
                await the_stream_acknowledges_it_after_the_second_round()
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
    records, and the reason when none was given. The notice its EXIT ended
    under is recorded whole, as the envelope every raised notice records.
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
        "broker_qty", "account_attributed_qty", "exit_not_flat",
    }
    notice = _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE)
    assert notice is not None
    assert json.loads(notice["facts_json"]) == recorded["exit_not_flat"]
    assert UncertaintyRaisedFacts.from_facts_json(notice["facts_json"]).evidence_refs == [
        _closing_order(repo).order_ref
    ]


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
    the bot already carries -- the same notice, now about this order -- in
    the same one write.
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
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert len(alpaca.sent) == 2


async def test_a_mirror_rebuild_replays_an_order_taken_up_by_its_alpaca_id(tmp_path: Path) -> None:
    """The mirror alone rebuilds the order's Alpaca order id, its fill and the flat EXIT, row for row."""
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
    db_path = repo.db_path
    repo.close()
    db_path.rename(db_path.with_suffix(".db.corrupt"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == transitions
        assert _exit_state(rebuilt, exit_id) == "succeeded"
        assert rebuilt.position(SID, "SPY") == 0.0
        taken_up = rebuilt.order(closing.order_ref)
        assert taken_up is not None
        assert (taken_up.broker_order_id, taken_up.broker_state) == (it_is_this_order.broker_order_id, "filled")
        assert _open_episode(rebuilt, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    finally:
        rebuilt.close()


async def test_a_mirror_rebuild_replays_a_settled_order(tmp_path: Path) -> None:
    """The ledger's mirror alone rebuilds the settled EXIT, its notice and the position, row for row."""
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
        assert _open_episode(rebuilt, EXIT_NOT_FLAT_REASON_CODE) is not None
    finally:
        rebuilt.close()
