"""A settlement the process dies in the middle of is finished, never left half done (#2854).

Settling a closing order whose outcome is unknown writes to the ledger more
than once, and every write commits alone. These tests run the real Clerk on
a real temp SQLite ledger, let the process die right after each durable
write of a settlement in turn, and assert what the ledger holds afterwards:
after the operator asks again, and after the Clerk's ordinary passes alone,
the order is settled exactly as an uninterrupted settlement settles it.
Nothing is sent in any of them.
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
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import OrderOutcomeAttestation
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
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
    _a_stopped_bot_whose_closing_order_alpaca_has_unseen,
    _a_stopped_bot_whose_closing_order_was_lost,
    _AlpacaAccount,
    _attested,
    _facade,
    _open_episode,
    _presented,
    _settle,
)

# More durable writes than any settlement here makes: every window is covered
# (``test_every_durable_write_of_a_settlement_is_a_window_these_tests_cover``).
WRITES_COVERED = 6
# A minute of the Clerk's passes, every 15 s.
PASSES = 4


class _ProcessDied(Exception):
    """The process is gone: its last write is in the ledger and nothing after it ran."""


@contextmanager
def _the_process_dies_after(repo: ClerkSqliteRepository, writes: int) -> Iterator[list[str]]:
    """Inside the block, let ``writes`` transitions commit, then die. Yields the kinds written, as they are written."""
    written: list[str] = []
    append = repo.append_transition

    def append_transition(transition: TransitionInput, **kwargs: object) -> CommittedTransition:
        committed = append(transition, **kwargs)  # type: ignore[arg-type]
        written.append(transition.transition_kind)
        if len(written) == writes:
            raise _ProcessDied(f"died after {' → '.join(written)}")
        return committed

    repo.append_transition = append_transition  # type: ignore[method-assign]
    try:
        yield written
    finally:
        repo.append_transition = append  # type: ignore[method-assign]


@dataclass(frozen=True)
class _Settled:
    """What the ledger holds once a settlement is whole."""

    exit_state: str
    held: float
    exit_not_flat_is_open: bool
    filled: float
    taken_up: bool


type _Scenario = Callable[[ClerkSqliteRepository, _AlpacaAccount], Awaitable[tuple[str, OrderOutcomeAttestation]]]


async def _alpaca_never_had_it(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, OrderOutcomeAttestation]:
    return await _a_stopped_bot_whose_closing_order_was_lost(repo, alpaca), ABSENT


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
        _Settled("failed", held=10.0, exit_not_flat_is_open=True, filled=0.0, taken_up=False),
        id="absent",
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
    leaves it: after a minute of the Clerk's ordinary passes alone, and when
    the operator asks for the same settlement again first. Asked again, the
    Clerk answers with the settlement's own record -- the one attestation row
    there is -- and never with a record of something that did not happen.
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
    alpaca.holds = 0.0
    with pytest.raises(RecoveryExecutionError, match="has not recorded that difference"):
        await _settle(facade, current_context, ABSENT, presented=presented)
    alpaca.holds = 10.0

    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert len(alpaca.sent) == 2, "the Clerk sent the order again by itself"
    alpaca.fills(closing.client_order_id)
    await _passes(repo, _the_sweep(repo, alpaca), 1)
    assert _exit_state(repo, exit_id) == "succeeded"

    with pytest.raises((StaleRecoveryTokenError, RecoveryActionUnavailableError)):
        await _settle(facade, current_context, ABSENT, presented=presented)

    assert _attested(repo) == []
