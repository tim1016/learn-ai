"""Tests for the Layer A ``ExecutionDivergenceClassifier``.

Asserts on the classification output — the right category, magnitude,
applied tolerance, severity, and source-row identifiers — for synthetic
matched-ledger rows with deliberately-injected divergence.
"""

from __future__ import annotations

import math

from app.engine.live.artifacts import DecisionRow, ExecutionRow
from app.engine.live.divergence.execution_divergence import (
    ExecutionDivergenceCategory,
    ExecutionTolerances,
    Severity,
    classify_execution_divergences,
)
from app.engine.live.divergence.execution_matcher import MatchedLedgerRow


def _decision(*, bar_close_ms: int = 1000, intended_price: float = 100.0) -> DecisionRow:
    return DecisionRow(
        bar_close_ms=bar_close_ms,
        signal="ENTER",
        intended_price=intended_price,
        strategy_instance_id="spy-ema:inst-1",
        intended_action="BUY",
        decision_latency_ms=10.0,
    )


def _execution(
    *,
    fill_price: float = 100.0,
    ts_ms: int = 1005,
    fill_quantity: int = 10,
    fee: float = 1.0,
) -> ExecutionRow:
    return ExecutionRow(
        ts_ms=ts_ms,
        exec_id="exec-1",
        perm_id=777,
        client_order_id="co-1",
        account_id="DU123",
        symbol="SPY",
        fill_quantity=fill_quantity,
        fill_price=fill_price,
        fee=fee,
    )


