"""An operator's Flatten rests on the Clerk's own check of the account (#2839).

The Clerk compares the whole account with Alpaca every 15 s. Until #2839 a
stopped bot's Flatten was still refused unless someone had pressed Reconcile
now in the last 30 s. The gate now accepts the Clerk's latest pass for a
stopped bot it vouches for, because the Flatten reads the account again as it
is sent (``test_flatten_send_cover``).

These tests drive the real facade and read each context the way every
presentation and every commit reads it (``read_recovery_context``): the pass
first, then the snapshot.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.recovery_reduction import ConfirmedRecoveryLimit
from app.broker.alpaca.clerk.sqlite.commands import submit_stop_run
from app.broker.alpaca.clerk.sqlite.flatten_cover import FLATTEN_NOT_COVERED_AT_BROKER
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.broker.alpaca.clerk.sqlite.projections import read_recovery_context
from app.broker.alpaca.clerk.sqlite.reconcile import reconcile_account
from app.broker.alpaca.clerk.sqlite.recovery_execution import (
    RecoveryExecutionError,
    RecoveryExecutionRequest,
    execute_recovery_action,
)
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    FRESH_EVIDENCE_MAX_AGE_MS,
    SWEEP_LIVENESS_BOUND_MS,
    RecoveryPolicyContext,
    StaleRecoveryTokenError,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.clerk.sqlite.safe_flatten_execution import SafeFlattenExecutionError
from app.broker.alpaca.clerk.trade_evidence import SqliteTradeUpdateEvidenceSink
from app.broker.contract.models import BrokerOrderEvent, OrderType, TimeInForce
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite import test_safe_flatten_execution as safe_flatten
from tests.broker.alpaca.clerk.sqlite.budgeted_run_fixture import submit_budgeted_test_run
from tests.broker.alpaca.clerk.sqlite.conftest import BUDGETED_RTH_MS, _clock_at, _make_held_position, _walk_clock_to
from tests.broker.alpaca.clerk.sqlite.historical_run_fixture import seed_historical_run_started
from tests.broker.alpaca.clerk.sqlite.test_flatten_send_cover import (
    SIBLING_RUN_ID,
    SIBLING_SID,
    _Account,
    _NoSubmitTrade,
    _TradeIntoAccount,
)
from tests.broker.alpaca.clerk.sqlite.test_safe_flatten_execution import (
    ACCOUNT_ID,
    RUN_ID,
    SID,
    crashed_with_exposure,  # noqa: F401 -- the stopped-bot repository these tests reuse
)

_NEEDS_A_CHECK = (
    "CLEAN_RECONCILIATION_REQUIRED",
    "A successful account reconciliation is required before preparing reduction.",
    "Run Reconcile now and refresh the custody snapshot.",
)
"""What the gate has always said with no check of the account to rest on: code, reason, next step."""

_ORDER_ARRIVED_AFTER_THE_RECEIPT = (
    "UNCERTAINTY_EVIDENCE_NOT_RECONCILED",
    "Broker evidence the Clerk could not record arrived after the account reconciliation.",
    "Run Reconcile now and refresh the custody snapshot.",
)
"""What a Reconcile now receipt has said since #2363 of an order that arrived after it."""

type _Context = Callable[[], Awaitable[RecoveryPolicyContext]]


def _context_of(facade: SqliteAlpacaClerkFacade, sid: str | None = SID) -> _Context:
    """The context exactly as the panel and the Clerk's routes read it, for a presentation and at a commit."""

    async def current_context() -> RecoveryPolicyContext:
        context = await read_recovery_context(facade, strategy_instance_id=sid)
        assert context is not None
        return context

    return current_context


async def _offered(
    facade: SqliteAlpacaClerkFacade, action_id: str = "execute_safe_flatten", *, sid: str | None = SID
) -> RecoveryCapability:
    context = await _context_of(facade, sid)()
    return next(item for item in build_recovery_catalog(context) if item.action_id == action_id)


