"""Typed uncertainty envelope and admission tests (#1380, Part A).

Covers: BOT uncertainty blocks only the affected bot; ACCOUNT_CLERK blocks
every bot; unrecognized/default-shaped uncertainties fail closed; admission
is one function checking both uncertainties and the #1378 hold mechanism;
raise/resolve are idempotent.
"""

from __future__ import annotations

import dataclasses
import typing
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

import app.broker.alpaca.clerk.sqlite.order_evidence as order_evidence
from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.folds import POSITION_QTY_EPSILON
from app.broker.alpaca.clerk.sqlite.models import TransitionInput
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    _REASON_POLICIES,
    BROKER_SNAPSHOT_STALE_REASON_CODE,
    EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    EXIT_NOT_FLAT_REASON_CODE,
    EXIT_STUCK_REASON_CODE,
    ORDER_OUTCOME_UNKNOWN_REASON_CODE,
    POSITION_DRIFT_REASON_CODE,
    RECONCILIATION_INCOMPLETE_REASON_CODE,
    AdmissionBlockedError,
    AgePolicy,
    Capability,
    CauseCleared,
    ReasonPolicy,
    RedriveThenEscalate,
    ReductionIntent,
    RefusalClass,
    VoidAfter,
    admit_new_exposure,
    classify_admission_refusal,
    decide_capability,
    raise_uncertainty,
    reason_age_policy,
    require_admission,
    resolve_reconciliation_uncertainty,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import (
    ExitStuckCause,
    OrderOutcomeUnknownCause,
    PositionDriftCause,
    PositionDriftObservation,
    UnknownOrderIdentity,
)

ACCOUNT_ID = "PA-TEST"
SID = "spy-bot"
OTHER_SID = "qqq-bot"


def _clock_seq() -> Callable[[], int]:
    counter = {"t": 1_700_000_000_000}

    def clock() -> int:
        counter["t"] += 1
        return counter["t"]

    return clock


@pytest.fixture
def repo(tmp_path: Path) -> Iterator[ClerkSqliteRepository]:
    r = ClerkSqliteRepository.initialize(account_id=ACCOUNT_ID, artifacts_root=tmp_path, clock=_clock_seq())
    r.register_strategy_instance(strategy_instance_id=SID, symbol="SPY", config_hash="h1")
    r.register_strategy_instance(strategy_instance_id=OTHER_SID, symbol="QQQ", config_hash="h2")
    yield r
    r.close()


def _raise(
    repo: ClerkSqliteRepository,
    *,
    strategy_instance_id: str | None,
    **overrides: Any,
) -> bool:
    kwargs: dict[str, Any] = {
        "reason_code": "ORDER_OUTCOME_UNKNOWN",
        "headline": "headline",
        "explanation": "explanation",
        "operator_impact": "operator impact",
        "next_step": "next step",
    }
    kwargs.update(overrides)
    return raise_uncertainty(repo, strategy_instance_id=strategy_instance_id, **kwargs)


def test_cause_encoders_emit_the_unique_sorted_order_required_by_decoders() -> None:
    position_cause = PositionDriftCause(
        positions=(
            PositionDriftObservation(symbol="SPY", broker_qty=2.0, attributed_qty=1.0),
            PositionDriftObservation(symbol="QQQ", broker_qty=3.0, attributed_qty=1.0),
        )
    )
    order_cause = OrderOutcomeUnknownCause(
        identities=(
            UnknownOrderIdentity(effect_operation_id="effect:b", order_ref="order:2"),
            UnknownOrderIdentity(effect_operation_id="effect:a", order_ref="order:1"),
        )
    )

    assert PositionDriftCause.from_mapping(position_cause.to_mapping()).positions[0].symbol == "QQQ"
    assert (
        OrderOutcomeUnknownCause.from_mapping(order_cause.to_mapping()).identities[0].effect_operation_id
        == "effect:a"
    )


