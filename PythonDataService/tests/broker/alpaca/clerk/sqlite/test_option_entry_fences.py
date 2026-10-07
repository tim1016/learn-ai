"""The fences an option entry passes at the account Clerk, each on its own (#2913).

``test_option_entry_and_exit`` follows an entry that fills. Here every refusal
that stands between a caller and an option entry is met alone: the run that is
not the bot's, the hold on new exposure, the budget that is missing or not
switched on, the account that is not a Dry Run, the facts that are not what
they must be, and the same decision asking twice for different things.

The prices are that file's: one SPY call, bid $1.90 and offered at $1.94, a
hundred shares a contract. One contract is $194.00 of premium and six cents
of fees to set aside: $194.06.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes
from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_CASH_EXCEEDED, LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence, OptionPlanRefused
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.shadow_broker import compose_shadow_ports
from app.broker.alpaca.clerk.sqlite.budget_authority import BUDGET_COMMITMENT_MISSING, BUDGETS_NOT_SWITCHED_ON
from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms, record_contract_terms
from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims
from app.broker.alpaca.clerk.sqlite.idempotency import DurableConflictError, NoActiveRunError
from app.broker.alpaca.clerk.sqlite.option_admission import OptionEntryRefused, work_out_option_entry
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts, option_entry_of_order
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade, _OptionQuoteBoundTradePort
from app.broker.alpaca.clerk.sqlite.uncertainty import AdmissionBlockedError
from app.broker.contract.errors import BrokerOptionsNotSupported, BrokerUnavailable
from app.broker.contract.models import BrokerOrderLeg
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.resolver import ResolverRefusal
from app.services import market_liveness
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.session_authority import et_minute_of_day_ms
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import (
    ENVELOPE_RUN_ID,
    ENVELOPE_SID,
    NOON,
    _start_legacy_run,
    _TestClock,
)
from tests.broker.alpaca.clerk.sqlite.conftest import envelope_gate as _gate
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.sqlite.test_claimed_broker_io import _Repo
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    BUYS_ONE_CALL,
    CALL,
    EIGHT,
    GENERATION,
    LEG,
    RUN,
    SID,
    SYMBOL,
    TEN,
    _binding,
    _DryRun,
    _open,
    _quote,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.test_shadow_broker import DAY, _Clock, _LiveRead

EXPIRY = date(2026, 9, 18)
EXPIRY_NOON = et_minute_of_day_ms(EXPIRY, 12 * 60)
EXPIRY_CLOSE = et_minute_of_day_ms(EXPIRY, 16 * 60)
OTHER_STRIKE = OptionContract.of(root="SPY", expiry_date=EXPIRY, right="call", strike=590)


def _rows(repo: ClerkSqliteRepository, table: str) -> int:
    with repo._write_lock:
        return repo._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def _refused(receipt: EffectOperationReceipt, code: str) -> None:
    assert receipt.state is EffectOperationState.REJECTED, receipt.explanation
    assert receipt.explanation.startswith(f"{code}: "), receipt.explanation
    assert receipt.child_order_refs == ()


def _worked_out(account: _DryRun, *, units: int = 1, **changes: Any) -> OptionEntryFacts:
    entry = work_out_option_entry(BUYS_ONE_CALL, evidence=account.evidence(**changes), units=units, now_ms=account.clock())
    assert isinstance(entry, OptionEntryFacts), entry
    return entry


def _evidence_at(now_ms: int, **changes: Any) -> OptionEntryEvidence:
    """What a runner hands over at ``now_ms``, with no account behind it: the pick and a quote 300 ms old."""
    return replace(
        OptionEntryEvidence(
            picked={LEG: CALL}, multipliers={LEG: 100}, quotes={LEG: _quote("1.90", "1.94", received_ms=now_ms - 300)},
            generation=GENERATION, rechecked_at_ms=now_ms - 100, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
        ),
        **changes,
    )


def _refusal_at(now_ms: int, **changes: Any) -> OptionEntryRefused:
    refused = work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(now_ms, **changes), units=1, now_ms=now_ms)
    assert isinstance(refused, OptionEntryRefused), "the entry was worked out"
    return refused


# ── the same decision, sent again ────────────────────────────────────────


async def test_a_decision_sent_again_for_more_contracts_is_a_conflict_and_the_books_keep_the_first(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """One contract was bought under this decision. Asking it for three is a second request under its name."""
    first = await dry_run.enter(dry_run.evidence(), decision="enter-once")
    dry_run.wait(1_000)

    with pytest.raises(DurableConflictError):
        await dry_run.enter(dry_run.evidence(), decision="enter-once", units=3)

    assert first.state is EffectOperationState.SUBMITTED
    assert dry_run.orders() == 1 and dry_run.held() == {SYMBOL: 1.0}


async def test_a_decision_sent_again_for_fewer_contracts_is_a_conflict_and_the_books_keep_the_first(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Three contracts were bought under this decision. Asking it for one is no retry of that entry either."""
    first = await dry_run.enter(dry_run.evidence(), decision="enter-once", units=3)
    dry_run.wait(1_000)

    with pytest.raises(DurableConflictError):
        await dry_run.enter(dry_run.evidence(), decision="enter-once", units=1)

    assert first.state is EffectOperationState.SUBMITTED
    assert dry_run.orders() == 1 and dry_run.held() == {SYMBOL: 3.0}


