"""The live adapter's decision cycle is always present, so refusing is total.

Before the staged Signal Session protocol (#1730) a strategy mutated its own
position state at signal *emission*, so a refused candidate needed a
compensating rollback and the adapter carried ``rollback_blocked_entry`` /
``rollback_blocked_exit`` callables to apply it. Position custody now moves only
inside ``commit_signal_decision``, which a session runs only on
``Settlement.COMMIT``, so a refusal has nothing to unwind and DISCARD is the
whole disposition.

Every evaluation the adapter yields owns a decision cycle to settle. It holds because
``_build_signal_strategy`` refuses to construct a runtime for a strategy with no
registered Signal Program -- without a session there is no stage, so such a
runtime could only stream bars and decide nothing, which is the failure shape
this module works hardest to make impossible. The test proves the program-less
runtime is unconstructable rather than merely unused.
"""

from __future__ import annotations

import pytest

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.services.bot_trade_strategy import (
    _build_signal_strategy,
    supported_alpaca_paper_strategy_keys,
)


def _key_without_a_signal_program() -> str:
    """Return a key the live adapter cannot build a Signal Program for.

    This used to derive one from the registry, precisely so it would not
    quietly stop covering anything when a strategy got promoted. Every
    registered strategy now owns a Signal Program (the signal/asset decoupling
    sweep removed the last that did not), so the registry no longer supplies a
    subject -- and that is the rule holding, not the test rotting.

    An unregistered key still exercises the refusal, and it is the case that
    now actually occurs in the field: a durable binding can name a strategy a
    later build no longer registers, and the live adapter must refuse it
    rather than run something else.
    """
    live = supported_alpaca_paper_strategy_keys()
    candidate = next(
        (key for key in sorted(_STRATEGY_REGISTRY) if key not in live),
        "retired-strategy-not-in-this-build",
    )
    assert candidate not in live
    return candidate


def test_building_a_live_runtime_refuses_a_strategy_with_no_signal_program() -> None:
    key = _key_without_a_signal_program()

    with pytest.raises(ValueError, match="not live-executable"):
        _build_signal_strategy(key, "SPY", None)
