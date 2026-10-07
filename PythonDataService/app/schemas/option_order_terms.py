"""The four order terms an option bot is deployed with, as they are sealed with the bot (ADR 0076 decision 24).

An owner sets them at Deploy and they are fixed for the bot's life: the
widest bid-ask gap a leg may have at an entry, how long a resting entry waits
for its fill, the most opening and closing the whole structure at once may
cost, and how long before the close a contract is closed on its expiry day.

They are written down once, here, and carried beside the bot's plan on its
start request, its binding, its instance record and its seal. A bot in
shares has none, and every one of those is then written and hashed exactly
as before the terms existed (``strip_absent_option_order_terms``).

Each number is held to the range ``app.options.order_terms`` states for it,
by the four types below. The structure preview's request uses the very same
types, so a term a preview took is a term a Deploy takes. A percent is kept
as an owner writes it out (``10``, never ``10.000``), so one choice has one
hash.

It lives in a module of its own so that the binding and the seal can carry
it without importing the Deploy request.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from app.options import order_terms
from app.options.deadlines import CloseTerms
from app.options.order_terms import close_terms_with


def _as_an_owner_writes_it(percent: Decimal) -> Decimal:
    """``1e1`` is 10, ``10.000`` is 10 and ``-0`` is 0: a percent is carried back and printed as written out."""
    if not percent:
        # Nothing, whatever sign it was written with.
        return Decimal(0)
    return Decimal(format(percent.normalize(), "f"))


def _places(step: Decimal | None) -> int:
    """How many decimal places a step allows: six for a millionth."""
    assert step is not None
    return -int(step.normalize().as_tuple().exponent)


# The four order terms, each held to the one range ``app.options.order_terms`` states for it. A percent is
# bounded before it is written out, so that a number too large to write is refused and never formatted.
GapLimitPercent = Annotated[
    Decimal,
    Field(decimal_places=_places(order_terms.GAP_LIMIT_PERCENT.step)),
    *order_terms.GAP_LIMIT_PERCENT.held_by(Decimal),
    AfterValidator(_as_an_owner_writes_it),
]
CostLimitPercent = Annotated[
    Decimal,
    Field(decimal_places=_places(order_terms.COST_LIMIT_PERCENT.step)),
    *order_terms.COST_LIMIT_PERCENT.held_by(Decimal),
    AfterValidator(_as_an_owner_writes_it),
]
EntryWaitSeconds = Annotated[int, *order_terms.ENTRY_WAIT_SECONDS.held_by(int)]
ExpiryCloseLeadMinutes = Annotated[int, *order_terms.EXPIRY_CLOSE_LEAD_MINUTES.held_by(int)]


class SealedOptionOrderTerms(BaseModel):
    """An option bot's four order terms. Every one is said; none is filled in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The widest bid-ask gap any one leg may have at an entry, as a percent of its mid.
    gap_limit_percent: GapLimitPercent
    # How long a resting entry waits for its fill before the Clerk cancels what is left. A whole number,
    # strictly: ``true`` is not one second, and nothing is sealed that an owner did not write as a number.
    entry_wait_seconds: Annotated[EntryWaitSeconds, Field(strict=True)]
    # The most opening and closing at once may cost, as a percent of what the entry sets aside.
    cost_limit_percent: CostLimitPercent
    # How long before the session close a held contract is closed on its expiry day. A whole number, strictly.
    expiry_close_lead_minutes: Annotated[ExpiryCloseLeadMinutes, Field(strict=True)]

    @property
    def close(self) -> CloseTerms:
        """The close terms these two times make, every other term as the deadlines start it."""
        return close_terms_with(
            entry_wait_seconds=self.entry_wait_seconds, expiry_close_lead_minutes=self.expiry_close_lead_minutes
        )


def strip_absent_option_order_terms(payload: dict) -> dict:
    """Drop absent order terms from a payload about to be content-hashed or stored.

    Every seal and record of a bot in shares carries none, and each was
    written before the field existed. Writing the ``None`` would change
    every such hash, and a record that refuses unknown fields could no
    longer be read by the build that wrote it. Sealed terms stay, under the
    hash. Mutates ``payload`` in place and returns it.
    """
    if payload.get("option_order_terms") is None:
        payload.pop("option_order_terms", None)
    return payload


__all__ = [
    "CostLimitPercent",
    "EntryWaitSeconds",
    "ExpiryCloseLeadMinutes",
    "GapLimitPercent",
    "SealedOptionOrderTerms",
    "strip_absent_option_order_terms",
]
