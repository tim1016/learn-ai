"""The account Clerk works out an entry of two to four legs, and what its reservation claims (#2913).

Money is asserted to the cent, each figure by hand. The fee rates in force
on the test's day, 2026-09-08: SEC $0.0000206 a dollar sold, TAF $0.00329 a
contract sold, CAT $0.0003 a contract, ORF $0.015 and OCC $0.025 a contract.
Each fee type is summed over the entry's fills and rounded up to the cent.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.alpaca.clerk.live_envelope import EnvelopeReservation
from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal
from app.broker.alpaca.clerk.sqlite.enter import EnterSubmission, _reserved, accept_option_enter
from app.broker.alpaca.clerk.sqlite.envelope_reservations import (
    EntryCashClaim,
    append_envelope_reservation_row,
    entry_cash_claims,
)
from app.broker.alpaca.clerk.sqlite.option_admission import (
    HeldByAnother,
    OptionEntryRefused,
    held_on_the_other_side,
    work_out_option_entry,
)
from app.broker.alpaca.clerk.sqlite.option_entry import MultiLegOptionEntryFacts, OptionEntryFacts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.models import OrderSide, OrderType
from app.broker.contract.options import MultiLegOptionOrderRequest
from app.schemas.action_plan import ActionPlan
from tests.broker.alpaca.clerk.sqlite.conftest import NOON
from tests.broker.alpaca.clerk.sqlite.several_legs_fixtures import (
    PAID_PICKS,
    PAID_SPREAD,
    RECEIVED_PICKS,
    RECEIVED_SPREAD,
    STRADDLE,
    STRADDLE_PICKS,
    Pick,
    away_from,
    call,
    evidence,
    leg,
    plan,
    put,
    worked_entry,
)

IRON_CONDOR = plan(
    leg("short_put", position="short", right="put"),
    leg("long_put", right="put", strike=away_from("short_put", -5.0)),
    leg("short_call", position="short", strike=away_from("short_put", 10.0)),
    leg("long_call", strike=away_from("short_call", 5.0)),
)
IRON_CONDOR_PICKS: Mapping[str, Pick] = {
    "short_put": (put(580), "2.10", "2.14"), "long_put": (put(575), "1.30", "1.34"),
    "short_call": (call(590), "1.20", "1.24"), "long_call": (call(595), "0.50", "0.54"),
}


def _worked_out(
    action_plan: ActionPlan, picks: Mapping[str, Pick], *, units: int = 1, **changes: object
) -> MultiLegOptionEntryFacts | OptionEntryFacts | OptionEntryRefused:
    return work_out_option_entry(
        action_plan, evidence=evidence(NOON, picks, **changes), units=units, now_ms=NOON, real_account=False
    )


def _refused(action_plan: ActionPlan, picks: Mapping[str, Pick], **changes: object) -> OptionEntryRefused:
    refused = _worked_out(action_plan, picks, **changes)
    assert isinstance(refused, OptionEntryRefused), refused
    return refused


# ── what admission makes of a structure ───────────────────────────────────


def test_the_paid_spread_is_one_order_of_two_legs_at_the_net_price_it_pays() -> None:
    entry = _worked_out(PAID_SPREAD, PAID_PICKS, units=2)

    assert isinstance(entry, MultiLegOptionEntryFacts)
    order = entry.order
    assert isinstance(order, MultiLegOptionOrderRequest)
    # Bought at its ask, $3.14; sold at its bid, $1.20: $1.94 paid a share, positive.
    assert (order.quantity, order.order_type, order.net_limit_price) == (2, OrderType.LIMIT, Decimal("1.94"))
    assert [(each.contract, each.side, each.ratio) for each in order.legs] == [
        (call(585), OrderSide.BUY, 1), (call(590), OrderSide.SELL, 1),
    ]
    # 2 structures x $1.94 x 100 = $388.00 of premium; no requirement; the most it can lose.
    assert entry.money.net_entry_premium_cents == 38_800
    assert entry.money.broker_requirement_cents == 0
    assert entry.money.loss_at_expiry_cents == 38_800
    # Fees for two contracts bought and two sold. SEC: 2 x $1.20 x 100 x 0.0000206 = $0.004944 -> 1 cent.
    # TAF: 2 x $0.00329 -> 1. CAT: 4 x $0.0003 -> 1. ORF: 4 x $0.015 = 6. OCC: 4 x $0.025 = 10. Together 19.
    assert entry.money.fee_provision_cents == 19
    assert entry.money.entry_commitment_cents == 38_819


def test_the_received_spread_is_sent_at_a_net_price_below_zero_and_sets_aside_its_requirement_less_its_premium() -> None:
    entry = _worked_out(RECEIVED_SPREAD, RECEIVED_PICKS)

    assert isinstance(entry, MultiLegOptionEntryFacts)
    # Sold at its bid, $5.31; bought at its ask, $2.24: $3.07 received a share, below zero.
    assert entry.order.net_limit_price == entry.net_limit_price == Decimal("-3.07")
    assert [(each.contract, each.side) for each in entry.order.legs] == [
        (call(580), OrderSide.SELL), (call(585), OrderSide.BUY),
    ]
    # $307.00 received against the $5 between the strikes, $500.00: $193.00 set aside, and the most it can lose.
    assert entry.money.net_entry_premium_cents == -30_700
    assert entry.money.broker_requirement_cents == 50_000
    assert entry.money.loss_at_expiry_cents == 19_300
    # SEC: $5.31 x 100 x 0.0000206 = $0.01093866 -> 2 cents. TAF -> 1. CAT: 2 x $0.0003 -> 1. ORF 3. OCC: $0.025 up to 3 a leg, 6. Together 13.
    assert entry.money.fee_provision_cents == 13
    assert entry.money.entry_commitment_cents == 19_313
    assert entry.unit_set_aside == Decimal("193.00")


def test_the_fee_provision_of_a_four_leg_entry_is_every_legs_fill_settled_as_one_day() -> None:
    one, three = _worked_out(IRON_CONDOR, IRON_CONDOR_PICKS), _worked_out(IRON_CONDOR, IRON_CONDOR_PICKS, units=3)

    assert isinstance(one, MultiLegOptionEntryFacts) and isinstance(three, MultiLegOptionEntryFacts)
    assert len(one.legs) == 4
    # One unit: $2.10 + $1.20 sold = $330.00. SEC: 330 x 0.0000206 = $0.006798 -> 1 cent.
    # TAF: 2 sold x $0.00329 = $0.00658 -> 1. CAT: 4 x $0.0003 = $0.0012 -> 1. ORF: 4 x $0.015 = 6. OCC: $0.025 up to 3 a leg, 12.
    assert one.money.fee_provision_cents == 1 + 1 + 1 + 6 + 12
    # Three units: SEC: 990 x 0.0000206 = $0.020394 -> 3. TAF: 6 x $0.00329 = $0.01974 -> 2.
    # CAT: 12 x $0.0003 = $0.0036 -> 1. ORF: 12 x $0.015 = 18. OCC: 3 x $0.025 = $0.075 up to 8 a leg, 32.
    assert three.money.fee_provision_cents == 3 + 2 + 1 + 18 + 32
    # Bought at $1.34 and $0.54, sold at $2.10 and $1.20: $1.42 received a share.
    assert one.net_limit_price == Decimal("-1.42")
    # One side can lose at a time: the requirement is the $5 between one side's strikes, $500.00.
    assert (one.money.broker_requirement_cents, one.money.net_entry_premium_cents) == (50_000, -14_200)
    assert one.unit_set_aside == Decimal("358.00")


@pytest.mark.parametrize(
    ("action_plan", "picks", "units"),
    [(PAID_SPREAD, PAID_PICKS, 2), (RECEIVED_SPREAD, RECEIVED_PICKS, 1), (STRADDLE, STRADDLE_PICKS, 3)],
    ids=["paid", "received", "two bought legs"],
)
def test_admission_makes_the_entry_the_canonical_functions_make(
    action_plan: ActionPlan, picks: Mapping[str, Pick], units: int
) -> None:
    assert _worked_out(action_plan, picks, units=units) == worked_entry(action_plan, picks, units=units, now_ms=NOON)


def test_a_plan_of_one_leg_still_makes_one_order_of_one_contract() -> None:
    entry = _worked_out(plan(leg("long_call")), {"long_call": (call(585), "1.90", "1.94")}, units=2)

    assert isinstance(entry, OptionEntryFacts)
    # One buy of two contracts: CAT -> 1 cent, ORF 2 x $0.015 = 3, OCC 2 x $0.025 = 5. No SEC and no TAF on a buy.
    assert entry.money.fee_provision_cents == 9
    assert _reserved(entry) == (2, Decimal("1.94"))


def test_what_the_reservation_keeps_is_units_and_the_dollars_one_unit_sets_aside() -> None:
    paid, received = _worked_out(PAID_SPREAD, PAID_PICKS, units=2), _worked_out(RECEIVED_SPREAD, RECEIVED_PICKS, units=2)

    assert isinstance(paid, MultiLegOptionEntryFacts) and isinstance(received, MultiLegOptionEntryFacts)
    assert _reserved(paid) == (2, Decimal("194.00"))
    # Above zero for a structure that receives its premium, where the net limit is below it.
    assert _reserved(received) == (2, Decimal("193.00"))


# ── the refusals, each by its name ────────────────────────────────────────


def test_a_net_price_of_exactly_zero_is_refused_by_name_before_any_order_is_made() -> None:
    # Bought at $1.20 and sold at $1.20: nothing paid, nothing received.
    no_requirement = _refused(PAID_SPREAD, {"long_call": (call(585), "1.16", "1.20"), "short_call": (call(590), "1.20", "1.24")})
    # The same of a structure with a requirement, whose money would otherwise be admitted: $500.00 set aside.
    with_requirement = _refused(
        RECEIVED_SPREAD, {"short_call": (call(580), "2.24", "2.28"), "long_call": (call(585), "2.20", "2.24")}
    )

    for refused in (no_requirement, with_requirement):
        assert refused.reason_code == OptionEffectRefusal.ENTRY_NET_PRICE_ZERO.value == "OPTION_ENTRY_NET_PRICE_ZERO"


def test_a_structure_whose_legs_do_not_share_an_expiry_and_has_a_sold_leg_is_refused_under_the_money_rules_name() -> None:
    from datetime import date

    later = date(2026, 10, 16)
    picks = {"short_call": (call(580), "5.31", "5.35"), "long_call": (call(585, expiry=later), "4.20", "4.24")}

    assert _refused(RECEIVED_SPREAD, picks).reason_code == "OPTION_STRUCTURE_MIXED_EXPIRY_WITH_SOLD_LEG"


def test_a_leg_handed_over_with_no_quote_is_refused_and_the_words_name_the_legs() -> None:
    handed = evidence(NOON, PAID_PICKS)
    without = {leg_id: held for leg_id, held in handed.quotes.items() if leg_id != "short_call"}

    refused = _refused(PAID_SPREAD, PAID_PICKS, quotes=without)

    assert refused.reason_code == OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value
    assert "'long_call'" in refused.explanation and "'short_call'" in refused.explanation


def test_a_sold_leg_needs_a_bid_and_the_refusal_names_its_contract() -> None:
    from app.options.quotes import HeldQuote, QuoteDataType, QuoteSide

    handed = evidence(NOON, PAID_PICKS)
    no_bid = HeldQuote(bid=None, ask=QuoteSide(Decimal("1.24"), NOON - 300), data_type=QuoteDataType.LIVE, generation=4)

    refused = _refused(PAID_SPREAD, PAID_PICKS, quotes={**handed.quotes, "short_call": no_bid})

    assert call(590).occ_symbol in refused.explanation
    assert refused.reason_code == "QUOTE_ONE_SIDED"


def test_each_leg_is_asked_in_turn_and_the_first_no_names_its_contract() -> None:
    not_standard = _refused(PAID_SPREAD, PAID_PICKS, multipliers={"long_call": 100, "short_call": 10})
    finer = _refused(PAID_SPREAD, {**PAID_PICKS, "short_call": (call(590), "1.205", "1.24")})

    assert not_standard.reason_code == OptionEffectRefusal.CONTRACT_NOT_STANDARD.value
    assert call(590).occ_symbol in not_standard.explanation
    assert finer.reason_code == OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value
    assert call(590).occ_symbol in finer.explanation


def test_a_real_account_is_refused_under_the_settings_code_while_the_setting_is_off() -> None:
    refused = work_out_option_entry(
        PAID_SPREAD, evidence=evidence(NOON, PAID_PICKS), units=1, now_ms=NOON, real_account=True
    )

    assert isinstance(refused, OptionEntryRefused)
    assert refused.reason_code == "OPTION_SEVERAL_LEGS_SETTING_OFF"


# ── a contract the account holds on the other side ────────────────────────


def test_an_entry_that_would_sell_what_another_bot_holds_bought_or_buy_what_it_holds_sold_is_refused_by_name() -> None:
    entry = _worked_out(PAID_SPREAD, PAID_PICKS)
    assert isinstance(entry, MultiLegOptionEntryFacts)
    bought, sold = call(585).occ_symbol, call(590).occ_symbol

    # The entry sells the 590 call. Another bot holds it bought.
    sells_theirs = held_on_the_other_side(entry, [HeldByAnother(sold, "bot-b", 2.0)])
    # The entry buys the 585 call. The owner's own orders hold it sold.
    buys_theirs = held_on_the_other_side(entry, [HeldByAnother(bought, None, -1.0)])

    for refused, holder in ((sells_theirs, "bot-b"), (buys_theirs, "owner")):
        assert refused is not None
        assert refused.reason_code == "OPTION_CONTRACT_HELD_ON_THE_OTHER_SIDE"
        assert holder in refused.explanation
    assert sold in sells_theirs.explanation and bought in buys_theirs.explanation  # type: ignore[union-attr]


def test_an_entry_on_the_same_side_as_another_holding_or_in_other_contracts_is_taken() -> None:
    entry = _worked_out(PAID_SPREAD, PAID_PICKS)
    assert isinstance(entry, MultiLegOptionEntryFacts)

    same_side = [HeldByAnother(call(585).occ_symbol, "bot-b", 1.0), HeldByAnother(call(590).occ_symbol, "bot-b", -1.0)]
    elsewhere = [HeldByAnother(call(600).occ_symbol, "bot-b", -3.0)]

    assert held_on_the_other_side(entry, same_side) is None
    assert held_on_the_other_side(entry, elsewhere) is None
    assert held_on_the_other_side(entry, []) is None


# ── what the reservation claims ───────────────────────────────────────────


@pytest.fixture
def books(tmp_path: Path) -> ClerkSqliteRepository:
    from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import open_option_books

    repo = open_option_books(tmp_path, bots={"bot-a": "SPY"})
    yield repo
    repo.close()


def _reserve(repo: ClerkSqliteRepository, entry: MultiLegOptionEntryFacts) -> EnterSubmission:
    """Accept the entry and write its reservation as an admitted ENTER's fold writes it: units and dollars a unit."""
    from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import ACCOUNT_ID, RUN_ID

    accepted = accept_option_enter(
        repo, account_id=ACCOUNT_ID, strategy_instance_id="bot-a", decision_id="d1", lifecycle_run_id=RUN_ID, entry=entry
    )
    quantity, set_aside = _reserved(entry)
    with repo._write_lock:
        append_envelope_reservation_row(
            repo._conn, effect_operation_id=accepted.effect_operation_id,
            reservation=EnvelopeReservation(
                quantity=float(quantity), reference_price=float(set_aside), exact_reference_price=str(set_aside),
                fee_provision_cents=entry.money.fee_provision_cents,
            ),
            reserved_at_ms=NOON,
        )
        repo._conn.commit()
    return accepted