@pytest.mark.parametrize(
    ("quantity", "allowed", "reason_code"),
    (
        pytest.param(
            POSITION_QTY_EPSILON / 2,
            True,
            None,
            id="below_absolute_share_tolerance",
        ),
        pytest.param(
            POSITION_QTY_EPSILON,
            False,
            "ATTRIBUTED_EXPOSURE_EXISTS",
            id="at_inclusive_absolute_share_tolerance",
        ),
        pytest.param(
            POSITION_QTY_EPSILON * 2,
            False,
            "ATTRIBUTED_EXPOSURE_EXISTS",
            id="above_absolute_share_tolerance",
        ),
    ),
)
def test_new_exposure_uses_the_canonical_attributed_quantity_boundary_fixture(
    repo: ClerkSqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    quantity: float,
    allowed: bool,
    reason_code: str | None,
) -> None:
    """Pin the below/at/above fixture for the one Clerk exposure boundary."""
    monkeypatch.setattr(repo, "attributed_positions_for_strategy", lambda _sid: {"SPY": quantity})

    decision = decide_capability(
        repo=repo,
        capability=Capability.NEW_EXPOSURE,
        strategy_instance_id=SID,
    )

    assert (decision.allowed, decision.reason_code) == (allowed, reason_code)
# ── raise_uncertainty / resolve_uncertainty ─────────────────────────────────


def test_raise_uncertainty_account_clerk_scope(repo: ClerkSqliteRepository) -> None:
    created = _raise(repo, strategy_instance_id=None)
    assert created is True
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="ORDER_OUTCOME_UNKNOWN", strategy_instance_id=None
    )
    assert uncertainty is not None


def test_raise_uncertainty_bot_scope(repo: ClerkSqliteRepository) -> None:
    created = _raise(repo, strategy_instance_id=SID)
    assert created is True
    uncertainty = repo.active_uncertainty(
        scope="CUSTODY_SUBJECT",
        reason_code="ORDER_OUTCOME_UNKNOWN",
        strategy_instance_id=SID,
    )
    assert uncertainty is not None


def test_raise_uncertainty_is_idempotent(repo: ClerkSqliteRepository) -> None:
    first = _raise(repo, strategy_instance_id=SID)
    before = len(repo.custody_transitions())
    second = _raise(repo, strategy_instance_id=SID)
    assert first is True
    assert second is False
    assert len(repo.custody_transitions()) == before


def test_evidence_backed_resolution_allows_a_fresh_raise_afterward(
    repo: ClerkSqliteRepository,
) -> None:
    _raise(repo, strategy_instance_id=None, reason_code="POSITION_DRIFT")
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert uncertainty is not None

    resolve_reconciliation_uncertainty(repo, reason_code="POSITION_DRIFT", evidence_refs=("fresh_snapshot",))
    assert (
        repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None) is None
    )

    reraised = _raise(repo, strategy_instance_id=None, reason_code="POSITION_DRIFT")
    assert reraised is True


def test_raise_uncertainty_default_shape_fails_closed(repo: ClerkSqliteRepository) -> None:
    """#1380 acceptance: unrecognized reasons/facts default to ACCOUNT_CLERK
    and block new exposure — a caller that doesn't override severity/
    blocks_new_exposure/scope gets the fail-closed default for free."""
    _raise(repo, strategy_instance_id=SID, reason_code="SOME_NEW_UNCATALOGUED_SITUATION")
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="SOME_NEW_UNCATALOGUED_SITUATION", strategy_instance_id=None
    )
    assert uncertainty is not None
    assert uncertainty["scope"] == "ACCOUNT_CLERK"
    assert uncertainty["blocks_new_exposure"] == 1
    assert uncertainty["allows_reduction"] == 0
    assert uncertainty["facts_schema_version"] == 1
    assert '"cause_facts"' in uncertainty["facts_json"]


# ── admit_new_exposure ───────────────────────────────────────────────────────


def test_admit_new_exposure_allows_when_nothing_active(repo: ClerkSqliteRepository) -> None:
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert decision.allowed is True


def test_admit_new_exposure_blocked_by_account_clerk_uncertainty_blocks_every_bot(
    repo: ClerkSqliteRepository,
) -> None:
    _raise(repo, strategy_instance_id=None, reason_code="ACCOUNT_WIDE_ISSUE")

    for sid in (SID, OTHER_SID):
        decision = admit_new_exposure(repo, strategy_instance_id=sid)
        assert decision.allowed is False
        assert decision.reason_code == "ACCOUNT_WIDE_ISSUE"