async def test_a_decision_sent_again_for_another_contract_is_a_conflict(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.enter(dry_run.evidence(), decision="enter-once")
    dry_run.wait(1_000)

    with pytest.raises(DurableConflictError):
        await dry_run.enter(dry_run.evidence(picked={LEG: OTHER_STRIKE}), decision="enter-once")

    assert dry_run.orders() == 1 and dry_run.held() == {SYMBOL: 1.0}


async def test_an_honest_retry_with_newer_quotes_is_answered_from_the_entry_already_made(dry_run: _DryRun) -> None:  # noqa: F811
    """The same contract and the same size. The market has moved to $2.00 by $2.04: nothing is bought again."""
    first = await dry_run.enter(dry_run.evidence(), decision="enter-once")
    dry_run.wait(1_000)

    again = await dry_run.enter(dry_run.evidence("2.00", "2.04"), decision="enter-once")

    assert again.state is EffectOperationState.SUBMITTED
    assert again.child_order_refs == first.child_order_refs
    assert dry_run.orders() == 1
    assert dry_run.fills() == [(SYMBOL, "buy", 1.0, 1.94, 100)]


async def test_an_exits_decision_used_again_for_an_entry_is_a_conflict_and_never_answered_as_that_exit(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    closed = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"), decision="one-decision")
    assert closed.state is EffectOperationState.FLAT
    dry_run.wait(20_000)
    await dry_run.read_account()
    orders = dry_run.orders()

    with pytest.raises(DurableConflictError):
        await dry_run.enter(dry_run.evidence("2.40", "2.44"), decision="one-decision")

    assert dry_run.orders() == orders and dry_run.held() == {}


# ── the fences every ENTER passes, met by an option entry ────────────────


async def test_an_entry_under_a_run_that_is_not_the_bots_active_one_is_refused_and_writes_nothing(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    commands = _rows(dry_run.repo, "commands")

    with pytest.raises(NoActiveRunError):
        await dry_run.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id="another-run", decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=BUYS_ONE_CALL, quantity=1, option_entry=dry_run.evidence(),
        )

    assert (_rows(dry_run.repo, "commands"), dry_run.orders()) == (commands, 0)


async def test_an_entry_after_the_bot_was_stopped_is_refused(dry_run: _DryRun) -> None:  # noqa: F811
    await dry_run.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN)

    with pytest.raises(NoActiveRunError):
        await dry_run.enter(dry_run.evidence())

    assert dry_run.orders() == 0 and dry_run.held() == {}


async def test_a_second_entry_while_the_bot_holds_a_contract_is_refused(dry_run: _DryRun) -> None:  # noqa: F811
    """New exposure is on hold for a bot that holds any: one position at a time."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    await dry_run.read_account()

    refused = await dry_run.enter(dry_run.evidence("2.00", "2.04"))

    _refused(refused, "ATTRIBUTED_EXPOSURE_EXISTS")
    assert dry_run.orders() == 1 and dry_run.held() == {SYMBOL: 1.0}


async def test_an_entry_before_the_account_was_read_again_after_a_fill_is_refused_until_it_is(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The account's reading must have seen every fill before it admits another entry."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))
    dry_run.wait(20_000)

    unread = await dry_run.enter(dry_run.evidence("2.40", "2.44"))

    _refused(unread, LIVE_ENVELOPE_UNOBSERVED)
    await dry_run.read_account()
    assert (await dry_run.enter(dry_run.evidence("2.40", "2.44"))).state is EffectOperationState.SUBMITTED


async def test_an_entry_on_a_budgeted_deployment_with_no_envelope_to_admit_it_is_refused(dry_run: _DryRun) -> None:  # noqa: F811
    """The bot has a budget. With nothing to hold the entry against it, the entry is not accepted unbounded."""
    entry = _worked_out(dry_run)

    with pytest.raises(AdmissionBlockedError) as refused:
        accept_option_enter(
            dry_run.repo, account_id=dry_run.repo.account_id, strategy_instance_id=SID, decision_id="enter-unbounded",
            lifecycle_run_id=RUN, entry=entry, envelope=None,
        )

    assert refused.value.decision.reason_code == LIVE_ENVELOPE_UNOBSERVED
    assert dry_run.orders() == 0 and dry_run.repo.get_command(f"cmd:{SID}:enter-unbounded") is None


def _entry_on_a_real_looking_account(clock: _TestClock) -> OptionEntryFacts:
    entry = work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(clock()), units=1, now_ms=clock())
    assert isinstance(entry, OptionEntryFacts), entry
    return entry


def test_an_option_entry_on_an_account_not_switched_to_budgets_is_refused(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock
) -> None:
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID, symbol="SPY", run_id=ENVELOPE_RUN_ID)
    before = envelope_repo.control_meta_snapshot().control_revision

    with pytest.raises(AdmissionBlockedError) as refused:
        accept_option_enter(
            envelope_repo, account_id=envelope_repo.account_id, strategy_instance_id=ENVELOPE_SID, decision_id="d1",
            lifecycle_run_id=ENVELOPE_RUN_ID, entry=_entry_on_a_real_looking_account(envelope_clock), envelope=_gate(),
        )

    assert refused.value.decision.reason_code == BUDGETS_NOT_SWITCHED_ON
    assert envelope_repo.control_meta_snapshot().control_revision == before


def test_an_option_entry_for_a_bot_with_no_budget_commitment_is_refused(
    envelope_repo: ClerkSqliteRepository, envelope_clock: _TestClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The account is on budgets, and this bot is running without one.

    The Clerk's own doors never make this: an account is switched only with
    every bot stopped, and a switched account starts no bot without a
    budget. So the run is started before the switch and the account is read
    as switched: the fence is what stands behind those doors.
    """
    _start_legacy_run(envelope_repo, envelope_clock, strategy_instance_id=ENVELOPE_SID, symbol="SPY", run_id=ENVELOPE_RUN_ID)
    monkeypatch.setattr(ClerkSqliteRepository, "budget_authority_version", lambda _repo: 2)
    before = envelope_repo.control_meta_snapshot().control_revision

    with pytest.raises(AdmissionBlockedError) as refused:
        accept_option_enter(
            envelope_repo, account_id=envelope_repo.account_id, strategy_instance_id=ENVELOPE_SID, decision_id="d1",
            lifecycle_run_id=ENVELOPE_RUN_ID, entry=_entry_on_a_real_looking_account(envelope_clock), envelope=_gate(),
        )

    assert refused.value.decision.reason_code == BUDGET_COMMITMENT_MISSING
    assert envelope_repo.control_meta_snapshot().control_revision == before


# ── what is set aside: the premium and the fee, for every contract ───────


@pytest.fixture
async def budget_of(tmp_path: Path) -> AsyncIterator[Any]:
    """A Dry Run whose bot buys one call, with the budget a test names, in cents."""
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)

    async def opened(cents: int) -> _DryRun:
        clock = _TestClock(NOON)
        account = await _open(tmp_path, clock, _binding(budget_cents=cents))
        await account.clerk.register_strategy_run(account.binding)
        clock.value = NOON + 1_000
        return account

    try:
        yield opened
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


