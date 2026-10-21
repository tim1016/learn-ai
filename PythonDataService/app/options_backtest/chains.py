"""Chronological replay chains on the canonical options simulator (#3428).

Formula: the requested work is one chain per distinct slippage plus one per
  distinct rounded cash level, with their shared base chain replayed once.
Reference: app/options_backtest/run.py and app/options_backtest/cash_ladder.py.
Canonical implementation: run.py plans, replays and assembles; this file replays one chain apiece and merges them.
Validated against: tests/options_backtest/test_chains.py.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal

from app.options_backtest.cash_ladder import DEFAULT_CASH_LADDER
from app.options_backtest.data import RunData
from app.options_backtest.run import (
    DEFAULT_SLIPPAGE_LADDER,
    ReplayChain,
    RunResult,
    assemble_run_result,
    plan_run,
    run_sessions,
    validate_run_sessions,
)
from app.options_backtest.setup import OptionsBacktestSetup
from app.utils.session_anchors import et_date_at_ms


def plan_chains(
    setup: OptionsBacktestSetup,
    *,
    slippage_ladder: Sequence[Decimal] = DEFAULT_SLIPPAGE_LADDER,
    cash_ladder: Sequence[Decimal] = DEFAULT_CASH_LADDER,
) -> tuple[ReplayChain, ...]:
    """The run's plan without the cash level the own slippage chain already replays."""
    return tuple(
        chain
        for chain in plan_run(setup, slippage_ladder=slippage_ladder, cash_ladder=cash_ladder)
        if not chain.repeats_the_own_chain(setup)
    )


def replay_chain(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    chain: ReplayChain,
    data: RunData,
    *,
    should_stop: Callable[[], bool] = lambda: False,
    on_session: Callable[[int, int, date], None] = lambda done, total, day: None,
) -> RunResult:
    """One cash path; every fill, fee, mark and decimal operation stays canonical."""
    return run_sessions(chain.setup_for(setup), sessions, data, slippage_ladder=(), cash_ladder=(),
                        should_stop=should_stop, on_session=on_session)


def merge_chains(
    setup: OptionsBacktestSetup,
    sessions: Sequence[date],
    results: Mapping[str, RunResult],
    *,
    slippage_ladder: Sequence[Decimal] = DEFAULT_SLIPPAGE_LADDER,
    cash_ladder: Sequence[Decimal] = DEFAULT_CASH_LADDER,
) -> RunResult:
    """Refuse missing/foreign chains, then merge in the serial plan's order."""
    validate_run_sessions(setup, sessions)
    plan = plan_run(setup, slippage_ladder=slippage_ladder, cash_ladder=cash_ladder)
    replayed = {chain.key: chain for chain in plan if not chain.repeats_the_own_chain(setup)}
    if set(results) != set(replayed):
        raise ValueError("a whole run requires exactly every planned chain")
    expected_dates = tuple(sessions)
    for key, chain in replayed.items():
        result = results[key]
        if (result.setup != chain.setup_for(setup)
                or tuple(et_date_at_ms(s.session_open_ms) for s in result.sessions) != expected_dates):
            raise ValueError("a chain result does not match its frozen plan")
    own = results[ReplayChain("slippage", setup.slippage_percent, setup.starting_cash_usd).key]

    def replayed_by(chain: ReplayChain) -> RunResult:
        return own if chain.repeats_the_own_chain(setup) else results[chain.key]

    points = tuple(
        replace(replayed_by(chain).slippage[0], is_setups_own=chain.slippage_percent == setup.slippage_percent)
        for chain in plan if chain.kind == "slippage"
    )
    levels = tuple(
        replace(replayed_by(chain).summary.cash_ladder[0], multiple=chain.multiple,
                is_setups_own=chain.repeats_the_own_chain(setup))
        for chain in plan if chain.kind == "cash"
    )
    return assemble_run_result(setup, own.sessions, points, levels)
