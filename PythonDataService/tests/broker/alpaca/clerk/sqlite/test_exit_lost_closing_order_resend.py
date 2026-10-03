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
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Literal

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_recovery import DEFAULT_RECOVERY_INTERVAL_MS
from app.broker.alpaca.clerk.sqlite.exit_resolution import flatten_send_refusal
from app.broker.alpaca.clerk.sqlite.flatten_cover import FLATTEN_NOT_COVERED_AT_BROKER
from app.broker.alpaca.clerk.sqlite.models import OrderResource
from app.broker.alpaca.clerk.sqlite.order_evidence import submit_absence_grace_ms
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    _Account,
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

type _SubmitFate = Literal["never_arrives", "arrives_unanswered"]


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

    The exact lookup answers a closing order only once Alpaca has it and shows
    it. Any entry it is asked about filled before its EXIT was decided, as
    every entry in these tests did. A submit for an order Alpaca already has
    is the second sale these tests exist to rule out, and fails the test.

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

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.sent.append(_Sent(client_order_id, leg, self._clock()))
        self.reads.append("submit")
        assert client_order_id not in self._has, "sent again while Alpaca already has the order"
        order = safe_flatten._broker_order(
            client_order_id, order_id=f"alpaca-{client_order_id}", status="new",
            side=leg.side, quantity=leg.quantity,
        )
        if not self._fates:
            self._has[client_order_id] = order
            self.open_orders.append(order)
            return order
        if self._fates.pop(0) == "arrives_unanswered":
            self._has[client_order_id] = order
            self._unanswered.add(client_order_id)
        self.while_awaited.get(len(self.sent), lambda: None)()
        raise BrokerUnavailable("the submit timed out")

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
