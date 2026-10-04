"""A settlement the process dies in the middle of is finished, never left half done (#2854).

Settling a closing order whose outcome is unknown writes to the ledger more
than once, and every write commits alone. These tests run the real Clerk on
a real temp SQLite ledger, let the process die right after each durable
write of a settlement in turn, and assert what the ledger holds afterwards:
after the operator asks again, and after the Clerk's ordinary passes alone,
the order is settled exactly as an uninterrupted settlement settles it.
Nothing is sent in any of them.

"Alpaca has no such order" is one write: its row ends the EXIT and opens,
with the notice that the position is still open, the episode under which
the Clerk sends nothing for it by itself. So a process that dies right after
that write leaves the position held for the operator, never free for the
watchdog to sell.

A process that dies releases nothing: the claim it held on the EXIT stands
until it expires, and every pass before then defers the EXIT. So the passes
here run until the first one after the claim's expiry.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

from app.broker.alpaca.clerk.sqlite.models import CommittedTransition, TransitionInput
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionError
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryActionUnavailableError,
    StaleRecoveryTokenError,
)
from app.broker.alpaca.clerk.sqlite.repository import (
    DEFAULT_CLAIM_TTL_MS,
    ClerkSqliteRepository,
    OperationClaimError,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import EXIT_STUCK_REASON_CODE
from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import OrderOutcomeAttestation
from app.broker.contract.errors import BrokerUnavailable
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
    SWEEP_INTERVAL_MS,
    _closing_order,
    _exit_state,
    _passes,
    _the_sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import _context_of
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    SID,
    crashed_with_exposure,  # noqa: F401 -- the repository, in the regular session, these tests reuse
)
from tests.broker.alpaca.clerk.sqlite.test_unknown_order_attestation import (
    ABSENT,
    SIXTY_PASSES,
    _a_stopped_bot_whose_closing_order_alpaca_has_unseen,
    _a_stopped_bot_whose_closing_order_was_lost,
    _AlpacaAccount,
    _attested,
    _elsewhere,
    _facade,
    _open_episode,
    _presented,
    _settle,
    _the_ledger_records_the_bots_shares_written_off,
)

# More durable writes than any settlement here makes: every window is covered
# (``test_every_durable_write_of_a_settlement_is_a_window_these_tests_cover``).
WRITES_COVERED = 6
# The Clerk's passes, every 15 s, up to the first one after a dead process's
# claim on the EXIT has expired: the pass that finishes what it left.
PASSES = DEFAULT_CLAIM_TTL_MS // SWEEP_INTERVAL_MS + 1


class _ProcessDied(Exception):
    """The process is gone: its last write is in the ledger and nothing after it ran."""


@contextmanager
def _the_process_dies_after(repo: ClerkSqliteRepository, writes: int) -> Iterator[list[str]]:
    """Inside the block, let ``writes`` transitions commit, then die. Yields the kinds written, as they are written.

    Dead, the process releases nothing. The exception that stands for its
    death still unwinds the settlement's ``finally``, so from the death on a
    release of an operation claim does nothing, and the claim the settlement
    held is left as a death leaves it: held until it expires.
    """
    written: list[str] = []
    append, release = repo.append_transition, repo.release_operation_claim

    def append_transition(transition: TransitionInput, **kwargs: object) -> CommittedTransition:
        committed = append(transition, **kwargs)  # type: ignore[arg-type]
        written.append(transition.transition_kind)
        if len(written) == writes:
            raise _ProcessDied(f"died after {' → '.join(written)}")
        return committed

    def release_operation_claim(*, effect_operation_id: str, token: str) -> bool:
        if len(written) >= writes:
            return False
        return release(effect_operation_id=effect_operation_id, token=token)

    repo.append_transition = append_transition  # type: ignore[method-assign]
    repo.release_operation_claim = release_operation_claim  # type: ignore[method-assign]
    try:
        yield written
    finally:
        repo.append_transition = append  # type: ignore[method-assign]
        repo.release_operation_claim = release  # type: ignore[method-assign]


@dataclass(frozen=True)
class _Settled:
    """What the ledger holds once a settlement is whole."""

    exit_state: str
    held: float
    exit_not_flat_is_open: bool
    filled: float
    taken_up: bool
    # The position is left to the operator: the Clerk sends nothing for it by itself.
    held_for_the_operator: bool = False


type _Scenario = Callable[[ClerkSqliteRepository, _AlpacaAccount], Awaitable[tuple[str, OrderOutcomeAttestation]]]


async def _alpaca_never_had_it(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    return await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca), ABSENT


async def _alpaca_never_had_it_and_the_bot_holds_nothing(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    alpaca.holds = 0.0
    _the_ledger_records_the_bots_shares_written_off(repo)
    return exit_id, ABSENT


async def _alpaca_filled_it(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    alpaca.fills(_closing_order(repo).client_order_id)
    return exit_id, it_is_this_order


async def _alpaca_has_it_working(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    return await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)


async def _alpaca_canceled_it_unfilled(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    exit_id, it_is_this_order = await _a_stopped_bot_whose_closing_order_alpaca_has_unseen(repo, alpaca)
    closing = _closing_order(repo)
    assert it_is_this_order.broker_order_id is not None
    now_ms = repo.clock()
    alpaca.by_broker_id[it_is_this_order.broker_order_id] = alpaca.at_alpaca(
        closing.client_order_id
    ).model_copy(update={"status": "canceled", "canceled_at_ms": now_ms, "updated_at_ms": now_ms})
    return exit_id, it_is_this_order


SETTLEMENTS = [
    pytest.param(
        "never_arrives", _alpaca_never_had_it,
        _Settled(
            "failed", held=10.0, exit_not_flat_is_open=True, filled=0.0, taken_up=False,
            held_for_the_operator=True,
        ),
        id="absent",
    ),
    pytest.param(
        "never_arrives", _alpaca_never_had_it_and_the_bot_holds_nothing,
        _Settled("succeeded", held=0.0, exit_not_flat_is_open=False, filled=0.0, taken_up=False),
        id="absent-and-the-bot-holds-nothing",
    ),
    pytest.param(
        "arrives_unanswered", _alpaca_filled_it,
        _Settled("succeeded", held=0.0, exit_not_flat_is_open=False, filled=10.0, taken_up=True),
        id="exists-filled",
    ),
    pytest.param(
        "arrives_unanswered", _alpaca_has_it_working,
        _Settled("in_progress", held=10.0, exit_not_flat_is_open=False, filled=0.0, taken_up=True),
        id="exists-working",
    ),
    pytest.param(
        "arrives_unanswered", _alpaca_canceled_it_unfilled,
        _Settled("failed", held=10.0, exit_not_flat_is_open=True, filled=0.0, taken_up=True),
        id="exists-canceled-unfilled",
    ),
]


def _assert_settled(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount, exit_id: str,
    attestation: OrderOutcomeAttestation, expected: _Settled,
) -> None:
    closing = _closing_order(repo)
    assert _exit_state(repo, exit_id) == expected.exit_state
    assert repo.position(SID, "SPY") == expected.held
    assert _open_episode(repo, ORDER_OUTCOME_UNKNOWN_REASON_CODE) is None
    assert (_open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is not None) == expected.exit_not_flat_is_open
    assert (_open_episode(repo, EXIT_STUCK_REASON_CODE) is not None) == expected.held_for_the_operator
    assert repo.effective_fill_totals_for_order(closing.order_ref)[0] == expected.filled
    assert closing.broker_order_id == (attestation.broker_order_id if expected.taken_up else None)
    assert len(alpaca.sent) == 1, "no settlement, whole or interrupted, ever sends an order"


@pytest.mark.parametrize(("submit", "scenario", "expected"), SETTLEMENTS)
async def test_every_durable_write_of_a_settlement_is_a_window_these_tests_cover(
    crashed_with_exposure,  # noqa: F811
    submit: str,
    scenario: _Scenario,
    expected: _Settled,
) -> None:
    """An uninterrupted settlement makes fewer writes than the windows below reach, and ends as expected."""
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(submit,))  # type: ignore[arg-type]
    exit_id, attestation = await scenario(repo, alpaca)

    with _the_process_dies_after(repo, WRITES_COVERED) as written:
        result = await _settle(_facade(repo, alpaca), _context_of(repo, SID), attestation)

    assert result.applied is True
    assert 1 <= len(written) < WRITES_COVERED
    _assert_settled(repo, alpaca, exit_id, attestation, expected)


@pytest.mark.parametrize("the_operator_asks_again", [False, True], ids=["passes-only", "asked-again"])
@pytest.mark.parametrize("dies_after", range(1, WRITES_COVERED))
@pytest.mark.parametrize(("submit", "scenario", "expected"), SETTLEMENTS)
async def test_a_settlement_the_process_dies_in_is_finished_by_a_retry_or_the_clerks_passes(
    crashed_with_exposure,  # noqa: F811
    submit: str,
    scenario: _Scenario,
    expected: _Settled,
    dies_after: int,
    the_operator_asks_again: bool,
) -> None:
    """The process dies right after the settlement's ``dies_after``-th durable write.

    Whichever write it was, the order ends where an uninterrupted settlement
    leaves it: after the Clerk's ordinary passes alone, and when the operator
    asks for the same settlement again first. The dead process's claim on
    the EXIT is still held, as a death leaves it; the passes defer the EXIT
    until it expires, and the first pass after that finishes the settlement.
    Asked again, the Clerk answers with the settlement's own record -- the
    one attestation row there is -- and never with a record of something
    that did not happen. One send, and one credit of a fill, however it ends.

    An order settled as absent leaves its position held for the operator
    from its one write on. However the process died, fifteen minutes of
    passes afterwards send nothing, though Alpaca holds the bot's 10 SPY as
    the Clerk's records say.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=(submit,))  # type: ignore[arg-type]
    exit_id, attestation = await scenario(repo, alpaca)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    presented = await _presented(current_context)

    with _the_process_dies_after(repo, dies_after) as written:
        try:
            await _settle(facade, current_context, attestation, presented=presented)
        except _ProcessDied:
            assert len(written) == dies_after
            with pytest.raises(OperationClaimError):
                # The dead process's claim on the EXIT still stands.
                repo.claim_before_broker_contact(exit_id)
        else:
            assert len(written) < dies_after, "the settlement was whole before the process died"

    (attested,) = _attested(repo)
    assert attested.attestation == attestation.kind
    if the_operator_asks_again:
        again = await _settle(facade, current_context, attestation, presented=presented)
        assert again.applied is False
        assert len(_attested(repo)) == 1, "asked again, the same settlement is not recorded twice"
    await _passes(repo, _the_sweep(repo, alpaca), PASSES)

    _assert_settled(repo, alpaca, exit_id, attestation, expected)
    assert len(_attested(repo)) == 1
    if expected.held_for_the_operator:
        await _passes(repo, _the_sweep(repo, alpaca), SIXTY_PASSES)
        _assert_settled(repo, alpaca, exit_id, attestation, expected)


