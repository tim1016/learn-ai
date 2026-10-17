"""Calendar-bound planning and conservative workload reservation for Options search.

Formula: search bound per window + all baseline/fold/final/stress/materialization
  and restored-proof work must fit before any candidate runs.
Reference: https://github.com/tim1016/learn-ai/issues/3332.
Canonical implementation: this file.
Validated against: tests/research/options_golden_search/test_protocol_search.py.
"""
from __future__ import annotations

import math
from datetime import date
from typing import Any

from app.lean_sidecar.trading_calendar import expected_sessions, session_open_ms_utc, trading_session_count
from app.options_backtest.setup import OptionsBacktestSetup
from app.research.golden_search.activity import window_activity
from app.research.options_golden_search.protocol import (
    Fold,
    FrozenModel,
    Knob,
    OptionsSearchProtocol,
    Policy,
    Window,
)
from app.research.walk_forward_study.folds import add_months, plan_folds
from app.utils.session_anchors import et_date_at_ms, et_midnight_ms


def freeze_window(key: str, start_ms: int, end_ms: int, rate: int) -> Window:
    """Freeze canonical calendar sessions, activity and descriptive annualization."""
    if any(et_midnight_ms(et_date_at_ms(ms)) != ms for ms in (start_ms, end_ms)):
        raise ValueError("window boundaries must be whole ET calendar dates")
    start, last = et_date_at_ms(start_ms), et_date_at_ms(end_ms - 1)
    days = expected_sessions(start, last)
    yearly = [trading_session_count(date(year, 1, 1), date(year, 12, 31)) for year in range(start.year, last.year + 1)]
    return Window(key=key, start_ms=start_ms, end_ms=end_ms,
                  session_ms=tuple(session_open_ms_utc(day) for day in days),
                  annualization_sessions=sum(yearly) / len(yearly),
                  activity_floor=window_activity(rate, start_ms, end_ms)["minimum_trades"])


def freeze_protocol(setup: OptionsBacktestSetup, knobs: tuple[Knob, ...], *, start_ms: int, end_ms: int,
                    train_months: int = 6, test_months: int = 2, final_months: int = 6,
                    policy: Policy | None = None, snapshot_id: str = "pending", identities: dict[str, str] | None = None,
                    **kwargs: Any) -> OptionsSearchProtocol:
    """Require whole-month development and complete folds; never shorten requested evidence."""
    policy = policy or Policy()
    if min(train_months, test_months, final_months) < 1:
        raise ValueError("all interval lengths must be positive whole months")
    end = et_date_at_ms(end_ms)
    final_start = add_months(end, -final_months)
    if add_months(final_start, final_months) != end:
        raise ValueError("final interval must contain whole months without month-end drift")
    final_start_ms = et_midnight_ms(final_start)
    raw_folds = plan_folds(start=et_date_at_ms(start_ms), end_exclusive=final_start,
                           training_months=train_months, test_months=test_months)
    # The shared planner snaps weekend boundaries to sessions. Frozen Options windows retain
    # original month boundaries so each test covers its declared calendar interval exactly once.
    folds = tuple(Fold(index=fold.fold_index,
                       training=freeze_window(f"training_{fold.fold_index}",
                           et_midnight_ms(add_months(et_date_at_ms(start_ms), fold.fold_index * test_months)),
                           et_midnight_ms(add_months(et_date_at_ms(start_ms), fold.fold_index * test_months + train_months)),
                           policy.min_activity_per_year),
                       forward=freeze_window(f"forward_{fold.fold_index}",
                           et_midnight_ms(add_months(et_date_at_ms(start_ms), fold.fold_index * test_months + train_months)),
                           et_midnight_ms(add_months(et_date_at_ms(start_ms), (fold.fold_index + 1) * test_months + train_months)),
                           policy.min_activity_per_year)) for fold in raw_folds)
    return OptionsSearchProtocol(setup=setup, knobs=knobs, policy=policy,
        development=freeze_window("development", start_ms, final_start_ms, policy.min_activity_per_year),
        final=freeze_window("final", final_start_ms, end_ms, policy.min_activity_per_year), folds=folds,
        snapshot_id=snapshot_id, identities=identities or {}, **kwargs)


class BudgetPlan(FrozenModel):
    selection_per_window: int
    selection_total: int
    baseline: int
    forward: int
    final: int
    proof_and_materialization: int
    mandatory_downstream: int
    unique_upper_bound: int
    replay_session_upper_bound: int
    leg_session_upper_bound: int
    fits: bool
    warnings: tuple[str, ...]


def budget_plan(protocol: OptionsSearchProtocol) -> BudgetPlan:
    """Reserve conservative unique evaluations; cache deduplication never licenses spending reservations."""
    axes = protocol.search_knobs
    grid = math.prod(len(k.values) for k in axes)
    if protocol.method == "grid":
        selection = grid + 1  # the original is scored even if outside searched categories
    else:
        selection = 1 + protocol.zoom.passes * (protocol.zoom.refinements + 1) * sum(min(len(k.values), protocol.zoom.points) + 1 for k in axes)
    by_path = {k.path: k for k in axes}
    # Pair audits can propose a winner and repeat during every independent training search.
    selection += sum(len(by_path[a].values) * len(by_path[b].values) for a, b in protocol.pair_audits)
    n = len(protocol.folds)
    baseline = 1 + 2 * n
    forward = n
    final = (protocol.finalist_count + 1) * (1 + len(protocol.stress))
    # A complete ordinary source has base + configured slippage and capital ladders.
    # Freeze the existing ordinary-run default ladder count conservatively, plus two full proof replays.
    from app.options_backtest.run import DEFAULT_CASH_LADDER, DEFAULT_SLIPPAGE_LADDER
    complete = 1 + len(DEFAULT_SLIPPAGE_LADDER) + len(DEFAULT_CASH_LADDER)
    proof = 3 * complete
    downstream = baseline + forward + final + proof
    total = (n + 1) * selection + downstream
    session_work = selection * (len(protocol.development.session_ms) + sum(len(f.training.session_ms) for f in protocol.folds))
    session_work += len(protocol.development.session_ms) + sum(len(f.training.session_ms) + 2 * len(f.forward.session_ms) for f in protocol.folds)
    session_work += (final + proof) * len(protocol.final.session_ms)
    warnings: list[str] = []
    expected_final = protocol.final.activity_floor
    if expected_final < protocol.policy.min_final_sessions:
        warnings.append("The final interval's configured activity expectation is below the approval floor; use a longer final interval before lock.")
    return BudgetPlan(selection_per_window=selection, selection_total=(n + 1) * selection, baseline=baseline,
        forward=forward, final=final, proof_and_materialization=proof, mandatory_downstream=downstream,
        unique_upper_bound=total, replay_session_upper_bound=session_work,
        leg_session_upper_bound=session_work * len(protocol.setup.legs), fits=total <= min(protocol.budget.evaluation_cap, protocol.budget.max_execution_attempts),
        warnings=tuple(warnings))


def pooled_window(protocol: OptionsSearchProtocol) -> Window:
    return freeze_window("pooled_forward", protocol.folds[0].forward.start_ms,
                         protocol.folds[-1].forward.end_ms, protocol.policy.min_activity_per_year)