def test_admit_new_exposure_bot_scoped_uncertainty_blocks_only_that_bot(
    repo: ClerkSqliteRepository,
) -> None:
    """#1380 acceptance: BOT uncertainty blocks only the affected bot while
    account truth is fresh; unrelated bots keep trading."""
    _raise(repo, strategy_instance_id=SID, reason_code="ORDER_OUTCOME_UNKNOWN")

    blocked = admit_new_exposure(repo, strategy_instance_id=SID)
    assert blocked.allowed is False
    assert blocked.reason_code == "ORDER_OUTCOME_UNKNOWN"

    unaffected = admit_new_exposure(repo, strategy_instance_id=OTHER_SID)
    assert unaffected.allowed is True


def test_reduction_requires_a_recognized_cause_that_allows_it(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={"positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}]},
    )
    allowed = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1.0),
    )
    assert allowed.allowed is True


def test_reduction_fails_closed_for_future_or_action_mismatched_drift_facts(
    repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={
            "positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}],
            "future_authorization": True,
        },
    )
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1.0),
    )
    assert decision.allowed is False


@pytest.mark.parametrize(
    ("side", "quantity"),
    [("BUY", 1.0), ("SELL", 6.0)],
)
def test_reduction_must_reduce_both_proven_quantities_without_crossing(
    repo: ClerkSqliteRepository,
    monkeypatch: pytest.MonkeyPatch,
    side: str,
    quantity: float,
) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={"positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}]},
    )
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side=side, quantity=quantity),
    )
    assert decision.allowed is False


def test_reduction_requires_fresh_drift_evidence(repo: ClerkSqliteRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo, "attributed_positions_by_symbol", lambda: {"SPY": 5.0})
    _raise(
        repo,
        strategy_instance_id=None,
        reason_code="POSITION_DRIFT",
        cause_facts={"positions": [{"symbol": "SPY", "broker_qty": 5.0, "attributed_qty": 5.0}]},
    )
    uncertainty = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code="POSITION_DRIFT", strategy_instance_id=None
    )
    assert uncertainty is not None
    monkeypatch.setattr(repo, "_clock", lambda: uncertainty["observed_at_ms"] + 30_001)
    decision = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="SPY", side="SELL", quantity=1.0),
    )
    assert decision.allowed is False


def test_unknown_cause_blocks_reduction_account_wide(repo: ClerkSqliteRepository) -> None:
    _raise(repo, strategy_instance_id=SID, reason_code="NEW_CAUSE")
    decision = decide_capability(repo, capability=Capability.REDUCE, strategy_instance_id=OTHER_SID)
    assert decision.allowed is False


def test_admit_new_exposure_blocked_by_an_active_hold(repo: ClerkSqliteRepository) -> None:
    """Admission folds the #1378 hold mechanism behind the same surface —
    a caller must be blocked by either, with no separate check needed."""
    facts = AccountHoldRaisedFacts(reason_code="UNEXPLAINED_ORDER_HOLD", evidence_refs=["bo-1"])
    repo.append_transition(
        TransitionInput(
            transition_kind="ACCOUNT_HOLD_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ACCOUNT_HOLD_RAISED",
            facts_json=facts.to_facts_json(),
        )
    )
    decision = admit_new_exposure(repo, strategy_instance_id=SID)
    assert decision.allowed is False
    assert decision.reason_code == "UNEXPLAINED_ORDER_HOLD"


@pytest.mark.parametrize("capability", [Capability.CANCEL, Capability.RECONCILE])
def test_safety_capabilities_remain_allowed_under_active_hold(
    repo: ClerkSqliteRepository, capability: Capability
) -> None:
    facts = AccountHoldRaisedFacts(reason_code="UNEXPLAINED_ORDER_HOLD", evidence_refs=["bo-1"])
    repo.append_transition(
        TransitionInput(
            transition_kind="ACCOUNT_HOLD_RAISED",
            custody_owner="ACCOUNT_CLERK",
            execution_authority="ACCOUNT_CLERK",
            operation_state="succeeded",
            clerk_observed_at_ms=repo.clock(),
            summary_code="ACCOUNT_HOLD_RAISED",
            facts_json=facts.to_facts_json(),
        )
    )

    decision = decide_capability(
        repo,
        capability=capability,
        strategy_instance_id=SID,
    )

    assert decision.allowed is True


