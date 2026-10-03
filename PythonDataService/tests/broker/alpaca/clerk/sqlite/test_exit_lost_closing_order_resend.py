"""A closing order whose submit was lost is sent again by the Clerk's own passes (#2845).

An EXIT's reducing order that never reached Alpaca -- its submit timed out, or
the Clerk was interrupted after creating the order and before sending it -- is
sent again once the 30 s submit-absence wait since its last send has passed.
That wait was measured from the newest ``ORDER_SUBMIT_UNCERTAIN``, and every
pass that found the order still absent recorded one. The pass runs every 15 s,
so the wait never ended: the EXIT stayed ``unknown`` and the position open
until the passes stopped for 30 s.

These tests run the Clerk's reconciliation pass at its own cadence against an
Alpaca that loses submits, and assert what reached Alpaca and when. The wait
is still kept after every send: an order whose answer was lost may be at
Alpaca and not yet show, and sending it again inside the wait risks a second
sale.

An order sent again may also have been at Alpaca all along, shown by no read.
Alpaca then refuses the second send: as it answers any client order id it
already has, or because the first order holds the shares. No refusal of a
send made again fails the EXIT, which would release the position to be sold
again under a new id; custody is kept and the same id goes out again.

A send Alpaca answers with an order under another client order id is never
sent again: the order may be at Alpaca under that other identity, and the
same id cannot guard a second send against it.

In both of those Alpaca answered, so the notice on the bot's page says what
it answered instead of that a response was lost (#2854).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest
from alpaca.common.exceptions import APIError

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_start_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import DEFAULT_RECOVERY_INTERVAL_MS
from app.broker.alpaca.clerk.sqlite.exit_resolution import flatten_send_refusal
from app.broker.alpaca.clerk.sqlite.facts import (
    OrderSubmitFailedFacts,
    OrderSubmitUncertainFacts,
    UnknownOutcomeNotice,
)
from app.broker.alpaca.clerk.sqlite.flatten_cover import FLATTEN_NOT_COVERED_AT_BROKER
from app.broker.alpaca.clerk.sqlite.idempotency import UnknownEntryOrderError
from app.broker.alpaca.clerk.sqlite.intake_fence import ReentrantAsyncLock
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.order_evidence import fold_uncertain, submit_absence_grace_ms
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedUncertainty
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import ORDER_OUTCOME_UNKNOWN_REASON_CODE
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.alpaca.errors import AlpacaRequest, map_api_error
from app.broker.contract.errors import BrokerError, BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg, BrokerPosition
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.utils.session_anchors import et_date_at_ms
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import (
    FIXTURE_RTH_MS,
    _clock_at,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    _Account,
    _exit_not_flat,
    _flatten,
    _flatten_exit,
    _stopped_bot_offered_its_flatten,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    crashed_with_exposure,  # noqa: F401 -- the repository, in the regular session, these tests reuse
)

# The interval between the Clerk's passes: ``reconcile_account``'s own
# default, the 15 s ``ReconciliationSweep`` runs them at.
SWEEP_INTERVAL_MS = DEFAULT_RECOVERY_INTERVAL_MS
WAIT_MS = submit_absence_grace_ms()
# The first pass on or after the end of the wait: where a lost closing order is sent again.
RESEND_AFTER_MS = -(-WAIT_MS // SWEEP_INTERVAL_MS) * SWEEP_INTERVAL_MS
# Five minutes of passes: what the reviewer who found #2845 drove, with the EXIT ``unknown`` throughout.
FIVE_MINUTES_OF_PASSES = 300_000 // SWEEP_INTERVAL_MS
# The broker order id of an order Alpaca answers a send with that is not under the client order id sent.
ANOTHER_BROKER_ORDER_ID = "alpaca-another-order"


@dataclass(frozen=True)
class _Refused:
    """A submit Alpaca answers with an HTTP error: its status, its words and its own code."""

    status: int
    message: str
    code: int | None = None

    def as_the_clerk_receives_it(self) -> BrokerError:
        """The error the Alpaca client raises for this answer to an order submission."""
        body = {"message": self.message} if self.code is None else {"code": self.code, "message": self.message}
        response = SimpleNamespace(status_code=self.status, headers={})
        return map_api_error(
            APIError(json.dumps(body), http_error=SimpleNamespace(response=response, request=None)),
            broker="alpaca",
            request=AlpacaRequest.ORDER_SUBMIT,
        )


# Alpaca's answer to a client order id it already has (#2304), the one
# ``tests/broker/alpaca/test_client.py`` pins the client against.
ALPACA_HAS_THIS_CLIENT_ORDER_ID = _Refused(422, "client_order_id must be unique", code=40010001)

@dataclass(frozen=True)
class _AnsweredAs:
    """A submit Alpaca answers with an order that is not under the client order id sent: under another, or under none."""

    client_order_id: str | None


type _SubmitFate = Literal["never_arrives", "arrives_unanswered"] | _Refused | _AnsweredAs


@dataclass(frozen=True)
class _Sent:
    """One submit as it left the Clerk, and the Clerk's clock as it left."""

    client_order_id: str
    leg: BrokerOrderLeg
    at_ms: int


