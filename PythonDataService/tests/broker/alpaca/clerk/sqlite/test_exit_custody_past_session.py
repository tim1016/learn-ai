"""A closing order that was sent keeps its position's custody past its session (#2854, owner decision 2026-10-03).

A closing order's answer is lost, so its outcome is unknown. Once the order's
session ended, the next pass used to fail the EXIT on the absence proof alone
and release the position. A running bot's next exit decision and the sale at
a scheduled end send with no check of the account, so an order that had
reached Alpaca unseen could be followed by a second sale under a new client
order id. A lost scheduled-end sale held overnight instead, and was sent
again, under the same id, a little after the next open.

Now the EXIT stays ``unknown`` and the Clerk holds the position: nothing more
is sent for the bot, in that session or a later one, the bot's page says so
once, each pass still looks the order up, and an operator who has looked at
Alpaca settles it.

These tests run the Clerk's own reconciliation pass at its 15 s cadence
across a regular close and across the next open, against an Alpaca that loses
a submit, and assert what reached Alpaca and what the operator is told. The
session's close and the next open are the canonical calendar's.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.models import EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.recovery_reduction import UNPRICEABLE_RECOVERY
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.exit import accept_exit, resolve_exit
from app.broker.alpaca.clerk.sqlite.exit_resolution import (
    EXIT_REDRIVE_DECISION_PREFIX,
    SCHEDULED_END_DECISION_PREFIX,
)
from app.broker.alpaca.clerk.sqlite.facts import DecisionPricingFacts, UnknownOutcomeNotice
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection, ProjectedUncertainty
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_policy import build_recovery_catalog
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.scheduled_end import (
    ScheduledEnd,
    end_sales_waiting_for_open,
    install_bot_end_schedule,
)
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_policies import RedriveThenEscalate, reason_age_policy
from app.broker.alpaca.clerk.sqlite.unknown_order_attestation import OrderOutcomeAttestation
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerOrderEvent, BrokerOrderLeg
from app.lean_sidecar.trading_calendar import next_trading_day, session_close_ms_utc, session_open_ms_utc
from app.schemas.bot_end import BotEnd
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_carryover import record_stop_outcome
from app.services.broker_v2_panel.bot_page_projection import _CUSTODY_ENTRIES
from app.utils.session_anchors import et_date_at_ms
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import submit_budgeted_test_enter, submit_budgeted_test_run
from tests.broker.alpaca.clerk.sqlite.conftest import (
    BUDGETED_RTH_MS,
    _clock_at,
    _make_held_position,
    _TestClock,
    _walk_clock_to,
)
from tests.broker.alpaca.clerk.sqlite.test_exit_lost_closing_order_resend import (
    HELD_PAST_SESSION_EXPLANATION,
    RESEND_AFTER_MS,
    SETTLE_NEXT_STEP,
    SWEEP_INTERVAL_MS,
    _a_second_exit_is_refused,
    _Alpaca,
    _closing_order,
    _exit_state,
    _notices_recorded,
    _passes,
    _Refused,
    _sent_after_the_first_ms,
    _the_open_notice,
    _the_runner_sends_the_exit,
    _the_sweep,
)
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    SIBLING_RUN_ID,
    SIBLING_SID,
    _context_of,
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
from tests.broker.alpaca.clerk.sqlite.test_scheduled_end import _Schedule
from tests.broker.alpaca.clerk.sqlite.test_unknown_order_attestation import (
    ABSENT,
    SIXTY_PASSES,
    _AlpacaAccount,
    _facade,
    _open_episode,
    _presented,
    _settle,
    _the_ledger_records_the_bots_shares_written_off,
    _the_position_is_left_to_the_operator,
)

# The session these tests cross, and the one after it, from the canonical calendar: an
# ordinary Wednesday (2026-09-16) and the Thursday that follows it.
SESSION_DAY = et_date_at_ms(BUDGETED_RTH_MS)
CLOSE_MS = session_close_ms_utc(SESSION_DAY)
NEXT_OPEN_MS = session_open_ms_utc(next_trading_day(SESSION_DAY))
# The closing order leaves the Clerk this long before the close: inside the session, and too
# late for the 30 s wait after a lost send to end inside it. So the first pass that could send
# it again comes after the close, and exactly one submit is ever made.
LOST_BEFORE_THE_CLOSE_MS = 20_000
# Twenty minutes of passes from the lost send: long past the close, past the longest wait
# between sends (five minutes) and past the age at which the watchdog re-drives a failed EXIT.
PASSES_ACROSS_THE_CLOSE = 20 * 60_000 // SWEEP_INTERVAL_MS
# The passes resume this long before the next open and run ten minutes: across the open, and
# past the 75 s after it at which a lost scheduled-end sale used to go out again.
RESUMED_BEFORE_THE_OPEN_MS = 2 * 60_000
PASSES_ACROSS_THE_OPEN = 10 * 60_000 // SWEEP_INTERVAL_MS
HELD_PAST_SESSION = UnknownOutcomeNotice(explanation=HELD_PAST_SESSION_EXPLANATION, next_step=SETTLE_NEXT_STEP)

type _Pass = Callable[[], Awaitable[object]]


def _shares(expected: float) -> object:
    """A share quantity, compared at the ledger's own tolerance for one: absolute, with no relative part."""
    return pytest.approx(expected, abs=POSITION_QTY_EPSILON, rel=0)


