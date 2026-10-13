"""A bot's option entries beside Alpaca's option buying power, as a page is served them (ADR 0076 decision 26).

Reads the Clerk's books and authors every word. Nothing is computed here but
the counts and one subtraction: each figure is the comparison's own record
(``ENTRY_BUYING_POWER_COMPARED``), and an entry with no record yet says so by
name. The counts are over every option entry of the bot, so an entry nobody
compared is never silent.
"""

from __future__ import annotations

from collections import Counter
from decimal import Decimal
from itertools import islice
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
    OptionBuyingPowerCounts,
    OptionBuyingPowerHold,
    OptionBuyingPowerHoldEntry,
    OptionBuyingPowerReading,
    OptionBuyingPowerReasonCount,
)

if TYPE_CHECKING:
    from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository

# How many of a bot's entries one answer lists, newest first. The counts are over all of them.
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
    NotComparedReason.FIRST_READING_NOT_READY: (
        "The account reading had not finished when the contract listing answered. The entry did not wait for it."
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
    NotComparedReason.ACCOUNT_NOT_CLEAN_AFTER: (
        "The check after the second reading found something on the account the books could not explain, "
        "so this change cannot be attributed to the entry."
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
    NotComparedReason.COMPARISON_FAILED: (
        "This entry's comparison failed, so it was recorded without judging the account's change."
    ),
    NotComparedReason.SIMULATION: (
        "This bot trades in a simulation. There is no Alpaca figure to compare with."
    ),
    NotComparedReason.NOT_COMPARED_YET: "The comparison for this entry has not been made yet.",
}

_REQUIREMENT_NOT_RECORDED = "The account's own option requirement was not recorded across this entry."

REVIEW_CONSEQUENCE = (
    "Once you have reviewed it, this bot may open new option entries again. Nothing is sent to Alpaca "
    "and no order changes."
)


def option_buying_power_checks(
    repo: ClerkSqliteRepository, *, strategy_instance_id: str, simulation: bool, judged: bool
) -> OptionBuyingPowerChecksResponse:
    """The bot's option entries, newest first, each with its comparison, and the notice holding the bot if one is."""
    with repo.write_fence() as conn:
        rows = conn.execute(
            "SELECT o.order_ref, o.effect_operation_id FROM orders o "
            "CROSS JOIN effect_operations e ON e.effect_operation_id = o.effect_operation_id "
            "WHERE o.role = 'ENTRY' AND e.strategy_instance_id = ? AND ("
            "EXISTS (SELECT 1 FROM order_contracts bound WHERE bound.order_ref=o.order_ref) OR "
            "EXISTS (SELECT 1 FROM option_order_legs leg WHERE leg.parent_order_ref=o.order_ref)) "
            "ORDER BY o.submitted_at_ms DESC, o.order_ref DESC",
            (strategy_instance_id,),
        ).fetchall()
        recorded = [_recorded(repo, row["effect_operation_id"]) for row in rows]
        cause = under_reserved_cause(repo, strategy_instance_id=strategy_instance_id)
        held = frozenset() if cause is None else frozenset(entry.order_ref for entry in cause.entries)
        checks = [
            check
            for row, facts in islice(zip(rows, recorded, strict=True), CHECKS_SERVED)
            if (
                check := _check(
                    repo, order_ref=row["order_ref"], facts=facts, simulation=simulation, held=held,
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
                review_consequence=REVIEW_CONSEQUENCE,
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
        counts=_counts(recorded, simulation=simulation),
        checks=checks,
    )


def _recorded(repo: ClerkSqliteRepository, effect_operation_id: str) -> EntryBuyingPowerComparedFacts | None:
    """One entry's comparison record, or ``None`` when none is on the books."""
    recorded = repo.first_effect_transition(effect_operation_id=effect_operation_id, transition_kind=COMPARED_KIND)
    return None if recorded is None else EntryBuyingPowerComparedFacts.from_facts_json(recorded["facts_json"])


def _counts(recorded: list[EntryBuyingPowerComparedFacts | None], *, simulation: bool) -> OptionBuyingPowerCounts:
    """How many entries came to a result and, for the rest, why not: an entry with no record counts as that."""
    unmade = NotComparedReason.SIMULATION if simulation else NotComparedReason.NOT_COMPARED_YET
    reasons: Counter[NotComparedReason] = Counter()
    for facts in recorded:
        if facts is None:
            reasons[unmade] += 1
        elif facts.reason_code is not None:
            reasons[NotComparedReason(facts.reason_code)] += 1
    total, not_compared = len(recorded), sum(reasons.values())
    compared = total - not_compared
    return OptionBuyingPowerCounts(
        entries=total,
        compared=compared,
        not_compared=not_compared,
        not_compared_by_reason=[
            OptionBuyingPowerReasonCount(reason_code=reason.value, sentence=REASON_SENTENCES[reason], count=count)
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0].value))
        ],
        sentence=(
            "This bot has made no option entry."
            if total == 0
            else f"{compared} of {total} option {'entry was' if total == 1 else 'entries were'} compared with "
            f"Alpaca's option buying power, {not_compared} not."
        ),
    )


def _check(
    repo: ClerkSqliteRepository, *, order_ref: str, facts: EntryBuyingPowerComparedFacts | None, simulation: bool,
    held: frozenset[str],
) -> OptionBuyingPowerCheck | None:
    entry = option_entry_of_order(repo, order_ref)
    if entry is None:
        return None
    set_aside = _amount(entry.money.entry_commitment_cents)
    if facts is None:
        reason = NotComparedReason.SIMULATION if simulation else NotComparedReason.NOT_COMPARED_YET
        return OptionBuyingPowerCheck(
            order_ref=order_ref, contract=", ".join(leg.symbol for leg in entry.leg_terms), result="unknown",
            result_label=RESULT_LABELS[BuyingPowerCheckResult.UNKNOWN], reason_code=reason.value,
            sentence=REASON_SENTENCES[reason], contracts_asked=sum(entry.units * leg.ratio for leg in entry.legs), contracts_filled=None,
            set_aside=set_aside, set_aside_for_filled=None, premium_paid=None, first_reading=None,
            second_reading=None, sent_at_ms=None, fall=None, compared_at_ms=None, holds=False,
            account_requirement_before=None, account_requirement_after=None, account_requirement_change=None,
            account_requirement_sentence=_REQUIREMENT_NOT_RECORDED,
        )
    result = BuyingPowerCheckResult(facts.result)
    return OptionBuyingPowerCheck(
        order_ref=order_ref,
        contract=", ".join(leg.symbol for leg in entry.leg_terms),
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
        account_requirement_before=_amount(facts.account_requirement_before_cents),
        account_requirement_after=_amount(facts.account_requirement_after_cents),
        account_requirement_change=_amount(_requirement_change(facts)),
        account_requirement_sentence=_requirement_sentence(facts),
    )


def _requirement_change(facts: EntryBuyingPowerComparedFacts) -> int | None:
    before, after = facts.account_requirement_before_cents, facts.account_requirement_after_cents
    return None if before is None or after is None else after - before


def _requirement_sentence(facts: EntryBuyingPowerComparedFacts) -> str:
    before, after = facts.account_requirement_before_cents, facts.account_requirement_after_cents
    if before is None or after is None:
        return _REQUIREMENT_NOT_RECORDED
    change = after - before
    moved = "did not change" if change == 0 else f"{'rose' if change > 0 else 'fell'} by ${dollars(abs(change))}"
    return (
        f"The account's option requirement, over every contract the books hold for its bots, {moved} across this "
        f"entry: ${dollars(before)} before and ${dollars(after)} after."
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
