"""An option entry on a Paper account, compared with the account's option buying power (#2913).

The real SQLite Clerk of a Paper account, on real books, over the resting
venue of ``resting_entry_harness``. The account here also states an option
buying power, which each test sets: $100,000.00 before the entry, and
whatever the test says Alpaca shows afterwards. Nothing was sent to Alpaca
or recorded from it: how the figure really moves is not known, and these
tests show what the Clerk does with each way it could.

One call at a $1.94 limit sets aside $194.06: $194.00 of premium and six
cents of fees.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import set_active_clerk_runtime
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.models import EffectOperationReceipt, EffectOperationState
from app.broker.alpaca.clerk.sqlite import entry_buying_power
from app.broker.alpaca.clerk.sqlite.entry_buying_power import (
    COMPARED_KIND,
    FIRST_READING_MAX_AGE_MS,
    OUTCOME_PROOF_BOUND_MS,
    PASS_AFTER_SECOND_READING_BOUND_MS,
    REVIEWED_SUMMARY_CODE,
    NotComparedReason,
    _another_order_moved,
    under_reserved_cause,
)
from app.broker.alpaca.clerk.sqlite.entry_outcome import EntryStanding
from app.broker.alpaca.clerk.sqlite.external_orders import observe_external_order
from app.broker.alpaca.clerk.sqlite.facts import EntryBuyingPowerComparedFacts
from app.broker.alpaca.clerk.sqlite.uncertainty import admit_new_exposure
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerOrder, BrokerPosition
from app.broker.contract.options import BrokerOptionAccountSnapshot
from app.config import settings
from app.options.buying_power_check import BuyingPowerCheckResult
from app.routers.alpaca_clerk_sqlite import _option_buying_power_checks as served
from app.routers.alpaca_clerk_sqlite import router
from app.schemas.option_buying_power_check import OptionBuyingPowerNotComparedReason, OptionBuyingPowerResult
from app.services import market_liveness
from app.services.option_buying_power_views import REASON_SENTENCES, RESULT_LABELS
from tests._helpers.market_quotes import publish_quotes
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _FakeTradePort, _TestClock
from tests.broker.alpaca.clerk.sqlite.resting_entry_harness import (
    WAIT_MS,
    PaperAccountRead,
    PaperOptions,
    _compose,
)
from tests.broker.alpaca.clerk.sqlite.test_budget_commands import _deploy, _new_budget_repo
from tests.broker.alpaca.clerk.sqlite.test_live_envelope_sync import _cash_flow
from tests.broker.alpaca.clerk.sqlite.test_option_entry_and_exit import (
    _DryRun,
    dry_run,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.alpaca.clerk.sqlite.test_two_bots_one_symbol import (
    _Account,
    _OneAccount,
    one_account,  # noqa: F401 -- pytest fixture, used by name
)

SET_ASIDE = 19_406
BEFORE = Decimal("100000")
HOLD = OPTION_BUYING_POWER_UNDER_RESERVED_REASON_CODE


class OptionAccount(PaperAccountRead):
    """The Paper account, which also states an option buying power: the figure the test last set."""

    def __init__(self, venue: _FakeTradePort) -> None:
        super().__init__(venue)
        self.buying_power = BEFORE
        self.option_account_error: Exception | None = None
        self.option_account_never_answers = False
        # Raised by the read of the account's positions while set: a pass then reaches no verdict.
        self.positions_error: Exception | None = None

    async def list_positions(self) -> list[BrokerPosition]:
        if self.positions_error is not None:
            raise self.positions_error
        return await super().list_positions()

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        if self.option_account_never_answers:
            self.option_account_reads += 1
            await asyncio.Event().wait()
        if self.option_account_error is not None:
            self.option_account_reads += 1
            raise self.option_account_error
        return await super().get_option_account()


class Paper(PaperOptions):
    """The harness's Paper account, with what these tests ask of it."""

    account: OptionAccount

    async def filled(self, *, price: str = "1.94", shows: str | None, sid: str = "a") -> str:
        """An entry of one contract that rests, fills at ``price``, after which Alpaca shows ``shows`` dollars."""
        order_ref = await self.resting(sid)
        self.wait(1_000)
        await self.stream_fill(order_ref, contracts=1, filled_in_all=1, price=price, execution_id=f"x-{order_ref}")
        self.account.buying_power = None if shows is None else Decimal(shows)
        return order_ref

    async def pass_and_round(self, *, after_ms: int = 15_000) -> tuple[str, ...]:
        """Time passes, the sweep's pass is published, and the comparison's round runs once."""
        self.wait(after_ms)
        self.clerk.publish_reconciliation(await self.pass_())
        return await self.clerk.compare_entries_with_buying_power()

    async def compared(self, order_ref: str) -> EntryBuyingPowerComparedFacts:
        """Two passes and two rounds: the second reading, then the judgment. The entry's record."""
        await self.pass_and_round()
        await self.pass_and_round()
        return self.comparison(order_ref)

    def comparison(self, order_ref: str) -> EntryBuyingPowerComparedFacts:
        recorded = self.comparisons(order_ref)
        assert len(recorded) == 1, f"{len(recorded)} comparisons are recorded for {order_ref}"
        return recorded[0]

    def comparisons(self, order_ref: str) -> list[EntryBuyingPowerComparedFacts]:
        order = self.repo.order(order_ref)
        assert order is not None
        with self.repo.write_fence() as conn:
            rows = conn.execute(
                "SELECT facts_json FROM custody_transitions WHERE effect_operation_id = ? AND transition_kind = ? "
                "ORDER BY sequence",
                (order.effect_operation_id, COMPARED_KIND),
            ).fetchall()
        return [EntryBuyingPowerComparedFacts.from_facts_json(row[0]) for row in rows]

    def notices(self, sid: str = "a") -> list[str]:
        """The reason codes of the notices open on one bot."""
        return [
            notice["reason_code"]
            for notice in self.repo.active_uncertainties()
            if notice["strategy_instance_id"] == sid
        ]