def test_slippage_out_of_tolerance_emits_divergence() -> None:
    # intended 100.00, filled 100.05 → 5 bps slippage, tolerance is 2 bps.
    row = MatchedLedgerRow(
        decision=_decision(intended_price=100.0),
        execution=_execution(fill_price=100.05),
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    slippage = [d for d in divergences if d.category is ExecutionDivergenceCategory.SLIPPAGE]
    assert len(slippage) == 1
    d = slippage[0]
    assert abs(d.magnitude - 5.0) < 1e-9  # bps
    assert d.applied_tolerance == ExecutionTolerances().slippage_bps
    assert d.severity is Severity.GATING
    assert d.exec_id == "exec-1"
    assert d.client_order_id == "co-1"
    assert d.perm_id == 777
    assert d.bar_close_ms == 1000


def test_slippage_at_tolerance_boundary_emits_nothing() -> None:
    # intended 100.00, filled 100.02 → exactly 2 bps == tolerance → no divergence.
    row = MatchedLedgerRow(
        decision=_decision(intended_price=100.0),
        execution=_execution(fill_price=100.02),
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    assert not [
        d for d in divergences if d.category is ExecutionDivergenceCategory.SLIPPAGE
    ]


def test_fill_latency_out_of_tolerance_emits_divergence() -> None:
    # Bar closes at 1000; fill lands 2500ms later → exceeds 2000ms tolerance.
    row = MatchedLedgerRow(
        decision=_decision(bar_close_ms=1000),
        execution=_execution(ts_ms=3500),
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    latency = [
        d for d in divergences if d.category is ExecutionDivergenceCategory.LATENCY_FILL
    ]
    assert len(latency) == 1
    assert latency[0].magnitude == 2500.0  # ms
    assert latency[0].applied_tolerance == ExecutionTolerances().latency_fill_ms
    assert latency[0].severity is Severity.GATING


def test_prompt_fill_emits_no_latency_divergence() -> None:
    row = MatchedLedgerRow(
        decision=_decision(bar_close_ms=1000),
        execution=_execution(ts_ms=1005),  # 5ms latency, well within tolerance
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    assert not [
        d for d in divergences if d.category is ExecutionDivergenceCategory.LATENCY_FILL
    ]


def test_submit_latency_out_of_tolerance_emits_divergence() -> None:
    decision = DecisionRow(
        bar_close_ms=1000,
        signal="ENTER",
        intended_price=100.0,
        strategy_instance_id="spy-ema:inst-1",
        intended_action="BUY",
        decision_latency_ms=750.0,  # exceeds 500ms submit tolerance
    )
    row = MatchedLedgerRow(
        decision=decision, execution=_execution(), match_basis="client_order_id"
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    submit = [
        d for d in divergences if d.category is ExecutionDivergenceCategory.LATENCY_SUBMIT
    ]
    assert len(submit) == 1
    assert submit[0].magnitude == 750.0
    assert submit[0].applied_tolerance == ExecutionTolerances().latency_submit_ms
    assert submit[0].severity is Severity.GATING


def test_missed_half_pair_emits_missed_divergence() -> None:
    row = MatchedLedgerRow(decision=_decision(), execution=None, match_basis="unmatched")

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    missed = [d for d in divergences if d.category is ExecutionDivergenceCategory.MISSED]
    assert len(missed) == 1
    assert missed[0].severity is Severity.GATING
    assert missed[0].bar_close_ms == 1000


def test_extra_half_pair_emits_extra_divergence() -> None:
    row = MatchedLedgerRow(decision=None, execution=_execution(), match_basis="unmatched")

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    extra = [d for d in divergences if d.category is ExecutionDivergenceCategory.EXTRA]
    assert len(extra) == 1
    assert extra[0].severity is Severity.GATING
    assert extra[0].exec_id == "exec-1"


def test_partial_flag_emits_partial_divergence() -> None:
    row = MatchedLedgerRow(
        decision=_decision(),
        execution=_execution(),
        match_basis="client_order_id",
        flags=("partial",),
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    partial = [d for d in divergences if d.category is ExecutionDivergenceCategory.PARTIAL]
    assert len(partial) == 1
    assert partial[0].severity is Severity.GATING


def test_rejected_is_never_classified_from_fill_data() -> None:
    # REJECTED is an unreachable stub until an order-lifecycle artifact exists.
    # A signalled decision with no fill must classify as MISSED, never REJECTED
    # (a rejection is indistinguishable from a missed order in fill-only data).
    row = MatchedLedgerRow(decision=_decision(), execution=None, match_basis="unmatched")

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    assert any(d.category is ExecutionDivergenceCategory.MISSED for d in divergences)
    assert not [
        d for d in divergences if d.category is ExecutionDivergenceCategory.REJECTED
    ]


def test_commission_drift_emits_non_gating_divergence() -> None:
    # 10 sh @ $100 → IBKR predicts $1.00 (min-per-order floor). Recorded $1.50
    # drifts by $0.50, well past the $0.01 tolerance.
    row = MatchedLedgerRow(
        decision=_decision(),
        execution=_execution(fill_quantity=10, fill_price=100.0, fee=1.50),
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    drift = [
        d for d in divergences if d.category is ExecutionDivergenceCategory.COMMISSION_DRIFT
    ]
    assert len(drift) == 1
    assert abs(drift[0].magnitude - 0.50) < 1e-9
    # Non-gating until the commissionReport callback stabilises (Branch-A analogue).
    assert drift[0].severity is Severity.NON_GATING


def test_commission_matching_prediction_emits_no_drift() -> None:
    row = MatchedLedgerRow(
        decision=_decision(),
        execution=_execution(fill_quantity=10, fill_price=100.0, fee=1.00),
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    assert not [
        d for d in divergences if d.category is ExecutionDivergenceCategory.COMMISSION_DRIFT
    ]


def test_missing_fee_emits_commission_missing_not_drift() -> None:
    row = MatchedLedgerRow(
        decision=_decision(),
        execution=_execution(fill_quantity=10, fill_price=100.0, fee=math.nan),
        match_basis="client_order_id",
    )

    divergences = classify_execution_divergences(row, ExecutionTolerances())

    missing = [
        d
        for d in divergences
        if d.category is ExecutionDivergenceCategory.COMMISSION_MISSING
    ]
    assert len(missing) == 1
    assert missing[0].severity is Severity.NON_GATING
    # A missing fee is not also reported as drift.
    assert not [
        d for d in divergences if d.category is ExecutionDivergenceCategory.COMMISSION_DRIFT
    ]