def _claim(repo: ClerkSqliteRepository, *, seen_before_ms: int) -> EntryCashClaim:
    with repo._write_lock:
        [claim] = entry_cash_claims(repo._conn, seen_before_ms=seen_before_ms)
    return claim


def _fill(repo: ClerkSqliteRepository, accepted: EnterSubmission, leg_number: int, *, symbol: str, side: str, price: float, n: str) -> None:
    from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import fill
    from tests.broker.alpaca.clerk.sqlite.test_several_legs_books import _leg_submission

    fill(
        repo, _leg_submission(accepted, leg_number), execution_id=f"x-{leg_number}-{n}", symbol=symbol, side=side,
        quantity=1, price=price,
    )


def _end(repo: ClerkSqliteRepository, accepted: EnterSubmission, *, state: str) -> None:
    from tests.broker.alpaca.clerk.sqlite.test_several_legs_books import _acknowledge

    _acknowledge(repo, accepted, accepted.order_ref or "", state=state, broker_id="parent-1")


def test_the_received_spreads_claim_before_the_fill_after_one_unit_of_two_and_after_the_fill(
    books: ClerkSqliteRepository,
) -> None:
    entry = _worked_out(RECEIVED_SPREAD, RECEIVED_PICKS, units=2)
    assert isinstance(entry, MultiLegOptionEntryFacts)
    accepted = _reserve(books, entry)
    sold, bought = call(580).occ_symbol, call(585).occ_symbol
    never_seen = NOON - 1

    # Before the fill: two units x $193.00, and the whole provision. No contract's hundred is taken.
    before = _claim(books, seen_before_ms=never_seen)
    assert (before.unfilled_cost, before.unseen_fill_cost) == (Decimal("386.00"), Decimal(0))
    assert before.unfilled_fee == Decimal(entry.money.fee_provision_cents) / 100
    # That is the commitment that was admitted.
    assert int((before.unfilled_cost + before.unfilled_fee) * 100) == entry.money.entry_commitment_cents == 38_600 + 21

    # One unit of two: one contract of each leg.
    _fill(books, accepted, 1, symbol=sold, side="SELL", price=5.31, n="a")
    _fill(books, accepted, 2, symbol=bought, side="BUY", price=2.24, n="a")
    half = _claim(books, seen_before_ms=never_seen)
    # One unit still to fill: $193.00, and the whole provision while any unit is unfilled.
    assert half.unfilled_cost == Decimal("193.00")
    assert half.unfilled_fee == before.unfilled_fee
    # The cash reading has not seen the fills: the bought contract's cost, 1 x $2.24 x 100, is claimed.
    # The sold contract's $531.00 is cash in, and is counted where every unseen sale is, not here.
    assert half.unseen_fill_cost == Decimal("224.00")

    # Out of step for a moment: the sold leg's second contract alone adds no unit.
    _fill(books, accepted, 1, symbol=sold, side="SELL", price=5.31, n="b")
    assert _claim(books, seen_before_ms=never_seen).unfilled_cost == Decimal("193.00")

    _fill(books, accepted, 2, symbol=bought, side="BUY", price=2.24, n="b")
    filled = _claim(books, seen_before_ms=never_seen)
    assert (filled.unfilled_cost, filled.unfilled_fee, filled.unseen_fill_cost) == (Decimal(0), Decimal(0), Decimal("448.00"))
    # Once the cash reading has seen the fills, nothing of this entry is claimed.
    seen = _claim(books, seen_before_ms=NOON + 3_600_000)
    assert (seen.unfilled_cost, seen.unfilled_fee, seen.unseen_fill_cost) == (Decimal(0), Decimal(0), Decimal(0))