@pytest.fixture
async def paper(tmp_path: Path) -> AsyncIterator[Paper]:
    """A Paper account at noon on a trading day, two bots of $500.00 each, $100,000.00 of option buying power."""
    market_liveness.reset_market_liveness_store_for_testing()
    publish_quotes(NOON, SPY=585.0)
    repo = _new_budget_repo(tmp_path)
    _deploy(repo, "a", 50_000)
    _deploy(repo, "b", 50_000)
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    venue = _FakeTradePort()
    venue.venue_clock = clock
    account = OptionAccount(venue)
    sync, keeper, clerk = _compose(repo, clock, venue, account)
    assert await sync.tick() == "observed"
    clock.advance(1_000)
    opened = Paper(
        repo=repo, clock=clock, clerk=clerk, venue=venue, account=account, keeper=keeper, sync=sync,
        artifacts_root=tmp_path,
    )
    try:
        yield opened
    finally:
        await opened.keeper.stop()
        await opened.sync.stop()
        await opened.clerk.drain_effects()
        opened.repo.close()
        market_liveness.reset_market_liveness_store_for_testing()


@pytest.fixture
def judged(monkeypatch: pytest.MonkeyPatch) -> None:
    """The account's setting says Alpaca has been seen to move the figure by an order's cost."""
    monkeypatch.setattr(settings, "ALPACA_OPTION_BUYING_POWER_JUDGED", True)


def _refused(receipt: EffectOperationReceipt, code: str) -> None:
    assert receipt.state is EffectOperationState.REJECTED, receipt.explanation
    assert receipt.explanation.startswith(f"{code}: "), receipt.explanation


# ── the four results ─────────────────────────────────────────────────────


@pytest.mark.usefixtures("judged")
async def test_a_fall_of_exactly_the_premium_paid_is_consistent_and_the_fee_provision_raises_nothing(
    paper: Paper,
) -> None:
    order_ref = await paper.filled(price="1.94", shows="99806.00")

    found = await paper.compared(order_ref)

    # $194.00 fell; $194.06 was set aside. The six cents of fees Alpaca has not taken are not a difference.
    assert (found.result, found.reason_code, found.holds) == ("consistent", None, False)
    assert (found.fall_cents, found.premium_paid_cents, found.cost_of_filled_cents) == (19_400, 19_400, 19_400)
    assert (found.commitment_cents, found.set_aside_for_filled_cents) == (SET_ASIDE, SET_ASIDE)
    assert (found.contracts_asked, found.contracts_filled) == (1, 1)
    assert (found.first_buying_power, found.second_buying_power) == ("100000", "99806.00")
    assert paper.notices() == []


@pytest.mark.usefixtures("judged")
async def test_a_fill_better_than_the_limit_is_compared_with_what_was_paid_not_with_the_limit(paper: Paper) -> None:
    order_ref = await paper.filled(price="1.93", shows="99807.00")

    found = await paper.compared(order_ref)

    assert (found.result, found.fall_cents, found.premium_paid_cents) == ("consistent", 19_300, 19_300)
    assert found.set_aside_for_filled_cents == SET_ASIDE


@pytest.mark.usefixtures("judged")
@pytest.mark.parametrize(
    ("shows", "fall_cents"),
    [("99805.99", 19_401), ("99805.94", SET_ASIDE), ("99806.01", 19_399), ("100000", 0), ("100050.00", -5_000)],
    ids=["a cent over the premium", "exactly the set-aside", "a cent under the premium", "no fall", "a rise"],
)
async def test_a_fall_that_is_not_the_cost_and_not_above_the_set_aside_is_over_reserved_and_holds_nothing(
    paper: Paper, shows: str, fall_cents: int
) -> None:
    order_ref = await paper.filled(shows=shows)

    found = await paper.compared(order_ref)

    assert (found.result, found.fall_cents, found.holds) == ("over_reserved", fall_cents, False)
    assert paper.notices() == []
    # Nothing is held: the bot's exit and its next entry go on as before.
    sold = await paper.exit()
    assert sold.state is not EffectOperationState.REJECTED, sold.explanation


@pytest.mark.usefixtures("judged")
async def test_a_fall_of_more_than_the_bot_set_aside_holds_that_bots_entries_and_nothing_else(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")

    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code, found.holds) == ("under_reserved_unexplained", None, True)
    assert (found.fall_cents, found.set_aside_for_filled_cents) == (25_000, SET_ASIDE)
    # A notice of the bot's own, naming both figures.
    assert paper.notices() == [HOLD] and paper.notices("b") == []
    cause = under_reserved_cause(paper.repo, strategy_instance_id="a")
    assert cause is not None
    assert [(e.order_ref, e.fall_cents, e.set_aside_cents) for e in cause.entries] == [(order_ref, 25_000, SET_ASIDE)]
    [notice] = [n for n in paper.repo.active_uncertainties() if n["reason_code"] == HOLD]
    assert "250.00" in notice["explanation"] and "194.06" in notice["explanation"]
    # Its exit still goes out and closes what it holds.
    sold = await paper.exit()
    assert sold.state is not EffectOperationState.REJECTED, sold.explanation
    closing_ref = next(ref for ref in sold.child_order_refs if ref != order_ref)
    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=1, price="1.90", execution_id="x-close")
    await paper.pass_()
    assert paper.held() == {}
    assert await paper.sync.tick() == "observed"
    # Its next entry is refused under the notice's own name, and nothing is sent for it.
    sent_before = list(paper.venue.option_submit_calls)
    _refused(await paper.enter("a"), HOLD)
    assert paper.venue.option_submit_calls == sent_before
    # Another bot's entry is not.
    other = await paper.enter("b")
    assert other.state is EffectOperationState.SUBMITTED, other.explanation