class _Alpaca(_Account):
    """Alpaca as both ports see it: the account, and an order entry that can lose a submit.

    ``submits`` names what becomes of each submit in turn; a submit past the
    end of the list is accepted and then works in the account.

    - ``"never_arrives"``: the request times out and Alpaca never has the order.
    - ``"arrives_unanswered"``: the request times out and Alpaca has the order,
      though nothing shows it until the test says so.
    - a :class:`_Refused`: Alpaca answers the request with that error and has no order.
    - an :class:`_AnsweredAs`: Alpaca answers the request with an order under
      that client order id, and no read shows an order under either id.

    The exact lookup answers a closing order only once Alpaca has it and shows
    it. Any entry it is asked about filled before its EXIT was decided, as
    every entry in these tests did. A submit under a client order id Alpaca
    already has places no second order and is answered as Alpaca answers it
    (:data:`ALPACA_HAS_THIS_CLIENT_ORDER_ID`).

    ``sent`` is every submit with the Clerk's clock as it left. ``reads``, the
    account's record of its reads, also names each submit where it fell
    among them.
    """

    def __init__(
        self, repo: ClerkSqliteRepository, clock: _TestClock, *, submits: tuple[_SubmitFate, ...] = ()
    ) -> None:
        super().__init__(holds=10.0)
        self._repo = repo
        self._clock = clock
        self._fates = list(submits)
        self._has: dict[str, BrokerOrder] = {}
        self._unanswered: set[str] = set()
        self.sent: list[_Sent] = []
        self.cancel_calls: list[str] = []
        # What happens while a submit that times out is awaited, by which submit it is (the first is 1).
        self.while_awaited: dict[int, Callable[[], None]] = {}
        # What happens, once, while the account's positions are next read.
        self.while_positions_are_read: Callable[[], Awaitable[None]] | None = None

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.sent.append(_Sent(client_order_id, leg, self._clock()))
        self.reads.append("submit")
        if client_order_id in self._has:
            raise ALPACA_HAS_THIS_CLIENT_ORDER_ID.as_the_clerk_receives_it()
        order = safe_flatten._broker_order(
            client_order_id, order_id=f"alpaca-{client_order_id}", status="new",
            side=leg.side, quantity=leg.quantity,
        )
        if not self._fates:
            self._has[client_order_id] = order
            self.open_orders.append(order)
            return order
        fate = self._fates.pop(0)
        if isinstance(fate, _Refused):
            raise fate.as_the_clerk_receives_it()
        if isinstance(fate, _AnsweredAs):
            return order.model_copy(
                update={"client_order_id": fate.client_order_id, "order_id": ANOTHER_BROKER_ORDER_ID}
            )
        if fate == "arrives_unanswered":
            self._has[client_order_id] = order
            self._unanswered.add(client_order_id)
        self.while_awaited.get(len(self.sent), lambda: None)()
        raise BrokerUnavailable("the submit timed out")

    async def list_positions(self) -> list[BrokerPosition]:
        meanwhile, self.while_positions_are_read = self.while_positions_are_read, None
        if meanwhile is not None:
            await meanwhile()
        return await super().list_positions()

    async def cancel(self, order_id: str) -> None:
        self.cancel_calls.append(order_id)

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        clerk_order = self._repo.order(client_order_id)
        if clerk_order is not None and clerk_order.role == "ENTRY":
            assert clerk_order.broker_order_id is not None
            return safe_flatten._broker_order(
                client_order_id, order_id=clerk_order.broker_order_id, status="filled",
                quantity=10.0, filled_quantity=10.0, filled_avg_price=100.0,
            )
        if client_order_id in self._unanswered:
            return None
        return self._has.get(client_order_id)

    def lists_among_its_open_orders(self, client_order_id: str) -> None:
        """The account's open orders now list the order; its exact lookup still answers absent."""
        self.open_orders.append(self._has[client_order_id])

    def now_has(self, client_order_id: str) -> None:
        """Alpaca has a working order under this id after all, and every read shows it."""
        self._has[client_order_id] = safe_flatten._broker_order(
            client_order_id, order_id=f"alpaca-{client_order_id}", status="new", side="sell", quantity=10.0,
        )
        self.shows(client_order_id)

    def shows(self, client_order_id: str) -> None:
        """Every read now shows the order Alpaca had all along."""
        self._unanswered.discard(client_order_id)
        if self._has[client_order_id] not in self.open_orders:
            self.open_orders.append(self._has[client_order_id])

    def loses_sight_of(self, client_order_id: str) -> None:
        """No read shows the order any more, though Alpaca acknowledged it."""
        self._unanswered.add(client_order_id)
        self.open_orders = [order for order in self.open_orders if order.client_order_id != client_order_id]

    def fills(self, client_order_id: str) -> None:
        """The closing sale fills whole at $100, and the shares leave the account."""
        working = self._has[client_order_id]
        assert working.quantity is not None
        self._has[client_order_id] = working.model_copy(
            update={
                "status": "filled", "filled_quantity": working.quantity, "filled_avg_price": 100.0,
                "updated_at_ms": self._clock(), "filled_at_ms": self._clock(),
            }
        )
        self.open_orders = [order for order in self.open_orders if order.client_order_id != client_order_id]
        self.holds -= working.quantity


def _sent_after_the_first_ms(alpaca: _Alpaca) -> list[int]:
    """When each submit left the Clerk, in ms of the Clerk's clock after its first."""
    return [sent.at_ms - alpaca.sent[0].at_ms for sent in alpaca.sent]


def _closing_order(repo: ClerkSqliteRepository) -> OrderResource:
    (closing,) = [order for order in repo.orders_for_strategy(SID) if order.role == "REDUCING"]
    return closing


def _exit_state(repo: ClerkSqliteRepository, effect_operation_id: str) -> str:
    effect = repo.effect_operation(effect_operation_id)
    assert effect is not None
    return effect.state


def _refused_by_the_broker(repo: ClerkSqliteRepository, effect_operation_id: str) -> bool:
    """Whether the EXIT failed as one whose closing order the broker refused outright."""
    failed = repo.first_effect_transition(
        effect_operation_id=effect_operation_id, transition_kind="EXIT_NOT_FLAT"
    )
    return failed is not None and failed["summary_code"] == "ORDER_SUBMIT_FAILED"


def _latest_unknown(repo: ClerkSqliteRepository, closing: OrderResource) -> OrderSubmitUncertainFacts:
    """What the Clerk last recorded of why the closing order's outcome is unknown."""
    row = repo.last_order_transition(order_ref=closing.order_ref, transition_kind="ORDER_SUBMIT_UNCERTAIN")
    assert row is not None
    return OrderSubmitUncertainFacts.from_facts_json(row["facts_json"])


def _the_open_notice(repo: ClerkSqliteRepository) -> ProjectedUncertainty | None:
    """What the bot's page shows for an order whose outcome is unknown, or ``None`` when it shows no such notice."""
    reader = SqliteClerkProjectionReader.from_repository(repo)
    try:
        snapshot = reader.bot_snapshot(SID)
    finally:
        reader.close()
    assert snapshot is not None
    notices = [item for item in snapshot.uncertainties if item.reason_code == ORDER_OUTCOME_UNKNOWN_REASON_CODE]
    assert len(notices) <= 1
    return notices[0] if notices else None


