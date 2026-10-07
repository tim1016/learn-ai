"""A Dry Run bot that trades one bought option: what the account Clerk does with its ENTER and its EXIT (#2913).

Each test hands the real SQLite Clerk of a Dry Run an option entry the way a
runner will: the contract its rules picked, that contract's multiplier, and
the quote read just before. The Clerk works out the money itself. What is
asserted is what the owner would see: what was bought and at what price, what
was set aside, what the bot holds, what the exit sold and for how much, and
each refusal by its own name.

The prices are one SPY call, bid $1.90 and offered at $1.94, a hundred shares
a contract: one contract costs $194.00 to buy.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.broker.alpaca.clerk.account_authority import synthetic_account_id_for_strategy
from app.broker.alpaca.clerk.active_authority import close_synthetic_clerk_runtimes, get_clerk_runtime
from app.broker.alpaca.clerk.decision_evidence import EffectDecisionEvidence
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_CASH_EXCEEDED
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence, OptionExitEvidence, OptionPlanRefused
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.database_verification import verify_database
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.fee_evidence import custody_fee_attribution
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order, order_contract
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade, _OptionQuoteBoundTradePort
from app.broker.alpaca.clerk.sqlite.scheduled_end import end_sales_waiting_for_open
from app.broker.alpaca.clerk.sqlite.simulated_account import SimulatedAccountProjection, SimulationEvidenceUnavailable
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.simulated_fill import SimulatedFillModel
from app.schemas.action_plan import ActionPlan
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import market_liveness
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import RetainedSourceBar
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS
from tests.broker.alpaca.clerk.sqlite.test_runtime_pricing_bar import _minute_bar
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import (
    _OneAccount,
    one_account,  # noqa: F401 -- pytest fixture, used by name
)

SID = "dry-run-buys-a-call"
RUN = "run-dry"
GENERATION = 4
CALL = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="call", strike=585)
SYMBOL = CALL.occ_symbol
LEG = "long_call"
EIGHT, TEN = Decimal(8), Decimal(10)


def _option_leg(
    leg_id: str, *, position: str = "long", right: str = "call", strike: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "leg_id": leg_id,
        "instrument": {"kind": "option", "underlying": "SPY"},
        "position": position,
        "qty_ratio": 1,
        "right": right,
        "strike": strike or {"selector": "atm"},
        "expiry": {"selector": "nearest_weekly"},
    }


def _plan(*legs: dict[str, Any]) -> ActionPlan:
    return ActionPlan.model_validate(
        {"on_enter": list(legs), "on_exit": [{"kind": "close_leg", "entry_leg_id": leg["leg_id"]} for leg in legs]}
    )


BUYS_ONE_CALL = _plan(_option_leg(LEG))


def _quote(bid: str, ask: str, *, received_ms: int, generation: int = GENERATION) -> HeldQuote:
    return HeldQuote(
        bid=QuoteSide(Decimal(bid), received_ms),
        ask=QuoteSide(Decimal(ask), received_ms),
        data_type=QuoteDataType.LIVE,
        generation=generation,
    )


@dataclass
class _DryRun:
    """One Dry Run account with its real Clerk, a clock the test moves, and a bot registered on it."""

    clerk: SqliteAlpacaClerkFacade
    clock: _TestClock
    authority: SyntheticBindingAuthority
    binding: BrokerBotBinding
    artifacts_root: Path
    decisions: int = 0

    def evidence(self, bid: str = "1.90", ask: str = "1.94", **changes: Any) -> OptionEntryEvidence:
        """What a runner hands over: the pick, its multiplier, and a quote received 300 ms ago."""
        quote = _quote(bid, ask, received_ms=self.clock() - 300)
        return replace(
            OptionEntryEvidence(
                picked={LEG: CALL}, multipliers={LEG: 100}, quotes={LEG: quote}, generation=GENERATION,
                rechecked_at_ms=self.clock() - 100, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
            ),
            **changes,
        )

    async def enter(
        self, evidence: OptionEntryEvidence | None, *, decision: str | None = None, units: int = 1,
        plan: ActionPlan = BUYS_ONE_CALL,
    ) -> EffectOperationReceipt:
        self.decisions += 1
        return await self.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id=decision or f"enter-{self.decisions}",
            purpose=EffectPurpose.ENTER, action_plan=plan, quantity=units, option_entry=evidence,
        )

    async def exit(self, quotes: OptionExitEvidence | None = None, *, decision: str | None = None) -> EffectOperationReceipt:
        self.decisions += 1
        return await self.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id=decision or f"exit-{self.decisions}",
            purpose=EffectPurpose.EXIT, action_plan=BUYS_ONE_CALL, quantity=1, option_exit=quotes,
        )

    def exit_quotes(self, bid: str, ask: str, *, age_ms: int = 300) -> OptionExitEvidence:
        return OptionExitEvidence(
            quotes={SYMBOL: _quote(bid, ask, received_ms=self.clock() - age_ms)}, generation=GENERATION,
            read_at_ms=self.clock() - 100,
        )

    @property
    def repo(self) -> Any:
        return self.clerk.repository

    def wait(self, ms: int) -> None:
        """Let time pass, the Clerk's hold on its account kept alive as its heartbeat keeps it."""
        while ms > 0:
            step = min(ms, 20_000)
            self.clock.advance(step)
            self.repo.renew_execution_lease()
            ms -= step

    def held(self) -> dict[str, float]:
        return {
            symbol: quantity
            for symbol, quantity in self.repo.attributed_positions_for_strategy(SID).items()
            if quantity
        }

    def fills(self) -> list[tuple[str, str, float, float, int | None]]:
        """Every fill on the books: its symbol, side, contracts, price a share and multiplier."""
        with self.repo._write_lock:
            records = effective_fill_records(self.repo._conn, account_id=self.repo.account_id)
        return [(fill.symbol, fill.side.value, fill.quantity, fill.fill_price, fill.multiplier) for fill in records]

    def realized_pnl(self) -> Decimal:
        with self.repo._write_lock:
            records = effective_fill_records(self.repo._conn, account_id=self.repo.account_id)
        return compute_fifo_pnl(records).exact_realized_pnl

    def reservations(self) -> list[tuple[float, str, int]]:
        with self.repo._write_lock:
            rows = self.repo._conn.execute(
                "SELECT quantity, exact_reference_price, fee_provision_cents FROM envelope_reservations"
            ).fetchall()
        return [(row[0], row[1], row[2]) for row in rows]

    def orders(self) -> int:
        with self.repo._write_lock:
            return self.repo._conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    def fees(self) -> Any:
        with self.repo._write_lock:
            return custody_fee_attribution(
                self.repo._conn, now_ms=self.clock(), simulated_fill_cutoff_ms=self.clock()
            )

    def account_reading(self) -> Any:
        """The simulated account's cash, equity and open P&L now, as its envelope would read them."""
        projection = SimulatedAccountProjection(repo=self.repo, artifacts_root=self.artifacts_root)
        return projection.observe(reference_cash=0, observed_at_ms=self.clock(), now_ms=self.clock())

    async def read_account(self) -> None:
        """One tick of the account's own reading cadence, which a later entry waits on."""
        runtime = get_clerk_runtime(self.binding.sealed_account_id or "")
        assert runtime is not None and runtime.envelope_sync is not None
        await runtime.envelope_sync.tick()