@pytest.mark.usefixtures("judged")
async def test_the_hold_ends_only_when_the_owner_reviews_it(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.compared(order_ref)
    sold = await paper.exit()
    closing_ref = next(ref for ref in sold.child_order_refs if ref != order_ref)
    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=1, price="1.90", execution_id="x-close")
    # Passes and rounds do not end it: the bot is flat, and still held.
    await paper.pass_()
    assert await paper.sync.tick() == "observed"
    for _ in range(3):
        await paper.pass_and_round(after_ms=1_000)
    assert paper.held() == {} and paper.notices() == [HOLD]
    assert await paper.sync.tick() == "observed"
    _refused(await paper.enter("a"), HOLD)

    assert await paper.clerk.review_option_buying_power_hold("a", operator="owner") is True

    assert paper.notices() == []
    again = await paper.enter("a")
    assert again.state is EffectOperationState.SUBMITTED, again.explanation
    # The comparison it named stays as it was recorded, and a second review changes nothing.
    assert paper.comparison(order_ref).holds is True
    assert await paper.clerk.review_option_buying_power_hold("a", operator="owner") is False


@pytest.mark.usefixtures("judged")
async def test_a_restart_of_the_clerk_does_not_end_the_hold_and_a_review_after_it_does(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.compared(order_ref)

    await paper.restart()
    await paper.pass_and_round()

    refusal = admit_new_exposure(paper.repo, strategy_instance_id="a")
    assert (refusal.allowed, refusal.reason_code) == (False, HOLD)
    assert admit_new_exposure(paper.repo, strategy_instance_id="b").reason_code != HOLD
    assert await paper.clerk.review_option_buying_power_hold("a", operator="owner") is True
    assert admit_new_exposure(paper.repo, strategy_instance_id="a").reason_code != HOLD


@pytest.mark.usefixtures("judged")
async def test_a_review_is_recorded_as_the_notices_end_under_the_owners_name(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.compared(order_ref)

    await paper.clerk.review_option_buying_power_hold("a", operator="owner")

    [(kind, _summary, facts)] = [row for row in paper.log() if row[1] == REVIEWED_SUMMARY_CODE]
    assert kind == "UNCERTAINTY_RESOLVED" and "operator:owner" in facts
    with pytest.raises(ValueError, match="operator"):
        await paper.clerk.review_option_buying_power_hold("a", operator="")


# ── the setting ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("switched_on", [False, True], ids=["off", "on"])
async def test_turning_the_setting_on_is_the_only_change_between_recording_and_judging(
    paper: Paper, monkeypatch: pytest.MonkeyPatch, switched_on: bool
) -> None:
    monkeypatch.setattr(settings, "ALPACA_OPTION_BUYING_POWER_JUDGED", switched_on)
    order_ref = await paper.filled(shows="99750.00")

    found = await paper.compared(order_ref)

    # Either way: both readings, the fall, what was paid and what was set aside are on the books.
    assert (found.first_buying_power, found.second_buying_power, found.fall_cents) == ("100000", "99750.00", 25_000)
    assert (found.premium_paid_cents, found.set_aside_for_filled_cents) == (19_400, SET_ASIDE)
    assert found.first_began_ms is not None and found.first_ended_ms is not None and found.sent_at_ms is not None
    assert found.first_began_ms <= found.first_ended_ms <= found.sent_at_ms
    assert found.second_began_ms is not None and found.second_ended_ms is not None
    assert found.sent_at_ms < found.second_began_ms <= found.second_ended_ms <= found.compared_at_ms
    if switched_on:
        assert (found.result, found.reason_code, found.holds) == ("under_reserved_unexplained", None, True)
        assert paper.notices() == [HOLD]
    else:
        assert (found.result, found.reason_code, found.holds) == ("unknown", "JUDGMENT_NOT_SWITCHED_ON", False)
        assert paper.notices() == []
        sold = await paper.exit()
        assert sold.state is not EffectOperationState.REJECTED, sold.explanation


async def test_with_the_setting_off_a_pair_that_is_not_clean_still_says_why_it_is_not(paper: Paper) -> None:
    order_ref = await paper.filled(shows=None)

    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code) == ("unknown", "SECOND_READING_WITHOUT_FIGURE")


# ── each reason a pair is not compared ───────────────────────────────────


@pytest.mark.usefixtures("judged")
async def test_a_first_reading_that_fails_does_not_stop_the_entry_and_is_recorded_as_that(paper: Paper) -> None:
    paper.account.option_account_error = BrokerUnavailable("account read timed out")

    order_ref = await paper.filled(shows="99806.00")
    paper.account.option_account_error = None
    await paper.pass_and_round()

    assert paper.order_state(order_ref) == "filled" and paper.held() == {paper.venue.option_orders[order_ref]["symbol"]: 1.0}
    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code) == ("unknown", "FIRST_READING_FAILED")
    # No second reading is made for a first that cannot be compared with.
    assert (found.first_buying_power, found.second_began_ms, found.fall_cents) == (None, None, None)
    assert paper.account.option_account_reads == 1