def _refusal(capability: RecoveryCapability) -> tuple[str | None, str | None, str]:
    assert capability.available is False
    assert capability.reduction_plan is None
    return capability.unavailable_reason_code, capability.unavailable_reason, capability.next_step


async def _clerk_pass(facade: SqliteAlpacaClerkFacade) -> None:
    """One pass of the Clerk's own 15 s check: it compares the account, publishes its verdict, writes no receipt."""
    result = await facade.reconcile_account(trigger="AUTOMATIC")
    assert result.verdict == "clean"
    assert result.receipt_id is None


async def _reconcile_now(facade: SqliteAlpacaClerkFacade) -> None:
    result = await facade.reconcile_account(trigger="OPERATOR_RECONCILE_NOW")
    assert result.verdict == "clean"
    assert result.receipt_id is not None


async def _the_clerks_passes_catch_up(facade: SqliteAlpacaClerkFacade) -> None:
    """Two of the Clerk's passes, 15 s apart, after a Flatten left an exit open.

    The first records its own evaluation of that open exit once its verdict
    is reached: a record of the bot's that it did not itself see. The second
    is the first pass to have seen every record the bot has.
    """
    await _clerk_pass(facade)
    _walk_clock_to(facade.repository, facade.repository.clock() + 15_000)
    await _clerk_pass(facade)


async def _stopped_bot_the_clerk_has_checked(
    repo: ClerkSqliteRepository, *, account: _Account, trade: safe_flatten._FakeTrade, pre_market: bool = False
) -> SqliteAlpacaClerkFacade:
    """A stopped bot holding 10 SPY that only the Clerk's own pass has checked: nobody pressed Reconcile now.

    ``pre_market`` moves the clock to 07:00 ET under an extended-hours window
    with a live quote, where a Flatten goes out only as the operator's
    confirmed limit (#2007).

    The account's reads so far are forgotten: what is left in ``account.reads``
    afterwards is what a Flatten itself read.
    """
    await safe_flatten._held_position(repo)
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID,
        lifecycle_run_id=RUN_ID, operator_reason="operator_stop",
    )
    if pre_market:
        _walk_clock_to(repo, safe_flatten._PRE_MARKET_MS)
        facade = SqliteAlpacaClerkFacade(
            account_mode="paper", repo=repo, read=account, trade=trade,
            program_leg_policy=safe_flatten._XH_POLICY,
            quote_source=lambda _symbol, now_ms: safe_flatten._live_quote(now_ms),
        )
    else:
        facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=trade)
    await _clerk_pass(facade)
    account.reads.clear()
    return facade


async def _flatten(facade: SqliteAlpacaClerkFacade, sid: str = SID):
    """The operator's Flatten, dispatched exactly as the panel and the recovery route dispatch it."""
    return await safe_flatten._execute(facade, _context_of(facade, sid), confirmed_limit=None)


def _flatten_exits(repo: ClerkSqliteRepository) -> set[str]:
    """The effect of every EXIT the bot's Flattens were accepted as."""
    return {order.effect_operation_id for order in repo.orders_for_strategy(SID) if order.role == "REDUCING"}


async def test_a_flatten_under_the_clerks_own_pass_reads_the_account_and_sells_what_it_covers(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """No Reconcile now: the Clerk's pass offers the Flatten, and the send-time read of the account still runs."""
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    checked_at_ms = clock.value
    _walk_clock_to(repo, checked_at_ms + 12_000)

    offered = await _offered(facade)
    assert offered.available, offered.unavailable_reason
    plan = offered.reduction_plan
    assert plan is not None
    # The plan names the Clerk's check and dates it by the pass, not by this read twelve seconds later.
    assert (plan.account_check_kind, plan.reconciliation_id) == ("clerk_pass", None)
    assert plan.account_check_observed_at_ms == checked_at_ms
    assert plan.expires_at_ms == checked_at_ms + SWEEP_LIVENESS_BOUND_MS

    result = await _flatten(facade)

    assert result.applied is True
    assert account.reads == ["orders", "positions"]
    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]