@pytest.mark.parametrize(
    ("budget_cents", "admitted"),
    [
        pytest.param(19_406, True, id="$194.06 covers the premium and its six cents of fees"),
        pytest.param(19_405, False, id="$194.05 is a cent short of the fees"),
    ],
)
async def test_an_entry_is_admitted_on_its_premium_and_its_fee_together(
    budget_of: Any, budget_cents: int, admitted: bool
) -> None:
    account: _DryRun = await budget_of(budget_cents)

    answer = await account.enter(account.evidence())

    if admitted:
        assert answer.state is EffectOperationState.SUBMITTED, answer.explanation
        assert account.held() == {SYMBOL: 1.0}
    else:
        _refused(answer, LIVE_ENVELOPE_CASH_EXCEEDED)
        assert account.orders() == 0


async def test_three_contracts_set_aside_three_premiums_and_the_fees_of_three(dry_run: _DryRun) -> None:  # noqa: F811
    """Three contracts at $1.94 are $582.00 of premium.

    A buy owes no SEC fee and no TAF. Three contracts' CAT fee is $0.0009,
    their ORF $0.045 and their OCC fee $0.075; each is rounded up to the
    cent: 1 + 5 + 8 = 14 cents, not three times one contract's six.
    """
    entry = _worked_out(dry_run, units=3)

    assert (entry.units, entry.order.quantity) == (3, 3)
    assert entry.money.net_entry_premium_cents == 58_200
    assert entry.money.fee_provision_cents == 14
    assert entry.money.entry_commitment_cents == 58_214

    accept_option_enter(
        dry_run.repo, account_id=dry_run.repo.account_id, strategy_instance_id=SID, decision_id="enter-three",
        lifecycle_run_id=RUN, entry=entry, envelope=dry_run.clerk.live_envelope,
    )

    assert dry_run.reservations() == [(3.0, "1.94", 14)]
    with dry_run.repo._write_lock:
        [claim] = entry_cash_claims(dry_run.repo._conn, seen_before_ms=dry_run.clock() + 1)
    assert (claim.unfilled_cost, claim.unfilled_fee) == (Decimal("582"), Decimal("0.14"))
    budget = dry_run.repo.account_budget(cash=1000, seen_before_ms=dry_run.clock() + 1)
    assert budget.deployments[0].pending_orders == Decimal("582.14")