@pytest.mark.usefixtures("judged")
async def test_a_first_reading_that_never_answers_is_given_up_on_and_the_entry_is_sent(
    paper: Paper, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entry_buying_power, "FIRST_READING_BOUND_MS", 50)
    paper.account.option_account_never_answers = True
    taken_at = paper.clock()

    order_ref = await paper.resting()

    # Sent at the same clock it was asked at, to the venue, once.
    assert paper.venue.option_submit_calls == [order_ref] and paper.clock() == taken_at
    paper.account.option_account_never_answers = False
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    await paper.pass_and_round()
    assert paper.comparison(order_ref).reason_code == "FIRST_READING_FAILED"


@pytest.mark.usefixtures("judged")
async def test_a_first_reading_that_states_no_buying_power_is_not_read_as_zero(paper: Paper) -> None:
    paper.account.buying_power = None

    order_ref = await paper.filled(shows="99806.00")
    await paper.pass_and_round()

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code, found.first_buying_power) == (
        "unknown", "FIRST_READING_WITHOUT_FIGURE", None,
    )
    assert found.first_began_ms is not None


@pytest.mark.usefixtures("judged")
@pytest.mark.parametrize(
    ("age_ms", "reason"),
    [(FIRST_READING_MAX_AGE_MS, None), (FIRST_READING_MAX_AGE_MS + 1, "FIRST_READING_STALE"), (-1, "FIRST_READING_STALE")],
    ids=["as old as a reading may be", "a millisecond older", "ended after the order was sent"],
)
async def test_a_first_reading_too_old_when_the_order_was_sent_is_not_compared(
    paper: Paper, age_ms: int, reason: str | None
) -> None:
    order_ref = await paper.filled(shows="99806.00")
    # The reading as it would be had the order been sent that much later than it ended.
    watched = paper.clerk._buying_power._watched[order_ref]
    watched.first = replace(watched.first, began_ms=watched.sent_at_ms - age_ms - 5, ended_ms=watched.sent_at_ms - age_ms)

    found = await paper.compared(order_ref)

    assert found.reason_code == reason
    assert found.result == ("consistent" if reason is None else "unknown")


@pytest.mark.usefixtures("judged")
async def test_a_second_reading_that_fails_is_recorded_as_that_with_the_first_kept(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99806.00")
    paper.account.option_account_error = BrokerUnavailable("account read timed out")

    await paper.pass_and_round()

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code) == ("unknown", "SECOND_READING_FAILED")
    assert (found.first_buying_power, found.second_buying_power, found.fall_cents) == ("100000", None, None)
    assert found.second_began_ms is not None


@pytest.mark.usefixtures("judged")
@pytest.mark.parametrize(("before", "after"), [("100000.005", "99806.005"), ("100000", "99805.995")])
async def test_a_reading_with_a_fraction_of_a_cent_is_not_rounded_into_a_comparison(
    paper: Paper, before: str, after: str
) -> None:
    paper.account.buying_power = Decimal(before)

    order_ref = await paper.filled(shows=after)
    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code, found.fall_cents) == ("unknown", "READING_NOT_WHOLE_CENTS", None)
    assert (found.first_buying_power, found.second_buying_power) == (before, after)


@pytest.mark.usefixtures("judged")
async def test_fills_that_do_not_come_to_whole_cents_are_not_compared(paper: Paper) -> None:
    order_ref = await paper.filled(price="1.933333", shows="99806.67")

    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code) == ("unknown", "PREMIUM_NOT_WHOLE_CENTS")
    assert (found.premium_paid_cents, found.cost_of_filled_cents, found.fall_cents) == (None, None, 19_333)


@pytest.mark.usefixtures("judged")
async def test_another_bots_order_working_between_the_readings_makes_the_pair_unknown(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.pass_and_round()
    # Bot b's entry rests at Alpaca before a's pair is judged: the account-wide figure is no longer a's alone.
    assert await paper.sync.tick() == "observed"
    await paper.resting("b")

    await paper.pass_and_round()

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code, found.fall_cents) == ("unknown", "OTHER_ORDER_MOVED", 25_000)
    assert paper.notices() == []


async def test_an_order_still_working_counts_as_another_order_though_nothing_was_recorded_about_it(paper: Paper) -> None:
    # Bot b's entry rests at Alpaca, unchanged, while a's entry fills.
    other = await paper.resting("b")
    paper.wait(1_000)
    order_ref = await paper.filled(shows="99806.00", sid="a")
    order = paper.repo.order(order_ref)
    assert order is not None

    def another_order_moved() -> bool:
        """Asked from the newest record on the books, so no record of any order comes after it."""
        with paper.repo.write_fence() as conn:
            return _another_order_moved(
                conn, order_ref=order_ref, effect_operation_id=order.effect_operation_id,
                broker_order_id=order.broker_order_id, after_sequence=paper.repo.last_custody_sequence(),
            )

    assert paper.order_state(other) == "new" and another_order_moved() is True
    # Once b's order has ended, nothing else of the account is working.
    paper.venue.option_order_ends(other)
    await paper.pass_()
    assert paper.order_state(other) == "canceled" and another_order_moved() is False


@pytest.mark.usefixtures("judged")
async def test_another_bots_order_that_came_and_went_between_the_readings_makes_the_pair_unknown(paper: Paper) -> None:
    order_ref = await paper.resting("a")
    other = await paper.resting("b")
    paper.wait(1_000)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-a")
    await paper.stream_fill(other, contracts=1, filled_in_all=1, price="1.94", execution_id="x-b")
    paper.account.buying_power = Decimal("99612.00")

    await paper.pass_and_round()
    await paper.pass_and_round()

    # Each saw the other's order between its own readings: neither is judged, and neither bot is held.
    assert paper.comparison(order_ref).reason_code == "OTHER_ORDER_MOVED"
    assert paper.comparison(other).reason_code == "OTHER_ORDER_MOVED"
    assert paper.notices("a") == [] and paper.notices("b") == []


