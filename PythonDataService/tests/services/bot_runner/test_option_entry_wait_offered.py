"""The longest entry wait an owner is offered is one the bot's runner can see through, and the reason given for
the range says what the runner does (#2913).

The runner's own guards are asked, on a real ``OptionRun``: the start guard
that refuses a wait no entry could be seen through, and the per-entry guard
that refuses an entry taken too late for its wait. Nothing here restates
their sum.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.options.order_terms import ENTRY_WAIT_RANGE_REASON, ENTRY_WAIT_SECONDS
from app.options.structure_catalogue import order_term_starts
from app.services.bot_option_run import (
    ENTRY_OUTCOME_WAIT_MS,
    OptionEntryRefusal,
    OptionMarket,
    OptionRun,
)
from app.services.option_watch import OptionWatchList

from ._option_floor import STARTING_TERMS, binding_for, one_call

_CLOSE_MS = 1_790_000_000_000
_LOWEST, _HIGHEST = int(ENTRY_WAIT_SECONDS.lowest), int(ENTRY_WAIT_SECONDS.highest)


def _run(entry_wait_seconds: int, *, taken_after_ms: int = 0) -> OptionRun:
    """A run of a bot deployed with ``entry_wait_seconds``, whose clock stands ``taken_after_ms`` after the bar."""
    terms = STARTING_TERMS.model_copy(update={"entry_wait_seconds": entry_wait_seconds})
    market = OptionMarket(
        quotes=lambda: None, watch=OptionWatchList(), entry_searches=lambda: None, model_inputs=lambda *_: None,
        line_budget=0, strikes_each_side=0, clock=lambda: _CLOSE_MS + taken_after_ms,
    )
    return OptionRun(binding_for(one_call(), terms=terms), market, held_contracts=tuple, contracts_to_watch=tuple)


def _latest_second_taken(entry_wait_seconds: int) -> int:
    """The last whole second after its bar closed at which an entry is still taken, by the runner's own guard."""
    taken = [
        second
        for second in range(ENTRY_OUTCOME_WAIT_MS // 1000 + 1)
        if _run(entry_wait_seconds, taken_after_ms=second * 1000).too_late_to_see_through(_CLOSE_MS) is None
    ]
    assert taken, f"no entry could ever be taken with a wait of {entry_wait_seconds} seconds"
    return max(taken)


@pytest.mark.parametrize("entry_wait_seconds", range(_LOWEST, _HIGHEST + 1))
def test_every_entry_wait_an_owner_is_offered_starts_a_run(entry_wait_seconds: int) -> None:
    _run(entry_wait_seconds)._require_the_entry_wait_fits()


def test_an_entry_taken_as_its_bar_closes_is_taken_under_the_longest_wait_offered() -> None:
    assert _run(_HIGHEST).too_late_to_see_through(_CLOSE_MS) is None


def test_a_wait_the_runner_could_never_see_through_is_not_offered() -> None:
    """The start guard does refuse some wait, so it is a guard, and every such wait is above what is offered."""
    refused = []
    for seconds in range(_HIGHEST + 1, ENTRY_OUTCOME_WAIT_MS // 1000 + 2):
        try:
            _run(seconds)._require_the_entry_wait_fits()
        except OptionPlanRefused as refusal:
            assert refusal.reason is OptionEffectRefusal.ENTRY_WAIT_OVER_OUTCOME_WAIT
            refused.append(seconds)
    assert refused and min(refused) > _HIGHEST


def test_the_reason_given_for_the_range_is_what_the_runner_does() -> None:
    """The sentence an owner reads says three things. Each is asked of the runner."""
    # "50 seconds from its decision bar's close to learn what an entry came to."
    assert f"{ENTRY_OUTCOME_WAIT_MS // 1000} seconds" in ENTRY_WAIT_RANGE_REASON
    # "An entry taken too late for its wait is refused by name, and nothing is sent."
    too_late = _run(_HIGHEST, taken_after_ms=ENTRY_OUTCOME_WAIT_MS).too_late_to_see_through(_CLOSE_MS)
    assert too_late is not None and too_late.reason_code == OptionEntryRefusal.TAKEN_TOO_LATE.value
    assert "refused by name" in ENTRY_WAIT_RANGE_REASON and "nothing is sent" in ENTRY_WAIT_RANGE_REASON
    assert "No order was sent" in too_late.why
    # "The longer the wait, the earlier that is."
    assert _latest_second_taken(_HIGHEST) < _latest_second_taken(STARTING_TERMS.entry_wait_seconds)
    assert _latest_second_taken(STARTING_TERMS.entry_wait_seconds) < _latest_second_taken(_LOWEST)
    assert "the longer the wait, the earlier" in ENTRY_WAIT_RANGE_REASON


def test_the_catalogue_serves_that_reason_with_the_entry_waits_range() -> None:
    assert order_term_starts()["entry_wait_seconds"].range_reason == ENTRY_WAIT_RANGE_REASON