def _binding(plan: ActionPlan = BUYS_ONE_CALL, *, budget_cents: int = 100_000) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=SID, strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="dry_run", quantity=1, action_plan=plan, run_id=RUN, created_at_ms=0,
        sealed_account_id=synthetic_account_id_for_strategy(SID), exit_terms=TERMS,
        budget_consent=DeployBudgetConsent(
            committed_cents=budget_cents, risk_revision=0, actor="owner", request_fingerprint="reviewed",
            world="synthetic",
        ),
    )


async def _open(tmp_path: Path, clock: _TestClock, binding: BrokerBotBinding) -> _DryRun:
    authority = SyntheticBindingAuthority(
        binding=binding, artifacts_root=tmp_path,
        lifecycle_repo_for=lambda _sid: pytest.fail("a Dry Run's Clerk never reads the runner's lifecycle state"),
        runtime_in_use=lambda _sid: True, brokers={}, clock=clock,
    )
    await authority.ensure_recoverable()
    runtime = get_clerk_runtime(binding.sealed_account_id or "")
    assert runtime is not None and runtime.clerk is not None
    return _DryRun(clerk=runtime.clerk, clock=clock, authority=authority, binding=binding, artifacts_root=tmp_path)


@pytest.fixture
async def dry_run(tmp_path: Path) -> AsyncIterator[_DryRun]:
    """A Dry Run with a $1,000.00 budget whose bot buys one call, at noon on a trading day."""
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    clock = _TestClock(NOON)
    try:
        account = await _open(tmp_path, clock, _binding())
        await account.clerk.register_strategy_run(account.binding)
        clock.value = NOON + 1_000
        yield account
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


# ── an entry that fills, and what the books then say ─────────────────────


async def test_an_entry_buys_the_picked_contract_at_its_ask_and_the_bot_holds_it(dry_run: _DryRun) -> None:
    bought = await dry_run.enter(dry_run.evidence())

    assert bought.state is EffectOperationState.SUBMITTED, bought.explanation
    assert dry_run.held() == {SYMBOL: 1.0}
    # One contract at $1.94 a share, counted with the contract's own hundred.
    assert dry_run.fills() == [(SYMBOL, "buy", 1.0, 1.94, 100)]
    [order_ref] = bought.child_order_refs
    assert order_contract(dry_run.repo, order_ref) == ContractTerms(symbol=SYMBOL, multiplier=100)


async def test_the_entry_keeps_the_contract_the_quote_and_the_money_it_was_admitted_on(dry_run: _DryRun) -> None:
    evidence = dry_run.evidence()
    bought = await dry_run.enter(evidence)

    [order_ref] = bought.child_order_refs
    kept = option_entry_of_order(dry_run.repo, order_ref)
    assert kept is not None
    assert (kept.the_leg.leg_id, kept.the_leg.contract, kept.the_leg.multiplier, kept.units) == (LEG, CALL, 100, 1)
    assert kept.the_leg.quote.held == evidence.quotes[LEG]
    assert (kept.order.side, kept.order.quantity, kept.order.limit_price) == (OrderSide.BUY, 1, Decimal("1.94"))
    # $194.00 of premium, no requirement for a bought option, and six cents of fees. A buy owes no
    # SEC fee and no TAF. One contract's CAT fee is $0.0003, its ORF $0.015 and its OCC fee $0.025;
    # each is rounded up to the cent: 1 + 2 + 3.
    assert kept.money.net_entry_premium_cents == 19_400
    assert kept.money.broker_requirement_cents == 0
    assert kept.money.fee_provision_cents == 6
    assert kept.money.entry_commitment_cents == 19_406
    assert kept.money.loss_at_expiry_cents == 19_400
    assert kept.money.round_trip_cost_cents == 400
    assert (kept.leg_gap_limit_percent, kept.cost_limit_percent) == (EIGHT, TEN)
    assert kept.admitted_at_ms == dry_run.clock()


