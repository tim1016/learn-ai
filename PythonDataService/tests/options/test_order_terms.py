"""The four option order terms: what each may be, and how an owner's two times become close terms."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.options import order_terms
from app.options.deadlines import CloseTerms
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, DEFAULT_LEG_GAP_LIMIT_PERCENT


def test_an_owners_two_times_make_close_terms_with_every_other_term_as_the_deadlines_start_it() -> None:
    made = order_terms.close_terms_with(entry_wait_seconds=25, expiry_close_lead_minutes=90)
    start = CloseTerms()

    assert (made.entry_wait_ms, made.expiry_close_lead_ms) == (25_000, 90 * 60_000)
    assert (made.cancel_allowance_ms, made.entry_margin_ms, made.sold_leg_close_lead_ms) == (
        start.cancel_allowance_ms, start.entry_margin_ms, start.sold_leg_close_lead_ms,
    )
    # The cutoff an entry is held to is counted from all of them: 90 minutes, 25 s, and 30 s twice.
    assert made.entry_cutoff_lead_ms == 90 * 60_000 + 25_000 + 30_000 + 30_000
    # Read back in the units an owner types, they are what was typed.
    assert (order_terms.entry_wait_seconds(made), order_terms.expiry_close_lead_minutes(made)) == (25, 90)


def test_the_terms_as_the_deadlines_start_them_make_the_starting_close_terms_again() -> None:
    start = CloseTerms()

    assert order_terms.close_terms_with(
        entry_wait_seconds=order_terms.entry_wait_seconds(start),
        expiry_close_lead_minutes=order_terms.expiry_close_lead_minutes(start),
    ) == start


def test_a_term_that_is_no_whole_number_of_its_unit_is_never_shown_rounded() -> None:
    with pytest.raises(ValueError, match="whole number"):
        order_terms.entry_wait_seconds(CloseTerms(entry_wait_ms=20_500))
    with pytest.raises(ValueError, match="whole number"):
        order_terms.expiry_close_lead_minutes(CloseTerms(expiry_close_lead_ms=90_000))


def test_each_term_starts_inside_what_it_may_be() -> None:
    start = CloseTerms()

    assert order_terms.GAP_LIMIT_PERCENT.fault(DEFAULT_LEG_GAP_LIMIT_PERCENT) is None
    assert order_terms.COST_LIMIT_PERCENT.fault(DEFAULT_COST_LIMIT_PERCENT) is None
    assert order_terms.ENTRY_WAIT_SECONDS.fault(Decimal(order_terms.entry_wait_seconds(start))) is None
    assert order_terms.EXPIRY_CLOSE_LEAD_MINUTES.fault(Decimal(order_terms.expiry_close_lead_minutes(start))) is None


def test_an_owner_chooses_an_entry_wait_of_one_to_thirty_seconds_and_starts_at_twenty() -> None:
    """Narrower than the Clerk's own second to five minutes: the bot waits fifty seconds at most to learn what
    an entry came to, and a longer wait could be counted as entered while its order still rests."""
    offered = order_terms.ENTRY_WAIT_SECONDS

    assert (offered.lowest, offered.lowest_included, offered.highest, offered.highest_included) == (1, True, 30, True)
    assert offered.step == 1
    assert [offered.fault(Decimal(seconds)) for seconds in (0, 1, 20, 30, 31, 300)] == [
        "below", None, None, None, "above", "above",
    ]
    assert order_terms.entry_wait_seconds(CloseTerms()) == 20
    assert "50 seconds" in order_terms.ENTRY_WAIT_RANGE_REASON


def test_no_range_an_owner_is_offered_is_wider_than_what_the_clerk_takes() -> None:
    """A form range wider than the Clerk's would deploy a bot whose every entry is refused. Held in the Clerk's
    own unit, milliseconds, so that a slip between seconds, minutes and milliseconds shows here."""
    wait, lead = order_terms.ENTRY_WAIT_SECONDS, order_terms.EXPIRY_CLOSE_LEAD_MINUTES

    assert order_terms.MIN_ENTRY_WAIT_MS <= wait.lowest * 1_000 <= wait.highest * 1_000 <= order_terms.MAX_ENTRY_WAIT_MS
    assert 0 <= lead.lowest * 60_000 <= lead.highest * 60_000 <= order_terms.MAX_ORDER_TERM_MS
    # The Clerk's own limits, as they are stated: a second to five minutes, and a regular session of 390 minutes.
    assert (order_terms.MIN_ENTRY_WAIT_MS, order_terms.MAX_ENTRY_WAIT_MS) == (1_000, 300_000)
    assert order_terms.MAX_ORDER_TERM_MS == 390 * 60_000
    # Every term the deadlines start from is inside them too.
    start = CloseTerms()
    assert order_terms.MIN_ENTRY_WAIT_MS <= start.entry_wait_ms <= order_terms.MAX_ENTRY_WAIT_MS
    assert max(
        start.cancel_allowance_ms, start.entry_margin_ms, start.expiry_close_lead_ms, start.sold_leg_close_lead_ms
    ) <= order_terms.MAX_ORDER_TERM_MS


def test_a_gap_limit_of_nothing_is_not_taken_and_a_cost_limit_of_nothing_is() -> None:
    """At a gap limit of zero every leg with any gap at all is refused. A cost limit of zero is the owner's to set."""
    assert order_terms.GAP_LIMIT_PERCENT.fault(Decimal(0)) == "below"
    assert order_terms.COST_LIMIT_PERCENT.fault(Decimal(0)) is None