async def test_a_flatten_under_the_clerks_pass_is_refused_when_alpaca_no_longer_covers_it(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The pass saw 10 SPY; four are sold at Alpaca before the Flatten goes out. Nothing is sent."""
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    account.holds = 6.0

    with pytest.raises(RecoveryExecutionError) as refused:
        await _flatten(facade)

    assert account.reads == ["orders", "positions"]
    assert trade.submit_calls == []
    assert refused.value.refusal is not None
    assert refused.value.refusal.reason_code == FLATTEN_NOT_COVERED_AT_BROKER
    assert "Alpaca holds 6 SPY, less than the 10 this flatten would sell; nothing was sent." in str(refused.value)
    assert repo.position(SID, "SPY") == 10


async def test_a_confirmed_limit_flatten_under_the_clerks_pass_reads_the_account_and_is_sent_when_covered(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Before the open a Flatten goes out as the operator's confirmed limit (#2007).

    Offered on the Clerk's pass alone, it is checked against the account as it
    is sent, like a market Flatten, and goes out at the price the operator
    confirmed.
    """
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade, pre_market=True)
    plan = (await _offered(facade)).reduction_plan
    assert plan is not None and plan.account_check_kind == "clerk_pass"

    result = await safe_flatten._execute(
        facade, _context_of(facade),
        confirmed_limit=ConfirmedRecoveryLimit(
            limit_price=Decimal("99.95"), quote_observed_at_ms=safe_flatten._PRE_MARKET_MS
        ),
    )

    assert result.applied is True
    assert account.reads == ["orders", "positions"]
    ((leg,),) = (trade.submitted_legs,)
    assert (leg.side, leg.quantity, leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
        "sell", 10, OrderType.LIMIT, TimeInForce.DAY, 99.95, True,
    )


@pytest.mark.parametrize(
    "check_the_account_again",
    [
        pytest.param(_the_clerks_passes_catch_up, id="the-clerks-own-passes"),
        pytest.param(_reconcile_now, id="reconcile-now"),
    ],
)
async def test_a_flatten_refused_under_the_pass_is_a_new_exit_that_reads_the_account_when_retried(
    crashed_with_exposure,  # noqa: F811
    check_the_account_again: Callable[[SqliteAlpacaClerkFacade], Awaitable[None]],
) -> None:
    """A refused Flatten must not be replayed as its stored refusal once the account covers the sale.

    The refusal is a record of the bot's the pass never saw, so the plan is
    withdrawn at once. A later check of the account -- the Reconcile now the
    refusal names, or the Clerk's own passes once one has seen every record
    the refusal left -- offers a new plan: a new EXIT, which reads the
    account again and sends.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    account.holds = 6.0
    with pytest.raises(RecoveryExecutionError) as refused:
        await _flatten(facade)
    assert refused.value.refusal is not None
    assert refused.value.refusal.next_step == "Run Reconcile now, then prepare the flatten again."
    refused_exits = _flatten_exits(repo)
    assert len(refused_exits) == 1

    assert _refusal(await _offered(facade)) == _NEEDS_A_CHECK

    account.holds = 10.0
    _walk_clock_to(repo, clock.value + 15_000)
    await check_the_account_again(facade)
    assert trade.submit_calls == []
    account.reads.clear()
    sent = await _flatten(facade)

    assert sent.applied is True
    assert account.reads == ["orders", "positions"]
    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]
    assert len(_flatten_exits(repo) - refused_exits) == 1


async def test_a_flatten_presented_under_a_pass_is_refused_at_commit_once_that_pass_is_old(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The sweep stops publishing after the plan is presented. The commit reads the pass again and refuses.

    Nothing is read from the account and nothing is sent: the commit never
    trusts what the presentation saw.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    presented = await _offered(facade)
    assert presented.available and presented.reduction_plan is not None

    _walk_clock_to(repo, clock.value + SWEEP_LIVENESS_BOUND_MS + 1)

    with pytest.raises(StaleRecoveryTokenError):
        await execute_recovery_action(
            facade,
            request=RecoveryExecutionRequest(
                action_id="execute_safe_flatten",
                concurrency_token=presented.concurrency_token,
                execution_ref=presented.execution_ref,
                reason="operator-flatten",
            ),
            current_context=_context_of(facade),
        )
    # The executor's own backstop: the presented plan expired with its pass.
    with pytest.raises(SafeFlattenExecutionError, match="expired"):
        await facade.execute_safe_flatten(plan=presented.reduction_plan)

    assert _refusal(await _offered(facade)) == _NEEDS_A_CHECK
    assert account.reads == []
    assert trade.submit_calls == []
    assert _flatten_exits(repo) == set()


async def test_a_custody_record_of_the_bots_written_after_the_pass_withdraws_the_flatten_until_the_next_pass(
    tmp_path: Path,
) -> None:
    """A retained pre-budget Resume and Stop invalidate the pass until the next check."""
    clock = _clock_at(BUDGETED_RTH_MS)
    repo = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=clock)
    repo.register_strategy_instance(exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id=RUN_ID)
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    assert (await _offered(facade)).available

    seed_historical_run_started(repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2")
    submit_stop_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SID, lifecycle_run_id="run-2", operator_reason="operator_stop"
    )

    assert _refusal(await _offered(facade, "prepare_safe_flatten")) == _NEEDS_A_CHECK

    _walk_clock_to(repo, clock.value + 15_000)
    await _clerk_pass(facade)

    assert (await _offered(facade)).available
    repo.close()


async def test_a_running_bots_flatten_still_waits_for_reconcile_now(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A running bot can move its own custody at any moment: the Clerk's pass opens nothing for it."""
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    await safe_flatten._held_position(repo)  # the fixture's run is still ACTIVE
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=trade)
    await _clerk_pass(facade)
    published = await facade.published_pass(SID)
    assert published is not None and published.vouches

    assert _refusal(await _offered(facade, "prepare_safe_flatten")) == _NEEDS_A_CHECK