async def test_the_entrys_identity_is_hashed_over_its_option_order_under_a_key_no_stock_entry_has(
    dry_run: _DryRun,
) -> None:
    import hashlib
    import json

    bought = await dry_run.enter(dry_run.evidence(), decision="enter-hashed")

    [order_ref] = bought.child_order_refs
    kept = option_entry_of_order(dry_run.repo, order_ref)
    assert kept is not None
    command = dry_run.repo.get_command(f"cmd:{SID}:enter-hashed")
    expected = hashlib.sha256(
        json.dumps(
            {
                "account_id": dry_run.repo.account_id, "strategy_instance_id": SID, "decision_id": "enter-hashed",
                "action": "ENTER",
                "option_order": {
                    "contract": {"root": "SPY", "expiry_ms": CALL.expiry_ms, "right": "call", "strike_thousandths": 585_000},
                    "side": "buy", "intent": "open", "quantity": 1, "order_type": "limit", "limit_price": "1.94",
                },
            },
            sort_keys=True, separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert command is not None and command.payload_hash == expected


async def test_the_simulated_fill_records_its_model_its_quotes_age_and_its_limit(dry_run: _DryRun) -> None:
    bought = await dry_run.enter(dry_run.evidence())

    [order_ref] = bought.child_order_refs
    fill = dry_run.clerk.synthesized_option_fills()[order_ref]
    assert fill.fill_model is SimulatedFillModel.NATURAL_PRICE
    assert (fill.fill_price, fill.request.limit_price, fill.quote_age_ms) == (Decimal("1.94"), Decimal("1.94"), 300)
    assert (fill.quote.bid, fill.quote.ask) == (Decimal("1.90"), Decimal("1.94"))


async def test_the_entry_sets_aside_its_commitment_and_a_filled_entry_claims_nothing_more(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())

    # One contract reserved at $1.94 a share with six cents of fees.
    assert dry_run.reservations() == [(1.0, "1.94", 6)]
    with dry_run.repo._write_lock:
        from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims

        [seen], [unseen] = (
            entry_cash_claims(dry_run.repo._conn, seen_before_ms=dry_run.clock() + 1),
            entry_cash_claims(dry_run.repo._conn, seen_before_ms=0),
        )
    # Once cash has seen the fill nothing is claimed; until then the fill is claimed at what it cost.
    assert (seen.unfilled_cost, seen.unseen_fill_cost, seen.unfilled_fee) == (0, 0, 0)
    assert (unseen.unfilled_cost, unseen.unseen_fill_cost) == (0, Decimal("194"))


async def test_an_entry_accepted_and_not_yet_filled_claims_its_whole_commitment(dry_run: _DryRun) -> None:
    """A simulation fills at once, so the wait is made here: the entry is accepted and nothing is sent.

    One contract at $1.94 a share is $194.00 of the budget, with six cents of
    fees: counted with the contract's hundred, not as one share at $1.94.
    """
    from app.broker.alpaca.clerk.sqlite.enter import accept_option_enter
    from app.broker.alpaca.clerk.sqlite.envelope_reservations import entry_cash_claims
    from app.broker.alpaca.clerk.sqlite.option_admission import work_out_option_entry

    entry = work_out_option_entry(BUYS_ONE_CALL, evidence=dry_run.evidence(), units=1, now_ms=dry_run.clock())
    accept_option_enter(
        dry_run.repo, account_id=dry_run.repo.account_id, strategy_instance_id=SID, decision_id="enter-waits",
        lifecycle_run_id=RUN, entry=entry, envelope=dry_run.clerk.live_envelope,
    )

    with dry_run.repo._write_lock:
        [claim] = entry_cash_claims(dry_run.repo._conn, seen_before_ms=dry_run.clock() + 1)
    assert (claim.unfilled_cost, claim.unfilled_fee, claim.unseen_fill_cost) == (Decimal("194"), Decimal("0.06"), 0)
    budget = dry_run.repo.account_budget(cash=1000, seen_before_ms=dry_run.clock() + 1)
    assert budget.deployments[0].pending_orders == Decimal("194.06")
    assert budget.deployments[0].free == Decimal("805.94")


# ── refusals, each by its own name ───────────────────────────────────────


async def _refused(receipt: EffectOperationReceipt, code: str, dry_run: _DryRun) -> None:
    assert receipt.state is EffectOperationState.REJECTED, receipt.explanation
    assert receipt.explanation.startswith(f"{code}: "), receipt.explanation
    assert receipt.child_order_refs == ()
    assert dry_run.orders() == 0 and dry_run.held() == {}


async def test_an_entry_whose_commitment_is_more_than_the_budget_has_free_is_refused(dry_run: _DryRun) -> None:
    """Six contracts are $1,164.00 of premium. The budget is $1,000.00."""
    await _refused(await dry_run.enter(dry_run.evidence(), units=6), LIVE_ENVELOPE_CASH_EXCEEDED, dry_run)

    # Five are $970.00 and fit.
    assert (await dry_run.enter(dry_run.evidence(), units=5)).state is EffectOperationState.SUBMITTED


async def test_an_entry_that_costs_more_than_the_owners_limit_to_trade_is_refused(dry_run: _DryRun) -> None:
    """Bid $1.80, ask $1.94: buying and selling at once loses $14 of the $194 set aside, over a 5% limit."""
    refused = await dry_run.enter(
        dry_run.evidence("1.80", "1.94", leg_gap_limit_percent=Decimal(20), cost_limit_percent=Decimal(5))
    )

    await _refused(refused, "OPTION_STRUCTURE_COST_OVER_LIMIT", dry_run)


async def test_an_entry_whose_leg_is_quoted_wider_than_the_owners_limit_is_refused(dry_run: _DryRun) -> None:
    """Bid $1.70, ask $1.94 is 13% of the mid, over the 8% limit."""
    await _refused(await dry_run.enter(dry_run.evidence("1.70", "1.94")), "OPTION_STRUCTURE_LEG_GAP_OVER_LIMIT", dry_run)


async def test_an_entry_handed_no_quotes_is_refused_and_nothing_is_filled(dry_run: _DryRun) -> None:
    await _refused(await dry_run.enter(None), "OPTION_ENTRY_QUOTES_NOT_KEPT", dry_run)


async def test_an_entry_whose_quote_has_gone_stale_by_the_clerks_own_clock_is_refused(dry_run: _DryRun) -> None:
    evidence = dry_run.evidence()
    dry_run.wait(6_000)

    await _refused(await dry_run.enter(evidence), "QUOTE_STALE", dry_run)


async def test_an_entry_whose_quote_is_from_before_a_reconnect_is_refused(dry_run: _DryRun) -> None:
    await _refused(await dry_run.enter(dry_run.evidence(generation=GENERATION + 1)), "QUOTE_OLD_GENERATION", dry_run)


async def test_a_contract_that_is_not_the_plans_leg_is_refused(dry_run: _DryRun) -> None:
    """The plan buys a SPY call. A put handed over for that leg is not what the plan opens."""
    put = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 18), right="put", strike=585)

    await _refused(await dry_run.enter(dry_run.evidence(picked={LEG: put})), "OPTION_STRUCTURE_NOT_THE_PLANS_LEGS", dry_run)


