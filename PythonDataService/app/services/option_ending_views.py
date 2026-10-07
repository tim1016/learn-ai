"""The served view of an option position that ended with no order of the bot's (ADR 0076 decision 22).

One function, read by History and by Activity, so both show the same
closed set and the same sentence.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.option_event_books import RecordedOptionEnding
from app.schemas.option_endings import OptionEndingView


def option_ending_view(ending: RecordedOptionEnding) -> OptionEndingView:
    """One recorded ending as a screen reads it. The sentence is the books' own."""
    facts = ending.facts
    return OptionEndingView(
        kind=facts.ended_by,
        contract=facts.contract_symbol,
        contracts=facts.contracts,
        shares=None if facts.shares is None else facts.shares["quantity"],
        simulated=facts.simulated,
        headline=ending.sentence,
        recorded_at_ms=ending.recorded_at_ms,
    )