async def _passes_until_sent(
    repo: ClerkSqliteRepository, one_pass: Callable[[], Awaitable[object]], alpaca: _Alpaca, sends: int
) -> None:
    """Passes of the Clerk, each one sweep interval after the last, until ``sends`` submits have left it."""
    for _ in range(4 * FIVE_MINUTES_OF_PASSES):
        if len(alpaca.sent) >= sends:
            return
        await _passes(repo, one_pass, 1)
    raise AssertionError(f"only {len(alpaca.sent)} of {sends} submits left the Clerk")


def _a_second_exit_is_refused(repo: ClerkSqliteRepository) -> None:
    """The bot decides to exit again: the EXIT that holds custody of its entry is the only one there is."""
    (entry,) = repo.entry_orders_for_strategy(SID)
    with pytest.raises(UnknownEntryOrderError):
        accept_exit(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="program-exit-2",
            lifecycle_run_id=RUN_ID, entry_order_ref=entry.order_ref,
        )


async def _the_stream_reports(repo: ClerkSqliteRepository, order: BrokerOrder, event: BrokerOrderEvent) -> None:
    """One ``trade_updates`` frame about the Clerk's own order, folded as the stream folds it."""
    sink = SqliteTradeUpdateEvidenceSink(
        repo=repo, intake=ReentrantAsyncLock(), reconciler=safe_flatten._NoReconciler()
    )
    disposition = await sink.record_lifecycle_event(
        client_order_id=order.client_order_id,
        event=event,
        event_key=f"{event.event_type}:{order.order_id}",
        order=order,
        recovery_source=None,
        recovery_window_limit=None,
    )
    assert disposition == "order_event"


async def _passes(repo: ClerkSqliteRepository, one_pass: Callable[[], Awaitable[object]], count: int) -> None:
    """``count`` passes of the Clerk, each one sweep interval after the last."""
    for _ in range(count):
        _walk_clock_to(repo, repo.clock() + SWEEP_INTERVAL_MS)
        await one_pass()


def _the_sweep(repo: ClerkSqliteRepository, alpaca: _Alpaca) -> Callable[[], Awaitable[object]]:
    """The pass ``ReconciliationSweep`` runs: the account read, then every unfinished operation."""

    async def one_pass() -> object:
        return await reconcile_account(
            repo, read=alpaca, trade=alpaca, trigger="AUTOMATIC", pricing=UNPRICEABLE_RECOVERY
        )

    return one_pass


async def _a_program_decides_to_exit(repo: ClerkSqliteRepository) -> str:
    """A running bot holding 10 SPY decides to exit. Returns the EXIT's effect, not yet driven."""
    entry_ref = await safe_flatten._held_position(repo)
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="program-exit-1",
        lifecycle_run_id=RUN_ID, entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    return accepted.effect_operation_id


async def _the_runner_sends_the_exit(repo: ClerkSqliteRepository, alpaca: _Alpaca) -> str:
    """The deciding runner drives its EXIT once, sending the closing order. Returns the EXIT's effect."""
    effect_operation_id = await _a_program_decides_to_exit(repo)
    await resolve_exit(
        repo, effect_operation_id=effect_operation_id, trade=alpaca, pricing=UNPRICEABLE_RECOVERY, read=None
    )
    assert len(alpaca.sent) == 1
    return effect_operation_id