async def test_an_option_bot_is_deployed_with_no_minimum_budget_and_each_entry_is_held_to_its_own_cost(
    budget_of: Any,
) -> None:
    """A bot in shares needs a budget that covers one position at the price shown at Deploy: here $585.00.

    What one option position costs is known only from its contract's quotes,
    at each entry. So a budget of $100.00 deploys, and the entry of $194.06
    is the thing refused.
    """
    account: _DryRun = await budget_of(10_000)

    refused = await account.enter(account.evidence())

    _refused(refused, LIVE_ENVELOPE_CASH_EXCEEDED)
    # The same budget, the same harness, a bot in shares: its Deploy is what is refused.
    shares = await _open(
        account.artifacts_root / "shares", _TestClock(NOON),
        _binding(alpaca_v1_action_plan("SPY"), budget_cents=10_000).model_copy(
            update={"strategy_instance_id": "buys-shares", "sealed_account_id": "sim:buys-shares"}
        ),
    )
    with pytest.raises(BudgetUnavailable):
        await shares.clerk.register_strategy_run(shares.binding)


# ── the facts handed over: each must be what it is ───────────────────────


def _sided(now_ms: int, *, bid_age_ms: int, ask_age_ms: int) -> HeldQuote:
    return HeldQuote(
        bid=QuoteSide(Decimal("1.90"), now_ms - bid_age_ms), ask=QuoteSide(Decimal("1.94"), now_ms - ask_age_ms),
        data_type=QuoteDataType.LIVE, generation=GENERATION,
    )


@pytest.mark.parametrize(
    ("bid_age_ms", "ask_age_ms", "refused_as"),
    [
        pytest.param(5_000, 5_000, None, id="both sides exactly five seconds old: used"),
        pytest.param(5_001, 300, "QUOTE_STALE", id="the bid alone a millisecond too old"),
        pytest.param(300, 5_001, "QUOTE_STALE", id="the ask alone a millisecond too old"),
        pytest.param(-1, 300, "QUOTE_STALE", id="the bid received after the Clerk's own clock"),
    ],
)
def test_each_side_of_a_quote_is_aged_on_its_own_receipt_time(
    bid_age_ms: int, ask_age_ms: int, refused_as: str | None
) -> None:
    quote = _sided(NOON, bid_age_ms=bid_age_ms, ask_age_ms=ask_age_ms)

    answer = work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(NOON, quotes={LEG: quote}), units=1, now_ms=NOON)

    if refused_as is None:
        assert isinstance(answer, OptionEntryFacts), answer
    else:
        assert isinstance(answer, OptionEntryRefused) and answer.reason_code == refused_as


