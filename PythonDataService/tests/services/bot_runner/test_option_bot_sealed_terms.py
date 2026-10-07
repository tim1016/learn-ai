"""An option bot trades under the four order terms sealed with it at Deploy, and under no others (#2913).

The real runner on the real Clerk, as in the two files these scenes come
from. Nothing here replaces the runner's read of the terms: each bot's
binding carries them, as a Deploy writes them.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.option_effects import OptionEffectRefusal, OptionPlanRefused
from app.options.requirement import StructureMoneyRefusal
from app.schemas.option_order_terms import SealedOptionOrderTerms

from ._option_floor import STARTING_TERMS, Floor
from ._paper_option_floor import PaperFloor
from .test_option_bot_dry_run import (
    _entry_then_three_bars,
    _nothing_was_sent,
    _the_call_is_quoted_nine_percent_wide,
    on_the_floor,  # noqa: F401 -- pytest fixture, used by name
)
from .test_option_bot_entry_outcome import (
    TAKEN_MS,
    _two_greens,
    on_paper,  # noqa: F401 -- pytest fixture, used by name
)


def _sealed_with(floor: Floor, **terms: object) -> SealedOptionOrderTerms:
    """Seal ``floor``'s bot with the starting terms, but for ``terms``. Before its run is registered."""
    sealed = SealedOptionOrderTerms.model_validate({**STARTING_TERMS.model_dump(mode="json"), **terms})
    floor.binding = floor.binding.model_copy(update={"option_order_terms": sealed})
    return sealed


async def test_on_paper_the_entry_wait_sealed_with_the_bot_sets_when_the_clerk_cancels(
    on_paper: Callable[..., PaperFloor],  # noqa: F811
) -> None:
    """A bot deployed with an entry wait of seven seconds and a close ninety minutes before the session's.
    The Clerk keeps those terms with the entry and cancels it seven seconds after it took it."""
    paper = on_paper()
    sealed = _sealed_with(paper, entry_wait_seconds=7, expiry_close_lead_minutes=90)
    await paper.open()

    await paper.run(_two_greens())

    entry = paper.entry_as_the_clerk_kept_it()
    assert entry.terms is not None and entry.terms.close_terms == sealed.close
    assert entry.terms.close_terms.expiry_close_lead_ms == 90 * 60_000
    assert entry.cancel_at_ms == TAKEN_MS + 7_000


async def test_the_two_limits_sealed_with_the_bot_are_the_ones_its_entry_is_judged_by(
    on_the_floor: Callable[..., Floor],  # noqa: F811
) -> None:
    """A bot deployed with a gap limit of 50% and a cost limit of 5%. A call quoted 9% wide is inside the
    first and over the second, so the entry is refused for its cost. Under the terms a Deploy starts from
    the same market is refused for its gap."""
    floor = on_the_floor()
    _sealed_with(floor, gap_limit_percent=Decimal(50), cost_limit_percent=Decimal(5))
    await floor.open()

    ran = await floor.run(_entry_then_three_bars(_the_call_is_quoted_nine_percent_wide))

    assert [code for code, _ in ran.blocked()] == [StructureMoneyRefusal.COST_OVER_LIMIT.value]
    _nothing_was_sent(floor, ran)


async def test_a_bot_sealed_with_no_terms_is_refused_as_its_run_starts_and_asks_for_nothing(
    on_the_floor: Callable[..., Floor],  # noqa: F811
) -> None:
    """Nothing is assumed for a bot that carries no order terms: no quote line, no bar, no entry."""
    floor = on_the_floor()
    floor.binding = floor.binding.model_copy(update={"option_order_terms": None})
    await floor.open()

    with pytest.raises(OptionPlanRefused) as refused:
        await floor.run(_entry_then_three_bars())

    assert refused.value.reason is OptionEffectRefusal.ORDER_TERMS_NOT_SEALED
    assert floor.asked_of_the_clerk == []
    assert floor.orders_on_the_books() == 0
