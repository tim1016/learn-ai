"""A failed EXIT's closing order is looked up again by the Clerk's passes (#2854).

An EXIT can fail while the outcome of its closing order is not known. An
operator's Flatten whose send was lost finds its own order at Alpaca as it
would be sent again, and is held. The order then fills where no read shows
it: the open orders no longer list it, and its exact lookup still answers
absent. The check before the next send finds the shares gone and fails the
EXIT -- "nothing was sent" -- while its own order sold them.

Nothing was sold twice, but nothing looked the failed EXIT's order up again
either: only a failed ENTER's order was. The position stayed attributed, and
the account read as drifted, until a stream frame arrived or the operator
discharged it. The pass now asks Alpaca about that order too, and folds what
it answers.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite import reconcile as reconcile_module
from app.broker.alpaca.clerk.sqlite.exit import resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import flatten_send_refusal
from app.broker.alpaca.clerk.sqlite.flatten_cover import FLATTEN_NOT_COVERED_AT_BROKER
from app.broker.alpaca.clerk.sqlite.reconcile import AccountReconciliationResult, reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import RecoveryExecutionError
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.uncertainty import POSITION_DRIFT_REASON_CODE
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.utils.session_anchors import et_date_at_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _TestClock, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
    FIVE_MINUTES_OF_PASSES,
    RESEND_AFTER_MS,
    SWEEP_INTERVAL_MS,
    _Alpaca,
    _closing_order,
    _exit_state,
    _passes,
    _the_runner_sends_the_exit,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    _exit_not_flat,
    _flatten,
    _flatten_exit,
    _stopped_bot_offered_its_flatten,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    SID,
    crashed_with_exposure,  # noqa: F401 -- the repository, in the regular session, these tests reuse
)

# How long a closing order rests between two lookups: every other pass.
REST_BETWEEN_LOOKUPS_MS = 30_000
# How long after its EXIT failed a closing order is still looked up.
A_DAY_MS = 24 * 60 * 60 * 1000


def _position_drift(repo: ClerkSqliteRepository) -> dict | None:
    """The account's open "position doesn't match broker records" notice, if it has one."""
    return repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=POSITION_DRIFT_REASON_CODE, strategy_instance_id=None
    )


def _the_sweep(facade: SqliteAlpacaClerkFacade) -> Callable[[], Awaitable[AccountReconciliationResult]]:
    """The pass ``ReconciliationSweep`` runs through the account's own facade."""

    async def one_pass() -> AccountReconciliationResult:
        return await facade.reconcile_account(trigger="AUTOMATIC")

    return one_pass


async def _one_pass(repo: ClerkSqliteRepository, facade: SqliteAlpacaClerkFacade) -> AccountReconciliationResult:
    """One pass of the Clerk, one sweep interval after the last."""
    _walk_clock_to(repo, repo.clock() + SWEEP_INTERVAL_MS)
    return await facade.reconcile_account(trigger="AUTOMATIC")