def test_a_contract_is_bought_on_its_expiry_day_at_noon_and_never_after_its_session_closes() -> None:
    on_the_day = work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(EXPIRY_NOON), units=1, now_ms=EXPIRY_NOON)

    assert isinstance(on_the_day, OptionEntryFacts)
    # A millisecond before the close the contract still trades, but an entry could not be closed again.
    assert _refusal_at(EXPIRY_CLOSE - 1).reason_code == "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
    # From the close of its last session on. The quote is fresh: it is the contract that is refused.
    at_the_close = _refusal_at(EXPIRY_CLOSE)
    assert at_the_close.reason_code == "OPTION_CONTRACT_EXPIRED" and SYMBOL in at_the_close.explanation
    assert _refusal_at(EXPIRY_CLOSE + 68 * 3_600_000).reason_code == "OPTION_CONTRACT_EXPIRED"


# ── the entry cutoff, asked at the Clerk's own clock ─────────────────────

# With the starting terms a position is closed 45 minutes before the session's close on its contract's
# expiry day, and an entry needs 80 seconds before that: 20 to wait for its fill, 30 to prove a cancel,
# 30 of margin. So no entry from 15:13:40 on a full day, and from 12:13:40 on a half day.
EXPIRY_CUTOFF = et_minute_of_day_ms(EXPIRY, 15 * 60 + 13) + 40_000
# The day after Thanksgiving: the session closes at 13:00 New York time.
HALF_DAY = date(2026, 11, 27)
HALF_DAY_CALL = OptionContract.of(root="SPY", expiry_date=HALF_DAY, right="call", strike=585)
HALF_DAY_CUTOFF = et_minute_of_day_ms(HALF_DAY, 12 * 60 + 13) + 40_000


@pytest.mark.parametrize(
    ("contract", "cutoff_ms"),
    [
        pytest.param(CALL, EXPIRY_CUTOFF, id="a full day: until 15:13:40"),
        pytest.param(HALF_DAY_CALL, HALF_DAY_CUTOFF, id="a half day: until 12:13:40"),
    ],
)
def test_an_entry_on_its_contracts_expiry_day_is_taken_until_the_cutoff_and_refused_by_name_from_it(
    contract: OptionContract, cutoff_ms: int
) -> None:
    """Opened later, it could not be seen through before its position must be closed. The contract still trades."""
    just_before = work_out_option_entry(
        BUYS_ONE_CALL, evidence=_evidence_at(cutoff_ms - 1, picked={LEG: contract}), units=1, now_ms=cutoff_ms - 1
    )

    assert isinstance(just_before, OptionEntryFacts), just_before
    at_the_cutoff = _refusal_at(cutoff_ms, picked={LEG: contract})
    assert at_the_cutoff.reason_code == "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
    assert contract.occ_symbol in at_the_cutoff.explanation
    # The same code the runner's own recheck refuses under, so one reason is told one way.
    assert at_the_cutoff.reason_code == ResolverRefusal.PAST_ENTRY_CUTOFF.value


def test_a_contract_that_expires_another_day_has_no_cutoff_today() -> None:
    """The day before it expires, a minute before that day's close: nothing of its position is due today."""
    thursday_late = et_minute_of_day_ms(date(2026, 9, 17), 15 * 60 + 59)

    taken = work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(thursday_late), units=1, now_ms=thursday_late)

    assert isinstance(taken, OptionEntryFacts), taken


async def _on_the_expiry_day(account: _DryRun, at_ms: int) -> None:
    """Days pass. The Clerk takes its account up again where it left it, and reads it afresh."""
    account.clock.value = at_ms
    account.repo.revive_execution_lease()
    await account.read_account()


async def test_an_entry_a_second_before_its_cutoff_is_bought_at_the_clerk(dry_run: _DryRun) -> None:  # noqa: F811
    await _on_the_expiry_day(dry_run, EXPIRY_CUTOFF - 1_000)

    bought = await dry_run.enter(dry_run.evidence())

    assert bought.state is EffectOperationState.SUBMITTED, bought.explanation
    assert dry_run.held() == {SYMBOL: 1.0}


