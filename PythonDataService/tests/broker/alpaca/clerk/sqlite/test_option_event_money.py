"""What the account's budget says once an option event has moved shares onto a bot's books (#2913).

The books are the Clerk's real ones, budgeted as a Paper account's are: two
bots with $500.00 each. The rows are the ones Alpaca's pages document, with
a contract and a count laid over them. Nothing was recorded from Alpaca.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms, risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.options.contract import OptionContract
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _TestClock, documented_option_activity
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    RUN_ID,
    accept_option_order,
    fill,
    option_fold_registry,
)
from tests.broker.alpaca.clerk.sqlite.option_event_fixtures import held, later, poll
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

FRIDAY = date(2026, 9, 18)
FRIDAY_NOON = et_minute_of_day_ms(FRIDAY, 12 * 60)
SATURDAY_NOON = et_minute_of_day_ms(date(2026, 9, 19), 12 * 60)
PUT = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="put", strike=585).occ_symbol
CALL = OptionContract.of(root="SPY", expiry_date=FRIDAY, right="call", strike=585).occ_symbol


def _reading(repo: ClerkSqliteRepository, cash: float) -> LiveEnvelopeGate:
    at_ms = repo.clock()
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(AccountObservation(
        observed_at_ms=at_ms, broker_cash_usd=cash, cash_available_usd=cash, last_equity_usd=cash,
        unrealized_pl_usd=0, position_count=0, risk_revision=1, equity_usd=cash,
        risk_cash_flow_evidence_complete=True, risk_cash_flow_window_start_ms=day_pnl_window_start_ms(at_ms),
        risk_equity_window_start_ms=day_pnl_window_start_ms(at_ms), risk_fill_sequence=risk_fill_sequence(repo),
    ))
    return gate


@pytest.fixture
def books(tmp_path: Path):
    """A budgeted account on Friday noon: bots ``a`` and ``b`` on SPY, $500.00 each, nothing held."""
    repo = ClerkSqliteRepository.initialize(
        account_id="BUDGET-PAPER", artifacts_root=tmp_path, clock=_TestClock(FRIDAY_NOON),
        fold_registry=option_fold_registry(),
    )
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    append_risk_policy(repo, policy=AccountRiskPolicy(1, .1, 100, "profile", 1, "owner", FRIDAY_NOON), expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=FRIDAY_NOON, history_complete=True)
    for sid in ("a", "b"):
        repo.register_strategy_instance(strategy_instance_id=sid, symbol="SPY", config_hash=f"seal-{sid}", exit_terms=TERMS)
        submit_budgeted_deploy(
            repo, strategy_instance_id=sid, lifecycle_run_id=RUN_ID, world="real_paper", committed_cents=50_000,
            configuration_hash=f"seal-{sid}", exit_terms_hash=canonical_sha256(TERMS.model_dump(mode="json")),
            risk_revision=1, actor="owner", envelope=_reading(repo, 100_000), minimum_position_cost=Decimal("100.01"),
        )
    try:
        yield repo
    finally:
        repo.close()


def _buys_one(repo: ClerkSqliteRepository, sid: str, symbol: str) -> None:
    """Bot ``sid`` buys one contract at $1.94, with the cash set aside as an admitted ENTER sets it aside."""
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    order = accept_option_order(
        repo, terms=ContractTerms(symbol=symbol, multiplier=100), side="buy", contracts=1,
        decision_id=f"{sid}-{symbol}", strategy_instance_id=sid, reference_price="1.94",
    )
    fill(
        repo, order, execution_id=f"x-{sid}-{symbol}", symbol=symbol, side="BUY", quantity=1, price=1.94,
        occurred_at_ms=clock(),
    )
    clock.advance(1_000)


async def _exercised_on_saturday(repo: ClerkSqliteRepository, symbol: str, shares: int) -> int:
    """Alpaca's record of Friday's exercise is read on Saturday and booked. When the account's cash was last read."""
    later(repo, SATURDAY_NOON - repo.clock())
    cash_last_read_at = repo.clock()
    later(repo, 1_000)
    account = _FakeReadPort()
    account.option_events.rows = [
        documented_option_activity("trading_api", "option_exercise", id="exc-1", symbol=symbol, date="2026-09-18", qty="-1"),
        documented_option_activity(
            "trading_api", "option_exercise", 1, id="trd-1", symbol="SPY", date="2026-09-18", qty=str(shares),
            price="585", net_amount=str(-585 * shares),
        ),
    ]
    await poll(repo, account)
    record_fee_evidence(repo, [], checked_at_ms=repo.clock(), history_complete=True)
    return cash_last_read_at


async def test_a_bot_an_exercise_left_short_is_named_with_the_event_and_what_the_owner_does(
    books: ClerkSqliteRepository,
) -> None:
    """Bot ``a`` bought one put and held no shares. The put was exercised: 100 SPY sold at the strike.

    The bot is short 100 SPY and the books say why. So the budget's refusal
    does not call it unexplained: it names the bot and the exercise, and
    says what the owner does. The long-only rule itself is unchanged, so
    the account still has no budget while the bot is short.
    """
    _buys_one(books, "a", PUT)
    await _exercised_on_saturday(books, PUT, -100)
    assert held(books, "a") == {"SPY": -100.0}
    later(books, 60_000)
    record_fee_evidence(books, [], checked_at_ms=books.clock(), history_complete=True)

    refusals = []
    for read in (books.account_budget, books.account_money):
        with pytest.raises(BudgetUnavailable) as refused:
            read(cash=100_000 - 194 + 58_500, seen_before_ms=books.clock() + 1)
        refusals.append(str(refused.value))

    for said in refusals:
        assert "nexplained" not in said
        assert "Bot a is short 100 SPY" in said and "bot b" not in said.lower()
        assert f"1 of {PUT} was exercised" in said and "sold 100 SPY at 585" in said
        assert "Stop bot a, then Flatten it" in said
        # What is true of the rest of the account while the rule stands as it is.
        assert "no bot of this account takes a new entry" in said


async def test_right_after_an_exercise_the_budget_says_it_is_waiting_for_the_next_cash_reading_and_no_more(
    books: ClerkSqliteRepository,
) -> None:
    """Bot ``a``'s call was exercised: 100 SPY bought at 585. The cash was last read before that was booked.

    The shares were never ordered, so nothing was set aside for them, and
    for that one reading no budget can be worked out. Nothing is left to
    resolve, and the words say so. At the next reading the budget is back.
    """
    _buys_one(books, "a", CALL)
    cash_last_read_at = await _exercised_on_saturday(books, CALL, 100)
    assert held(books, "a") == {"SPY": 100.0}

    with pytest.raises(BudgetUnavailable) as waiting:
        books.account_budget(cash=100_000 - 194, seen_before_ms=cash_last_read_at)
    later(books, 60_000)
    record_fee_evidence(books, [], checked_at_ms=books.clock(), history_complete=True)
    after = books.account_budget(cash=100_000 - 194 - 58_500, seen_before_ms=books.clock() + 1)

    said = str(waiting.value)
    assert "option exercise or assignment" in said and "next reading" in said
    assert "Resolve" not in said and "Nothing needs doing" in said
    assert {budget.strategy_instance_id for budget in after.deployments} == {"a", "b"}
