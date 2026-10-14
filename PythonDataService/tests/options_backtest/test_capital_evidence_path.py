"""What the cash this path needed means, proved on replays: the starting cash at which a recorded path repeats (#3108).

The figure is worked from one replay's money steps. It claims one thing, which a replay can be asked: replayed again
from exactly that cash, the path is the same, every step shifted by the difference in cash; and replayed from a cent
less, a step of the path is short. A step is short if its entry is refused (the path then changes) or if its close
leaves free cash below zero (the path is the same and a close is unfunded). The sessions are the generated ones of
``test_replay_selling_invariants.py``: legs of different sizes, a cover that did not trade, a sold contract that
stopped trading, cash of 300.00 and over. The seeds are fixed, so a failure names the seed that reproduces it.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.options_backtest.capital_evidence import CapitalSession, capital_evidence
from app.options_backtest.results import FillGroup, MoneyStep, SessionOutcome
from tests.options_backtest._market import OPEN_MS, fee_per_contract, replay
from tests.options_backtest.test_replay_selling_invariants import (
    RULE_SAMPLE,
    RULE_SEEDS,
    SEEDS,
    _a_scene,
    _a_scene_with_stops,
    _Scene,
    _the_sample_every_pr_and_the_rest_daily,
)

D = Decimal


def _steps_before_the_first_unfunded_close(outcome: SessionOutcome) -> list[MoneyStep]:
    """Read from the steps and not from the module under test: every step up to a closing group that left the cash
    under the collateral."""
    before: list[MoneyStep] = []
    for step in outcome.money_steps:
        if step.group is FillGroup.CLOSING and step.cash_after_usd < step.collateral_after_usd:
            break
        before.append(step)
    return before


def _is_the_same_path(steps: list[MoneyStep], other: list[MoneyStep], shift: Decimal) -> bool:
    """Every step of ``steps`` is in ``other``, in its place, with the same holdings and the cash moved by ``shift``."""
    if len(other) < len(steps):
        return False
    return all(
        (a.at_ms, a.group, a.collateral_before_usd, a.collateral_after_usd, a.obligation_after_usd)
        == (b.at_ms, b.group, b.collateral_before_usd, b.collateral_after_usd, b.obligation_after_usd)
        and a.bought_back_with_cover_held == b.bought_back_with_cover_held
        and b.cash_before_usd == a.cash_before_usd + shift
        and b.cash_after_usd == a.cash_after_usd + shift
        for a, b in zip(steps, other, strict=False)
    )


@pytest.mark.parametrize("seed", SEEDS)
def test_replayed_from_the_cash_it_needed_a_path_repeats_and_from_a_cent_less_a_step_of_it_is_short(seed: int) -> None:
    _a_path_repeats_from_the_cash_it_needed(_a_scene(seed), seed)


@pytest.mark.parametrize("seed", _the_sample_every_pr_and_the_rest_daily(RULE_SEEDS, RULE_SAMPLE))
def test_the_same_holds_of_sessions_with_stops_whose_buy_backs_and_protection_closes_can_be_unfunded(
    seed: int,
) -> None:
    # A stop on a sold leg, or on the bought leg that protects it (#3107), makes closes the first family has not: the
    # buy-back is decided by a price and not by the clock, and a protection's group sells a bought leg with it. Every
    # pull request runs the sample; the other seeds of the family are marked slow and run daily.
    _a_path_repeats_from_the_cash_it_needed(_a_scene_with_stops(seed), seed)


def _a_path_repeats_from_the_cash_it_needed(scene: _Scene, seed: int) -> None:
    fee = fee_per_contract(scene.fee)
    outcome = replay(scene.written, scene.market, cash=scene.cash, fee_for=fee)
    start = D(scene.cash)
    evidence = capital_evidence([CapitalSession.of_outcome(OPEN_MS, outcome)], starting_cash=start)
    stretch = _steps_before_the_first_unfunded_close(outcome)

    # Every step of the stretch was funded, so the cash it needed is within the cash it had.
    assert D("0") <= evidence.cash_needed_usd <= start, f"seed {seed}"
    if evidence.cash_needed_usd == 0:
        assert stretch == [] or all(step.collateral_after_usd <= step.cash_after_usd - start for step in stretch)
        return

    needed = evidence.cash_needed_usd
    at_need = replay(scene.written, scene.market, cash=str(needed), fee_for=fee)
    assert _is_the_same_path(stretch, list(at_need.money_steps), needed - start), (
        f"seed {seed}: not repeated at {needed}"
    )
    if len(stretch) == len(outcome.money_steps):
        # No unfunded close: the whole session is the stretch, so it is the same session to the last event.
        assert at_need.trades == outcome.trades, f"seed {seed}"
        assert at_need.events == outcome.events, f"seed {seed}"

    a_cent_less = replay(scene.written, scene.market, cash=str(needed - D("0.01")), fee_for=fee)
    if _is_the_same_path(stretch, list(a_cent_less.money_steps), needed - D("0.01") - start):
        # Every entry of the path was still taken, so a close of it must have been left short.
        short = [
            step
            for step in a_cent_less.money_steps[: len(stretch)]
            if step.group is FillGroup.CLOSING and step.cash_after_usd < step.collateral_after_usd
        ]
        assert short, f"seed {seed}: nothing was short at {needed - D('0.01')}"