async def test_an_entry_at_its_cutoff_is_refused_by_name_at_the_clerk_and_nothing_is_set_aside(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """The runner's own recheck let it through, or was never asked. The Clerk asks at its own clock."""
    await _on_the_expiry_day(dry_run, EXPIRY_CUTOFF)

    refused = await dry_run.enter(dry_run.evidence())

    _refused(refused, "OPTION_CONTRACT_PAST_ENTRY_CUTOFF")
    assert SYMBOL in refused.explanation
    assert dry_run.held() == {} and dry_run.fills() == [] and dry_run.orders() == 0
    assert dry_run.reservations() == []
    budget = dry_run.repo.account_budget(cash=1000, seen_before_ms=dry_run.clock() + 1)
    assert (budget.deployments[0].pending_orders, budget.deployments[0].free) == (Decimal("0"), Decimal("1000"))


async def test_an_entry_taken_before_its_cutoff_and_reached_for_sending_after_it_is_not_sent(
    dry_run: _DryRun, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The cutoff is asked once as the entry is taken and again as it is sent, at the clock then.

    The entry is taken two seconds before the cutoff. By the time its order
    is reached for sending the cutoff has come: it is not sent, and nothing
    stays set aside for it.
    """
    await _on_the_expiry_day(dry_run, EXPIRY_CUTOFF - 2_000)
    take = clerk_runtime.accept_option_enter

    def take_and_two_seconds_pass(*args: Any, **kwargs: Any) -> Any:
        taken = take(*args, **kwargs)
        dry_run.clock.advance(2_000)
        return taken

    monkeypatch.setattr(clerk_runtime, "accept_option_enter", take_and_two_seconds_pass)

    refused = await dry_run.enter(dry_run.evidence())

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert dry_run.held() == {} and dry_run.fills() == []
    [order_ref] = refused.child_order_refs
    failed = dry_run.repo.last_order_transition(order_ref=order_ref, transition_kind="ENTER_SUBMISSION_REFUSED")
    assert failed is not None and failed["summary_code"] == "OPTION_CONTRACT_PAST_ENTRY_CUTOFF"
    budget = dry_run.repo.account_budget(cash=1000, seen_before_ms=dry_run.clock() + 1)
    assert (budget.deployments[0].pending_orders, budget.deployments[0].free) == (Decimal("0"), Decimal("1000"))


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"multipliers": {}}, id="no multiplier came"),
        pytest.param({"quotes": {}}, id="no quote came"),
        pytest.param({"picked": {LEG: CALL, "another_leg": OTHER_STRIKE}}, id="a contract came for a leg the plan has not"),
        pytest.param({"leg_gap_limit_percent": Decimal(-1)}, id="a gap limit below zero"),
        pytest.param({"cost_limit_percent": Decimal("NaN")}, id="a cost limit that is no number"),
        pytest.param({"leg_gap_limit_percent": 8.0}, id="a gap limit that is not an exact decimal"),
        pytest.param({"generation": "4"}, id="a generation that is not a number"),
        pytest.param({"rechecked_at_ms": -5}, id="a read time before time began"),
    ],
)
def test_facts_that_are_missing_or_not_what_they_must_be_are_refused_as_incomplete(changes: dict[str, Any]) -> None:
    assert _refusal_at(NOON, **changes).reason_code == "OPTION_ENTRY_EVIDENCE_INCOMPLETE"


def test_a_quote_priced_finer_than_a_cent_is_refused_as_incomplete_and_says_the_price() -> None:
    sub_penny = HeldQuote(
        bid=QuoteSide(Decimal("1.90"), NOON - 300), ask=QuoteSide(Decimal("1.945"), NOON - 300),
        data_type=QuoteDataType.LIVE, generation=GENERATION,
    )

    refused = _refusal_at(NOON, quotes={LEG: sub_penny})

    assert refused.reason_code == "OPTION_ENTRY_EVIDENCE_INCOMPLETE"
    assert "1.945" in refused.explanation


def test_a_fault_in_working_out_the_money_is_raised_as_itself_and_never_told_as_incomplete_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The facts handed over are whole. Something breaks further in: that is not the caller's evidence."""
    from app.broker.alpaca.clerk.sqlite import option_admission

    def broken(*_args: Any, **_kwargs: Any) -> None:
        raise ValueError("a fault of the money rules themselves")

    monkeypatch.setattr(option_admission, "prepare_entry", broken)

    with pytest.raises(ValueError, match="a fault of the money rules themselves"):
        work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(NOON), units=1, now_ms=NOON)


@pytest.mark.parametrize("multiplier", [1, 10, 1000, True, 100.0])
def test_a_contract_handed_over_with_any_multiplier_but_a_hundred_is_refused_by_name(multiplier: Any) -> None:
    refused = _refusal_at(NOON, multipliers={LEG: multiplier})

    assert refused.reason_code == "OPTION_CONTRACT_NOT_STANDARD"
    assert SYMBOL in refused.explanation


async def test_a_non_standard_multiplier_buys_nothing_at_the_clerk(dry_run: _DryRun) -> None:  # noqa: F811
    """A hundred contracts counted at a multiplier of one would be admitted for $194.00. They are not."""
    refused = await dry_run.enter(dry_run.evidence(multipliers={LEG: 1}), units=100)

    _refused(refused, "OPTION_CONTRACT_NOT_STANDARD")
    assert dry_run.orders() == 0 and dry_run.held() == {}


async def test_an_entry_in_a_contract_the_books_hold_with_another_multiplier_is_a_refusal_and_writes_nothing(
    dry_run: _DryRun,  # noqa: F811
) -> None:
    """Books from elsewhere record this contract at ten shares. The entry is refused with a receipt, not a crash."""
    with dry_run.repo._write_lock:
        record_contract_terms(dry_run.repo._conn, ContractTerms(symbol=SYMBOL, multiplier=10), recorded_at_ms=dry_run.clock())
        dry_run.repo._conn.commit()
    commands = _rows(dry_run.repo, "commands")

    refused = await dry_run.enter(dry_run.evidence())

    _refused(refused, "OPTION_CONTRACT_TERMS_CONFLICT")
    assert (_rows(dry_run.repo, "commands"), dry_run.orders()) == (commands, 0)
    assert dry_run.reservations() == [] and dry_run.held() == {}


@pytest.mark.parametrize("units", [1.5, 2.0, "1", None])
async def test_a_number_of_structures_that_is_not_a_whole_number_is_the_callers_mistake(
    dry_run: _DryRun, units: Any  # noqa: F811
) -> None:
    with pytest.raises(ValueError, match="whole number of structures"):
        await dry_run.enter(dry_run.evidence(), units=units)

    assert dry_run.orders() == 0


def test_the_order_line_an_option_entry_writes_has_exactly_the_keys_of_a_leg_in_shares(dry_run: _DryRun) -> None:  # noqa: F811
    """Every reader of an order's symbol and quantity reads either. A key one has and the other lacks would
    be read as missing by some of them."""
    line = _worked_out(dry_run).order_line()
    in_shares = BrokerOrderLeg(symbol="SPY", side="buy", quantity=1).model_dump(mode="json")

    assert set(line) == set(in_shares) == set(BrokerOrderLeg.model_fields)
    # The same kind of value under each key; the limit is a number where a market leg in shares has none.
    assert all(type(line[key]) is type(in_shares[key]) for key in line if key != "limit_price")
    assert (line["symbol"], line["quantity"], line["limit_price"]) == (SYMBOL, 1.0, 1.94)


def test_the_fold_that_makes_a_reducing_orders_row_reads_its_symbol_and_nothing_else() -> None:
    """It binds an option close to its contract, and needs only the symbol for that. A fact that carries a
    key this build does not know, as one replayed from another build may, is read all the same."""
    from app.broker.alpaca.clerk.sqlite.facts import ExitReducingOrderCreatedFacts

    written_by_this_build = ExitReducingOrderCreatedFacts(symbol=SYMBOL, side="SELL", quantity=1.0).to_facts_json()
    from_another_build = '{"a_key_this_build_does_not_know":1,"quantity":1.0,"side":"SELL","symbol":"spy"}'

    assert ExitReducingOrderCreatedFacts.reduced_symbol(written_by_this_build) == SYMBOL
    assert ExitReducingOrderCreatedFacts.reduced_symbol(from_another_build) == "SPY"
    assert ExitReducingOrderCreatedFacts.reduced_symbol('{"side":"SELL","quantity":1.0}') is None


# ── the session, asked again just before the order is sent ───────────────


async def test_an_entry_accepted_in_the_session_and_reached_for_sending_after_it_closed_is_not_sent(
    dry_run: _DryRun, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The session is asked once as the entry is taken and again as it is sent. The second answer is no."""
    answers = iter([True, False])
    monkeypatch.setattr(clerk_runtime, "market_leg_sendable", lambda _now_ms: next(answers))

    refused = await dry_run.enter(dry_run.evidence())

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert dry_run.held() == {} and dry_run.fills() == []
    [order_ref] = refused.child_order_refs
    failed = dry_run.repo.last_order_transition(order_ref=order_ref, transition_kind="ENTER_SUBMISSION_REFUSED")
    assert failed is not None and failed["summary_code"] == "MARKET_CLOSED"
    assert option_entry_of_order(dry_run.repo, order_ref) is not None
    # Nothing is left set aside for an entry that was never sent.
    budget = dry_run.repo.account_budget(cash=1000, seen_before_ms=dry_run.clock() + 1)
    assert (budget.deployments[0].pending_orders, budget.deployments[0].free) == (Decimal("0"), Decimal("1000"))


# ── a Live Shadow account takes no option plan ───────────────────────────

SHADOW_ACCOUNT = "shadow:9LIVE0001"
SHADOW_NOON = et_minute_of_day_ms(DAY, 12 * 60)


@pytest.fixture
def live_shadow(tmp_path: Path) -> Any:
    ports = compose_shadow_ports(live_read=_LiveRead(), live_account_id="9LIVE0001", artifacts_root=tmp_path)
    repo = ClerkSqliteRepository.initialize(account_id=SHADOW_ACCOUNT, artifacts_root=tmp_path, clock=_Clock(SHADOW_NOON))
    try:
        yield SqliteAlpacaClerkFacade(
            repo=repo, read=ports.read, trade=ports.trade, authority_kind="shadow", account_mode="live",
            program_leg_policy=ProgramLegPolicy.from_read_port(ports.read),
        )
    finally:
        repo.close()


def _written(clerk: SqliteAlpacaClerkFacade) -> tuple[int, int, int, int]:
    repo = clerk.repository
    return tuple(_rows(repo, table) for table in ("strategy_instances", "commands", "orders", "custody_transitions"))  # type: ignore[return-value]


async def test_a_live_shadow_account_refuses_to_register_a_bot_that_trades_options(
    live_shadow: SqliteAlpacaClerkFacade,
) -> None:
    binding = BrokerBotBinding(
        strategy_instance_id="options-on-shadow", strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="trade", quantity=1, action_plan=BUYS_ONE_CALL, run_id="run-1", created_at_ms=0,
        sealed_account_id=SHADOW_ACCOUNT, exit_terms=TERMS,
    )
    before = _written(live_shadow)

    with pytest.raises(OptionPlanRefused) as refused:
        await live_shadow.register_strategy_run(binding)

    assert refused.value.reason.value == "OPTION_PLAN_ACCOUNT_NOT_OPEN"
    assert _written(live_shadow) == before
    assert live_shadow.repository.strategy_instance("options-on-shadow") is None


@pytest.mark.parametrize("purpose", [EffectPurpose.ENTER, EffectPurpose.EXIT])
async def test_a_live_shadow_account_refuses_an_option_entry_and_an_option_exit_and_writes_nothing(
    live_shadow: SqliteAlpacaClerkFacade, purpose: EffectPurpose
) -> None:
    before = _written(live_shadow)

    refused = await live_shadow.execute_for_instance(
        strategy_instance_id="options-on-shadow", run_id="run-1", decision_id="d-1", purpose=purpose,
        action_plan=BUYS_ONE_CALL, quantity=1,
        option_entry=_evidence_at(SHADOW_NOON) if purpose is EffectPurpose.ENTER else None,
    )

    _refused(refused, "OPTION_PLAN_ACCOUNT_NOT_OPEN")
    assert _written(live_shadow) == before
    assert await live_shadow._read.list_orders() == []


async def test_beneath_those_gates_a_shadow_ports_send_refuses_an_option_order_by_name(
    live_shadow: SqliteAlpacaClerkFacade,
) -> None:
    """Whatever let an option order reach the send on a Live Shadow account: a refusal, definitive, not a crash."""
    entry = work_out_option_entry(BUYS_ONE_CALL, evidence=_evidence_at(SHADOW_NOON), units=1, now_ms=SHADOW_NOON)
    assert isinstance(entry, OptionEntryFacts)
    port = _OptionQuoteBoundTradePort(live_shadow._trade, symbol=SYMBOL, quote=entry.the_leg.quote.held)
    io = ClaimedBrokerIO(repo=_Repo(lose_on_call=0), effect_operation_id="op-1", claim_token="tok", trade=port)  # type: ignore[arg-type]

    with pytest.raises(BrokerOptionsNotSupported) as refused:
        await io.submit(entry.order, client_order_id="ref-1")

    assert not isinstance(refused.value, BrokerUnavailable)
    assert await live_shadow._read.list_orders() == []
