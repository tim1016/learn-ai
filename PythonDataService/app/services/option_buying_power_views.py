"""A bot's option entries beside Alpaca's option buying power, as a page is served them (ADR 0076 decision 26).

Reads the Clerk's books and authors every word. Nothing is computed here:
each figure is the comparison's own record (``ENTRY_BUYING_POWER_COMPARED``),
and an entry with no record yet says so by name.
"""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

from app.broker.alpaca.clerk.money import dollars
from app.broker.alpaca.clerk.sqlite.entry_buying_power import COMPARED_KIND, NotComparedReason, under_reserved_cause
from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
from app.broker.alpaca.clerk.sqlite.option_entry import option_entry_of_order
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE
from app.options.buying_power_check import BuyingPowerCheckResult, whole_cents
from app.schemas.option_buying_power_check import (
    OptionBuyingPowerAmount,
    OptionBuyingPowerCheck,
    OptionBuyingPowerChecksResponse,
    OptionBuyingPowerHold,
    OptionBuyingPowerHoldEntry,
    OptionBuyingPowerReading,
)

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

# How many of a bot's entries one answer carries, newest first.
CHECKS_SERVED = 20

RESULT_LABELS: dict[BuyingPowerCheckResult, str] = {
    BuyingPowerCheckResult.CONSISTENT: "Consistent",
    BuyingPowerCheckResult.UNKNOWN: "Not compared",
    BuyingPowerCheckResult.OVER_RESERVED: "Over-reserved",
    BuyingPowerCheckResult.UNDER_RESERVED_UNEXPLAINED: "Under-reserved",
}

REASON_SENTENCES: dict[NotComparedReason, str] = {
    NotComparedReason.JUDGMENT_NOT_SWITCHED_ON: (
        "How Alpaca moves this figure has not been checked on this account yet, so the two readings were "
        "recorded and not judged."
    ),
    NotComparedReason.FIRST_READING_FAILED: (
        "Alpaca's account figures could not be read before the order was sent."
    ),
    NotComparedReason.FIRST_READING_WITHOUT_FIGURE: (
        "Alpaca's account answered before the order was sent and stated no option buying power."
    ),
    NotComparedReason.FIRST_READING_STALE: (
        "Alpaca's account figures were too old by the time the order was sent."
    ),
    NotComparedReason.SECOND_READING_FAILED: (
        "Alpaca's account figures could not be read after the entry's outcome was proven."
    ),
    NotComparedReason.SECOND_READING_WITHOUT_FIGURE: (
        "Alpaca's account answered after the entry and stated no option buying power."
    ),
    NotComparedReason.READING_NOT_WHOLE_CENTS: (
        "Alpaca stated its option buying power with a fraction of a cent, which is not compared."
    ),
    NotComparedReason.PREMIUM_NOT_WHOLE_CENTS: (
        "The fills recorded for this entry do not come to whole cents, so what it cost is not exact."
    ),
    NotComparedReason.OTHER_ORDER_MOVED: "Another order moved on the account at the same time.",
    NotComparedReason.CASH_MOVED: "Cash was moved into or out of the account between the two readings.",
    NotComparedReason.CASH_MOVEMENT_UNREAD: (
        "Whether cash moved on the account between the two readings could not be read."
    ),
    NotComparedReason.ACCOUNT_NOT_CHECKED_AFTER: (
        "The account was not checked against Alpaca soon enough after the second reading."
    ),
    NotComparedReason.ENTRY_BOUGHT_NOTHING: "The entry bought nothing, so there was no cost to compare.",
    NotComparedReason.UNFILLED_PART_RELEASE_NOT_VERIFIED: (
        "The entry filled in part and the rest was cancelled. Whether Alpaca had given back what it held for "
        "the unfilled part is not verified, and the figure moved by no more than the entry set aside in all."
    ),
    NotComparedReason.OUTCOME_NOT_PROVEN: "What the entry came to was not proven in time.",
    NotComparedReason.CLERK_RESTARTED: (
        "The account's Clerk restarted before the second reading, and the first was lost with it."
    ),
    NotComparedReason.SIMULATION: (
        "This bot trades in a simulation. There is no Alpaca figure to compare with."
    ),
    NotComparedReason.NOT_COMPARED_YET: "The comparison for this entry has not been made yet.",
}


def option_buying_power_checks(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, simulation: bool, judged: bool
) -> OptionBuyingPowerChecksResponse:
    """The bot's option entries, newest first, each with its comparison, and the notice holding the bot if one is."""
    with repo.write_fence() as conn:
        rows = conn.execute(
            "SELECT o.order_ref, o.effect_operation_id FROM order_contracts bound "
            "CROSS JOIN orders o ON o.order_ref = bound.order_ref "
            "CROSS JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
            "WHERE o.role = 'ENTRY' AND e.strategy_instance_id = ? "
            "ORDER BY o.submitted_at_ms DESC, o.order_ref DESC LIMIT ?",
            (strategy_instance_id, CHECKS_SERVED),
        ).fetchall()
        cause = under_reserved_cause(repo, strategy_instance_id=strategy_instance_id)
        held = frozenset() if cause is None else frozenset(entry.order_ref for entry in cause.entries)
        checks = [
            check
            for row in rows
            if (
                check := _check(
                    repo, order_ref=row["order_ref"], effect_operation_id=row["effect_operation_id"],
                    simulation=simulation, held=held,
                )
            )
            is not None
        ]
        notice = repo.active_uncertainty(
            scope="CUSTODY_SUBJECT", reason_code=OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE,
            strategy_instance_id=strategy_instance_id,
        )
    return OptionBuyingPowerChecksResponse(
        account_id=repo.account_id,
        strategy_instance_id=strategy_instance_id,
        simulation=simulation,
        judged=judged and not simulation,
        hold=(
            None
            if notice is None or cause is None
            else OptionBuyingPowerHold(
                headline=notice["headline"],
                explanation=notice["explanation"],
                impact=notice["operator_impact"],
                next_step=notice["next_step"],
                raised_at_ms=notice["observed_at_ms"],
                entries=[
                    OptionBuyingPowerHoldEntry(
                        order_ref=entry.order_ref, fall=_amount(entry.fall_cents),
                        set_aside=_amount(entry.set_aside_cents),
                    )
                    for entry in cause.entries
                ],
            )
        ),
        checks=checks,
    )