async def _a_flatten_failed_after_its_own_order_filled_unseen(
    repo: ClerkSqliteRepository, clock: _TestClock
) -> tuple[_Alpaca, SqliteAlpacaClerkFacade, str]:
    """An operator's Flatten told "nothing was sent" while its own lost order sold the position.

    Its send's answer is lost and the order reaches Alpaca. Two passes on,
    the account read made to send it again lists it, so it is held. It then
    fills: no open order, no shares, and an exact lookup that still answers
    absent. The next pass's check before the send fails the EXIT. Returns
    Alpaca, the account's facade and the failed EXIT.
    """
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

    await _passes(repo, the_exit_is_driven, RESEND_AFTER_MS // SWEEP_INTERVAL_MS)
    assert _exit_state(repo, exit_id) == "unknown"

    alpaca.fills(closing.client_order_id)
    await _passes(repo, the_exit_is_driven, 1)

    assert _exit_state(repo, exit_id) == "failed"
    refusal = flatten_send_refusal(repo, exit_id)
    assert refusal is not None and refusal.reason_code == FLATTEN_NOT_COVERED_AT_BROKER
    assert "nothing was sent" in refusal.explanation
    assert len(alpaca.sent) == 1
    assert repo.position(SID, "SPY") == 10
    return alpaca, facade, exit_id


async def test_a_failed_flattens_own_order_that_filled_unseen_is_found_and_credited_once(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The regression (#2854): the position stayed attributed, and the account drifted, for as long as no frame came.

    The passes now look the failed EXIT's closing order up, every other pass
    while Alpaca answers that it has none. When the lookup shows the order
    filled, the fill is credited once: the bot holds nothing, the drift and
    the open-exit notice clear, and the order's broker state ends its lookups.
    Nothing is sold a second time, before or after.
    """
    repo, clock = crashed_with_exposure
    alpaca, facade, exit_id = await _a_flatten_failed_after_its_own_order_filled_unseen(repo, clock)
    closing = _closing_order(repo)
    looked_up_before = alpaca.looked_up.count(closing.client_order_id)

    drifted = [await _one_pass(repo, facade) for _ in range(3)]

    assert [result.verdict for result in drifted] == ["position_drift"] * 3
    assert _position_drift(repo) is not None
    assert repo.position(SID, "SPY") == 10
    # Asked on the first pass, rested for the second, asked again on the third.
    assert alpaca.looked_up.count(closing.client_order_id) == looked_up_before + 2

    alpaca.answers_its_lookup(closing.client_order_id)
    _walk_clock_to(repo, repo.clock() + REST_BETWEEN_LOOKUPS_MS - SWEEP_INTERVAL_MS)
    found = await _one_pass(repo, facade)

    assert found.verdict == "clean"
    assert repo.position(SID, "SPY") == 0
    folded = repo.order(closing.order_ref)
    assert folded is not None
    assert (folded.broker_order_id, folded.broker_state) == (f"alpaca-{closing.client_order_id}", "filled")
    assert [(fill["side"], fill["qty"], fill["price"]) for fill in repo.fills_for_order(closing.order_ref)] == [
        ("SELL", 10.0, 100.0)
    ]
    assert _position_drift(repo) is None
    assert _exit_not_flat(repo) is None
    # The EXIT's own record is not rewritten: it failed, and its order's fill is on its timeline.
    assert _exit_state(repo, exit_id) == "failed"
    assert len(alpaca.sent) == 1

    looked_up_when_found = alpaca.looked_up.count(closing.client_order_id)
    await _passes(repo, _the_sweep(facade), FIVE_MINUTES_OF_PASSES)

    assert repo.position(SID, "SPY") == 0
    assert len(repo.fills_for_order(closing.order_ref)) == 1
    assert alpaca.looked_up.count(closing.client_order_id) == looked_up_when_found
    assert len(alpaca.sent) == 1


async def test_a_failed_exits_closing_order_is_not_looked_up_for_ever(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The bound: a closing order is looked up for a day after its EXIT failed, and no longer.

    Every closing order is a DAY order, which Alpaca ends with its day's last
    session, so one still absent a day on never reached it. The pass stops
    asking; the drift stands for the operator, as it did before.
    """
    repo, clock = crashed_with_exposure
    alpaca, facade, _exit_id = await _a_flatten_failed_after_its_own_order_filled_unseen(repo, clock)
    closing = _closing_order(repo)
    failed_at_ms = clock.value

    def lookups() -> int:
        return alpaca.looked_up.count(closing.client_order_id)

    _walk_clock_to(repo, failed_at_ms + A_DAY_MS - SWEEP_INTERVAL_MS)
    looked_up_before = lookups()
    in_its_last_pass = await _one_pass(repo, facade)

    assert repo.clock() == failed_at_ms + A_DAY_MS
    assert in_its_last_pass.verdict == "position_drift"
    assert lookups() == looked_up_before + 1

    # Past the day, and long past the rest since its last lookup: asked no more.
    await _passes(repo, _the_sweep(facade), 2 * REST_BETWEEN_LOOKUPS_MS // SWEEP_INTERVAL_MS)

    assert lookups() == looked_up_before + 1
    assert _position_drift(repo) is not None
    assert repo.position(SID, "SPY") == 10
    assert len(alpaca.sent) == 1


async def test_a_failed_exits_closing_order_that_was_never_sent_is_not_looked_up(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A Flatten refused before its first send left no order that could be at Alpaca: there is nothing to ask about."""
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock)
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    alpaca.holds = 0.0
    with pytest.raises(RecoveryExecutionError):
        await _flatten(facade, current_context)
    closing = _closing_order(repo)
    assert alpaca.sent == []

    drifted = [await _one_pass(repo, facade) for _ in range(3)]

    assert [result.verdict for result in drifted] == ["position_drift"] * 3
    assert closing.client_order_id not in alpaca.looked_up


async def test_an_exit_released_at_its_sessions_end_whose_lost_order_filled_is_found_too(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Any EXIT that failed with a closing order it had sent is asked about, not an operator's Flatten alone.

    A running bot's exit is lost a minute before the close, and its order
    fills at Alpaca unseen. After the close the order cannot go out again, so
    the EXIT fails and releases the position. The same lookup finds the fill.
    """
    repo, clock = crashed_with_exposure
    close_ms = session_close_ms_utc(et_date_at_ms(clock()))
    _walk_clock_to(repo, close_ms - 60_000)
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    closing = _closing_order(repo)
    alpaca.fills(closing.client_order_id)
    _walk_clock_to(repo, close_ms + 60_000)

    async def one_pass() -> AccountReconciliationResult:
        return await reconcile_account(
            repo, read=alpaca, trade=alpaca, trigger="AUTOMATIC", pricing=UNPRICEABLE_RECOVERY
        )

    released = await one_pass()

    assert _exit_state(repo, exit_id) == "failed"
    assert released.verdict == "position_drift"
    assert repo.position(SID, "SPY") == 10

    alpaca.answers_its_lookup(closing.client_order_id)
    _walk_clock_to(repo, repo.clock() + REST_BETWEEN_LOOKUPS_MS)
    found = await one_pass()

    assert found.verdict == "clean"
    assert repo.position(SID, "SPY") == 0
    assert [(fill["side"], fill["qty"], fill["price"]) for fill in repo.fills_for_order(closing.order_ref)] == [
        ("SELL", 10.0, 100.0)
    ]
    assert _position_drift(repo) is None
    assert len(alpaca.sent) == 1


async def test_a_pass_looks_up_no_more_closing_orders_than_its_share_and_takes_them_in_turn(
    crashed_with_exposure,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The count bound: a pass spends at most its share of lookups on failed EXITs' orders, least recently asked first.

    Two Flattens of one bot each fail with a lost order no read shows, and
    both orders are due a lookup in the same pass. With a share of one, that
    pass asks about one and the next pass about the other: neither is starved,
    and a pass's broker reads do not grow with the failed EXITs behind it.
    """
    monkeypatch.setattr(reconcile_module, "MAX_FAILED_EXIT_ORDER_LOOKUPS", 1)
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", "never_arrives"))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)

    def closing_orders() -> list[str]:
        return [order.client_order_id for order in repo.orders_for_strategy(SID) if order.role == "REDUCING"]

    def lookups_since(mark: int) -> list[str]:
        return [ref for ref in alpaca.looked_up[mark:] if ref in closing_orders()]

    async def its_lost_flatten_is_refused_when_it_would_be_sent_again() -> None:
        await _flatten(facade, current_context)
        alpaca.holds = 6.0
        _walk_clock_to(repo, repo.clock() + RESEND_AFTER_MS - SWEEP_INTERVAL_MS)
        await _one_pass(repo, facade)
        assert repo.active_exit_for_strategy(SID) is None

    await its_lost_flatten_is_refused_when_it_would_be_sent_again()
    (first,) = closing_orders()
    alpaca.holds = 10.0
    _walk_clock_to(repo, repo.clock() + 5_000)
    reconciled = await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    assert reconciled.verdict == "clean"
    mark = len(alpaca.looked_up)
    await its_lost_flatten_is_refused_when_it_would_be_sent_again()
    (second,) = [ref for ref in closing_orders() if ref != first]
    assert len(alpaca.sent) == 2

    # Both were due in the pass that failed the second Flatten: it asked about one, the newer.
    assert lookups_since(mark)[-1:] == [second]
    assert lookups_since(mark).count(first) == 0

    mark = len(alpaca.looked_up)
    await _one_pass(repo, facade)
    assert lookups_since(mark) == [first]

    mark = len(alpaca.looked_up)
    await _one_pass(repo, facade)
    assert lookups_since(mark) == [second]
    assert len(alpaca.sent) == 2