def test_the_paid_spreads_claim_is_units_times_what_one_sets_aside_and_comes_down_by_whole_units(
    books: ClerkSqliteRepository,
) -> None:
    entry = _worked_out(PAID_SPREAD, PAID_PICKS, units=2)
    assert isinstance(entry, MultiLegOptionEntryFacts)
    accepted = _reserve(books, entry)

    assert _claim(books, seen_before_ms=NOON - 1).unfilled_cost == Decimal("388.00")
    _fill(books, accepted, 1, symbol=call(585).occ_symbol, side="BUY", price=3.14, n="a")
    # The bought leg alone is no whole unit: nothing comes off, and its $314.00 is an unseen purchase.
    one_leg = _claim(books, seen_before_ms=NOON - 1)
    assert (one_leg.unfilled_cost, one_leg.unseen_fill_cost) == (Decimal("388.00"), Decimal("314.00"))
    _fill(books, accepted, 2, symbol=call(590).occ_symbol, side="SELL", price=1.20, n="a")
    assert _claim(books, seen_before_ms=NOON - 1).unfilled_cost == Decimal("194.00")


def test_an_ended_parent_claims_nothing_for_what_did_not_fill(books: ClerkSqliteRepository) -> None:
    entry = _worked_out(PAID_SPREAD, PAID_PICKS, units=2)
    assert isinstance(entry, MultiLegOptionEntryFacts)
    accepted = _reserve(books, entry)
    _fill(books, accepted, 1, symbol=call(585).occ_symbol, side="BUY", price=3.14, n="a")
    _fill(books, accepted, 2, symbol=call(590).occ_symbol, side="SELL", price=1.20, n="a")

    _end(books, accepted, state="canceled")

    ended = _claim(books, seen_before_ms=NOON - 1)
    # The unit that did not fill is released with its provision; the fill the reading has not seen is still claimed.
    assert (ended.unfilled_cost, ended.unfilled_fee, ended.unseen_fill_cost) == (Decimal(0), Decimal(0), Decimal("314.00"))