@pytest.mark.usefixtures("judged")
async def test_an_order_from_outside_seen_between_the_readings_makes_the_pair_unknown(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.pass_and_round()
    # The owner bought something in Alpaca's own app, and the Clerk saw it before it judged.
    paper.wait(1_000)
    observe_external_order(paper.repo, order=_an_outside_order(seen_at_ms=paper.clock()))

    paper.wait(15_000)
    await paper.clerk.compare_entries_with_buying_power()
    await paper.pass_and_round()

    assert paper.comparison(order_ref).reason_code == "OTHER_ORDER_MOVED"
    assert paper.notices() == []


@pytest.mark.usefixtures("judged")
async def test_cash_moved_between_the_readings_makes_the_pair_unknown(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    paper.account.cash_flows.append(_cash_flow("CSW", -56.0))

    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code, found.fall_cents) == ("unknown", "CASH_MOVED", 25_000)
    assert paper.notices() == []


@pytest.mark.usefixtures("judged")
async def test_cash_transfers_from_before_both_readings_do_not_spoil_the_pair(paper: Paper) -> None:
    paper.account.cash_flows.append(_cash_flow("CSD", 500.0))

    order_ref = await paper.filled(shows="99806.00")

    assert (await paper.compared(order_ref)).result == "consistent"


@pytest.mark.usefixtures("judged")
@pytest.mark.parametrize("unread_with", ["first", "second"])
async def test_a_pair_whose_cash_transfers_could_not_be_read_is_unknown(paper: Paper, unread_with: str) -> None:
    from app.broker.contract.errors import BrokerEvidenceUnavailable

    unread = BrokerEvidenceUnavailable("activities could not be read")
    paper.account.activity_error = unread if unread_with == "first" else None
    order_ref = await paper.filled(shows="99806.00")
    paper.account.activity_error = unread if unread_with == "second" else None
    await paper.pass_and_round()
    paper.account.activity_error = None

    await paper.pass_and_round()

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code, found.fall_cents) == ("unknown", "CASH_MOVEMENT_UNREAD", 19_400)


@pytest.mark.usefixtures("judged")
async def test_a_pair_is_not_judged_before_a_pass_has_checked_the_account_after_the_second_reading(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.pass_and_round()
    assert paper.comparisons(order_ref) == []

    # Rounds with no newer pass judge nothing, for as long as a pass may still come.
    paper.wait(PASS_AFTER_SECOND_READING_BOUND_MS - 15_000)
    assert await paper.clerk.compare_entries_with_buying_power() == ()
    assert paper.comparisons(order_ref) == []
    # After that the sweep has stopped, and the pair is not judged at all.
    paper.wait(15_001)
    assert await paper.clerk.compare_entries_with_buying_power() == (order_ref,)

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code, found.fall_cents) == ("unknown", "ACCOUNT_NOT_CHECKED_AFTER", 25_000)
    assert paper.notices() == []


@pytest.mark.usefixtures("judged")
async def test_a_pass_that_reached_no_verdict_does_not_count_as_a_check_of_the_account(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.pass_and_round()
    paper.account.positions_error = BrokerUnavailable("positions read timed out")

    await paper.pass_and_round()

    assert paper.comparisons(order_ref) == []
    paper.account.positions_error = None
    await paper.pass_and_round()
    assert paper.comparison(order_ref).result == "under_reserved_unexplained"


@pytest.mark.usefixtures("judged")
async def test_an_entry_cancelled_unfilled_keeps_both_readings_and_is_not_judged(paper: Paper) -> None:
    order_ref = await paper.resting()
    paper.wait(WAIT_MS + 1)
    await paper.round()
    assert paper.order_state(order_ref) == "canceled"
    # Alpaca still shows the order's cost taken, a moment after the cancel: not a reason to hold a bot.
    paper.account.buying_power = Decimal("99806.00")

    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code, found.holds) == ("unknown", "ENTRY_BOUGHT_NOTHING", False)
    assert (found.contracts_filled, found.set_aside_for_filled_cents, found.premium_paid_cents) == (0, 0, 0)
    assert (found.first_buying_power, found.second_buying_power, found.fall_cents) == ("100000", "99806.00", 19_400)
    assert paper.notices() == []


@pytest.mark.usefixtures("judged")
async def test_an_entry_the_clerk_took_and_never_sent_is_recorded_with_no_second_reading(paper: Paper) -> None:
    from app.broker.contract.errors import BrokerOrderRejected

    paper.venue.option_submit_error = BrokerOrderRejected("insufficient options buying power")

    refused = await paper.enter()
    [order_ref] = refused.child_order_refs
    await paper.pass_and_round()

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code) == ("unknown", "ENTRY_BOUGHT_NOTHING")
    assert (found.first_buying_power, found.second_began_ms) == ("100000", None)
    assert paper.account.option_account_reads == 1


@pytest.mark.usefixtures("judged")
async def test_an_entry_whose_outcome_is_never_proven_is_given_up_on_ten_minutes_past_its_deadline(paper: Paper) -> None:
    # Alpaca takes the cancel at the deadline and goes on showing the order working.
    paper.venue.cancel_is_slow = True
    order_ref = await paper.resting()
    paper.wait(WAIT_MS + OUTCOME_PROOF_BOUND_MS - 15_000)
    assert await paper.pass_and_round(after_ms=0) == ()
    assert paper.outcome(order_ref).proven is False

    paper.wait(15_001)
    assert await paper.clerk.compare_entries_with_buying_power() == (order_ref,)

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code, found.second_began_ms) == ("unknown", "OUTCOME_NOT_PROVEN", None)


