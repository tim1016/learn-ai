"""The order terms an owner is offered are terms the account Clerk takes (#2913).

``app.options.order_terms`` states two things side by side: what the Clerk
takes with an entry, and the narrower ranges an owner may choose from. The
Clerk reads its limits from there. Here the Clerk's own check is asked, as an
entry reaches it: each end of what an owner is offered is taken, each end of
what the Clerk is stated to take is taken, and one step past the Clerk's own
ends is refused by name.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal
from app.broker.alpaca.clerk.sqlite.option_admission import OptionEntryRefused, work_out_option_entry
from app.broker.alpaca.clerk.sqlite.option_entry import OptionEntryFacts
from app.options import order_terms
from app.options.deadlines import CloseTerms

# One bought SPY call admitted a second after noon, days before it expires: an entry nothing else refuses.
from tests.broker.alpaca.clerk.sqlite.test_resting_entry_facts import _NOW, _PLAN, _evidence

_WAIT, _LEAD = order_terms.ENTRY_WAIT_SECONDS, order_terms.EXPIRY_CLOSE_LEAD_MINUTES
_MOST_SECONDS = order_terms.MAX_ENTRY_WAIT_MS // 1_000
_MOST_MINUTES = order_terms.MAX_ORDER_TERM_MS // 60_000


def _answer(terms: CloseTerms) -> OptionEntryFacts | OptionEntryRefused:
    return work_out_option_entry(_PLAN, evidence=_evidence(terms=terms), units=2, now_ms=_NOW)


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [
        pytest.param(int(_WAIT.lowest), int(_LEAD.lowest), id="the shortest wait and the latest close offered"),
        pytest.param(int(_WAIT.highest), int(_LEAD.highest), id="the longest wait and the earliest close offered"),
        pytest.param(int(_WAIT.lowest), int(_LEAD.highest), id="the shortest wait with the earliest close"),
        pytest.param(int(_WAIT.highest), int(_LEAD.lowest), id="the longest wait with the latest close"),
    ],
)
def test_each_end_of_what_an_owner_is_offered_is_taken_by_the_clerk(seconds: int, minutes: int) -> None:
    terms = order_terms.close_terms_with(entry_wait_seconds=seconds, expiry_close_lead_minutes=minutes)

    taken = _answer(terms)

    assert isinstance(taken, OptionEntryFacts), taken
    # The entry is cancelled after the wait the owner chose, to the millisecond.
    assert taken.cancel_at_ms == _NOW + seconds * 1_000
    assert taken.terms is not None and taken.terms.close_terms == terms


@pytest.mark.parametrize(
    "terms",
    [
        pytest.param(CloseTerms(entry_wait_ms=order_terms.MIN_ENTRY_WAIT_MS), id="the shortest wait the Clerk takes"),
        pytest.param(CloseTerms(entry_wait_ms=order_terms.MAX_ENTRY_WAIT_MS), id="the longest wait the Clerk takes"),
        pytest.param(
            order_terms.close_terms_with(entry_wait_seconds=_MOST_SECONDS, expiry_close_lead_minutes=_MOST_MINUTES),
            id="both at the Clerk's own ends, in the units an owner types",
        ),
        pytest.param(CloseTerms(expiry_close_lead_ms=order_terms.MAX_ORDER_TERM_MS), id="the earliest close the Clerk takes"),
    ],
)
def test_the_clerk_takes_the_ends_its_limits_are_stated_at(terms: CloseTerms) -> None:
    """The limits are stated once, in ``app.options.order_terms``. A copy of them in the Clerk that had drifted
    narrower would refuse one of these."""
    assert isinstance(_answer(terms), OptionEntryFacts)


@pytest.mark.parametrize(
    ("terms", "named"),
    [
        pytest.param(
            order_terms.close_terms_with(entry_wait_seconds=0, expiry_close_lead_minutes=45), "entry wait",
            id="a second under the shortest wait",
        ),
        pytest.param(
            order_terms.close_terms_with(entry_wait_seconds=_MOST_SECONDS + 1, expiry_close_lead_minutes=45), "entry wait",
            id="a second over the longest wait",
        ),
        pytest.param(
            order_terms.close_terms_with(entry_wait_seconds=20, expiry_close_lead_minutes=_MOST_MINUTES + 1),
            "expiry day",
            id="a minute earlier than a whole session",
        ),
        pytest.param(CloseTerms(entry_wait_ms=order_terms.MIN_ENTRY_WAIT_MS - 1), "entry wait", id="a millisecond under"),
        pytest.param(CloseTerms(entry_wait_ms=order_terms.MAX_ENTRY_WAIT_MS + 1), "entry wait", id="a millisecond over"),
    ],
)
def test_one_step_past_the_clerks_own_ends_is_refused_by_name(terms: CloseTerms, named: str) -> None:
    """A copy of the limits in the Clerk that had drifted wider would take one of these."""
    refused = _answer(terms)

    assert isinstance(refused, OptionEntryRefused), refused
    assert refused.reason_code == OptionEffectRefusal.ENTRY_EVIDENCE_INCOMPLETE.value
    assert named in refused.explanation
