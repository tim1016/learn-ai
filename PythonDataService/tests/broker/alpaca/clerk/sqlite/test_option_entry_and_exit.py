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
from app.broker.alpaca.clerk.fifo_pnl import compute_fifo_pnl
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_CASH_EXCEEDED
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState, EffectPurpose
from app.broker.alpaca.clerk.option_effects import OptionEntryEvidence, OptionExitEvidence
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order, order_contract
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import OrderSide
from app.options.contract import OptionContract
from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide
from app.options.simulated_fill import SimulatedFillModel
from app.schemas.action_plan import ActionPlan
from app.schemas.deployment_budget import DeployBudgetConsent
from app.services import market_liveness
from app.services.bot_binding_authority import SyntheticBindingAuthority
from app.services.bot_binding_repository import BrokerBotBinding
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import TERMS

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
            quotes={LEG: _quote(bid, ask, received_ms=self.clock() - age_ms)}, generation=GENERATION,
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
    return _DryRun(clerk=runtime.clerk, clock=clock, authority=authority, binding=binding)


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