# ── require_admission ────────────────────────────────────────────────────────


def test_require_admission_is_silent_when_allowed(repo: ClerkSqliteRepository) -> None:
    require_admission(repo, strategy_instance_id=SID)  # must not raise


def test_require_admission_raises_when_blocked(repo: ClerkSqliteRepository) -> None:
    _raise(repo, strategy_instance_id=SID, reason_code="ORDER_OUTCOME_UNKNOWN")
    with pytest.raises(AdmissionBlockedError) as exc_info:
        require_admission(repo, strategy_instance_id=SID)
    assert exc_info.value.decision.reason_code == "ORDER_OUTCOME_UNKNOWN"


# ── refusal taxonomy (F19) ───────────────────────────────────────────────────


def test_classify_admission_refusal_marks_sweep_resolvable_codes_transient() -> None:
    assert classify_admission_refusal(BROKER_SNAPSHOT_STALE_REASON_CODE) is RefusalClass.TRANSIENT
    assert classify_admission_refusal(RECONCILIATION_INCOMPLETE_REASON_CODE) is RefusalClass.TRANSIENT
    assert classify_admission_refusal("RECONCILIATION_IN_PROGRESS") is RefusalClass.TRANSIENT


def test_classify_admission_refusal_fails_closed_for_unknown_or_subject_codes() -> None:
    assert classify_admission_refusal(None) is RefusalClass.TERMINAL
    assert classify_admission_refusal("SOME_FUTURE_CODE") is RefusalClass.TERMINAL
    assert classify_admission_refusal(EXIT_NOT_FLAT_REASON_CODE) is RefusalClass.TERMINAL


# ── EXIT_STUCK escalation (stuck-EXIT watchdog) ──────────────────────────────


def test_exit_stuck_cause_roundtrips_and_rejects_malformed_mappings() -> None:
    cause = ExitStuckCause(
        symbol="SPY", attributed_qty=4.0, redrive_count=3, first_observed_at_ms=1_700_000_000_000
    )
    assert ExitStuckCause.from_mapping(cause.to_mapping()) == cause
    with pytest.raises(ValueError):
        ExitStuckCause.from_mapping({"symbol": "spy", "attributed_qty": 4.0,
                                     "redrive_count": 3, "first_observed_at_ms": 1})
    with pytest.raises(ValueError):
        ExitStuckCause.from_mapping({"symbol": "SPY", "attributed_qty": 4.0,
                                     "redrive_count": -1, "first_observed_at_ms": 1})


def test_exit_stuck_blocks_new_exposure_and_foreign_symbol_reduction(repo) -> None:
    raise_uncertainty(
        repo,
        strategy_instance_id=SID,
        reason_code=EXIT_STUCK_REASON_CODE,
        headline="A stuck EXIT exhausted automatic re-drives",
        explanation="test",
        operator_impact="new exposure paused; exact reduction available",
        next_step="execute the presented safe flatten",
        evidence_refs=(),
        cause_facts=ExitStuckCause(
            symbol="SPY", attributed_qty=4.0, redrive_count=3,
            first_observed_at_ms=1_700_000_000_000,
        ).to_mapping(),
        severity="error",
    )

    blocked_enter = decide_capability(repo, capability=Capability.NEW_EXPOSURE, strategy_instance_id=SID)
    assert blocked_enter.allowed is False
    assert blocked_enter.reason_code == EXIT_STUCK_REASON_CODE

    blocked_reduce = decide_capability(
        repo,
        capability=Capability.REDUCE,
        strategy_instance_id=SID,
        reduction_intent=ReductionIntent(symbol="QQQ", side="SELL", quantity=4),
    )
    assert blocked_reduce.allowed is False


# ── ADR 0048 Decision 1: per-reason age policy ──────────────────────────────


def test_reason_policy_age_field_is_required_with_no_default() -> None:
    """A reason cannot be registered without naming what ends its episode."""
    age_field = next(field for field in dataclasses.fields(ReasonPolicy) if field.name == "age")
    assert age_field.default is dataclasses.MISSING
    assert age_field.default_factory is dataclasses.MISSING