async def _dies_once_the_attestation_is_written(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount, attestation: OrderOutcomeAttestation
) -> str:
    """The process dies right after the attestation's own row is written. Returns the Alpaca order id it took up."""
    with _the_process_dies_after(repo, 1) as written, pytest.raises(_ProcessDied):
        await _settle(_facade(repo, alpaca), _context_of(repo, SID), attestation)
    assert written == ["ORDER_OUTCOME_ATTESTED"]
    assert attestation.broker_order_id is not None
    assert _closing_order(repo).broker_order_id == attestation.broker_order_id
    return attestation.broker_order_id


@pytest.mark.parametrize(
    ("scenario", "expected", "cost"),
    [
        pytest.param(
            _alpaca_filled_it,
            _Settled("succeeded", held=0.0, exit_not_flat_is_open=False, filled=10.0, taken_up=True),
            1_000.0,
            id="filled",
        ),
        pytest.param(
            _alpaca_canceled_it_unfilled,
            _Settled("failed", held=10.0, exit_not_flat_is_open=True, filled=0.0, taken_up=True),
            0.0,
            id="canceled-unfilled",
        ),
    ],
)
async def test_an_order_that_had_ended_when_it_was_taken_up_is_finished_from_that_read_once_alpaca_has_no_such_order(
    crashed_with_exposure,  # noqa: F811
    scenario: _Scenario,
    expected: _Settled,
    cost: float,
) -> None:
    """The regression (#2854 review): the process dies right after the attestation is written, and Alpaca forgets the order.

    The row took the order up by its Alpaca order id and recorded how much
    of it had filled, and not at what price. Finishing rested on reading the
    order by that id again; when Alpaca answered that it has no order under
    it, every pass folded the EXIT back to ``unknown``, for good, with a fill
    that could never be credited.

    The row now records the price of what had filled and Alpaca's time for
    it. An order that had ended does nothing more, so that read is its whole
    outcome: when Alpaca answers that it has no such order, the pass
    finishes from the row. The fill is credited once, at that price.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, attestation = await scenario(repo, alpaca)
    alpaca.has_no_order_under.add(await _dies_once_the_attestation_is_written(repo, alpaca, attestation))

    await _passes(repo, _the_sweep(repo, alpaca), 2 * PASSES)

    _assert_settled(repo, alpaca, exit_id, attestation, expected)
    assert repo.effective_fill_totals_for_order(_closing_order(repo).order_ref) == (expected.filled, cost)


async def test_an_order_taken_up_is_never_finished_from_its_row_while_alpaca_cannot_be_read(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A read that fails is no answer: nothing is credited from the row, and every pass asks Alpaca again.

    Only Alpaca answering that it has no such order lets the row stand in
    for it. Once Alpaca can be read, the pass finishes from what it shows.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, attestation = await _alpaca_filled_it(repo, alpaca)
    broker_order_id = await _dies_once_the_attestation_is_written(repo, alpaca, attestation)
    closing = _closing_order(repo)
    alpaca.by_broker_id[broker_order_id] = BrokerUnavailable("alpaca timed out")

    await _passes(repo, _the_sweep(repo, alpaca), 2 * PASSES)

    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.effective_fill_totals_for_order(closing.order_ref) == (0.0, 0.0)
    assert repo.position(SID, "SPY") == 10.0

    del alpaca.by_broker_id[broker_order_id]
    await _passes(repo, _the_sweep(repo, alpaca), 1)

    _assert_settled(
        repo, alpaca, exit_id, attestation,
        _Settled("succeeded", held=0.0, exit_not_flat_is_open=False, filled=10.0, taken_up=True),
    )


async def test_an_order_that_was_working_when_it_was_taken_up_is_not_finished_from_its_row(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """What a working order did after the Clerk read it is not on the row, so the row settles nothing.

    Alpaca answers that it has no order under the id: the EXIT stays
    ``unknown``, custody is kept, and nothing is credited or sent.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, attestation = await _alpaca_has_it_working(repo, alpaca)
    alpaca.has_no_order_under.add(await _dies_once_the_attestation_is_written(repo, alpaca, attestation))

    await _passes(repo, _the_sweep(repo, alpaca), 2 * PASSES)

    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None, "the EXIT keeps custody of the position"
    assert repo.effective_fill_totals_for_order(_closing_order(repo).order_ref) == (0.0, 0.0)
    assert repo.position(SID, "SPY") == 10.0
    assert len(alpaca.sent) == 1


async def test_an_order_that_settled_by_itself_is_never_reported_as_settled_by_the_operator(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The operator's "no such order" is refused, the Clerk then sends the order again, and it fills.

    The same request asked again is not that order's settlement: nothing the
    operator said took effect, so no receipt answers it and nothing is
    recorded for it.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca)
    closing = _closing_order(repo)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)
    presented = await _presented(current_context)
    alpaca.open_orders = [_elsewhere("someone-buys", side="buy")]
    with pytest.raises(RecoveryExecutionError, match="An order for SPY is working at Alpaca"):
        await _settle(facade, current_context, ABSENT, presented=presented)
    alpaca.open_orders = []

    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert len(alpaca.sent) == 2, "the Clerk sent the order again by itself"
    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert _exit_state(repo, exit_id) == "succeeded"

    with pytest.raises((StaleRecoveryTokenError, RecoveryActionUnavailableError)):
        await _settle(facade, current_context, ABSENT, presented=presented)

    assert _attested(repo) == []