def _check(
    repo: ClerkSqliteRepository, *, order_ref: str, effect_operation_id: str, simulation: bool, held: frozenset[str]
) -> OptionBuyingPowerCheck | None:
    entry = option_entry_of_order(repo, order_ref)
    if entry is None:
        return None
    recorded = repo.first_effect_transition(effect_operation_id=effect_operation_id, transition_kind=COMPARED_KIND)
    set_aside = _amount(entry.money.entry_commitment_cents)
    if recorded is None:
        reason = NotComparedReason.SIMULATION if simulation else NotComparedReason.NOT_COMPARED_YET
        return OptionBuyingPowerCheck(
            order_ref=order_ref, contract=entry.contract_terms.symbol, result="unknown",
            result_label=RESULT_LABELS[BuyingPowerCheckResult.UNKNOWN], reason_code=reason.value,
            sentence=REASON_SENTENCES[reason], contracts_asked=entry.order.quantity, contracts_filled=None,
            set_aside=set_aside, set_aside_for_filled=None, premium_paid=None, first_reading=None,
            second_reading=None, sent_at_ms=None, fall=None, compared_at_ms=None, holds=False,
        )
    facts = EntryBuyingPowerComparedFacts.from_facts_json(recorded["facts_json"])
    result = BuyingPowerCheckResult(facts.result)
    return OptionBuyingPowerCheck(
        order_ref=order_ref,
        contract=entry.contract_terms.symbol,
        result=facts.result,
        result_label=RESULT_LABELS[result],
        reason_code=facts.reason_code,
        sentence=_sentence(facts, result, held=order_ref in held),
        contracts_asked=facts.contracts_asked,
        contracts_filled=facts.contracts_filled,
        set_aside=set_aside,
        set_aside_for_filled=_amount(facts.set_aside_for_filled_cents),
        premium_paid=_amount(facts.premium_paid_cents),
        first_reading=_reading(facts.first_began_ms, facts.first_ended_ms, facts.first_buying_power),
        second_reading=_reading(facts.second_began_ms, facts.second_ended_ms, facts.second_buying_power),
        sent_at_ms=facts.sent_at_ms,
        fall=_amount(facts.fall_cents),
        compared_at_ms=facts.compared_at_ms,
        holds=order_ref in held,
    )


def _sentence(facts: EntryBuyingPowerComparedFacts, result: BuyingPowerCheckResult, *, held: bool) -> str:
    if result is BuyingPowerCheckResult.UNKNOWN:
        assert facts.reason_code is not None
        return REASON_SENTENCES[NotComparedReason(facts.reason_code)]
    assert facts.fall_cents is not None and facts.set_aside_for_filled_cents is not None
    assert facts.cost_of_filled_cents is not None
    moved = (
        f"fell ${dollars(facts.fall_cents)}" if facts.fall_cents >= 0 else f"rose ${dollars(-facts.fall_cents)}"
    )
    across = f"Alpaca's option buying power {moved} across this entry"
    if result is BuyingPowerCheckResult.CONSISTENT:
        return f"{across}, which is what the contracts that filled cost."
    if result is BuyingPowerCheckResult.OVER_RESERVED:
        return (
            f"{across}. The contracts that filled cost ${dollars(facts.cost_of_filled_cents)}. The bot had set "
            f"aside ${dollars(facts.set_aside_for_filled_cents)} for the contracts that filled, so Alpaca took "
            "no more than was set aside."
        )
    ending = (
        "This bot will open nothing new until you have reviewed this."
        if held
        else "The notice this raised has been reviewed."
    )
    # Held to everything the entry set aside, which for an entry that filled whole is what filled.
    return (
        f"{across}. The bot had set aside ${dollars(facts.commitment_cents)} for the whole entry. "
        f"No other order moved in between. {ending}"
    )


def _amount(cents: int | None) -> OptionBuyingPowerAmount | None:
    return None if cents is None else OptionBuyingPowerAmount(cents=cents, dollars=dollars(cents))


def _reading(began_ms: int | None, ended_ms: int | None, stated: str | None) -> OptionBuyingPowerReading | None:
    if began_ms is None or ended_ms is None:
        return None
    cents = None if stated is None else whole_cents(Decimal(stated))
    return OptionBuyingPowerReading(began_ms=began_ms, ended_ms=ended_ms, buying_power=_amount(cents))


__all__ = ["CHECKS_SERVED", "REASON_SENTENCES", "RESULT_LABELS", "option_buying_power_checks"]
