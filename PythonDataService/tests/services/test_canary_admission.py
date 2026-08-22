"""Unit coverage for the guarded Alpaca Paper canary gate (issue #1729).

`tests/services/test_run_admission.py` exercises the composed gate through
`evaluate_run_admission`; this file tests the pure building blocks in
isolation: the allowlist's shipped emptiness, exact-pairing membership, gate
applicability, and the Clerk-proved rollback boundary.
"""

from __future__ import annotations

import pytest

from app.schemas.canary_admission import CanaryRollbackDecision
from app.services.canary_admission import (
    CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS,
    canary_gate_applies,
    canary_pairing_admitted,
    evaluate_canary_rollback,
)

_NOW = 1_700_000_010_000


def test_canary_allowlist_ships_empty() -> None:
    """#1729 safety constraint: no production entry may ever slip in without
    this test failing. Operational activation is an explicit human edit to
    this constant, reviewed as its own change -- never a default, a
    dev-convenience shortcut, or an env-var fallback."""
    assert frozenset() == CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS


@pytest.mark.parametrize(
    ("mode", "program_build_state", "expected"),
    [
        ("trade", "PROVEN", True),
        ("trade", "UNPROVEN", False),
        ("trade", "NOT_APPLICABLE", False),
        ("dry_run", "PROVEN", False),
        ("log_only", "PROVEN", False),
    ],
)
def test_canary_gate_applies_only_to_proven_trade_mode_signal_programs(
    mode: str, program_build_state: str, expected: bool
) -> None:
    assert canary_gate_applies(mode=mode, program_build_state=program_build_state) is expected


def test_canary_pairing_admitted_matches_only_the_exact_tuple(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )

    assert canary_pairing_admitted(program_key="ema_crossover_signal", account_id="paper-account") is True
    assert canary_pairing_admitted(program_key="ema_crossover_signal", account_id="other-account") is False
    assert canary_pairing_admitted(program_key="sma_crossover_signal", account_id="paper-account") is False


def test_canary_pairing_admitted_is_false_against_the_empty_shipped_allowlist() -> None:
    assert canary_pairing_admitted(program_key="ema_crossover_signal", account_id="paper-account") is False


def test_canary_rollback_admitted_when_flat() -> None:
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOPPED_FLAT",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is True
    assert decision.reason_code == "CANARY_ROLLBACK_ADMITTED"
    assert isinstance(decision, CanaryRollbackDecision)


def test_canary_rollback_admitted_with_an_approved_carried_exposure() -> None:
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is True
    assert decision.reason_code == "CANARY_ROLLBACK_ADMITTED"


def test_canary_rollback_refused_when_flatten_is_required() -> None:
    """#1729 AC10: rollback stops the canary ONLY at a Clerk-proved safe
    boundary -- unapproved attributed exposure is not one."""
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOP_REQUIRES_FLATTEN",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_ROLLBACK_REQUIRES_FLATTEN"
    assert decision.next_step is not None


def test_canary_rollback_refused_when_custody_is_unprovable() -> None:
    """#1729 AC10: an unprovable boundary is refused outright, unlike an
    ordinary Stop, which records `STOPPED_CUSTODY_UNPROVABLE` honestly and
    still proceeds -- rollback is the stricter of the two."""
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOPPED_CUSTODY_UNPROVABLE",
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_ROLLBACK_BOUNDARY_UNPROVABLE"


def test_canary_rollback_decision_carries_the_exact_stop_outcome_it_classified() -> None:
    decision = evaluate_canary_rollback(
        strategy_instance_id="sid-1",
        stop_outcome="STOP_REQUIRES_FLATTEN",
        evaluated_at_ms=_NOW,
    )

    assert decision.stop_outcome == "STOP_REQUIRES_FLATTEN"
    assert decision.strategy_instance_id == "sid-1"
    assert decision.evaluated_at_ms == _NOW