async def test_a_closing_order_whose_submit_never_arrived_is_sent_again_once_its_wait_has_passed(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The regression (#2845): passes every 15 s, a 30 s wait, and a submit that timed out.

    Before the fix nothing was sent again in five minutes of passes: each
    pass's own "still absent" record restarted the wait. Now the closing order
    goes out again on the first pass after the wait since its send, once, as
    the same order, and the EXIT completes.
    """
    repo, clock = crashed_with_exposure
    assert SWEEP_INTERVAL_MS < WAIT_MS, "the passes must come more often than the wait is long"
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    assert _exit_state(repo, exit_id) == "unknown"

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    lost, again = alpaca.sent
    assert (again.client_order_id, again.leg) == (lost.client_order_id, lost.leg)
    assert (again.leg.side, again.leg.quantity) == ("sell", 10)
    assert _exit_state(repo, exit_id) == "in_progress"

    alpaca.fills(again.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0
    assert len(alpaca.sent) == 2


async def test_a_closing_order_created_and_never_sent_is_sent_once_the_wait_since_its_creation_has_passed(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The Clerk is cancelled after it creates the closing order and before it records the send.

    Nothing reached Alpaca, and nothing says it might have. The passes send
    the order on the first pass after the wait since it was created.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock)
    exit_id = await _a_program_decides_to_exit(repo)
    created_at_ms = clock.value

    def cancelled_once_the_order_exists(_symbol: str, _now_ms: int) -> None:
        if any(order.role == "REDUCING" for order in repo.orders_for_strategy(SID)):
            raise asyncio.CancelledError

    interrupted = replace(UNPRICEABLE_RECOVERY, liveness_source=cancelled_once_the_order_exists)
    with pytest.raises(asyncio.CancelledError):
        await resolve_exit(
            repo, effect_operation_id=exit_id, trade=alpaca, pricing=interrupted, read=None
        )
    assert alpaca.sent == []
    assert not repo.has_order_transition(
        order_ref=_closing_order(repo).order_ref, transition_kind="ORDER_SUBMIT_REQUESTED"
    )

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert [sent.at_ms - created_at_ms for sent in alpaca.sent] == [RESEND_AFTER_MS]
    assert _exit_state(repo, exit_id) == "in_progress"

    alpaca.fills(alpaca.sent[0].client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0


async def test_a_second_lost_send_restarts_the_wait_from_that_send(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The order sent again is lost again: the third send waits the whole wait from the second.

    Measured from the order's creation, the wait would be long over by the
    second send, and the third would follow it one pass later.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", "never_arrives"))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS, 2 * RESEND_AFTER_MS]
    assert len({sent.client_order_id for sent in alpaca.sent}) == 1
    assert _exit_state(repo, exit_id) == "in_progress"


@pytest.mark.parametrize(
    ("slow_send", "sent_after_the_first_ms"),
    [
        pytest.param(1, [0, 20_000 + RESEND_AFTER_MS], id="the-first-send"),
        pytest.param(2, [0, RESEND_AFTER_MS, RESEND_AFTER_MS + 20_000 + RESEND_AFTER_MS], id="the-send-made-again"),
    ],
)
async def test_a_send_that_was_slow_to_time_out_waits_the_whole_wait_from_its_timeout(
    crashed_with_exposure,  # noqa: F811
    slow_send: int,
    sent_after_the_first_ms: list[int],
) -> None:
    """A submit hangs for 20 s before it times out: the request may have been on its way all that time.

    The wait runs from the timeout. Measured from the moment the send was
    recorded, the order would go out again 15 s after the Clerk gave up on it.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",) * slow_send)
    alpaca.while_awaited[slow_send] = lambda: clock.advance(20_000)
    await _the_runner_sends_the_exit(repo, alpaca)

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert _sent_after_the_first_ms(alpaca) == sent_after_the_first_ms


async def test_an_order_that_did_reach_alpaca_is_found_and_never_sent_again(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The submit's answer was lost, the order was not: Alpaca shows it by the pass the wait ends on."""
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)
    assert _exit_state(repo, exit_id) == "unknown"

    passes_before_the_wait_ends = RESEND_AFTER_MS // SWEEP_INTERVAL_MS - 1
    await _passes(repo, _the_sweep(repo, alpaca), passes_before_the_wait_ends)
    alpaca.shows(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert len(alpaca.sent) == 1
    found = repo.order(closing.order_ref)
    assert found is not None
    assert (found.broker_order_id, found.broker_state) == (f"alpaca-{closing.client_order_id}", "new")
    assert _exit_state(repo, exit_id) == "in_progress"

    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert len(alpaca.sent) == 1


async def test_a_send_made_again_that_alpaca_answers_as_a_duplicate_keeps_custody_and_sells_once(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order was at Alpaca all along and no read showed it: sent again, Alpaca says it has that id.

    That answer is not a refusal of the sale. Folded as one, the EXIT failed
    and released the position while its order worked at Alpaca, and the
    watchdog's re-drive, or the bot's next exit decision, sold it again under
    a new client order id. The EXIT now stays ``unknown`` and keeps custody:
    no second decision is accepted, the watchdog has no episode to re-drive,
    and the one id goes out again only a whole wait after each such answer.
    When a lookup shows the order it is folded as any acknowledged one.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)
    passes_until_sent_again = RESEND_AFTER_MS // SWEEP_INTERVAL_MS

    await _passes(repo, _the_sweep(repo, alpaca), passes_until_sent_again)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None
    assert ALPACA_HAS_THIS_CLIENT_ORDER_ID.message in _latest_unknown(repo, closing).why
    _a_second_exit_is_refused(repo)

    # Long past the age at which the watchdog re-drives an EXIT that failed with the position open.
    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert {sent.client_order_id for sent in alpaca.sent} == {closing.client_order_id}
    last_pass_ms = (passes_until_sent_again + FIVE_MINUTES_OF_PASSES) * SWEEP_INTERVAL_MS
    assert _sent_after_the_first_ms(alpaca) == list(range(0, last_pass_ms + 1, RESEND_AFTER_MS))
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None

    alpaca.shows(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    found = repo.order(closing.order_ref)
    assert found is not None
    assert (found.broker_order_id, found.broker_state) == (f"alpaca-{closing.client_order_id}", "new")
    assert _exit_state(repo, exit_id) == "in_progress"

    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0
    assert {sent.client_order_id for sent in alpaca.sent} == {closing.client_order_id}


@pytest.mark.parametrize(
    "refused",
    [
        pytest.param(
            _Refused(403, "insufficient qty available for order (requested: 10, available: 0)"),
            id="403-the-order-is-not-permitted",
        ),
        pytest.param(_Refused(401, "request is not authorized"), id="401-credentials"),
        pytest.param(_Refused(429, "rate limit exceeded"), id="429-throttled"),
        pytest.param(_Refused(409, "order conflict"), id="409-conflict"),
    ],
)
async def test_a_send_made_again_that_alpaca_refuses_keeps_custody_whatever_the_refusal(
    crashed_with_exposure,  # noqa: F811
    refused: _Refused,
) -> None:
    """No refusal of a send made again proves the lost send never arrived (#2845 review).

    The first order may be working at Alpaca and shown by no read: Alpaca
    refuses the second for shares the first one holds (403) as readily as for
    its client order id. Folded as a refusal of the sale, the EXIT failed and
    released the position, and the watchdog's re-drive or the bot's next exit
    decision could sell it again under a new id. Whatever Alpaca answers, the
    EXIT stays ``unknown`` with custody, the operator is not told the
    position is free, no second exit is accepted, and only the one id is ever
    sent: again a whole wait after each refusal, for as long as Alpaca
    refuses, and accepted on the first send after it stops.
    """
    repo, clock = crashed_with_exposure
    refused_for_five_minutes = 1 + FIVE_MINUTES_OF_PASSES * SWEEP_INTERVAL_MS // RESEND_AFTER_MS
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", *[refused] * refused_for_five_minutes))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)
    passes_until_sent_again = RESEND_AFTER_MS // SWEEP_INTERVAL_MS

    await _passes(repo, _the_sweep(repo, alpaca), passes_until_sent_again)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None
    assert refused.message in _latest_unknown(repo, closing).why
    _a_second_exit_is_refused(repo)

    # Long past the age at which the watchdog re-drives an EXIT that failed with the position open.
    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert {sent.client_order_id for sent in alpaca.sent} == {closing.client_order_id}
    last_pass_ms = (passes_until_sent_again + FIVE_MINUTES_OF_PASSES) * SWEEP_INTERVAL_MS
    assert _sent_after_the_first_ms(alpaca) == list(range(0, last_pass_ms + 1, RESEND_AFTER_MS))
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None

    # Alpaca stops refusing: the same order goes out on the first send after, one wait on.
    await _passes(repo, _the_sweep(repo, alpaca), passes_until_sent_again)

    assert _sent_after_the_first_ms(alpaca)[-1] == last_pass_ms + RESEND_AFTER_MS
    assert _exit_state(repo, exit_id) == "in_progress"

    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0
    assert {sent.client_order_id for sent in alpaca.sent} == {closing.client_order_id}


@pytest.mark.parametrize(
    ("refused", "looked_for_an_opposite_order"),
    [
        pytest.param(_Refused(422, "extended hours order must be DAY limit orders"), False, id="422-invalid"),
        pytest.param(
            _Refused(403, "insufficient qty available for order (requested: 10, available: 0)"), True,
            id="403-the-order-is-not-permitted",
        ),
    ],
)
async def test_a_first_send_alpaca_refuses_fails_the_exit_as_before(
    crashed_with_exposure,  # noqa: F811
    refused: _Refused,
    looked_for_an_opposite_order: bool,
) -> None:
    """Nothing was sent before, so a refusal of the first send cannot be about an earlier one.

    The EXIT fails and releases the position, the operator is told, and a
    403's fold still records what the Clerk found open on the other side of
    the symbol -- nothing, here -- which the watchdog's re-drive reads (#2622).
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=(refused,))

    exit_id = await _the_runner_sends_the_exit(repo, alpaca)

    assert _exit_state(repo, exit_id) == "failed"
    assert _refused_by_the_broker(repo, exit_id)
    assert repo.active_exit_for_strategy(SID) is None
    assert _exit_not_flat(repo) is not None
    failed = repo.first_effect_transition(effect_operation_id=exit_id, transition_kind="EXIT_NOT_FLAT")
    assert failed is not None
    opposite = OrderSubmitFailedFacts.from_facts_json(failed["facts_json"]).opposite_open_order_refs
    assert opposite == ([] if looked_for_an_opposite_order else None)


@pytest.mark.parametrize(
    ("submits", "recorded_as", "named_as"),
    [
        pytest.param(
            (_AnsweredAs("an-id-the-clerk-never-sent"),),
            "an-id-the-clerk-never-sent", "'an-id-the-clerk-never-sent'", id="the-first-send",
        ),
        pytest.param(
            ("never_arrives", _AnsweredAs("an-id-the-clerk-never-sent")),
            "an-id-the-clerk-never-sent", "'an-id-the-clerk-never-sent'", id="the-send-made-again",
        ),
        pytest.param((_AnsweredAs(None),), "", "no client order id", id="an-answer-that-names-no-id"),
    ],
)
async def test_a_send_alpaca_answers_under_another_client_order_id_is_never_sent_again(
    crashed_with_exposure,  # noqa: F811
    submits: tuple[_SubmitFate, ...],
    recorded_as: str,
    named_as: str,
) -> None:
    """Alpaca answers a send with an order that is not under the client order id the Clerk sent (#2845 review).

    An order may be at Alpaca under another identity. The id the Clerk sent
    reads absent, which for a lost answer is leave to send it again once the
    wait has passed -- but the same id guards a second send only against
    itself, and the two could both execute. So nothing is sent again, however
    long the passes run: the EXIT stays ``unknown`` with custody, no second
    exit is accepted, and the answer's broker order id is not taken as this
    order's. The answer is recorded once on the order; every later pass says
    which two ids it is about.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=submits)
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert len(alpaca.sent) == len(submits)
    assert {sent.client_order_id for sent in alpaca.sent} == {closing.client_order_id}
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None
    kept = repo.order(closing.order_ref)
    assert kept is not None and kept.broker_order_id is None
    told = _latest_unknown(repo, closing).why
    assert repr(closing.client_order_id) in told and named_as in told
    answers = [
        OrderSubmitUncertainFacts.from_facts_json(row["facts_json"])
        for row in repo.transitions_for_order(closing.order_ref)
        if row["transition_kind"] == "ORDER_SUBMIT_UNCERTAIN"
    ]
    (answer,) = [facts for facts in answers if facts.answered_as_client_order_id is not None]
    assert answer.answered_as_client_order_id == recorded_as
    assert repr(ANOTHER_BROKER_ORDER_ID) in answer.why
    _a_second_exit_is_refused(repo)

    # The one way out there is: Alpaca shows an order under the id the Clerk sent, and it is folded as any other.
    alpaca.now_has(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    found = repo.order(closing.order_ref)
    assert found is not None
    assert (found.broker_order_id, found.broker_state) == (f"alpaca-{closing.client_order_id}", "new")
    assert _exit_state(repo, exit_id) == "in_progress"
    assert len(alpaca.sent) == len(submits)


def test_an_unknown_outcomes_facts_omit_the_other_identity_so_every_ordinary_row_hashes_the_same() -> None:
    """``answered_as_client_order_id`` joins the hash-chained facts only on an answer under another identity.

    Every ``ORDER_SUBMIT_UNCERTAIN`` written before #2845's review -- and
    every ordinary one after it -- re-serializes byte-identically, so no
    sealed receipt moves; the marked answer reads back as it was written.
    """
    plain = OrderSubmitUncertainFacts(why="the submit timed out")
    earlier_row = '{"why":"the submit timed out"}'
    marked = OrderSubmitUncertainFacts(why="the submit timed out", answered_as_client_order_id="another-id")
    names_none = OrderSubmitUncertainFacts(why="the submit timed out", answered_as_client_order_id="")

    assert plain.to_facts_json() == earlier_row
    assert OrderSubmitUncertainFacts.from_facts_json(earlier_row) == plain
    assert marked.to_facts_json() == '{"answered_as_client_order_id":"another-id","why":"the submit timed out"}'
    assert OrderSubmitUncertainFacts.from_facts_json(marked.to_facts_json()) == marked
    # An answer that named no client order id is recorded as the empty string, and is still recorded.
    assert names_none.to_facts_json() == '{"answered_as_client_order_id":"","why":"the submit timed out"}'
    assert OrderSubmitUncertainFacts.from_facts_json(names_none.to_facts_json()) == names_none


LOST_ANSWER_EXPLANATION = "The broker response was lost or timed out."
LOST_ANSWER_NEXT_STEP = "Reconcile now; cancellation and exact-identity lookup remain available."
REFUSED_AGAIN_NEXT_STEP = (
    "Look at this account's orders and position at Alpaca. The Clerk sends the order again by "
    "itself, and takes it up as soon as Alpaca shows it."
)


def _refused_again_explanation(alpacas_answer: str) -> str:
    return (
        "Alpaca refused the order to sell 10 SPY when the Clerk sent it again. An earlier send of "
        "this order may have reached Alpaca, so the Clerk keeps custody of the position and sends "
        "the same order again, under the same client order id, for as long as Alpaca shows no "
        f"order under that id. Alpaca's answer: {alpacas_answer}"
    )


async def test_a_send_made_again_that_alpaca_refuses_is_told_in_alpacas_own_words(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The open notice said a broker response "was lost or timed out" while Alpaca answered every send (#2854).

    The first send's answer was lost, and the notice says so. Once the order
    is sent again and Alpaca refuses it, the bot's page says that instead:
    what Alpaca answered, that custody is kept and that the same order goes
    out again. The passes that find the order still absent between two sends
    leave that said, the next refusal replaces it with Alpaca's newer answer,
    and the notice closes when Alpaca accepts the order.
    """
    repo, clock = crashed_with_exposure
    no_shares = _Refused(403, "insufficient qty available for order (requested: 10, available: 0)")
    throttled = _Refused(429, "rate limit exceeded")
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", no_shares, throttled))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)

    lost = _the_open_notice(repo)
    assert lost is not None
    assert (lost.explanation, lost.next_step) == (LOST_ANSWER_EXPLANATION, LOST_ANSWER_NEXT_STEP)

    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=2)

    refused = _the_open_notice(repo)
    assert refused is not None
    assert refused.uncertainty_id == lost.uncertainty_id
    assert refused.headline == "A broker order outcome is unknown"
    assert refused.explanation == _refused_again_explanation(f"Alpaca refused the order: {no_shares.message}")
    assert refused.next_step == REFUSED_AGAIN_NEXT_STEP

    # The next pass finds the order still absent, inside its wait: it sends nothing, and the notice stands.
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert len(alpaca.sent) == 2
    still = _the_open_notice(repo)
    assert still is not None
    assert (still.explanation, still.next_step) == (refused.explanation, refused.next_step)

    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=3)

    refused_again = _the_open_notice(repo)
    assert refused_again is not None
    assert refused_again.explanation == _refused_again_explanation(
        f"Alpaca rate-limited the request: {throttled.message}"
    )

    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=4)

    assert _exit_state(repo, exit_id) == "in_progress"
    assert _the_open_notice(repo) is None


@pytest.mark.parametrize(
    ("submits", "answered_with"),
    [
        pytest.param(
            (_AnsweredAs("an-id-the-clerk-never-sent"),),
            f"client order id 'an-id-the-clerk-never-sent', Alpaca order id {ANOTHER_BROKER_ORDER_ID!r}",
            id="the-first-send",
        ),
        pytest.param(
            ("never_arrives", _AnsweredAs("an-id-the-clerk-never-sent")),
            f"client order id 'an-id-the-clerk-never-sent', Alpaca order id {ANOTHER_BROKER_ORDER_ID!r}",
            id="the-send-made-again",
        ),
        pytest.param(
            (_AnsweredAs(None),),
            f"no client order id, Alpaca order id {ANOTHER_BROKER_ORDER_ID!r}",
            id="an-answer-that-names-no-id",
        ),
    ],
)
async def test_a_send_alpaca_answers_under_another_client_order_id_is_told_as_never_sent_again(
    crashed_with_exposure,  # noqa: F811
    submits: tuple[_SubmitFate, ...],
    answered_with: str,
) -> None:
    """An order held for a human said "lost or timed out" and "Reconcile now", and neither was true (#2854).

    Alpaca answered, and no pass sends the order again. The bot's page says
    which two orders it is about, that this one will not be sent again, and
    the one thing an operator can do today: look at the order at Alpaca. It
    keeps saying so for as long as the order is held.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=submits)
    await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)

    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=len(submits))

    held = _the_open_notice(repo)
    assert held is not None
    assert held.explanation == (
        f"The Clerk sent the order to sell 10 SPY under client order id {closing.client_order_id!r}, "
        f"and Alpaca answered with an order under {answered_with}. An order may be at Alpaca under "
        "that other identity, and sending the same client order id again could not guard against "
        "it, so the Clerk will not send this order again. It keeps custody of the position."
    )
    assert held.next_step == (
        "Look at this account's orders at Alpaca for the order it answered with. The Clerk takes "
        "this order up by itself only if Alpaca shows one under the client order id that was sent."
    )

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert len(alpaca.sent) == len(submits)
    still = _the_open_notice(repo)
    assert still is not None
    assert (still.uncertainty_id, still.explanation, still.next_step) == (
        held.uncertainty_id, held.explanation, held.next_step,
    )


async def test_a_refusal_is_no_longer_told_once_its_order_is_found_while_another_order_is_still_unknown(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """One notice covers every order of the bot whose outcome is unknown (#2854).

    The refusal is told while its order is unknown, and still told when a
    second order's answer is lost. Once Alpaca accepts the refused order, the
    notice is about the second order alone and says nothing of a refusal.
    """
    repo, clock = crashed_with_exposure
    refused = _Refused(403, "insufficient qty available for order (requested: 10, available: 0)")
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", refused))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=2)
    (entry,) = repo.entry_orders_for_strategy(SID)

    fold_uncertain(
        repo, effect_operation_id=exit_id, order_ref=entry.order_ref,
        why="The cancel response for the entry was lost.", transition_kind="ORDER_CANCEL_UNCERTAIN",
    )

    both_unknown = _the_open_notice(repo)
    assert both_unknown is not None
    assert both_unknown.explanation == _refused_again_explanation(f"Alpaca refused the order: {refused.message}")

    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=3)

    accepted = repo.order(_closing_order(repo).order_ref)
    assert accepted is not None and accepted.broker_order_id is not None
    one_unknown = _the_open_notice(repo)
    assert one_unknown is not None
    assert one_unknown.uncertainty_id == both_unknown.uncertainty_id
    assert (one_unknown.explanation, one_unknown.next_step) == (
        "One or more broker responses remain unknown.", LOST_ANSWER_NEXT_STEP,
    )


async def test_a_store_rebuilt_from_its_mirror_tells_the_same_refusal(tmp_path: Path) -> None:
    """The notice's words are written by the fold of the row that records the refusal, never edited in place (#2854).

    So a store replayed from its mirror shows the bot's page the same notice,
    refreshed by the same later pass, as the store that lived through it.
    """
    clock = _clock_at(FIXTURE_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, symbol="SPY", config_hash="h1"
    )
    submit_start_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    refused = _Refused(403, "insufficient qty available for order (requested: 10, available: 0)")
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", refused))
    await _the_runner_sends_the_exit(repo, alpaca)
    await _passes_until_sent(repo, _the_sweep(repo, alpaca), alpaca, sends=2)
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    told = _the_open_notice(repo)
    assert told is not None
    assert told.explanation == _refused_again_explanation(f"Alpaca refused the order: {refused.message}")
    db_path = repo.db_path
    repo.close()
    db_path.rename(db_path.with_suffix(".db.corrupt"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert _the_open_notice(rebuilt) == told
    finally:
        rebuilt.close()


def test_an_unknown_outcomes_facts_omit_the_notice_so_every_ordinary_row_hashes_the_same() -> None:
    """The notice joins the hash-chained facts only on a fold that records one (#2854).

    Every ``ORDER_SUBMIT_UNCERTAIN`` written before it existed, and every
    ordinary one after it, re-serializes byte-identically, so no sealed
    receipt moves. A notice is its explanation and its next step together.
    """
    earlier_row = '{"why":"the submit timed out"}'
    earlier_marked_row = '{"answered_as_client_order_id":"another-id","why":"the submit timed out"}'
    plain = OrderSubmitUncertainFacts.from_facts_json(earlier_row)
    marked = OrderSubmitUncertainFacts.from_facts_json(earlier_marked_row)
    noticed = OrderSubmitUncertainFacts(
        why="the submit timed out", notice_explanation="Alpaca refused it.", notice_next_step="Look at Alpaca."
    )

    assert plain.to_facts_json() == earlier_row
    assert marked.to_facts_json() == earlier_marked_row
    assert plain.notice is None and marked.notice is None
    assert noticed.to_facts_json() == (
        '{"notice_explanation":"Alpaca refused it.","notice_next_step":"Look at Alpaca.",'
        '"why":"the submit timed out"}'
    )
    assert OrderSubmitUncertainFacts.from_facts_json(noticed.to_facts_json()) == noticed
    assert noticed.notice == UnknownOutcomeNotice(explanation="Alpaca refused it.", next_step="Look at Alpaca.")
    with pytest.raises(ValueError, match="its explanation and its next step, or neither"):
        OrderSubmitUncertainFacts(why="the submit timed out", notice_explanation="Alpaca refused it.")


async def test_a_clock_stepped_back_while_a_send_was_awaited_does_not_shorten_the_wait(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The host clock steps back a minute between a send and its timeout, then is put right.

    The timeout is recorded with an earlier time than the send it answers.
    The wait runs from the later of the two, so the next send still comes a
    whole wait after this one -- not one pass after it, as it would measured
    from the timeout's record. The send is the second: the first is also held
    by the wait since the order was created.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", "never_arrives"))

    def the_clock_steps_back() -> None:
        clock.value -= 60_000

    alpaca.while_awaited[2] = the_clock_steps_back
    await _the_runner_sends_the_exit(repo, alpaca)
    await _passes(repo, _the_sweep(repo, alpaca), RESEND_AFTER_MS // SWEEP_INTERVAL_MS)
    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    clock.value += 60_000

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS, 2 * RESEND_AFTER_MS]


async def test_a_clock_stepped_back_after_a_send_lengthens_the_wait(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The host clock steps back a minute after the second lost send, and stays there.

    Nothing is sent until that clock itself reads a whole wait past the send:
    a minute more of passes than the wait, never fewer.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", "never_arrives"))
    await _the_runner_sends_the_exit(repo, alpaca)
    await _passes(repo, _the_sweep(repo, alpaca), RESEND_AFTER_MS // SWEEP_INTERVAL_MS)
    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    stepped_back_ms = 4 * SWEEP_INTERVAL_MS
    clock.value -= stepped_back_ms

    passes_until_the_wait_ends = (stepped_back_ms + RESEND_AFTER_MS) // SWEEP_INTERVAL_MS
    await _passes(repo, _the_sweep(repo, alpaca), passes_until_the_wait_ends - 1)
    assert len(alpaca.sent) == 2

    await _passes(repo, _the_sweep(repo, alpaca), 1)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS, 2 * RESEND_AFTER_MS]


async def test_a_closing_order_alpaca_acknowledged_is_never_sent_again_while_it_reads_absent(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Alpaca accepted the order, then no read shows it: that contradicts absence, it does not prove it.

    However long the wait has been over, the order is not sent again and the
    EXIT keeps custody of it.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock)
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)
    assert closing.broker_order_id == f"alpaca-{closing.client_order_id}"
    alpaca.loses_sight_of(closing.client_order_id)

    await _passes(repo, _the_sweep(repo, alpaca), FIVE_MINUTES_OF_PASSES)

    assert len(alpaca.sent) == 1
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None


async def test_an_operators_flatten_whose_submit_never_arrived_is_sent_again_after_reading_the_account(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A Flatten's closing order is sent again by the passes too, and checked against the account first (#2839)."""
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    assert alpaca.reads == ["orders", "positions", "submit"]
    exit_id = _flatten_exit(repo)
    assert _exit_state(repo, exit_id) == "unknown"

    async def the_sweep() -> object:
        return await facade.reconcile_account(trigger="AUTOMATIC")

    await _passes(repo, the_sweep, FIVE_MINUTES_OF_PASSES)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    assert [(sent.leg.side, sent.leg.quantity) for sent in alpaca.sent] == [("sell", 10), ("sell", 10)]
    # The open orders, then the positions, were read for the second send as for the first.
    second_send = len(alpaca.reads) - 1 - alpaca.reads[::-1].index("submit")
    assert alpaca.reads[second_send - 2 : second_send] == ["orders", "positions"]
    assert _exit_state(repo, exit_id) == "in_progress"

    alpaca.fills(alpaca.sent[0].client_order_id)
    await _passes(repo, the_sweep, 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == 0


async def test_an_operators_flatten_is_not_sent_again_once_the_account_no_longer_covers_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The shares left the account while the Flatten's lost order waited: the passes send nothing."""
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    exit_id = _flatten_exit(repo)
    alpaca.holds = 0.0

    async def the_sweep() -> object:
        return await facade.reconcile_account(trigger="AUTOMATIC")

    await _passes(repo, the_sweep, FIVE_MINUTES_OF_PASSES)

    assert len(alpaca.sent) == 1
    refusal = flatten_send_refusal(repo, exit_id)
    assert refusal is not None and refusal.reason_code == FLATTEN_NOT_COVERED_AT_BROKER
    assert _exit_state(repo, exit_id) == "failed"
    assert repo.active_exit_for_strategy(SID) is None
    assert repo.position(SID, "SPY") == 10


async def test_a_flatten_whose_order_the_account_lists_is_never_sent_again_however_long_its_lookup_reads_absent(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The Flatten's lost order is at Alpaca: its open orders list it, its exact lookup does not answer yet.

    Each pass past the wait reads the account for the send, finds the order
    there and sends nothing (#2839). That record does not hold the next pass
    back, and no pass sends: the EXIT keeps custody until the lookup answers,
    and then the order is folded as any acknowledged one.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    exit_id = _flatten_exit(repo)
    closing = _closing_order(repo)
    alpaca.lists_among_its_open_orders(closing.client_order_id)

    async def the_exit_is_driven() -> object:
        # Driven as the sweep drives it, without the sweep's own account read
        # first: that read would fold an order the open orders list.
        return await resolve_exit(
            repo, effect_operation_id=exit_id, trade=alpaca, pricing=UNPRICEABLE_RECOVERY, read=alpaca
        )

    await _passes(repo, the_exit_is_driven, FIVE_MINUTES_OF_PASSES)

    assert len(alpaca.sent) == 1
    assert _exit_state(repo, exit_id) == "unknown"
    assert flatten_send_refusal(repo, exit_id) is None
    assert repo.active_exit_for_strategy(SID) is not None

    alpaca.shows(closing.client_order_id)
    await _passes(repo, the_exit_is_driven, 1)

    assert len(alpaca.sent) == 1
    found = repo.order(closing.order_ref)
    assert found is not None
    assert (found.broker_order_id, found.broker_state) == (f"alpaca-{closing.client_order_id}", "new")
    assert _exit_state(repo, exit_id) == "in_progress"


@pytest.mark.parametrize(
    ("reported", "left_in_the_account"),
    [
        pytest.param({"status": "new"}, 10.0, id="acknowledged"),
        pytest.param(
            {"status": "partially_filled", "filled_quantity": 4.0, "filled_avg_price": 100.0}, 6.0,
            id="partly-filled",
        ),
    ],
)
async def test_a_flatten_whose_order_the_stream_reports_while_the_account_is_read_for_its_send_is_not_sent_again(
    crashed_with_exposure,  # noqa: F811
    reported: dict[str, object],
    left_in_the_account: float,
) -> None:
    """The exact lookup read the Flatten's lost order absent; the stream reports it before the send is recorded.

    The account is read between the two (#2839). Whether the order ever
    reached Alpaca is read again in the run that records the send: once the
    stream has acknowledged or filled it, nothing is recorded and nothing is
    sent, and the Flatten is not refused for shares its own order is selling.
    The EXIT keeps custody for the next pass to fold the order.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    exit_id = _flatten_exit(repo)
    closing = _closing_order(repo)
    at_alpaca = safe_flatten._broker_order(
        closing.client_order_id, order_id=f"alpaca-{closing.client_order_id}", side="sell", quantity=10.0,
    ).model_copy(update=reported)

    filled = at_alpaca.filled_quantity
    frame = (
        BrokerOrderEvent(
            event_type="partial_fill", occurred_at_ms=clock(), price=100.0, quantity=filled,
            execution_id="exec-closing-1",
        )
        if filled
        else BrokerOrderEvent(event_type="new", occurred_at_ms=clock(), price=None, quantity=None)
    )

    async def the_stream_reports_the_order() -> None:
        alpaca.holds = left_in_the_account
        await _the_stream_reports(repo, at_alpaca, frame)

    alpaca.while_positions_are_read = the_stream_reports_the_order

    async def the_exit_is_driven() -> object:
        # Driven as the sweep drives it, without the sweep's own account read
        # first: the only account read is the one made for the send.
        return await resolve_exit(
            repo, effect_operation_id=exit_id, trade=alpaca, pricing=UNPRICEABLE_RECOVERY, read=alpaca
        )

    await _passes(repo, the_exit_is_driven, RESEND_AFTER_MS // SWEEP_INTERVAL_MS)

    assert alpaca.while_positions_are_read is None, "the account was never read for a send"
    assert len(alpaca.sent) == 1
    assert flatten_send_refusal(repo, exit_id) is None
    assert repo.active_exit_for_strategy(SID) is not None
    found = repo.order(closing.order_ref)
    assert found is not None and found.broker_order_id == at_alpaca.order_id
    assert repo.position(SID, "SPY") == left_in_the_account


async def test_a_flatten_whose_order_the_stream_acknowledges_as_it_would_be_sent_again_after_the_close_keeps_custody(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Whether the order reached Alpaca is read again before the session rule, not after it.

    An operator's Flatten is lost a minute before the regular close, and the
    Clerk next drives it a minute after. The exact lookup reads the order
    absent, and the stream acknowledges it while the account is read for the
    send. A market order can no longer go out at that hour -- but this one is
    not going out: it is at Alpaca. Judged by the session rule first, the
    EXIT would fail as one whose order missed its session and release a
    position its own order is selling. It keeps custody and reads in
    progress instead.
    """
    repo, clock = crashed_with_exposure
    close_ms = session_close_ms_utc(et_date_at_ms(clock()))
    _walk_clock_to(repo, close_ms - 60_000)
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    exit_id = _flatten_exit(repo)
    closing = _closing_order(repo)
    assert _exit_state(repo, exit_id) == "unknown"
    at_alpaca = safe_flatten._broker_order(
        closing.client_order_id, order_id=f"alpaca-{closing.client_order_id}", status="new",
        side="sell", quantity=10.0,
    )

    async def the_stream_acknowledges_the_order() -> None:
        await _the_stream_reports(
            repo, at_alpaca,
            BrokerOrderEvent(event_type="new", occurred_at_ms=clock(), price=None, quantity=None),
        )

    alpaca.while_positions_are_read = the_stream_acknowledges_the_order
    _walk_clock_to(repo, close_ms + 60_000)

    # Driven as the sweep drives it, without the sweep's own account read
    # first: the only account read is the one made for the send.
    await resolve_exit(
        repo, effect_operation_id=exit_id, trade=alpaca, pricing=UNPRICEABLE_RECOVERY, read=alpaca
    )

    assert alpaca.while_positions_are_read is None, "the account was never read for a send"
    assert len(alpaca.sent) == 1
    found = repo.order(closing.order_ref)
    assert found is not None
    assert (found.broker_order_id, found.broker_state) == (at_alpaca.order_id, "new")
    assert _exit_state(repo, exit_id) == "in_progress"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None
    assert repo.first_effect_transition(effect_operation_id=exit_id, transition_kind="EXIT_NOT_FLAT") is None
