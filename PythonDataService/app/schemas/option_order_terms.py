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
through the very types the structure preview's request uses, so a term a
preview took is a term a Deploy takes. A percent is kept as an owner writes
it out (``10``, never ``10.000``), so one choice has one hash.

It lives in a module of its own so that the binding and the seal can carry
it without importing the Deploy request.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.options.deadlines import CloseTerms
from app.options.order_terms import close_terms_with

# The preview request's own four number types: one range holds a preview's term and a Deploy's.
from app.schemas.option_preview import (
    _CostLimitPercent as CostLimitPercent,
)
from app.schemas.option_preview import (
    _EntryWaitSeconds as EntryWaitSeconds,
)
from app.schemas.option_preview import (
    _ExpiryCloseLeadMinutes as ExpiryCloseLeadMinutes,
)
from app.schemas.option_preview import (
    _GapLimitPercent as GapLimitPercent,
)


class SealedOptionOrderTerms(BaseModel):
    """An option bot's four order terms. Every one is said; none is filled in."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # The widest bid-ask gap any one leg may have at an entry, as a percent of its mid.
    gap_limit_percent: GapLimitPercent
    # How long a resting entry waits for its fill before the Clerk cancels what is left.
    entry_wait_seconds: EntryWaitSeconds
    # The most opening and closing at once may cost, as a percent of what the entry sets aside.
    cost_limit_percent: CostLimitPercent
    # How long before the session close a held contract is closed on its expiry day.
    expiry_close_lead_minutes: ExpiryCloseLeadMinutes

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
