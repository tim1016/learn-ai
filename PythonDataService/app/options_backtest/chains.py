"""Chronological replay chains on the canonical options simulator (#3428).

Formula: the requested work is one chain per distinct slippage plus one per
  distinct rounded cash level, with their shared base chain replayed once.
Reference: app/options_backtest/run.py and app/options_backtest/cash_ladder.py.
Canonical implementation: run.py owns replay; this file plans and merges it.
Validated against: tests/options_backtest/test_chains.py.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import Decimal

from app.options_backtest.cash_ladder import DEFAULT_CASH_LADDER, ladder_levels
from app.options_backtest.data import RunData
from app.options_backtest.run import (
    DEFAULT_SLIPPAGE_LADDER,
    RunResult,
    run_sessions,
    slippage_tolerance,
    validate_run_sessions,
)
from app.options_backtest.setup import OptionsBacktestSetup


@dataclass(frozen=True)
class ReplayChain:
    key: str
    slippage_percent: Decimal
    starting_cash_usd: Decimal


def plan_chains(
    setup: OptionsBacktestSetup,
    *,
    slippage_ladder: Sequence[Decimal] = DEFAULT_SLIPPAGE_LADDER,
    cash_ladder: Sequence[Decimal] = DEFAULT_CASH_LADDER,
) -> tuple[ReplayChain, ...]:
    """Deduplicate the base exactly as the serial planner does, without a product."""
    return tuple(
        ReplayChain(f"slippage:{value}", value, setup.starting_cash_usd)
        for value in sorted({*slippage_ladder, setup.slippage_percent})
    ) + tuple(
        ReplayChain(f"cash:{cash}", setup.slippage_percent, cash)
        for cash in ladder_levels(setup.starting_cash_usd, cash_ladder)
        if cash != setup.starting_cash_usd
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
    own = setup.model_copy(update={
        "slippage_percent": chain.slippage_percent,
        "starting_cash_usd": chain.starting_cash_usd,
    })
    return run_sessions(own, sessions, data, slippage_ladder=(), cash_ladder=(),
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
    plan = plan_chains(setup, slippage_ladder=slippage_ladder, cash_ladder=cash_ladder)
    if set(results) != {chain.key for chain in plan}:
        raise ValueError("a whole run requires exactly every planned chain")
    expected_dates = tuple(sessions)
    from app.utils.session_anchors import et_date_at_ms

    for chain in plan:
        result = results[chain.key]
        expected_setup = setup.model_copy(update={"slippage_percent": chain.slippage_percent,
                                                   "starting_cash_usd": chain.starting_cash_usd})
        if result.setup != expected_setup or tuple(et_date_at_ms(s.session_open_ms) for s in result.sessions) != expected_dates:
            raise ValueError("a chain result does not match its frozen plan")
    base = next(chain for chain in plan if chain.slippage_percent == setup.slippage_percent
                and chain.starting_cash_usd == setup.starting_cash_usd)
    own = results[base.key]
    points = tuple(
        replace(results[chain.key].slippage[0], is_setups_own=chain.slippage_percent == setup.slippage_percent)
        for chain in plan if chain.key.startswith("slippage:")
    )
    by_cash = {chain.starting_cash_usd: results[chain.key] for chain in plan
               if chain.slippage_percent == setup.slippage_percent}
    levels = tuple(
        replace(by_cash[cash].summary.cash_ladder[0], multiple=multiple,
                is_setups_own=cash == setup.starting_cash_usd)
        for cash, multiple in ladder_levels(setup.starting_cash_usd, cash_ladder).items()
    )
    return RunResult(setup, own.sessions, replace(own.summary, cash_ladder=levels), points,
                     slippage_tolerance(points))