def test_every_registered_reason_declares_a_closed_age_policy() -> None:
    """Exhaustive over the registry: every entry must be one of the three
    closed AgePolicy shapes, so a newly-registered reason cannot be added
    without its author choosing one."""
    for reason_code, policy in _REASON_POLICIES.items():
        assert isinstance(policy.age, (CauseCleared, VoidAfter, RedriveThenEscalate)), reason_code


@pytest.mark.parametrize(
    "reason_code",
    [
        POSITION_DRIFT_REASON_CODE,
        BROKER_SNAPSHOT_STALE_REASON_CODE,
        RECONCILIATION_INCOMPLETE_REASON_CODE,
        EXECUTION_COVERAGE_CONFLICT_REASON_CODE,
    ],
)
def test_reasons_with_no_prior_clock_declare_cause_cleared(reason_code: str) -> None:
    """ADR 0048 Decision 1 does not add a clock to a code that lacked one."""
    assert _REASON_POLICIES[reason_code].age == CauseCleared()


def test_order_outcome_unknown_declares_the_original_submit_absence_grace() -> None:
    """Byte-identical replacement of ``UNCERTAIN_SUBMIT_GRACE_MS = 30_000``
    (former ``order_evidence.py:37``) and its receipt summary code."""
    assert _REASON_POLICIES[ORDER_OUTCOME_UNKNOWN_REASON_CODE].age == VoidAfter(
        grace_ms=30_000, summary_code="ORDER_SUBMIT_FAILED_ABSENT"
    )


def test_exit_not_flat_declares_the_original_redrive_then_escalate() -> None:
    """Byte-identical replacement of ``EXIT_NOT_FLAT_REDRIVE_AFTER_MS = 120_000``
    and ``EXIT_NOT_FLAT_MAX_REDRIVES = 3`` (former ``exit_watchdog.py:46-47``)."""
    assert _REASON_POLICIES[EXIT_NOT_FLAT_REASON_CODE].age == RedriveThenEscalate(
        after_ms=120_000, max_count=3, escalate_to=EXIT_STUCK_REASON_CODE
    )


def test_exit_stuck_never_auto_voids_on_age() -> None:
    """A durable escalation must not carry a clock: only an attributed-flat
    proof or an operator may end it. Declaring ``VoidAfter`` here would
    silently discard the very episode the escalation exists to preserve."""
    assert _REASON_POLICIES[EXIT_STUCK_REASON_CODE].age == CauseCleared()
    assert not isinstance(_REASON_POLICIES[EXIT_STUCK_REASON_CODE].age, VoidAfter)


def test_age_policy_is_a_closed_three_shape_sum() -> None:
    """The sum admits exactly CauseCleared / VoidAfter / RedriveThenEscalate —
    not optional fields on one class (ADR 0048 Decision 1 rationale)."""
    args = typing.get_args(AgePolicy)
    assert set(args) == {CauseCleared, VoidAfter, RedriveThenEscalate}


def test_reason_age_policy_rejects_a_shape_the_caller_did_not_declare() -> None:
    """Narrowing happens once, at the accessor, with a named error.

    Each caller reads a shape-specific field, so it is already coupled to
    one variant. Asserting that at every call site invites a caller to
    forget and then fail with an ``AttributeError`` several frames from the
    mis-declaration.
    """
    with pytest.raises(TypeError, match="declares CauseCleared, not the VoidAfter"):
        reason_age_policy(POSITION_DRIFT_REASON_CODE, VoidAfter)


def test_submit_absence_receipt_code_is_the_declared_summary_code() -> None:
    """The receipt code written on the definitive-absence void is the one the
    policy declares — derived, not a second copy of the same literal.

    A drift guard rather than a regression test: both spellings agreed when
    this was two literals. The point is that there is now one definition, so
    they cannot stop agreeing.
    """
    declared = reason_age_policy(ORDER_OUTCOME_UNKNOWN_REASON_CODE, VoidAfter)
    assert declared.summary_code == order_evidence.SUBMIT_ABSENCE_SUMMARY_CODE