async def test_a_contract_already_expired_is_refused(dry_run: _DryRun) -> None:
    expired = OptionContract.of(root="SPY", expiry_date=date(2026, 9, 4), right="call", strike=585)

    await _refused(await dry_run.enter(dry_run.evidence(picked={LEG: expired})), "OPTION_CONTRACT_EXPIRED", dry_run)


async def test_more_than_a_hundred_structures_is_the_callers_mistake_and_writes_nothing(dry_run: _DryRun) -> None:
    with pytest.raises(ValueError, match="1 to 100"):
        await dry_run.enter(dry_run.evidence(), units=101)

    assert dry_run.orders() == 0


async def test_an_entry_sent_after_the_regular_session_is_refused(dry_run: _DryRun) -> None:
    dry_run.wait(5 * 3_600_000)

    await _refused(await dry_run.enter(dry_run.evidence()), "MARKET_CLOSED", dry_run)


@pytest.mark.parametrize(
    "plan",
    [
        pytest.param(
            _plan(
                _option_leg("long_call"),
                _option_leg("short_call", position="short", strike={"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}),
            ),
            id="a spread with a sold leg",
        ),
        pytest.param(_plan(_option_leg("long_call"), _option_leg("long_put", right="put")), id="a straddle of two bought legs"),
    ],
)
async def test_a_plan_the_clerk_does_not_take_yet_is_refused_by_name(dry_run: _DryRun, plan: ActionPlan) -> None:
    refused = await dry_run.enter(dry_run.evidence(), plan=plan)

    await _refused(refused, "OPTION_STRUCTURE_NOT_SUPPORTED", dry_run)
    assert "not built yet" in refused.explanation


async def test_the_books_and_the_simulated_broker_agree_on_what_the_bot_holds(dry_run: _DryRun) -> None:
    """The account's reconciliation pass compares the two. One contract held is no drift and no hold."""
    await dry_run.enter(dry_run.evidence())

    assert await dry_run.clerk.reconcile_once() == "clean"
    assert dry_run.held() == {SYMBOL: 1.0}
    assert dry_run.repo.active_holds_for_admission(strategy_instance_id=SID) == []


async def test_the_simulated_broker_states_the_contract_in_dollars_by_the_multiplier_the_books_record(
    dry_run: _DryRun,
) -> None:
    """One contract at $1.94 a share costs $194.00 at the simulated broker as it does on the books, not $1.94."""
    bought = await dry_run.enter(dry_run.evidence())

    [position] = await dry_run.clerk._read.list_positions()

    assert (position.symbol, position.asset_class, position.quantity) == (SYMBOL, "us_option", 1.0)
    # The price stays a share's. The simulation states no current price, so the value is the cost.
    assert (position.average_entry_price, position.cost_basis, position.market_value) == (1.94, 194.0, 194.0)
    # The multiplier the simulation counts by is the one the books record for the order's contract.
    [order_ref] = bought.child_order_refs
    recorded = order_contract(dry_run.repo, order_ref)
    assert recorded == ContractTerms(symbol=SYMBOL, multiplier=100)
    assert dry_run.clerk.synthesized_option_fills()[order_ref].multiplier == recorded.multiplier


async def test_the_multiplier_the_clerks_port_hands_the_simulation_is_the_terms_own_and_never_a_hundred_assumed(
    dry_run: _DryRun,
) -> None:
    """Beneath the Clerk's doors, which take a standard contract only: the port that binds an entry's quote is
    given terms of ten shares a contract. The simulation counts the position by ten, whatever the symbol looks like."""
    port = _OptionQuoteBoundTradePort(
        dry_run.clerk._trade, terms=ContractTerms(symbol=SYMBOL, multiplier=10),
        quote=_quote("1.90", "1.94", received_ms=dry_run.clock() - 300),
    )
    one_call = SingleOptionOrderRequest(
        contract=CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, quantity=1, order_type=OrderType.LIMIT,
        limit_price=Decimal("1.94"),
    )

    await port.submit_option_order(one_call, client_order_id="ref-of-ten")

    [position] = await dry_run.clerk._read.list_positions()
    assert (position.average_entry_price, position.cost_basis, position.market_value) == (1.94, 19.4, 19.4)


def _decision(name: str, outcome: str, at_ms: int) -> EffectDecisionEvidence:
    import hashlib

    evaluation_id = hashlib.sha256(name.encode()).hexdigest()
    return EffectDecisionEvidence(
        evaluation_id=evaluation_id, bar_ref=f"bar-{name}", symbol="SPY", outcome=outcome, observed_at_ms=at_ms,
        decision_bar_close_ms=at_ms,
    )


async def test_an_option_entrys_decision_receipt_is_kept_with_it_and_a_refusals_says_why(dry_run: _DryRun) -> None:
    refused_on = _decision("too-wide", "enter_intent", dry_run.clock())
    entered_on = _decision("enters", "enter_intent", dry_run.clock())

    refused = await dry_run.clerk.execute_for_instance(
        strategy_instance_id=SID, run_id=RUN, decision_id=refused_on.evaluation_id, purpose=EffectPurpose.ENTER,
        action_plan=BUYS_ONE_CALL, quantity=1, option_entry=dry_run.evidence("1.70", "1.94"),
        decision_evidence=refused_on,
    )
    bought = await dry_run.clerk.execute_for_instance(
        strategy_instance_id=SID, run_id=RUN, decision_id=entered_on.evaluation_id, purpose=EffectPurpose.ENTER,
        action_plan=BUYS_ONE_CALL, quantity=1, option_entry=dry_run.evidence(), decision_evidence=entered_on,
    )

    assert refused.state is EffectOperationState.REJECTED and bought.state is EffectOperationState.SUBMITTED
    with dry_run.repo._write_lock:
        receipts = dry_run.repo._conn.execute(
            "SELECT outcome, json_extract(facts_json, '$.reason_code') FROM decision_receipts ORDER BY rowid"
        ).fetchall()
    assert [tuple(row) for row in receipts] == [
        ("blocked", "OPTION_STRUCTURE_LEG_GAP_OVER_LIMIT"),
        ("enter_intent", "STRATEGY_ENTER"),
    ]


async def test_option_evidence_handed_over_with_a_plan_that_trades_shares_is_the_callers_mistake(
    dry_run: _DryRun,
) -> None:
    with pytest.raises(ValueError, match="trades shares"):
        await dry_run.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=alpaca_v1_action_plan("SPY"), quantity=1, option_entry=dry_run.evidence(),
        )


# ── one decision, one order ──────────────────────────────────────────────


async def test_the_same_decision_sent_twice_makes_one_order(dry_run: _DryRun) -> None:
    evidence = dry_run.evidence()
    first = await dry_run.enter(evidence, decision="enter-once")
    # The retry arrives after its quotes have aged past use. It is answered from the entry already made.
    dry_run.wait(30_000)
    again = await dry_run.enter(evidence, decision="enter-once")

    assert again.state is EffectOperationState.SUBMITTED
    assert again.child_order_refs == first.child_order_refs
    assert dry_run.orders() == 1 and dry_run.held() == {SYMBOL: 1.0}


# ── the exit closes what the entry opened ────────────────────────────────


async def test_an_exit_sells_the_stored_contract_and_proves_the_bot_flat(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(60_000)

    closed = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    assert closed.state is EffectOperationState.FLAT, closed.explanation
    assert dry_run.held() == {}
    assert dry_run.fills() == [(SYMBOL, "buy", 1.0, 1.94, 100), (SYMBOL, "sell", 1.0, 2.40, 100)]
    # Bought at $1.94 and sold at $2.40, a hundred shares: $46.00, not forty-six cents.
    assert dry_run.realized_pnl() == Decimal("46")
    sale_ref = closed.child_order_refs[-1]
    assert order_contract(dry_run.repo, sale_ref) == ContractTerms(symbol=SYMBOL, multiplier=100)
    sale = dry_run.clerk.synthesized_option_fills()[sale_ref]
    assert (sale.fill_model, sale.fill_price, sale.request.limit_price) == (
        SimulatedFillModel.NATURAL_PRICE, Decimal("2.40"), None
    )


async def test_an_exit_handed_no_quote_is_priced_from_an_older_one_and_says_so_with_its_age(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(120_000)

    closed = await dry_run.exit(None)

    assert closed.state is EffectOperationState.FLAT, closed.explanation
    sale = dry_run.clerk.synthesized_option_fills()[closed.child_order_refs[-1]]
    # The newest quote kept is the entry's own: its bid of $1.90, received two minutes and 300 ms earlier.
    assert sale.fill_model is SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE
    assert (sale.fill_price, sale.quote_age_ms) == (Decimal("1.90"), 120_300)
    assert dry_run.realized_pnl() == Decimal("-4")


async def test_an_exit_whose_own_quote_is_too_old_to_use_is_priced_from_it_as_an_older_quote(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(60_000)

    closed = await dry_run.exit(dry_run.exit_quotes("2.10", "2.16", age_ms=9_000))

    sale = dry_run.clerk.synthesized_option_fills()[closed.child_order_refs[-1]]
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("2.10"), 9_000
    )


async def test_an_exit_uses_a_newer_quote_the_runner_handed_over_while_the_bot_held(dry_run: _DryRun) -> None:
    """With no quote at the exit decision, the newest kept is the one handed as a mark, not the entry's."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(60_000)
    dry_run.clerk.keep_option_quotes({SYMBOL: _quote("2.20", "2.26", received_ms=dry_run.clock() - 200)})
    dry_run.wait(30_000)

    closed = await dry_run.exit(None)

    sale = dry_run.clerk.synthesized_option_fills()[closed.child_order_refs[-1]]
    assert (sale.fill_model, sale.fill_price, sale.quote_age_ms) == (
        SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("2.20"), 30_200
    )


async def test_an_exit_decided_after_the_regular_session_is_held_for_the_next_one_and_sells_nothing(
    dry_run: _DryRun,
) -> None:
    """An option order is never sent outside the regular session, and never re-priced for an extended one."""
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(5 * 3_600_000)

    held_back = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    # Accepted and waiting: it is still the bot's one EXIT, and nothing says it failed.
    assert held_back.state is EffectOperationState.ACCEPTED, held_back.explanation
    assert dry_run.repo.active_exit_for_strategy(SID) is not None
    assert dry_run.repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code="EXIT_NOT_FLAT", strategy_instance_id=SID
    ) is None
    assert dry_run.held() == {SYMBOL: 1.0}
    assert len(dry_run.fills()) == 1, "nothing was sold"
    # It is told as an option close waiting for the open, and never as a bot that reached its scheduled end.
    hold = dry_run.repo.last_strategy_transition(strategy_instance_id=SID, transition_kind="EXIT_MARKET_HOLD")
    assert hold is not None and hold["summary_code"] == "OPTION_CLOSE_WAITS_FOR_OPEN"
    assert end_sales_waiting_for_open(dry_run.repo) == []

    # The next morning, a minute after the open, the account's own pass sends it.
    dry_run.wait(16 * 3_600_000 + 31 * 60_000)
    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert dry_run.fills()[-1] == (SYMBOL, "sell", 1.0, 2.40, 100)


async def test_an_exit_of_an_option_position_takes_no_extended_hours_limit(dry_run: _DryRun) -> None:
    """A stock exit may carry a priced after-hours limit. An option position is closed by a market order only."""
    from app.broker.alpaca.clerk.program_leg import LegShape, ProgramLeg, regular_session_shape
    from app.broker.alpaca.clerk.sqlite.exit import accept_exit
    from app.broker.contract.models import OrderType, TimeInForce

    bought = await dry_run.enter(dry_run.evidence())
    [entry_ref] = bought.child_order_refs
    after_hours = ProgramLeg(
        shape=LegShape(
            order_type=OrderType.LIMIT, time_in_force=TimeInForce.DAY, limit_price=1.9, extended_hours=True,
            side=OrderSide.SELL,
        ),
        valid_until_ms=dry_run.clock() + 3_600_000,
    )

    with pytest.raises(ValueError, match="no priced leg"):
        accept_exit(
            dry_run.repo, account_id=dry_run.repo.account_id, strategy_instance_id=SID, decision_id="exit-priced",
            lifecycle_run_id=RUN, entry_order_ref=entry_ref, program_leg=after_hours,
        )
    accepted = accept_exit(
        dry_run.repo, account_id=dry_run.repo.account_id, strategy_instance_id=SID, decision_id="exit-market",
        lifecycle_run_id=RUN, entry_order_ref=entry_ref,
        program_leg=ProgramLeg(shape=regular_session_shape(OrderSide.SELL), valid_until_ms=None),
    )

    assert accepted.created
    assert dry_run.repo.get_command(f"cmd:{SID}:exit-priced") is None


async def test_a_second_exit_for_a_position_already_closed_sends_nothing(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(60_000)
    await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))
    orders = dry_run.orders()

    again = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    assert again.state is EffectOperationState.FLAT, again.explanation
    assert dry_run.orders() == orders, "no second sale was made"
    assert len(dry_run.fills()) == 2


async def test_an_exit_with_nothing_held_and_no_entry_is_refused(dry_run: _DryRun) -> None:
    refused = await dry_run.exit(None)

    assert refused.state is EffectOperationState.REJECTED
    assert refused.explanation.startswith("EXIT_CUSTODY_UNPROVEN: "), refused.explanation


async def test_an_exit_closes_every_contract_the_entry_bought(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence(), units=3)
    dry_run.wait(60_000)

    closed = await dry_run.exit(dry_run.exit_quotes("2.00", "2.04"))

    assert closed.state is EffectOperationState.FLAT, closed.explanation
    assert dry_run.fills()[-1] == (SYMBOL, "sell", 3.0, 2.00, 100)
    # Three contracts, six cents a share, a hundred shares each.
    assert dry_run.realized_pnl() == Decimal("18")


# ── the money a simulation keeps: fees, marks, and going on ──────────────


async def test_a_simulated_option_round_trip_is_charged_the_option_fees_of_its_day(dry_run: _DryRun) -> None:
    """One contract bought at $1.94 and sold at $2.40 on 2026-09-08.

    SEC, on the $240.00 sold: $0.004944. TAF, on the one contract sold:
    $0.00329. CAT, on two hundred equivalent shares: $0.0006. ORF, two
    contracts: $0.03. OCC, two contracts: $0.05. Each fee type is rounded up
    to the cent: 1 + 1 + 1 + 3 + 5.
    """
    await dry_run.enter(dry_run.evidence())
    after_the_buy = dry_run.fees()
    dry_run.wait(60_000)
    await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))

    fees = dry_run.fees()

    assert after_the_buy.known and after_the_buy.total_for(f"bot:{SID}") == Decimal("0.06")
    assert fees.known, fees.unresolved
    assert fees.total_for(f"bot:{SID}") == Decimal("0.11")
    assert {share.charge_id.rsplit(":", 1)[-1]: share.amount for share in fees.shares} == {
        "sec": Decimal("0.01"), "taf": Decimal("0.01"), "cat": Decimal("0.01"),
        "orf": Decimal("0.03"), "occ": Decimal("0.05"),
    }


async def test_an_open_contract_is_marked_at_the_bid_of_a_fresh_quote_and_not_at_all_without_one(
    dry_run: _DryRun,
) -> None:
    await dry_run.enter(dry_run.evidence())

    # The entry's own quote is fresh: bid $1.90 against $1.94 paid, a hundred shares.
    assert dry_run.account_reading().unrealized_pl_usd == Decimal("-4")

    dry_run.wait(180_000)
    with pytest.raises(SimulationEvidenceUnavailable, match=SYMBOL):
        dry_run.account_reading()

    dry_run.clerk.keep_option_quotes({SYMBOL: _quote("2.50", "2.56", received_ms=dry_run.clock() - 500)})
    reading = dry_run.account_reading()
    assert reading.unrealized_pl_usd == Decimal("56")
    # $1,000.00 less the $194.00 paid; the day's fees are an estimate until the day is over.
    assert reading.cash_available_usd == Decimal("806")
    assert reading.equity_usd == Decimal("1000") + Decimal("56") - Decimal("0.06")


async def test_a_quiet_contract_whose_bid_has_not_moved_is_still_marked_while_it_is_handed_over(
    dry_run: _DryRun,
) -> None:
    """Nobody has changed the bid for five minutes. It is still the bid, and each hand-over says so."""
    await dry_run.enter(dry_run.evidence())
    unchanged = _quote("2.50", "2.56", received_ms=dry_run.clock())

    dry_run.wait(300_000)
    dry_run.clerk.keep_option_quotes({SYMBOL: unchanged})

    assert dry_run.account_reading().unrealized_pl_usd == Decimal("56")
    dry_run.wait(100_000)
    with pytest.raises(SimulationEvidenceUnavailable, match=SYMBOL):
        dry_run.account_reading()


async def test_a_dry_run_goes_on_trading_after_its_first_option_round_trip(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))
    dry_run.wait(20_000)
    await dry_run.read_account()

    again = await dry_run.enter(dry_run.evidence("2.40", "2.44"))

    assert again.state is EffectOperationState.SUBMITTED, again.explanation
    assert dry_run.fills()[-1] == (SYMBOL, "buy", 1.0, 2.44, 100)


async def test_a_contract_held_overnight_starts_the_next_day_from_its_mark_at_the_close(dry_run: _DryRun) -> None:
    """The day's starting equity is yesterday's close. For a contract that is the last bid handed over before it.

    Bought at $1.94. Thirty seconds before Tuesday's close the bid handed
    over is $2.00, so the day ends $6.00 up, less six cents of fees now
    settled. Wednesday at 9:31 the bid is $2.50.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(4 * 3_600_000 - 31_000)  # 15:59:30 New York time
    dry_run.clerk.keep_option_quotes({SYMBOL: _quote("2.00", "2.06", received_ms=dry_run.clock() - 5_000)})
    dry_run.wait(17 * 3_600_000 + 31 * 60_000 + 30_000)  # 09:31:00 the next morning
    dry_run.clerk.keep_option_quotes({SYMBOL: _quote("2.50", "2.56", received_ms=dry_run.clock() - 200)})

    reading = dry_run.account_reading()

    assert reading.last_equity_usd == Decimal("1000") + Decimal("6") - Decimal("0.06")
    assert reading.unrealized_pl_usd == Decimal("56")
    assert reading.equity_usd == Decimal("1000") + Decimal("56") - Decimal("0.06")


async def test_a_quote_handed_over_the_next_morning_does_not_stand_in_for_the_close_it_missed(
    dry_run: _DryRun,
) -> None:
    """Nothing was handed over in the last minute of the day. The same old quote, handed over at 9:31, was
    received before the close, but it is a statement made after it: the close has no mark, and says so."""
    await dry_run.enter(dry_run.evidence())
    quiet = _quote("2.00", "2.06", received_ms=dry_run.clock())
    dry_run.wait(3 * 3_600_000)  # 15:00, an hour before the close
    dry_run.clerk.keep_option_quotes({SYMBOL: quiet})
    dry_run.wait(18 * 3_600_000 + 31 * 60_000 - 1_000)  # 09:31:00 the next morning
    dry_run.clerk.keep_option_quotes({SYMBOL: quiet})

    with pytest.raises(SimulationEvidenceUnavailable, match=SYMBOL):
        dry_run.account_reading()


# ── a close the simulation cannot price ──────────────────────────────────


async def test_a_close_with_nothing_bid_is_not_recorded_as_a_sale_for_nothing_and_the_position_stays(
    dry_run: _DryRun,
) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)

    refused = await dry_run.exit(dry_run.exit_quotes("0", "0.05"))

    assert refused.state is EffectOperationState.REJECTED, refused.explanation
    assert dry_run.held() == {SYMBOL: 1.0}
    assert len(dry_run.fills()) == 1
    still_open = dry_run.repo.active_uncertainty(
        scope="CUSTODY_SUBJECT", reason_code="EXIT_NOT_FLAT", strategy_instance_id=SID
    )
    assert still_open is not None, "the failed close is on the books as a position still open"
    # It is marked at what was last seen bid for it, nothing: the whole $194.00 paid is an open loss.
    # The entry's own quote, handed back to price the close, is not taken for a fresh one.
    assert dry_run.account_reading().unrealized_pl_usd == Decimal("-194")


async def test_a_close_is_still_priced_from_the_entrys_own_quote_when_the_quotes_kept_are_lost(
    dry_run: _DryRun,
) -> None:
    """The file of quotes kept is gone. The entry's own quote is in its facts, and the Clerk hands it back.

    Only an account with nothing kept and nothing in its entry's facts could
    not price a close, and an entry is never accepted without its quote.
    """
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    (dry_run.artifacts_root / "accounts" / "alpaca").rglob("kept_option_quotes.jsonl").__next__().unlink()

    closed = await dry_run.exit(None)

    assert closed.state is EffectOperationState.FLAT, closed.explanation
    sale = dry_run.clerk.synthesized_option_fills()[closed.child_order_refs[-1]]
    assert (sale.fill_model, sale.fill_price) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, Decimal("1.90"))


# ── a run that ends holding, and a restart ───────────────────────────────


async def test_a_run_that_ends_holding_has_its_contract_closed_at_the_newest_quote_kept(dry_run: _DryRun) -> None:
    await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    dry_run.clerk.keep_option_quotes({SYMBOL: _quote("2.10", "2.16", received_ms=dry_run.clock() - 1_000)})
    await dry_run.clerk.stop_strategy_run(strategy_instance_id=SID, run_id=RUN)

    await dry_run.clerk.reconcile_once()

    assert dry_run.held() == {}
    assert dry_run.fills()[-1] == (SYMBOL, "sell", 1.0, 2.10, 100)
    [sale] = [fill for fill in dry_run.clerk.synthesized_option_fills().values() if fill.request.side is OrderSide.SELL]
    assert (sale.fill_model, sale.quote_age_ms) == (SimulatedFillModel.NATURAL_PRICE_OLDER_QUOTE, 1_000)


async def test_after_a_restart_while_holding_the_exit_closes_the_contract_stored_with_the_entry(
    dry_run: _DryRun, tmp_path: Path
) -> None:
    """The process dies holding one call. Reopened, the store still says which contract, with what multiplier.

    A restart ends the run, and a Dry Run closes what an ended run left. The
    close is made from the entry's own stored contract: nothing asks the
    plan's rules which contract to sell.
    """
    await dry_run.enter(dry_run.evidence())
    await close_synthetic_clerk_runtimes()

    reopened = await _open(tmp_path, dry_run.clock, dry_run.binding)

    assert reopened.held() == {}
    assert reopened.fills() == [(SYMBOL, "buy", 1.0, 1.94, 100), (SYMBOL, "sell", 1.0, 1.90, 100)]
    assert reopened.realized_pnl() == Decimal("-4")


async def test_the_books_rebuilt_from_their_mirror_hold_the_contract_and_its_multiplier(
    dry_run: _DryRun, tmp_path: Path
) -> None:
    bought = await dry_run.enter(dry_run.evidence())
    dry_run.wait(20_000)
    closed = await dry_run.exit(dry_run.exit_quotes("2.40", "2.46"))
    account_id, database = dry_run.repo.account_id, dry_run.repo.db_path
    await close_synthetic_clerk_runtimes()
    database.rename(database.with_suffix(".saved"))

    rebuilt = ClerkSqliteRepository.rebuild_from_mirror(account_id=account_id, artifacts_root=tmp_path, clock=dry_run.clock)
    try:
        for order_ref in (bought.child_order_refs[0], closed.child_order_refs[-1]):
            assert order_contract(rebuilt, order_ref) == ContractTerms(symbol=SYMBOL, multiplier=100)
        with rebuilt._write_lock:
            records = effective_fill_records(rebuilt._conn, account_id=account_id)
        assert compute_fifo_pnl(records).exact_realized_pnl == Decimal("46")
        verify_database(rebuilt.db_path, expected_account_id=account_id)
    finally:
        rebuilt.close()


# ── what does not change, and who is still refused ───────────────────────


async def test_a_paper_account_refuses_an_option_entry_and_sends_nothing(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
) -> None:
    quote = _quote("1.90", "1.94", received_ms=NOON - 300)
    evidence = OptionEntryEvidence(
        picked={LEG: CALL}, multipliers={LEG: 100}, quotes={LEG: quote}, generation=GENERATION,
        rechecked_at_ms=NOON - 100, leg_gap_limit_percent=EIGHT, cost_limit_percent=TEN,
    )

    refused = await one_account.clerk.execute_for_instance(
        strategy_instance_id="b", run_id="run-b", decision_id="enter-1", purpose=EffectPurpose.ENTER,
        action_plan=BUYS_ONE_CALL, quantity=1, option_entry=evidence,
    )

    assert refused.state is EffectOperationState.REJECTED
    assert refused.explanation.startswith("OPTION_PLAN_NEEDS_A_DRY_RUN: "), refused.explanation
    assert one_account.trade.submitted_legs == []
    assert one_account.enter_commands() == 0


async def test_beneath_every_gate_the_clerks_own_send_takes_an_option_order_to_a_simulation_only() -> None:
    """Whatever let an option order get as far as the send, a real broker's port is not contacted with it."""
    from app.broker.alpaca.clerk.sqlite.claimed_broker_io import ClaimedBrokerIO
    from app.broker.contract.errors import BrokerOptionsNotSupported, BrokerUnavailable
    from app.broker.contract.models import OrderType
    from app.broker.contract.options import OptionPositionIntent, SingleOptionOrderRequest
    from tests.broker.alpaca.clerk.sqlite.test_claimed_broker_io import _Repo

    class _RealPort:
        broker_id = "alpaca"

        def __init__(self) -> None:
            self.sent: list[str] = []

        async def submit_option_order(self, order: Any, *, client_order_id: str) -> Any:
            self.sent.append(client_order_id)
            raise AssertionError("an option order reached a real broker's port")

    port = _RealPort()
    order = SingleOptionOrderRequest(
        contract=CALL, side=OrderSide.BUY, intent=OptionPositionIntent.OPEN, quantity=1,
        order_type=OrderType.LIMIT, limit_price=Decimal("1.94"),
    )
    io = ClaimedBrokerIO(repo=_Repo(lose_on_call=0), effect_operation_id="op-1", claim_token="tok", trade=port)  # type: ignore[arg-type]

    with pytest.raises(BrokerOptionsNotSupported) as refused:
        await io.submit(order, client_order_id="ref-1")

    # Definitive: nothing was sent, so there is no unknown outcome to settle.
    assert not isinstance(refused.value, BrokerUnavailable)
    assert port.sent == []


async def test_a_paper_account_refuses_to_register_a_bot_that_trades_options(
    one_account: _OneAccount,  # noqa: F811 -- the imported fixture
) -> None:
    binding = BrokerBotBinding(
        strategy_instance_id="options-on-paper", strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="trade", quantity=1, action_plan=BUYS_ONE_CALL, run_id="run-1", created_at_ms=0,
        sealed_account_id=one_account.repo.account_id, exit_terms=TERMS,
    )

    with pytest.raises(OptionPlanRefused) as refused:
        await one_account.clerk.register_strategy_run(binding)

    assert refused.value.reason.value == "OPTION_PLAN_NEEDS_A_DRY_RUN"
    assert one_account.repo.strategy_instance("options-on-paper") is None


async def test_a_dry_run_refuses_to_register_a_bot_whose_plan_it_does_not_take(tmp_path: Path) -> None:
    market_liveness.reset_market_liveness_store_for_testing()
    spread = _plan(
        _option_leg("long_call"),
        _option_leg("short_call", position="short", strike={"selector": "leg_distance", "leg_id": "long_call", "dollars": 5.0}),
    )
    try:
        account = await _open(tmp_path, _TestClock(NOON), _binding(spread))

        with pytest.raises(OptionPlanRefused) as refused:
            await account.clerk.register_strategy_run(account.binding)

        assert refused.value.reason.value == "OPTION_STRUCTURE_NOT_SUPPORTED"
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()


async def test_a_stock_dry_run_beside_it_buys_and_sells_as_it_always_did(tmp_path: Path) -> None:
    """The same harness, a bot in shares: its orders, fills and flat proof are a stock bot's own."""
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=100.0)
    clock = _TestClock(NOON)
    plan = alpaca_v1_action_plan("SPY")
    try:
        account = await _open(tmp_path, clock, _binding(plan))
        await account.clerk.register_strategy_run(account.binding)
        bars = account.authority.source_bars()
        try:
            bought_on: RetainedSourceBar = bars.append(_minute_bar("SPY", close="100.10"), run_id=RUN)
            sold_on: RetainedSourceBar = bars.append(
                _minute_bar("SPY", close="101.10", end_ms=NOON + 60_000), run_id=RUN
            )
        finally:
            bars.close()
        clock.value = NOON + 1_000

        bought = await account.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id="enter-1", purpose=EffectPurpose.ENTER,
            action_plan=plan, quantity=1, retained_source_bar=bought_on,
        )
        account.wait(60_000)
        sold = await account.clerk.execute_for_instance(
            strategy_instance_id=SID, run_id=RUN, decision_id="exit-1", purpose=EffectPurpose.EXIT,
            action_plan=plan, quantity=1, retained_source_bar=sold_on,
        )

        assert bought.state is EffectOperationState.SUBMITTED, bought.explanation
        assert sold.state is EffectOperationState.FLAT, sold.explanation
        assert account.fills() == [("SPY", "buy", 1.0, 100.10, None), ("SPY", "sell", 1.0, 101.10, None)]
        assert account.realized_pnl() == Decimal("1")
        for order_ref in (*bought.child_order_refs, sold.child_order_refs[-1]):
            assert order_contract(account.repo, order_ref) is None, "an order in shares trades no contract"
        assert account.clerk.synthesized_option_fills() == {}
    finally:
        await close_synthetic_clerk_runtimes()
        market_liveness.reset_market_liveness_store_for_testing()
