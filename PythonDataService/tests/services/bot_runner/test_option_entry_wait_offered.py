"""The offered entry waits leave time for Clerk acceptance and outcome proof."""

from __future__ import annotations

import pytest

from app.options.order_terms import ENTRY_WAIT_RANGE_REASON, ENTRY_WAIT_SECONDS
from app.options.structure_catalogue import order_term_starts
from app.services.bot_option_run import ENTRY_OUTCOME_WAIT_MS, OptionMarket, OptionRun
from app.services.option_watch import OptionWatchList

from ._option_floor import STARTING_TERMS, binding_for, one_call

_CLOSE_MS = 1_790_000_000_000
_LOWEST, _HIGHEST = int(ENTRY_WAIT_SECONDS.lowest), int(ENTRY_WAIT_SECONDS.highest)


def _run(entry_wait_seconds: int) -> OptionRun:
    terms = STARTING_TERMS.model_copy(update={"entry_wait_seconds": entry_wait_seconds})
    market = OptionMarket(
        quotes=lambda: None, watch=OptionWatchList(), entry_searches=lambda: None, model_inputs=lambda *_: None,
        line_budget=0, strikes_each_side=0, clock=lambda: _CLOSE_MS,
    )
    return OptionRun(binding_for(one_call(), terms=terms), market, held_contracts=tuple, contracts_to_watch=tuple)


@pytest.mark.parametrize("entry_wait_seconds", range(_LOWEST, _HIGHEST + 1))
def test_every_entry_wait_an_owner_is_offered_constructs_a_run(entry_wait_seconds: int) -> None:
    _run(entry_wait_seconds)


def test_the_offered_range_leaves_time_for_a_cancel_proof() -> None:
    assert _HIGHEST * 1000 < ENTRY_OUTCOME_WAIT_MS
    assert "50 seconds" in ENTRY_WAIT_RANGE_REASON
    assert "refused by name" in ENTRY_WAIT_RANGE_REASON
    assert "the longer the wait, the earlier" in ENTRY_WAIT_RANGE_REASON


def test_the_catalogue_serves_that_reason_with_the_entry_waits_range() -> None:
    assert order_term_starts()["entry_wait_seconds"].range_reason == ENTRY_WAIT_RANGE_REASON