async def test_the_accounts_own_flatten_plan_is_never_offered_on_the_clerks_pass(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Account-scope actions keep asking for a Reconcile now receipt, and get one as before."""
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    assert (await _offered(facade, "prepare_safe_flatten")).available

    assert _refusal(await _offered(facade, "prepare_safe_flatten", sid=None)) == _NEEDS_A_CHECK

    await _reconcile_now(facade)

    assert (await _offered(facade, "prepare_safe_flatten", sid=None)).available


async def test_a_fresh_receipt_still_wins_and_a_refused_flatten_is_told_again_as_before(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """With a fresh Reconcile now receipt the plan is the receipt's, whatever the Clerk's pass says.

    A Reconcile now publishes a pass too, so both stand. The plan, its expiry
    and its token are the receipt's: a refused Flatten prepared again inside
    the receipt's 30 s is the same EXIT, told its refusal with no new read --
    exactly what it was before the pass was accepted.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    _walk_clock_to(repo, clock.value + 5_000)
    await _reconcile_now(facade)
    receipt_at_ms = clock.value
    account.reads.clear()
    plan = (await _offered(facade)).reduction_plan
    assert plan is not None
    assert (plan.account_check_kind, plan.account_check_observed_at_ms, plan.expires_at_ms) == (
        "reconcile_now",
        receipt_at_ms,
        receipt_at_ms + FRESH_EVIDENCE_MAX_AGE_MS,
    )
    assert plan.reconciliation_id is not None

    account.holds = 6.0
    with pytest.raises(RecoveryExecutionError) as refused:
        await _flatten(facade)
    account.holds = 10.0
    account.reads.clear()
    with pytest.raises(RecoveryExecutionError) as told_again:
        await _flatten(facade)

    assert str(told_again.value) == str(refused.value)
    assert account.reads == []
    assert trade.submit_calls == []
    assert len(_flatten_exits(repo)) == 1


async def test_a_dry_runs_or_a_shadows_flatten_still_waits_for_reconcile_now(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """Their port fills inside ``submit`` and no account is read as the Flatten goes out.

    Only that read lets the gate accept the Clerk's own pass, so here the
    pass -- which does prove the bot's custody -- opens nothing, and a
    Reconcile now offers the Flatten exactly as it did.
    """
    repo, _clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), _NoSubmitTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    proof = await facade.published_custody(SID)
    assert proof is not None and not proof.unprovable

    assert _refusal(await _offered(facade)) == _NEEDS_A_CHECK

    await _reconcile_now(facade)
    account.reads.clear()
    result = await _flatten(facade)

    assert result.applied is True
    assert account.reads == []
    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10)]


