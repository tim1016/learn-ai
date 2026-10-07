"""The four order terms an owner sets for an option bot: what each may be, and what each starts at.

The terms are set at Deploy and fixed for the bot's life (ADR 0076):

- the widest bid-ask gap a leg may have at entry, a percent of the leg's mid;
- how long a resting entry waits for its fill, in seconds;
- the most opening and closing the whole structure at once may cost, a
  percent of what the entry sets aside;
- how long before the session close a held contract is closed on its expiry
  day, in minutes.

This is the one place that says what each may be, and it says it twice over:

- **What the account Clerk takes** with an entry, whoever hands it over: its
  own safety limits on the two times, in milliseconds. The Clerk reads them
  from here (``app.broker.alpaca.clerk.sqlite.option_admission``).
- **What an owner may choose**, in the unit an owner types. A request that
  carries a term is held to this range, and the builder is served the same
  range, so a form can refuse a number before it is sent. No range offered
  here is wider than the Clerk's: a bot is never deployed with a term its
  every entry would be refused for.

Why each range is what it is:

- A percent is no finer than a millionth of a percent. A limit is compared
  exactly, in that unit (``app.options.requirement.round_trip_cost_is_over_limit``).
- A gap limit is above zero: at zero every leg with any gap at all is refused.
- The Clerk takes an entry wait of a second at least, since a wait of nothing
  cancels the order as it is sent, and of five minutes at most, since the
  quotes it was priced from are seconds old.
- An owner chooses an entry wait of thirty seconds at most, which is less
  than the Clerk would take. The bot waits at most fifty seconds from its
  decision bar's close to learn what an entry came to (#2913). An order
  that rests longer than that could be counted as entered while it still
  rests. Thirty seconds leaves the cancel time to be proven inside the
  fifty.
- A close lead is no longer than a whole regular session of 390 minutes,
  which nothing could be counted back from. The Clerk holds every term but
  the entry wait to that.

What each starts at is not held here. The two limits start where the entry
keeps them (``app.options.entry``), and the two times where the deadlines
keep them (``app.options.deadlines.CloseTerms``); the starts below are read
from there, in the unit an owner types.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.options.bounds import Bounds
from app.options.deadlines import CloseTerms

_MILLIONTH = Decimal("0.000001")
_WHOLE = Decimal(1)
_MS_PER_SECOND = 1_000
_MS_PER_MINUTE = 60_000

# ---- What the account Clerk takes: its own safety limits (ADR 0076 decision 19) -------------------
# An entry wait: a second at least, since a wait of nothing cancels the order as it is sent;
# five minutes at most, since the quotes it was priced from are seconds old.
MIN_ENTRY_WAIT_MS = 1_000
MAX_ENTRY_WAIT_MS = 5 * 60_000
# Every other term: no longer than a whole regular session, which nothing could be counted back from.
MAX_ORDER_TERM_MS = 390 * 60_000

# ---- What an owner may choose ---------------------------------------------------------------------
GAP_LIMIT_PERCENT = Bounds(Decimal(0), False, Decimal(100), True, _MILLIONTH)
COST_LIMIT_PERCENT = Bounds(Decimal(0), True, Decimal(100), True, _MILLIONTH)
# Thirty seconds at most, inside the fifty the bot waits to learn what an entry came to (#2913).
ENTRY_WAIT_SECONDS = Bounds(Decimal(MIN_ENTRY_WAIT_MS // _MS_PER_SECOND), True, Decimal(30), True, _WHOLE)
# Said to the owner beside the range, and with a wait that is refused for being outside it.
ENTRY_WAIT_RANGE_REASON = (
    "The bot waits at most 50 seconds from its decision bar's close to learn what an entry came to, so a "
    "longer wait could be counted as entered while its order still rests."
)
EXPIRY_CLOSE_LEAD_MINUTES = Bounds(Decimal(0), True, Decimal(MAX_ORDER_TERM_MS // _MS_PER_MINUTE), True, _WHOLE)


def _whole(milliseconds: int, *, in_one: int, what: str) -> int:
    """A close term in the unit an owner types it in. One that is no whole number of it is not shown rounded."""
    whole, left_over = divmod(milliseconds, in_one)
    if left_over:
        raise ValueError(f"{what} of {milliseconds} ms is not a whole number of its unit")
    return whole


def entry_wait_seconds(terms: CloseTerms) -> int:
    """How long ``terms`` lets an entry wait for its fill, in whole seconds."""
    return _whole(terms.entry_wait_ms, in_one=_MS_PER_SECOND, what="an entry wait")


def expiry_close_lead_minutes(terms: CloseTerms) -> int:
    """How long before the session close ``terms`` closes a contract on its expiry day, in whole minutes."""
    return _whole(terms.expiry_close_lead_ms, in_one=_MS_PER_MINUTE, what="an expiry-day close lead")


def close_terms_with(*, entry_wait_seconds: int, expiry_close_lead_minutes: int) -> CloseTerms:
    """The close terms an owner's two times make: every other term as the deadlines start it."""
    return replace(
        CloseTerms(),
        entry_wait_ms=entry_wait_seconds * _MS_PER_SECOND,
        expiry_close_lead_ms=expiry_close_lead_minutes * _MS_PER_MINUTE,
    )