async def _a_running_bot_loses_its_exit_just_before_the_close(repo: ClerkSqliteRepository, alpaca: _Alpaca) -> str:
    """A running bot holding 10 SPY decides to exit 20 s before the close, and the submit's answer is lost."""
    _walk_clock_to(repo, CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    assert _exit_state(repo, exit_id) == "unknown"
    return exit_id


async def _passes_across_the_close(repo: ClerkSqliteRepository, one_pass: _Pass) -> None:
    await _passes(repo, one_pass, PASSES_ACROSS_THE_CLOSE)
    assert repo.clock() > CLOSE_MS + 15 * 60_000


async def _passes_across_the_next_open(repo: ClerkSqliteRepository, one_pass: _Pass) -> None:
    """The night goes by, and the passes run from two minutes before the next open to eight minutes after it."""
    _walk_clock_to(repo, NEXT_OPEN_MS - RESUMED_BEFORE_THE_OPEN_MS)
    await _passes(repo, one_pass, PASSES_ACROSS_THE_OPEN)
    assert repo.clock() > NEXT_OPEN_MS + 5 * 60_000


def _ledger_rows(repo: ClerkSqliteRepository) -> int:
    return len(repo.custody_transitions())


def _is_held(repo: ClerkSqliteRepository, exit_id: str) -> None:
    """The EXIT is ``unknown`` with custody of the position, and the bot's page says how it is settled."""
    assert _exit_state(repo, exit_id) == "unknown"
    assert repo.active_exit_for_strategy(SID) is not None
    assert _exit_not_flat(repo) is None, "the position was told as released"
    assert repo.position(SID, "SPY") == _shares(10)
    told = _the_open_notice(repo)
    assert told is not None
    assert (told.explanation, told.next_step) == (HELD_PAST_SESSION_EXPLANATION, SETTLE_NEXT_STEP)


# ── the rule ─────────────────────────────────────────────────────────────────


async def test_a_closing_order_lost_before_the_close_keeps_custody_past_it_and_is_told_once(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost market exit of a running bot, driven by the Clerk's passes across the close.

    Before this the first pass after the close failed the EXIT and released
    the position. Now one submit was ever made, the EXIT is ``unknown`` with
    custody, the bot's own next exit is refused, and the operator is told
    once: twenty minutes of passes record one notice, and once it is recorded
    the passes write nothing more to the ledger about the order.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    closing = _closing_order(repo)
    sweep = _the_sweep(repo, alpaca)

    # The first pass after the wait is the first after the close: it holds the order.
    await _passes(repo, sweep, RESEND_AFTER_MS // SWEEP_INTERVAL_MS)
    assert CLOSE_MS < clock() < CLOSE_MS + SWEEP_INTERVAL_MS
    _is_held(repo, exit_id)
    assert _notices_recorded(repo, closing) == [HELD_PAST_SESSION]
    rows_once_held = len(repo.transitions_for_order(closing.order_ref))

    await _passes_across_the_close(repo, sweep)

    assert len(alpaca.sent) == 1
    _is_held(repo, exit_id)
    assert _notices_recorded(repo, closing) == [HELD_PAST_SESSION]
    assert len(repo.transitions_for_order(closing.order_ref)) == rows_once_held
    _a_second_exit_is_refused(repo)
    # The settle action's own name is what the notice sends the operator to.
    assert f'"{_CUSTODY_ENTRIES["settle_unknown_exit_order"].label}"' in SETTLE_NEXT_STEP


async def test_an_order_held_overnight_is_looked_up_at_the_next_open_and_never_sent_again(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The next session opens, where a market order can go out again: yesterday's is not sent (#2854).

    Every pass still asks Alpaca for the order by its client order id. No
    pass sends it, however long the session has been open, and the one notice
    stands.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    closing = _closing_order(repo)
    sweep = _the_sweep(repo, alpaca)
    await _passes_across_the_close(repo, sweep)
    looked_up_by_the_close = alpaca.looked_up.count(closing.client_order_id)
    assert looked_up_by_the_close >= PASSES_ACROSS_THE_CLOSE

    await _passes_across_the_next_open(repo, sweep)

    assert len(alpaca.sent) == 1, "yesterday's order was sent again"
    assert alpaca.looked_up.count(closing.client_order_id) >= looked_up_by_the_close + PASSES_ACROSS_THE_OPEN
    _is_held(repo, exit_id)
    assert _notices_recorded(repo, closing) == [HELD_PAST_SESSION]


async def test_a_running_bots_next_exit_decision_is_answered_as_in_progress_and_sends_nothing(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The bot is still running the next morning and decides to exit again, a second after the open.

    Its decision is sent with no check of the account. Released at the close,
    the position was sold here under a new client order id while yesterday's
    order might be at Alpaca. Held, the decision is answered by the EXIT that
    already owns the position, and nothing is sent. No pass of the Clerk runs
    between the open and the decision, so the decision alone is what is
    judged.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    await _passes_across_the_close(repo, _the_sweep(repo, alpaca))
    _walk_clock_to(repo, NEXT_OPEN_MS + 1_000)
    assert repo.active_run(SID) is not None and len(alpaca.sent) == 1
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=alpaca, trade=alpaca)
    rows = _ledger_rows(repo)

    receipt = await facade.execute_for_instance(
        strategy_instance_id=SID, run_id=RUN_ID, decision_id="program-exit-2",
        purpose=EffectPurpose.EXIT, action_plan=alpaca_v1_action_plan("SPY"), quantity=10,
    )

    assert len(alpaca.sent) == 1, "the bot's next exit sold the position a second time"
    assert receipt.state is EffectOperationState.UNCERTAIN
    assert receipt.explanation is not None and receipt.explanation.startswith(f"EXIT_IN_PROGRESS: existing EXIT {exit_id}")
    assert _ledger_rows(repo) == rows
    _is_held(repo, exit_id)


# ── an operator's Flatten, the watchdog's re-drive, a restart ────────────────


async def test_an_operators_flatten_lost_before_the_close_is_held_and_not_sent_again_at_the_next_open(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """An operator's Flatten leaves 20 s before the close and its answer is lost.

    A Flatten is set against the account before every send, and the account
    still holds the shares the next morning, where a market order can go
    out. It is held like any sent order all the same: one submit, custody
    kept, the one notice.
    """
    repo, clock = crashed_with_exposure
    _walk_clock_to(repo, CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    facade, current_context = await _stopped_bot_offered_its_flatten(repo, account=alpaca, trade=alpaca)
    await _flatten(facade, current_context)
    exit_id, closing = _flatten_exit(repo), _closing_order(repo)
    assert _exit_state(repo, exit_id) == "unknown" and len(alpaca.sent) == 1

    async def sweep() -> object:
        return await facade.reconcile_account(trigger="AUTOMATIC")

    await _passes_across_the_close(repo, sweep)
    await _passes_across_the_next_open(repo, sweep)

    assert len(alpaca.sent) == 1, "yesterday's Flatten was sent again"
    assert alpaca.holds == _shares(10)
    _is_held(repo, exit_id)
    assert _notices_recorded(repo, closing) == [HELD_PAST_SESSION]


async def test_a_watchdog_re_drive_lost_before_the_close_is_held_and_not_sent_again_at_the_next_open(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Alpaca refuses a bot's exit; the watchdog sends it again 15 s before the close, and that answer is lost.

    The re-drive is an order of its own, under its own client order id. Past
    the close it is held as any sent order is: the watchdog starts no further
    re-drive behind it that night or after the next open, and nothing tells
    the exit as stuck.
    """
    repo, clock = crashed_with_exposure
    policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)
    alpaca = _Alpaca(repo, clock, submits=(REFUSED, "never_arrives"))
    _walk_clock_to(repo, CLOSE_MS - SWEEP_INTERVAL_MS - policy.after_ms)
    refused_exit = await _the_runner_sends_the_exit(repo, alpaca)
    assert _exit_state(repo, refused_exit) == "failed"
    sweep = _the_sweep(repo, alpaca)

    await _passes(repo, sweep, policy.after_ms // SWEEP_INTERVAL_MS)

    refused, redriven = alpaca.sent
    assert redriven.at_ms == CLOSE_MS - SWEEP_INTERVAL_MS
    assert (redriven.leg.side, redriven.leg.quantity) == ("sell", _shares(10))
    assert redriven.client_order_id != refused.client_order_id
    (redrive,) = [
        order
        for order in repo.orders_for_strategy(SID)
        if order.role == "REDUCING" and EXIT_REDRIVE_DECISION_PREFIX in order.effect_operation_id
    ]
    assert _exit_state(repo, redrive.effect_operation_id) == "unknown"

    await _passes_across_the_close(repo, sweep)
    await _passes_across_the_next_open(repo, sweep)

    assert len(alpaca.sent) == 2, "a re-drive was sent beside the one whose outcome is unknown"
    assert _exit_state(repo, redrive.effect_operation_id) == "unknown"
    active = repo.active_exit_for_strategy(SID)
    assert active is not None and active.effect_operation_id == redrive.effect_operation_id
    assert repo.position(SID, "SPY") == _shares(10)
    assert _notices_recorded(repo, redrive) == [HELD_PAST_SESSION]
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is None


async def test_a_clerk_restarted_the_next_morning_still_holds_the_order_and_says_so_once(tmp_path: Path) -> None:
    """The Clerk's process stops overnight with the order held, and starts again after the next open.

    Startup recovery retires the run and drives every unfinished operation.
    It looks the held order up and does not send it, and neither do the
    passes after it. The notice recorded the evening before is still the
    only one.
    """
    clock = _clock_at(BUDGETED_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, symbol="SPY", config_hash="h1"
    )
    submit_budgeted_test_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    await _passes_across_the_close(repo, _the_sweep(repo, alpaca))
    _is_held(repo, exit_id)
    closing = _closing_order(repo)
    repo.close()

    clock.value = NEXT_OPEN_MS + 60_000
    restarted = ClerkSqliteRepository.open(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    try:
        # Alpaca as the new process finds it: the order never arrived, and the shares are still there.
        the_next_morning = _Alpaca(restarted, clock)
        facade = SqliteAlpacaClerkFacade(
            account_mode="paper", repo=restarted, read=the_next_morning, trade=the_next_morning
        )

        await facade.recover()
        await _passes(restarted, _the_sweep(restarted, the_next_morning), PASSES_ACROSS_THE_OPEN)

        assert the_next_morning.sent == [], "the restarted Clerk sent yesterday's order again"
        assert the_next_morning.looked_up.count(closing.client_order_id) > PASSES_ACROSS_THE_OPEN
        assert restarted.active_run(SID) is None
        _is_held(restarted, exit_id)
        assert _notices_recorded(restarted, closing) == [HELD_PAST_SESSION]
    finally:
        restarted.close()


# ── the sale at a bot's scheduled end ────────────────────────────────────────


@pytest.fixture
def end_schedule() -> Iterator[_Schedule]:
    installed = _Schedule()
    install_bot_end_schedule(installed)
    yield installed
    install_bot_end_schedule(None)


def _sell_at(end_at_ms: int) -> ScheduledEnd:
    return ScheduledEnd(strategy_instance_id=SID, end=BotEnd(end_at_ms=end_at_ms, end_action="SELL"))


def _scheduled_end_sales(repo: ClerkSqliteRepository) -> list[str]:
    """The effect of every sale the Clerk accepted for the bot's scheduled end."""
    return sorted(
        {
            order.effect_operation_id
            for order in repo.orders_for_strategy(SID)
            if SCHEDULED_END_DECISION_PREFIX in order.effect_operation_id
        }
        | {
            effect.effect_operation_id
            for effect in repo.reconcilable_effect_operations()
            if SCHEDULED_END_DECISION_PREFIX in effect.effect_operation_id
        }
    )


async def test_a_scheduled_end_sale_that_was_sent_and_lost_is_not_sent_again_at_the_next_open(
    crashed_with_exposure,  # noqa: F811
    end_schedule: _Schedule,
) -> None:
    """The one exit that already kept custody overnight, and sent its order again the next morning.

    A bot's end comes 20 s before the close. The Clerk stops its run and
    sends the sale, and the answer is lost. The sale sells only in regular
    hours, so after the close it used to hold "for the open" and go out
    again, under the same client order id, about 75 s after it. An order
    already sent in an earlier session is now looked up, not sent again: one
    submit, the notice that it is held, and no line saying a sale goes out at
    the open.
    """
    repo, clock = crashed_with_exposure
    await safe_flatten._held_position(repo)
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    end_schedule.ends[SID] = _sell_at(CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    sweep = _the_sweep(repo, alpaca)
    _walk_clock_to(repo, CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS - SWEEP_INTERVAL_MS)

    await _passes(repo, sweep, 1)

    assert repo.active_run(SID) is None and end_schedule.stopped == [(SID, RUN_ID)]
    assert [(sent.leg.side, sent.leg.quantity, sent.at_ms) for sent in alpaca.sent] == [
        ("sell", _shares(10), CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    ]
    (sale,) = _scheduled_end_sales(repo)
    assert _exit_state(repo, sale) == "unknown"

    await _passes_across_the_close(repo, sweep)
    await _passes_across_the_next_open(repo, sweep)

    assert _sent_after_the_first_ms(alpaca) == [0], "the lost sale was sent again"
    _is_held(repo, sale)
    assert _notices_recorded(repo, _closing_order(repo)) == [HELD_PAST_SESSION]
    assert end_sales_waiting_for_open(repo) == []
    assert repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_MARKET_HOLD") is None
    assert _scheduled_end_sales(repo) == [sale]


async def test_a_scheduled_end_sale_never_sent_still_goes_out_at_the_next_open(
    crashed_with_exposure,  # noqa: F811
    end_schedule: _Schedule,
) -> None:
    """Unchanged: an end the Clerk finds after the close has sent nothing, so its sale waits and sells at the open."""
    repo, clock = crashed_with_exposure
    await safe_flatten._held_position(repo)
    alpaca = _Alpaca(repo, clock)
    end_schedule.ends[SID] = _sell_at(CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    sweep = _the_sweep(repo, alpaca)
    _walk_clock_to(repo, CLOSE_MS)

    await _passes_across_the_close(repo, sweep)

    assert alpaca.sent == []
    (sale,) = _scheduled_end_sales(repo)
    assert [waiting.strategy_instance_id for waiting in end_sales_waiting_for_open(repo)] == [SID]

    await _passes_across_the_next_open(repo, sweep)

    (sold,) = alpaca.sent
    assert (sold.leg.side, sold.leg.quantity) == ("sell", _shares(10))
    assert NEXT_OPEN_MS <= sold.at_ms < NEXT_OPEN_MS + SWEEP_INTERVAL_MS
    assert end_sales_waiting_for_open(repo) == []

    alpaca.fills(sold.client_order_id)
    await _passes(repo, sweep, 1)

    assert _exit_state(repo, sale) == "succeeded"
    assert repo.position(SID, "SPY") == _shares(0)
    assert len(alpaca.sent) == 1


async def test_a_scheduled_sell_end_starts_no_sale_of_its_own_while_the_bots_exit_is_held(
    crashed_with_exposure,  # noqa: F811
    end_schedule: _Schedule,
) -> None:
    """The bot's own exit is lost 20 s before the close, and its scheduled SELL end comes 10 s later.

    The end stops the run, and that is all it does: the position is the held
    EXIT's, so no ``scheduled-end-`` sale is accepted for it that night or
    after the next open, and the end stays pending. Released at the close,
    the position would have been taken by the end's own sale, which sends
    with no check of the account at the open.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    end = _sell_at(CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS + 10_000)
    end_schedule.ends[SID] = end
    sweep = _the_sweep(repo, alpaca)

    await _passes_across_the_close(repo, sweep)

    assert repo.active_run(SID) is None and end_schedule.stopped == [(SID, RUN_ID)], "the run could not end"
    assert _scheduled_end_sales(repo) == []
    _is_held(repo, exit_id)

    await _passes_across_the_next_open(repo, sweep)

    assert len(alpaca.sent) == 1, "the scheduled end sold the position a second time"
    assert _scheduled_end_sales(repo) == []
    assert end_schedule.ends == {SID: end} and end_schedule.carried_out == []
    _is_held(repo, exit_id)


# ── the ways out ─────────────────────────────────────────────────────────────


async def test_an_order_that_shows_filled_the_next_morning_is_credited_once_and_closes_the_notice(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order was at Alpaca all along: it fills, and the next morning a lookup shows it.

    The pass folds it as it folds any answer about the Clerk's own order. The
    fill is credited once, the EXIT ends flat, the notice closes, and nothing
    was ever sent a second time.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("arrives_unanswered",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    closing = _closing_order(repo)
    sweep = _the_sweep(repo, alpaca)
    await _passes_across_the_close(repo, sweep)
    await _passes_across_the_next_open(repo, sweep)
    _is_held(repo, exit_id)

    alpaca.fills(closing.client_order_id)
    alpaca.answers_its_lookup(closing.client_order_id)
    await _passes(repo, sweep, 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert repo.position(SID, "SPY") == _shares(0)
    assert _the_open_notice(repo) is None
    assert repo.active_exit_for_strategy(SID) is None

    await _passes(repo, sweep, 3)

    assert repo.position(SID, "SPY") == _shares(0)
    assert repo.effective_fill_totals_for_order(closing.order_ref)[0] == _shares(10)
    assert len(alpaca.sent) == 1


async def _a_bot_stopped_the_next_morning_with_its_exit_still_held(
    repo: ClerkSqliteRepository, alpaca: _AlpacaAccount
) -> tuple[str, SqliteAlpacaClerkFacade, Callable[[], Awaitable[object]]]:
    """The held EXIT, the next morning; the settle action is offered once the bot's run is stopped.

    Returns the EXIT's effect, the facade and the context the bot's page is
    presented from.
    """
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    sweep = _the_sweep(repo, alpaca)
    await _passes_across_the_close(repo, sweep)
    await _passes_across_the_next_open(repo, sweep)
    _is_held(repo, exit_id)
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)

    running = await _presented(current_context)
    assert (running.available, running.unavailable_reason_code) == (False, "RUN_STILL_ACTIVE")
    await facade.stop_strategy_run(strategy_instance_id=SID, run_id=RUN_ID, reason="operator_stop")
    stopped = await _presented(current_context)
    assert stopped.available is True and stopped.settlement is not None
    assert stopped.settlement.order_ref == _closing_order(repo).order_ref
    alpaca.reads.clear()
    return exit_id, facade, current_context


async def test_no_such_order_settles_the_held_exit_the_next_morning(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The operator looks at Alpaca the next morning, finds no such order, and says so.

    The Clerk reads Alpaca itself, finds nothing, and ends the EXIT as not
    sent: the unknown outcome closes and the held EXIT no longer stands over
    the entry. The bot still holds its 10 SPY, and they are the operator's
    now: the position is told as still open and its exit as stuck, so the
    Clerk sends nothing for them by itself, through fifteen minutes of
    passes in the session that has opened. Settling sent nothing either.

    The operator then sells from the bot's page. The Flatten that was refused
    behind the unconfirmed order is offered again, and sells the 10 once,
    under a client order id of its own: the bot is flat and both episodes
    close.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id, facade, current_context = await _a_bot_stopped_the_next_morning_with_its_exit_still_held(repo, alpaca)
    closing = _closing_order(repo)
    (entry,) = repo.entry_orders_for_strategy(SID)
    sweep = _the_sweep(repo, alpaca)

    result = await _settle(facade, current_context, ABSENT, reason="No sell of SPY at Alpaca since yesterday.")

    assert result.applied is True
    assert len(alpaca.sent) == 1, "settling never sends an order"
    _the_position_is_left_to_the_operator(repo, exit_id)
    assert repo.position(SID, "SPY") == _shares(10)
    assert repo.active_exit_for_order(entry.order_ref) is None
    assert _the_open_notice(repo) is None

    await _passes(repo, sweep, SIXTY_PASSES)

    assert NEXT_OPEN_MS < clock() < session_close_ms_utc(next_trading_day(SESSION_DAY))
    assert len(alpaca.sent) == 1, "the Clerk sold a position it had left to the operator"
    _the_position_is_left_to_the_operator(repo, exit_id)

    assert (await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")).verdict == "clean"
    await _flatten(facade, current_context)

    _lost, sold = alpaca.sent
    assert (sold.leg.side, sold.leg.quantity) == ("sell", _shares(10))
    assert sold.client_order_id != closing.client_order_id
    alpaca.fills(sold.client_order_id)
    await _passes(repo, sweep, 2)

    assert repo.position(SID, "SPY") == _shares(0)
    assert _open_episode(repo, EXIT_NOT_FLAT_REASON_CODE) is None
    assert _open_episode(repo, EXIT_STUCK_REASON_CODE) is None
    assert len(alpaca.sent) == 2, "the 10 SPY were sold once, by the operator"


async def test_a_sell_end_left_pending_behind_the_held_exit_sells_nothing_once_it_is_settled_as_not_sent(
    crashed_with_exposure,  # noqa: F811
    end_schedule: _Schedule,
) -> None:
    """The bot's scheduled SELL end stayed pending overnight behind its held EXIT, and the order is settled as not sent.

    The end stopped the run and started no sale of its own while the EXIT
    held the position. "No such order" ends that EXIT and leaves the position
    to the operator, so the end's sale must not go out in the session that
    has opened: no read shows whether the lost order sold the shares.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    end_schedule.ends[SID] = _sell_at(CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS + 10_000)
    sweep = _the_sweep(repo, alpaca)
    await _passes_across_the_close(repo, sweep)
    await _passes_across_the_next_open(repo, sweep)
    assert repo.active_run(SID) is None and _scheduled_end_sales(repo) == []

    await _settle(_facade(repo, alpaca), _context_of(repo, SID), ABSENT)
    _the_position_is_left_to_the_operator(repo, exit_id)
    await _passes(repo, sweep, SIXTY_PASSES)

    assert len(alpaca.sent) == 1, "the end's sale went out for a position left to the operator"


async def test_alpaca_shows_it_settles_the_held_exit_the_next_morning(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The lost order filled at Alpaca, and no lookup of the Clerk's own shows it: the operator names it.

    The Clerk reads that order by its Alpaca order id, takes it up as its
    own, and credits the fill once: the bot is flat, the EXIT succeeds, and
    nothing is left open about it -- no unknown outcome, no open position, no
    stuck exit. Nothing is sent.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("arrives_unanswered",))
    exit_id, facade, current_context = await _a_bot_stopped_the_next_morning_with_its_exit_still_held(repo, alpaca)
    closing = _closing_order(repo)
    alpaca.fills(closing.client_order_id)
    it_is_this_order = OrderOutcomeAttestation(
        kind="exists", broker_order_id=alpaca.order_id_of(closing.client_order_id)
    )

    result = await _settle(facade, current_context, it_is_this_order)

    def the_bot_is_flat_with_nothing_left_open() -> None:
        assert _exit_state(repo, exit_id) == "succeeded"
        assert repo.position(SID, "SPY") == _shares(0)
        assert repo.active_exit_for_strategy(SID) is None
        assert [
            reason_code
            for reason_code in (ORDER_OUTCOME_UNKNOWN_REASON_CODE, EXIT_NOT_FLAT_REASON_CODE, EXIT_STUCK_REASON_CODE)
            if _open_episode(repo, reason_code) is not None
        ] == []

    assert result.applied is True
    assert len(alpaca.sent) == 1
    the_bot_is_flat_with_nothing_left_open()

    await _passes(repo, _the_sweep(repo, alpaca), 3)

    the_bot_is_flat_with_nothing_left_open()
    assert repo.effective_fill_totals_for_order(closing.order_ref)[0] == _shares(10)
    assert len(alpaca.sent) == 1


# ── what the held state leaves possible ──────────────────────────────────────


async def test_the_run_ends_under_stop_and_flatten_is_refused_with_the_way_to_settle(
    crashed_with_exposure,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Overnight the operator can still Stop the bot; a Flatten is refused, and says what to do instead.

    Stop is offered and commits. The stop cannot prove the bot's custody --
    its closing order is unresolved -- and records that. A Flatten would
    close the position a second time, so it stays refused, and its refusal
    names the settle action in place of a reconciliation that cannot cure it.
    """
    repo, clock = crashed_with_exposure
    alpaca = _AlpacaAccount(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    closing = _closing_order(repo)
    await _passes_across_the_close(repo, _the_sweep(repo, alpaca))
    facade, current_context = _facade(repo, alpaca), _context_of(repo, SID)

    catalog = {action.action_id: action for action in build_recovery_catalog(await current_context())}
    assert catalog["stop_bot_decisions"].available is True
    await facade.stop_strategy_run(strategy_instance_id=SID, run_id=RUN_ID, reason="operator_stop")
    assert repo.active_run(SID) is None
    proof = await facade.prove_instance_custody(SID)
    assert proof.unprovable and proof.unresolved_intent_refs == (closing.order_ref,)
    binding = BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, strategy_key="deployment_validation",
        broker="alpaca", symbol="SPY", use_rth=True, mode="trade", quantity=10, carryover_policy="FORBID",
        sealed_account_id=ACCOUNT_ID, action_plan=alpaca_v1_action_plan("SPY"), run_id=RUN_ID, created_at_ms=1,
    )
    assert record_stop_outcome(
        binding, proof, checkpoint_path=tmp_path / "stop.json", now_ms=clock
    ) == "STOPPED_CUSTODY_UNPROVABLE"

    catalog = {action.action_id: action for action in build_recovery_catalog(await current_context())}
    flatten = catalog["prepare_safe_flatten"]
    assert (flatten.available, flatten.unavailable_reason_code) == (False, "EXPOSURE_NOT_PROVEN")
    assert flatten.unavailable_reason == (
        "The Clerk sent a closing order for this bot and could not confirm it, so a flatten could "
        "close the position a second time."
    )
    assert flatten.next_step == (
        "Settle that order first: look at this account's orders at Alpaca, then use "
        '"Settle the uncertain order".'
    )
    assert catalog["execute_safe_flatten"].available is False
    assert catalog["settle_unknown_exit_order"].available is True
    assert len(alpaca.sent) == 1
    _is_held(repo, exit_id)


# ── another bot on the same symbol ───────────────────────────────────────────

THIRD_SID = "third-bot"
THIRD_RUN_ID = "third-run-1"
# What Alpaca answers the other bot's own closing order with: any outright refusal fails that exit.
REFUSED = _Refused(403, "insufficient qty available for order (requested: 10, available: 0)")
SETTLE_ACTION = f'"{_CUSTODY_ENTRIES["settle_unknown_exit_order"].label}"'


async def _another_bot_holds_ten_spy(repo: ClerkSqliteRepository, sid: str, run_id: str) -> str:
    """A second running bot on SPY in the same account, holding 10 of its own. Returns its entry."""
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=sid, symbol="SPY", config_hash=f"h-{sid}"
    )
    submit_budgeted_test_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, lifecycle_run_id=run_id)
    return await _make_held_position(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, run_id=run_id,
        decision_id=f"{sid}-enter-1", execution_id=f"{sid}-exec-1",
    )


async def _the_bot_decides_to_exit(
    repo: ClerkSqliteRepository, alpaca: _Alpaca, *, sid: str, run_id: str, entry_ref: str
) -> str:
    """Another bot's own exit decision, driven once by its runner. Returns the EXIT's effect."""
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=sid, decision_id=f"{sid}-exit-1",
        lifecycle_run_id=run_id, entry_order_ref=entry_ref,
    )
    assert accepted.effect_operation_id is not None
    await resolve_exit(
        repo, effect_operation_id=accepted.effect_operation_id, trade=alpaca, pricing=UNPRICEABLE_RECOVERY,
        read=None,
    )
    return accepted.effect_operation_id


def _the_page_of(repo: ClerkSqliteRepository, sid: str) -> ClerkProjection:
    reader = SqliteClerkProjectionReader.from_repository(repo, clock=repo.clock)
    try:
        page = reader.bot_snapshot(sid)
    finally:
        reader.close()
    assert page is not None
    return page


def _notice(page: ClerkProjection, reason_code: str) -> ProjectedUncertainty:
    (notice,) = [item for item in page.uncertainties if item.reason_code == reason_code]
    return notice


@pytest.mark.parametrize(
    ("also_held", "why", "next_step"),
    [
        pytest.param(
            False,
            f"Another bot, {SID}, has a closing order for SPY that the Clerk sent and could not confirm. "
            "While that order's outcome is unknown, the Clerk does not send this bot's exit again by "
            f"itself. See {SID}'s page.",
            "Run Reconcile now and execute the presented safe flatten for this bot. The Clerk does not "
            f"send this bot's exit by itself again, even after {SID}'s closing order is settled; see "
            f"{SID}'s page.",
            id="one-bots-order-is-held",
        ),
        pytest.param(
            True,
            f"Other bots ({SID}, {THIRD_SID}) have closing orders for SPY that the Clerk sent and could "
            "not confirm. While their outcomes are unknown, the Clerk does not send this bot's exit "
            "again by itself. See those bots' pages.",
            "Run Reconcile now and execute the presented safe flatten for this bot. The Clerk does not "
            f"send this bot's exit by itself again, even after the closing orders of {SID}, {THIRD_SID} "
            "are settled; see their pages.",
            id="two-bots-orders-are-held",
        ),
    ],
)
async def test_another_bots_exit_held_up_by_the_unconfirmed_order_names_its_bot_and_is_flattened_by_itself(
    crashed_with_exposure,  # noqa: F811
    also_held: bool,
    why: str,
    next_step: str,
) -> None:
    """One bot's exit is lost and held overnight; the next morning Alpaca refuses another bot's exit of the same symbol.

    The held order is work in flight on SPY, so the watchdog never sends the
    other bot's exit again while its outcome is unknown, and after eight
    regular-session minutes of that the exit is told as stuck. It was told
    only that "the Clerk still has work in flight", which names no bot. The
    other bot's page now names the bot whose order is held and points at
    that bot's page, while it waits and once it is stuck. Its next step is
    its own flatten and never the settling of that order: the flatten needs
    nothing of it, the order may be one no action settles, and the step stays
    on the page after the order is settled. Nothing is sent that was not sent
    before.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives", *(["never_arrives"] if also_held else []), REFUSED))
    other_entry = await _another_bot_holds_ten_spy(repo, SIBLING_SID, SIBLING_RUN_ID)
    alpaca.holds = 20.0
    held = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    if also_held:
        third_entry = await _another_bot_holds_ten_spy(repo, THIRD_SID, THIRD_RUN_ID)
        alpaca.holds = 30.0
        third_exit = await _the_bot_decides_to_exit(
            repo, alpaca, sid=THIRD_SID, run_id=THIRD_RUN_ID, entry_ref=third_entry
        )
        assert _exit_state(repo, third_exit) == "unknown"
    lost = len(alpaca.sent)
    sweep = _the_sweep(repo, alpaca)
    await _passes_across_the_close(repo, sweep)
    _walk_clock_to(repo, NEXT_OPEN_MS + 60_000)

    refused_exit = await _the_bot_decides_to_exit(
        repo, alpaca, sid=SIBLING_SID, run_id=SIBLING_RUN_ID, entry_ref=other_entry
    )
    refused_at_ms = clock()
    assert _exit_state(repo, refused_exit) == "failed" and _exit_not_flat(repo, SIBLING_SID) is not None
    policy = reason_age_policy(EXIT_NOT_FLAT_REASON_CODE, RedriveThenEscalate)

    # Past the watchdog's settle age the re-drive is due, and is held up by the unconfirmed order.
    await _passes(repo, sweep, policy.after_ms // SWEEP_INTERVAL_MS + 1)
    waiting = _notice(_the_page_of(repo, SIBLING_SID), EXIT_NOT_FLAT_REASON_CODE).recovery_status
    assert waiting is not None
    assert (waiting.kind, waiting.reason_code, waiting.explanation) == ("on_hold", "EXIT_OTHER_ORDER_WORKING", why)

    def stuck() -> dict | None:
        return repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=EXIT_STUCK_REASON_CODE, strategy_instance_id=SIBLING_SID
        )

    failure_budget_ms = policy.after_ms * (policy.max_count + 1)
    while stuck() is None:
        assert clock() - refused_at_ms <= policy.after_ms + failure_budget_ms, "never told as stuck"
        await _passes(repo, sweep, 1)

    assert len(alpaca.sent) == lost + 1, "the other bot's exit was sent again beside an unconfirmed order"
    page = _the_page_of(repo, SIBLING_SID)
    told = _notice(page, EXIT_STUCK_REASON_CODE)
    assert told.explanation == (
        f"10 SPY remains attributed after {failure_budget_ms} ms of observed regular-session failures: {why}"
    )
    assert told.next_step == page.guidance.next_step == next_step
    assert _notice(page, EXIT_NOT_FLAT_REASON_CODE).next_step == (
        f"Automatic attempts to reduce this position have stopped. {next_step}"
    )
    # The flatten comes first and nothing sends the operator to settle the other bot's order before it.
    assert next_step.startswith("Run Reconcile now") and SETTLE_ACTION not in told.explanation + next_step
    _is_held(repo, held)


# ── what is unchanged ────────────────────────────────────────────────────────


async def test_evidence_that_the_bot_is_flat_ends_a_held_exit_first(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The attributed-flat proof still comes first: a bot that holds nothing has nothing to keep custody of.

    The ledger comes to say the bot holds no SPY while its closing order is
    held. The next pass ends the EXIT as flat, citing that order, and closes
    the notice; nothing is sent.
    """
    repo, clock = crashed_with_exposure
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    sweep = _the_sweep(repo, alpaca)
    await _passes_across_the_close(repo, sweep)
    _is_held(repo, exit_id)

    alpaca.holds = 0.0
    _the_ledger_records_the_bots_shares_written_off(repo)
    await _passes(repo, sweep, 1)

    assert _exit_state(repo, exit_id) == "succeeded"
    assert _the_open_notice(repo) is None
    assert _exit_not_flat(repo) is None
    assert len(alpaca.sent) == 1


async def test_an_order_lost_earlier_in_the_session_is_still_sent_again_inside_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Unchanged (#2845): with the session still open, the lost order goes out again once its wait has passed.

    Lost a minute before the close, it is sent again 30 s later, still inside
    the session, under the same client order id, and accepted. Nothing is
    held and nothing is told as held.
    """
    repo, clock = crashed_with_exposure
    _walk_clock_to(repo, CLOSE_MS - 60_000)
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _the_runner_sends_the_exit(repo, alpaca)
    sweep = _the_sweep(repo, alpaca)

    await _passes_across_the_close(repo, sweep)

    assert _sent_after_the_first_ms(alpaca) == [0, RESEND_AFTER_MS]
    assert alpaca.sent[-1].at_ms < CLOSE_MS
    assert len({sent.client_order_id for sent in alpaca.sent}) == 1
    assert _exit_state(repo, exit_id) == "in_progress"
    assert _notices_recorded(repo, _closing_order(repo)) == []


class _Simulation:
    """A Dry Run's or a Shadow's port: an order fills whole inside ``submit``, with its own execution identity.

    It declares its answers authoritative, which is what makes it a simulated
    port to the Clerk. ``loses`` names the sides whose submit times out with
    nothing filled; ``sent`` is every submit, by side.
    """

    submission_response_is_authoritative_evidence = True

    def __init__(self, clock: _TestClock) -> None:
        self._clock = clock
        self._filled: dict[str, BrokerOrder] = {}
        self.loses: set[str] = set()
        self.sent: list[str] = []
        self.bound: list[str] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.sent.append(leg.side)
        if leg.side in self.loses:
            raise BrokerUnavailable("the submit timed out")
        filled = safe_flatten._broker_order(
            client_order_id, order_id=f"sim-{client_order_id}", status="filled", side=leg.side,
            quantity=leg.quantity, filled_quantity=leg.quantity, filled_avg_price=100.0,
        ).model_copy(update={"events": [BrokerOrderEvent(
            event_type="fill", occurred_at_ms=self._clock(), price=100.0, quantity=leg.quantity,
            execution_id=f"sim-execution:{client_order_id}",
        )]})
        self._filled[client_order_id] = filled
        return filled

    async def cancel(self, order_id: str) -> None:
        raise AssertionError(f"nothing of a simulation's is working to cancel: {order_id!r}")

    def bind_decision_bar(
        self, client_order_id: str, *, bar_identity: str, decided_at_ms: int, symbol: str
    ) -> bool:
        """This double holds no bar ledger to look the record up in (#2919).

        What the acceptance recorded is bound as it is, exactly as a real
        simulation binds the bar it looked up.
        """
        del client_order_id, decided_at_ms, symbol
        self.bound.append(bar_identity)
        return True

    async def get_order_by_client_order_id(self, client_order_id: str) -> BrokerOrder | None:
        return self._filled.get(client_order_id)


async def test_an_order_sent_to_a_simulated_port_still_fails_and_releases_past_its_session(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Unchanged: a simulation is exempt, or a Dry Run could end the day holding.

    Nothing of a simulated order can be at a broker. Past its session its
    EXIT fails and releases the position as before, which is what leaves a
    Dry Run's run-end close free to sell what its ended run still holds.
    """
    repo, clock = crashed_with_exposure
    simulation = _Simulation(clock)
    _walk_clock_to(repo, CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    entered = await submit_budgeted_test_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-1",
        lifecycle_run_id=RUN_ID, leg=safe_flatten._leg(quantity=10, side="buy"), trade=simulation,
    )
    assert entered.order_ref is not None and repo.position(SID, "SPY") == _shares(10)
    priced_at_ms = clock()
    recorded_bar = f"fixture:SPY:{priced_at_ms - 60_000}:{priced_at_ms}"
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="program-exit-1",
        lifecycle_run_id=RUN_ID, entry_order_ref=entered.order_ref,
        # What a simulated authority's runtime records with every decision
        # EXIT (#2919): the bar its fill is priced at, and the decision's
        # close. Without it the send is refused -- never re-priced.
        decision_pricing=DecisionPricingFacts(bar_identity=recorded_bar, decided_at_ms=priced_at_ms),
    )
    exit_id = accepted.effect_operation_id
    assert exit_id is not None
    simulation.loses.add("sell")

    async def the_exit_is_driven() -> object:
        return await resolve_exit(
            repo, effect_operation_id=exit_id, trade=simulation, pricing=UNPRICEABLE_RECOVERY, read=None
        )

    await the_exit_is_driven()
    assert simulation.sent == ["buy", "sell"] and _exit_state(repo, exit_id) == "unknown"
    # The sale was bound to the bar the acceptance recorded, before it was sent (#2919).
    assert simulation.bound == [recorded_bar]
    closing = _closing_order(repo)

    await _passes(repo, the_exit_is_driven, RESEND_AFTER_MS // SWEEP_INTERVAL_MS)

    assert clock() > CLOSE_MS
    assert simulation.sent == ["buy", "sell"]
    assert _exit_state(repo, exit_id) == "failed"
    assert repo.active_exit_for_strategy(SID) is None
    assert _exit_not_flat(repo) is not None
    assert _notices_recorded(repo, closing) == []


async def test_a_simulated_decision_exit_that_recorded_no_price_is_refused_never_re_priced(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """#2919's fail-closed half: an acceptance with no recorded pricing evidence sends nothing.

    An EXIT accepted before the evidence existed, re-driven after a process
    death, is refused outright -- never silently filled at whatever bar is
    newest then. The EXIT fails releasably with the position still held, which
    is what leaves a Dry Run's run-end close and the operator's flatten as the
    ways back to flat.
    """
    repo, clock = crashed_with_exposure
    simulation = _Simulation(clock)
    _walk_clock_to(repo, CLOSE_MS - LOST_BEFORE_THE_CLOSE_MS)
    entered = await submit_budgeted_test_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="enter-1",
        lifecycle_run_id=RUN_ID, leg=safe_flatten._leg(quantity=10, side="buy"), trade=simulation,
    )
    assert entered.order_ref is not None
    accepted = accept_exit(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, decision_id="program-exit-1",
        lifecycle_run_id=RUN_ID, entry_order_ref=entered.order_ref,
    )
    exit_id = accepted.effect_operation_id
    assert exit_id is not None

    await resolve_exit(
        repo, effect_operation_id=exit_id, trade=simulation, pricing=UNPRICEABLE_RECOVERY, read=None
    )

    assert simulation.sent == ["buy"] and simulation.bound == []
    assert _exit_state(repo, exit_id) == "failed"
    assert repo.position(SID, "SPY") == _shares(10)


# ── the ledger ───────────────────────────────────────────────────────────────


async def test_a_store_rebuilt_from_its_mirror_holds_the_same_order_and_tells_the_same_notice(
    tmp_path: Path,
) -> None:
    """The held state is ledger rows and their folds: a store replayed from its mirror equals the one that lived it."""
    clock = _clock_at(BUDGETED_RTH_MS)
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock, lease_ttl_ms=300_000
    )
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, symbol="SPY", config_hash="h1"
    )
    submit_budgeted_test_run(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    alpaca = _Alpaca(repo, clock, submits=("never_arrives",))
    exit_id = await _a_running_bot_loses_its_exit_just_before_the_close(repo, alpaca)
    await _passes_across_the_close(repo, _the_sweep(repo, alpaca))
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID,
        operator_reason="operator_stop",
    )
    _is_held(repo, exit_id)
    told, transitions = _the_open_notice(repo), repo.custody_transitions()
    offered = repo.unknown_exit_orders(SID)
    assert len(offered) == 1
    db_path = repo.db_path
    repo.close()
    db_path.rename(db_path.with_suffix(".db.corrupt"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(
        account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock
    )
    try:
        assert rebuilt.custody_transitions() == transitions
        assert _the_open_notice(rebuilt) == told
        assert _exit_state(rebuilt, exit_id) == "unknown"
        assert rebuilt.position(SID, "SPY") == _shares(10)
        assert rebuilt.unknown_exit_orders(SID) == offered
    finally:
        rebuilt.close()