async def test_two_cohort_legs_on_one_symbol_are_both_sent_under_one_pass_of_the_clerks(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """A cohort Flatten with no Reconcile now: one bot's sale never ages the pass for its sibling.

    The first leg writes records of its own bot only, so the second bot's
    plan still stands at its commit, and the send-time read finds the
    account covers it behind the first leg's working sell.
    """
    repo, _clock = crashed_with_exposure
    account = _Account(holds=20.0)
    trade = _TradeIntoAccount(account)
    await safe_flatten._held_position(repo)
    repo.register_strategy_instance(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=SIBLING_SID, symbol="SPY", config_hash="h2"
    )
    submit_budgeted_test_run(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SIBLING_SID, lifecycle_run_id=SIBLING_RUN_ID
    )
    await _make_held_position(
        repo, account_id=ACCOUNT_ID, strategy_instance_id=SIBLING_SID, run_id=SIBLING_RUN_ID,
        decision_id="sibling-enter-1", execution_id="sibling-exec-1",
    )
    for sid, run_id in ((SID, RUN_ID), (SIBLING_SID, SIBLING_RUN_ID)):
        submit_stop_run(
            repo, account_id=ACCOUNT_ID, strategy_instance_id=sid,
            lifecycle_run_id=run_id, operator_reason="operator_stop",
        )
    facade = SqliteAlpacaClerkFacade(account_mode="paper", repo=repo, read=account, trade=trade)
    await _clerk_pass(facade)
    presented = {sid: await _offered(facade, sid=sid) for sid in (SID, SIBLING_SID)}
    assert all(leg.available for leg in presented.values())

    for sid, leg in presented.items():
        await execute_recovery_action(
            facade,
            request=RecoveryExecutionRequest(
                action_id="execute_safe_flatten",
                concurrency_token=leg.concurrency_token,
                execution_ref=leg.execution_ref,
                reason="cohort-flatten",
            ),
            current_context=_context_of(facade, sid),
        )

    assert [(leg.side, leg.quantity) for leg in trade.submitted_legs] == [("sell", 10), ("sell", 10)]


# ── A pass is dated at its verdict, never at its publish ─────────────────────

_VERDICT_TO_PUBLISH_MS = 3_000


async def _a_check_published_after_its_verdict(
    facade: SqliteAlpacaClerkFacade,
    account: _Account,
    trade: safe_flatten._FakeTrade,
    *,
    trigger: str,
    order_arrives_between: bool = False,
) -> int:
    """One check of the account, published three seconds after its verdict; returns the verdict's time.

    As production runs it: the sweep reconciles and then hands the result to
    ``publish_sweep_reconciliation``, and a Reconcile now to
    ``publish_reconciliation``. After its final broker comparison a pass
    releases the intake lock three more times before that publish, and the
    trade-update stream records in between. ``order_arrives_between`` is that:
    a foreign order the Clerk cannot record (#2363), reported two seconds
    after the verdict.
    """
    repo = facade.repository
    result = await reconcile_account(
        repo, read=account, trade=trade, trigger=trigger, intake=facade.intake, pricing=facade.recovery_pricing
    )
    assert result.verdict == "clean"
    verdict_at_ms = repo.clock()
    if order_arrives_between:
        _walk_clock_to(repo, verdict_at_ms + 2_000)
        await SqliteTradeUpdateEvidenceSink(
            repo=repo, intake=facade.intake, reconciler=safe_flatten._NoReconciler()
        ).record_lifecycle_event(
            client_order_id="alpaca-console:mleg-1",
            event=BrokerOrderEvent(event_type="new", occurred_at_ms=repo.clock(), price=None, quantity=None),
            event_key="mleg-open-1|new",
            order=safe_flatten._unfoldable_open_order(),
            recovery_source=None,
            recovery_window_limit=None,
        )
    _walk_clock_to(repo, verdict_at_ms + _VERDICT_TO_PUBLISH_MS)
    if trigger == "AUTOMATIC":
        facade.publish_sweep_reconciliation(result)
    else:
        facade.publish_reconciliation(result)
    return verdict_at_ms


async def test_a_plan_under_a_pass_published_late_is_dated_and_expires_by_the_passs_verdict(
    crashed_with_exposure,  # noqa: F811
) -> None:
    """The plan names when the account was compared with Alpaca, and is good for 60 s from then.

    Dated at the publish, the plan claimed a check three seconds newer than
    the one that was made, and outlived the sweep's bound by as long.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    _walk_clock_to(repo, clock.value + 15_000)

    verdict_at_ms = await _a_check_published_after_its_verdict(facade, account, trade, trigger="AUTOMATIC")

    plan = (await _offered(facade)).reduction_plan
    assert plan is not None
    assert (plan.account_check_kind, plan.account_check_observed_at_ms, plan.expires_at_ms) == (
        "clerk_pass",
        verdict_at_ms,
        verdict_at_ms + SWEEP_LIVENESS_BOUND_MS,
    )
    _walk_clock_to(repo, verdict_at_ms + SWEEP_LIVENESS_BOUND_MS)
    assert (await _offered(facade)).available
    _walk_clock_to(repo, verdict_at_ms + SWEEP_LIVENESS_BOUND_MS + 1)
    assert _refusal(await _offered(facade)) == _NEEDS_A_CHECK


@pytest.mark.parametrize(
    ("trigger", "refused_as"),
    [
        # Scenario A: the Clerk's own pass. No receipt stands, so the gate says what it says with no check at all.
        pytest.param("AUTOMATIC", _NEEDS_A_CHECK, id="the-clerks-own-pass"),
        # Scenario B: a Reconcile now. Its receipt is refused for the order; the
        # same reconciliation, published as a pass, must not then be accepted.
        pytest.param("OPERATOR_RECONCILE_NOW", _ORDER_ARRIVED_AFTER_THE_RECEIPT, id="reconcile-now"),
    ],
)
async def test_an_order_recorded_between_a_checks_verdict_and_its_publish_withdraws_the_flatten(
    crashed_with_exposure,  # noqa: F811
    trigger: str,
    refused_as: tuple[str, str, str],
) -> None:
    """The check's broker comparison never saw the order, so the Flatten waits for one that did (#2363).

    Dated at its publish, the pass read as newer than the order and the gate
    accepted a Flatten on broker truth that predates it -- under the Clerk's
    own pass, and under a Reconcile now whose receipt had just been refused
    for that very order.
    """
    repo, clock = crashed_with_exposure
    account, trade = _Account(holds=10.0), safe_flatten._FakeTrade()
    facade = await _stopped_bot_the_clerk_has_checked(repo, account=account, trade=trade)
    assert (await _offered(facade)).available
    _walk_clock_to(repo, clock.value + 15_000)

    await _a_check_published_after_its_verdict(
        facade, account, trade, trigger=trigger, order_arrives_between=True
    )

    for action_id in ("prepare_safe_flatten", "execute_safe_flatten"):
        assert _refusal(await _offered(facade, action_id)) == refused_as, action_id

    # The next pass compares the account after the order arrived: the Flatten rests on it.
    _walk_clock_to(repo, clock.value + 15_000)
    await _clerk_pass(facade)
    assert (await _offered(facade)).available