@pytest.mark.usefixtures("judged")
async def test_after_a_restart_the_first_reading_is_gone_and_the_entry_says_so(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99750.00")

    await paper.restart()
    await paper.pass_and_round()

    found = paper.comparison(order_ref)
    assert (found.result, found.reason_code) == ("unknown", "CLERK_RESTARTED")
    assert (found.first_began_ms, found.second_began_ms, found.fall_cents, found.sent_at_ms) == (None, None, None, None)
    assert (found.commitment_cents, found.premium_paid_cents) == (SET_ASIDE, 19_400)
    assert paper.notices() == []
    # No broker read is made for it, and it is recorded once however many rounds follow.
    assert paper.account.option_account_reads == 1
    await paper.pass_and_round()
    assert len(paper.comparisons(order_ref)) == 1


# ── once, and never in the entry's way ───────────────────────────────────


@pytest.mark.usefixtures("judged")
async def test_a_comparison_is_recorded_once_and_asked_of_alpaca_twice(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99806.00")

    await paper.compared(order_ref)
    for _ in range(3):
        assert await paper.pass_and_round() == ()

    assert len(paper.comparisons(order_ref)) == 1
    assert paper.account.option_account_reads == 2
    assert paper.clerk._buying_power.watching == ()


@pytest.mark.usefixtures("judged")
async def test_the_second_reading_waits_for_the_entrys_outcome_to_be_proven(paper: Paper) -> None:
    order_ref = await paper.resting()

    assert await paper.pass_and_round(after_ms=5_000) == ()
    assert paper.account.option_account_reads == 1

    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.account.buying_power = Decimal("99806.00")
    assert (await paper.compared(order_ref)).result == "consistent"


@pytest.mark.usefixtures("judged")
async def test_the_runners_wait_for_its_entry_is_answered_without_any_reading_of_the_account(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99806.00")

    settled = await paper.clerk.await_entry_outcome(
        strategy_instance_id="a", decision_id=f"enter-{paper.decisions}", until_ms=paper.clock() + 50_000
    )

    assert settled.recorded and settled.units_filled == 1
    assert paper.account.option_account_reads == 1 and paper.comparisons(order_ref) == []


@pytest.mark.usefixtures("judged")
async def test_an_entry_refused_before_it_is_taken_leaves_no_reading_behind(paper: Paper) -> None:
    refused = await paper.enter(evidence=paper.evidence(bid="1.00", ask="1.94"))

    assert refused.state is EffectOperationState.REJECTED
    assert paper.clerk._buying_power._first == {} and paper.clerk._buying_power.watching == ()
    assert await paper.pass_and_round() == ()


@pytest.mark.usefixtures("judged")
async def test_the_periodic_sweeps_pass_starts_a_round_by_itself(paper: Paper) -> None:
    order_ref = await paper.filled(shows="99806.00")

    for _ in range(2):
        paper.wait(15_000)
        paper.clerk.publish_sweep_reconciliation(await paper.pass_())
        await paper.clerk.drain_effects()

    assert paper.comparison(order_ref).result == "consistent"


# ── what never makes a comparison ────────────────────────────────────────


async def test_an_entry_in_shares_reads_no_option_figure_and_writes_no_comparison(
    one_account: _OneAccount, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    asked: list[str] = []

    async def get_option_account(_self: _Account) -> Any:
        asked.append("get_option_account")
        raise AssertionError("a stock entry never reads the account's option buying power")

    monkeypatch.setattr(_Account, "get_option_account", get_option_account, raising=False)
    transfers_read_before = len(one_account.account.activity_calls)

    bought = await one_account.decides_enter("b")

    assert bought.state is EffectOperationState.SUBMITTED, bought.explanation
    assert asked == [] and len(one_account.account.activity_calls) == transfers_read_before
    watch = one_account.clerk._buying_power
    assert watch is not None and watch.watching == () and watch._first == {}
    # The round has nothing to read and nothing to write on books with no option entry.
    before = one_account.repo.last_custody_sequence()
    assert await one_account.clerk.compare_entries_with_buying_power() == ()
    assert one_account.repo.last_custody_sequence() == before and asked == []


async def test_a_dry_run_makes_no_comparison_and_its_page_says_so(dry_run: _DryRun) -> None:  # noqa: F811
    bought = await dry_run.enter(dry_run.evidence())
    [order_ref] = bought.child_order_refs
    before = dry_run.repo.last_custody_sequence()

    assert dry_run.clerk.compares_option_buying_power is False
    assert await dry_run.clerk.compare_entries_with_buying_power() == ()
    assert dry_run.repo.last_custody_sequence() == before

    page = served(dry_run.clerk, dry_run.binding.strategy_instance_id)
    assert page.simulation is True and page.judged is False and page.hold is None
    [check] = page.checks
    assert (check.order_ref, check.result, check.reason_code) == (order_ref, "unknown", "SIMULATION")
    assert check.set_aside.cents == SET_ASIDE and check.first_reading is None and check.fall is None


def _an_outside_order(*, seen_at_ms: int) -> BrokerOrder:
    return BrokerOrder(
        broker="alpaca", order_id="outside-1", client_order_id="typed-by-the-owner", symbol="AAPL",
        asset_class="us_equity", side="buy", order_type="market", time_in_force="day", quantity=1.0,
        filled_quantity=1.0, limit_price=None, stop_price=None, filled_avg_price=56.0, status="filled",
        submitted_at_ms=seen_at_ms - 500, created_at_ms=seen_at_ms - 500, updated_at_ms=seen_at_ms,
        filled_at_ms=seen_at_ms, canceled_at_ms=None, expired_at_ms=None, events=[], observed_at_ms=seen_at_ms,
    )


# ── what a page is served, and the owner's review over HTTP ──────────────


@pytest.fixture
def routes(paper: Paper) -> Iterator[FastAPI]:
    """The Clerk's own routes over this Paper account, as its agent serves them."""
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=paper.clerk))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)


def _http(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _checks_route(paper: Paper, sid: str = "a") -> str:
    return f"/api/alpaca-clerk-sqlite/accounts/{paper.repo.account_id}/bots/{sid}/option-buying-power-checks"


def _review_route(paper: Paper, sid: str = "a") -> str:
    return f"/api/alpaca-clerk-sqlite/accounts/{paper.repo.account_id}/bots/{sid}/option-buying-power-hold/review"


@pytest.mark.usefixtures("judged")
async def test_a_page_is_served_each_figure_in_cents_and_dollars_with_the_times_of_both_readings(
    paper: Paper, routes: FastAPI
) -> None:
    order_ref = await paper.filled(price="1.93", shows="99807.00")
    found = await paper.compared(order_ref)

    async with _http(routes) as client:
        answered = await client.get(_checks_route(paper))
        other = await client.get(_checks_route(paper, "b"))

    assert answered.status_code == 200, answered.text
    page = answered.json()
    assert (page["simulation"], page["judged"], page["hold"]) == (False, True, None)
    [check] = page["checks"]
    assert (check["order_ref"], check["contract"], check["result"], check["reason_code"]) == (
        order_ref, paper.venue.option_orders[order_ref]["symbol"], "consistent", None,
    )
    assert check["result_label"] and check["sentence"] and "193.00" in check["sentence"]
    assert check["set_aside"] == {"cents": SET_ASIDE, "dollars": "194.06"}
    assert check["set_aside_for_filled"] == {"cents": SET_ASIDE, "dollars": "194.06"}
    assert check["premium_paid"] == {"cents": 19_300, "dollars": "193.00"}
    assert check["fall"] == {"cents": 19_300, "dollars": "193.00"}
    assert check["first_reading"] == {
        "began_ms": found.first_began_ms, "ended_ms": found.first_ended_ms,
        "buying_power": {"cents": 10_000_000, "dollars": "100000.00"},
    }
    assert check["second_reading"] == {
        "began_ms": found.second_began_ms, "ended_ms": found.second_ended_ms,
        "buying_power": {"cents": 9_980_700, "dollars": "99807.00"},
    }
    assert (check["sent_at_ms"], check["compared_at_ms"], check["holds"]) == (
        found.sent_at_ms, found.compared_at_ms, False,
    )
    assert (check["contracts_asked"], check["contracts_filled"]) == (1, 1)
    # A bot with no option entry is answered with none, never with another bot's.
    assert other.status_code == 200 and other.json()["checks"] == []


async def test_an_entry_not_compared_yet_is_served_as_that_and_never_as_agreement(
    paper: Paper, routes: FastAPI
) -> None:
    order_ref = await paper.resting()

    async with _http(routes) as client:
        page = (await client.get(_checks_route(paper))).json()

    [check] = page["checks"]
    assert (check["order_ref"], check["result"], check["reason_code"]) == (order_ref, "unknown", "NOT_COMPARED_YET")
    assert check["sentence"] and check["result_label"]
    assert (check["fall"], check["first_reading"], check["contracts_filled"], check["compared_at_ms"]) == (
        None, None, None, None,
    )
    assert page["judged"] is False


async def test_every_result_and_every_reason_has_a_label_and_a_sentence_of_its_own() -> None:
    assert set(RESULT_LABELS) == set(BuyingPowerCheckResult)
    assert set(REASON_SENTENCES) == set(NotComparedReason)
    assert len(set(RESULT_LABELS.values())) == len(RESULT_LABELS)
    assert len(set(REASON_SENTENCES.values())) == len(REASON_SENTENCES)
    # The wire's closed set is the Clerk's own.
    assert set(get_args(OptionBuyingPowerNotComparedReason)) == {reason.value for reason in NotComparedReason}
    assert set(get_args(OptionBuyingPowerResult)) == {result.value for result in BuyingPowerCheckResult}


@pytest.mark.usefixtures("judged")
async def test_the_owners_review_over_http_ends_the_hold_and_answers_with_the_bots_comparisons(
    paper: Paper, routes: FastAPI
) -> None:
    order_ref = await paper.filled(shows="99750.00")
    await paper.compared(order_ref)

    async with _http(routes) as client:
        held = (await client.get(_checks_route(paper))).json()
        reviewed = await client.post(_review_route(paper))
        again = await client.post(_review_route(paper))
        nobody = await client.post(_review_route(paper, "no-such-bot"))
        unknown = await client.get(_checks_route(paper, "no-such-bot"))

    # Before the review: the notice, with its two figures, and the entry it names.
    assert held["hold"]["entries"] == [{
        "order_ref": order_ref, "fall": {"cents": 25_000, "dollars": "250.00"},
        "set_aside": {"cents": SET_ASIDE, "dollars": "194.06"},
    }]
    assert held["hold"]["headline"] and held["hold"]["explanation"] and held["hold"]["impact"]
    assert held["hold"]["next_step"] and held["hold"]["raised_at_ms"] > 0
    [check] = held["checks"]
    assert (check["result"], check["holds"]) == ("under_reserved_unexplained", True)
    # The review ends it, under the server's own name for the operator.
    assert reviewed.status_code == 200, reviewed.text
    assert (reviewed.json()["reviewed"], reviewed.json()["state"]["hold"]) == (True, None)
    [after] = reviewed.json()["state"]["checks"]
    assert (after["result"], after["holds"]) == ("under_reserved_unexplained", False)
    assert paper.notices() == []
    [(_kind, _summary, facts)] = [row for row in paper.log() if row[1] == REVIEWED_SUMMARY_CODE]
    assert f"operator:{settings.PANEL_OPERATOR_IDENTITY}" in facts
    # Asked again it changes nothing and says so; a bot nobody registered is not found.
    assert (again.status_code, again.json()["reviewed"]) == (200, False)
    assert nobody.status_code == 404 and unknown.status_code == 404
    assert len([row for row in paper.log() if row[1] == REVIEWED_SUMMARY_CODE]) == 1


@pytest.mark.usefixtures("judged")
async def test_a_part_fill_is_compared_with_what_filled_and_with_its_share_of_the_set_aside(paper: Paper) -> None:
    order_ref = await paper.resting(units=2)
    paper.wait(1_000)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    # The other contract never fills: the keeper cancels it at the entry's deadline.
    paper.wait(WAIT_MS)
    await paper.round()
    assert paper.outcome(order_ref).standing is EntryStanding.PART_FILLED
    paper.account.buying_power = Decimal("99806.00")

    found = await paper.compared(order_ref)

    # Two contracts set aside $388.00 and nine cents of fees. One of the two is $194.045, up to the cent.
    assert (found.contracts_asked, found.contracts_filled, found.commitment_cents) == (2, 1, 38_809)
    assert (found.set_aside_for_filled_cents, found.premium_paid_cents, found.fall_cents) == (19_405, 19_400, 19_400)
    assert (found.result, found.holds) == ("consistent", False)


@pytest.mark.usefixtures("judged")
async def test_a_bots_second_entry_is_compared_on_its_own_fills_and_not_on_the_first_entrys(paper: Paper) -> None:
    first = await paper.filled(shows="99806.00")
    assert (await paper.compared(first)).result == "consistent"
    sold = await paper.exit()
    closing_ref = next(ref for ref in sold.child_order_refs if ref != first)
    await paper.stream_fill(closing_ref, contracts=1, filled_in_all=1, price="1.90", execution_id="x-close")
    await paper.pass_()
    assert paper.held() == {}
    # The sale gave $190.00 back; the second entry starts from there.
    paper.account.buying_power = Decimal("99996.00")
    assert await paper.sync.tick() == "observed"

    second = await paper.filled(shows="99802.00")
    found = await paper.compared(second)

    # The first entry's close was recorded in the same millisecond the second entry's reading began,
    # and before it: it is not "another order" to the second entry.
    assert second != first
    assert (found.result, found.premium_paid_cents, found.fall_cents) == ("consistent", 19_400, 19_400)
    assert (found.first_buying_power, found.second_buying_power) == ("99996.00", "99802.00")
    assert len(paper.comparisons(first)) == 1 and len(paper.comparisons(second)) == 1


@pytest.mark.usefixtures("judged")
async def test_an_entry_handed_to_the_watch_again_keeps_its_first_reading_and_is_recorded_once(paper: Paper) -> None:
    order_ref = await paper.resting()
    watch = paper.clerk._buying_power
    # The same order is handed over a second time, with no reading of its own, before it is compared...
    watch.sent(("a", "handed-over-again"), order_ref=order_ref, sent_at_ms=None)
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.account.buying_power = Decimal("99806.00")

    found = await paper.compared(order_ref)

    assert (found.result, found.first_buying_power) == ("consistent", "100000")
    # ...and a third time after its comparison is on the books.
    watch.sent(("a", "handed-over-once-more"), order_ref=order_ref, sent_at_ms=paper.clock())
    await paper.pass_and_round()
    await paper.pass_and_round()
    assert len(paper.comparisons(order_ref)) == 1 and watch.watching == ()


@pytest.mark.usefixtures("judged")
async def test_an_entry_being_sent_while_a_new_clerks_first_round_runs_is_not_taken_for_one_a_restart_left(
    paper: Paper,
) -> None:
    alpaca_answers = asyncio.Event()
    send = paper.venue.submit_option_order

    async def a_send_that_waits(order: Any, *, client_order_id: str) -> Any:
        await alpaca_answers.wait()
        return await send(order, client_order_id=client_order_id)

    paper.venue.submit_option_order = a_send_that_waits  # type: ignore[method-assign]
    entering = asyncio.ensure_future(paper.enter())
    for _ in range(50):
        await asyncio.sleep(0)
    assert paper.venue.option_submit_calls == [] and not entering.done()

    # The round finds an option entry on the books with no comparison, and leaves it alone: it is being sent.
    assert await paper.clerk.compare_entries_with_buying_power() == ()

    alpaca_answers.set()
    [order_ref] = (await entering).child_order_refs
    assert paper.comparisons(order_ref) == []
    await paper.stream_fill(order_ref, contracts=1, filled_in_all=1, price="1.94", execution_id="x-1")
    paper.account.buying_power = Decimal("99806.00")
    assert (await paper.compared(order_ref)).result == "consistent"


@pytest.mark.usefixtures("judged")
async def test_a_full_page_of_cash_transfers_cannot_prove_there_were_no_more(paper: Paper) -> None:
    paper.account.cash_flows.extend(_cash_flow("CSD", float(amount)) for amount in range(1, 101))

    order_ref = await paper.filled(shows="99806.00")
    found = await paper.compared(order_ref)

    assert (found.result, found.reason_code) == ("unknown", "CASH_MOVEMENT_UNREAD")
