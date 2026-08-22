from __future__ import annotations

import math

import pytest

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.schemas.market_liveness import (
    MarketClockLivenessEvidence,
    MarketLivenessFact,
    SymbolTradingStatusEvidence,
)
from app.schemas.run_admission import (
    MarketDataAdmissionFact,
    ProgramBuildAdmissionFact,
    ResumeCheckpointAdmissionFact,
    ResumeRunFacts,
    RunProcessAdmissionFact,
    StartRunFacts,
    StartRuntimeAdmissionFact,
    StrategyValidationAdmissionFact,
    TerminalEvidenceAdmissionFact,
)
from app.services.market_liveness import compose_market_liveness
from app.services.run_admission import evaluate_run_admission

_NOW = 1_700_000_010_000
_SID = "alpaca-start-1"
_READY_TERMINAL_EVIDENCE = TerminalEvidenceAdmissionFact(
    state="RECEIPT_READY",
    evidence_ref="terminal-evidence:run-prior:receipt:1:CRASHED:TypeError",
    explanation="An authoritative terminal receipt exists for the prior run.",
)


def _validation(observed_at_ms: int, *, state: str = "VERIFIED") -> StrategyValidationAdmissionFact:
    return StrategyValidationAdmissionFact(
        state=state,
        strategy_key="deployment_validation",
        evidence_status="accepted",
        event_id="validation-event-1",
        evidence_snapshot_sha256="c" * 64,
        verified_at_ms=observed_at_ms,
        evidence_refs=("strategy-validation:event:validation-event-1",),
        explanation="The validation proof is current.",
    )


def _program_build(
    observed_at_ms: int,
    *,
    state: str = "NOT_APPLICABLE",
) -> ProgramBuildAdmissionFact:
    return ProgramBuildAdmissionFact(
        state=state,
        program_key="deployment_validation",
        verified_at_ms=observed_at_ms,
        explanation=(
            "The running program is unproven."
            if state == "UNPROVEN"
            else "No registered Signal Program applies."
        ),
    )


def _bot(
    *,
    process_state: str = "ABSENT",
    market_state: str = "AVAILABLE",
    runtime_state: str = "READY",
    liveness_state: str = "TRADABLE",
    scheduled_phase: str = "UNKNOWN",
    observed_at_ms: int = _NOW - 1_000,
    mode: str = "trade",
) -> StartRunFacts:
    return StartRunFacts(
        strategy_instance_id=_SID,
        proposed_run_id="run-new",
        configuration_hash="a" * 64,
        sealed_account_id="paper-account",
        mode=mode,
        program_build=_program_build(observed_at_ms),
        validation=_validation(observed_at_ms),
        runtime=StartRuntimeAdmissionFact(
            state=runtime_state,
            observed_at_ms=observed_at_ms,
            explanation="The bot runtime is ready for Start.",
        ),
        process=RunProcessAdmissionFact(
            state=process_state,
            run_id=None,
            process_identity=None,
            registry_generation="registry-1",
            observed_at_ms=observed_at_ms,
        ),
        market_data=MarketDataAdmissionFact(
            state=market_state,
            feed_id="ibkr",
            last_bar_ms=None,
            observed_at_ms=observed_at_ms,
            reason=None,
            scheduled_phase=scheduled_phase,
        ),
        market_liveness=_liveness(liveness_state, observed_at_ms=observed_at_ms),
    )


def _count(state: str = "zero") -> CustodyCountFact:
    return CustodyCountFact(state=state, count=0 if state == "zero" else 1)


def _active_freeze() -> AccountFreezeState:
    return AccountFreezeState(
        active=True,
        category="ACCOUNT_STATE_UNPROVABLE",
        explanation="Fresh order and exposure truth is unavailable.",
        next_step="Restore broker observation, then reconcile.",
        observed_at_ms=_NOW - 1_000,
    )


def _clerk(
    *,
    exposure_state: str = "zero",
    reconciliation_state: str = "clean",
    reconciliation_fresh: bool = True,
    observed_at_ms: int = _NOW - 500,
) -> ClerkCustodySnapshot:
    return ClerkCustodySnapshot(
        broker="alpaca",
        account_id="paper-account",
        strategy_instance_id=_SID,
        clerk_generation="clerk-1",
        journal_sequence=7,
        reconciliation_state=reconciliation_state,
        reconciliation_fresh=reconciliation_fresh,
        reconciled_at_ms=observed_at_ms,
        exposure=CustodyExposureFact(
            state=exposure_state,
            positions={} if exposure_state == "zero" else None,
        ),
        working_orders=_count(),
        pending_orders=_count(),
        terminal_orders=_count(),
        unresolved_effects=_count(),
        hold=HoldState(active=False),
        freeze=AccountFreezeState(),
        reason_code=("CLERK_CUSTODY_PROVEN" if reconciliation_state == "clean" else "CLERK_CUSTODY_UNPROVABLE"),
        evidence_refs=("clerk:paper-account:7",),
        observed_at_ms=observed_at_ms,
    )


def _liveness(state: str = "TRADABLE", *, observed_at_ms: int) -> MarketLivenessFact:
    clock_state = "CLOSED" if state == "CLOSED" else "OPEN"
    symbol_status = (
        None
        if state == "CLOSED"
        else SymbolTradingStatusEvidence(
            symbol="SPY",
            state=state,
            source="test.symbol-status",
            observed_at_ms=observed_at_ms,
            source_timestamp_ms=observed_at_ms,
        )
    )
    return compose_market_liveness(
        "SPY",
        now_ms=observed_at_ms,
        market_clock=MarketClockLivenessEvidence(
            state=clock_state,
            source="test.clock",
            observed_at_ms=observed_at_ms,
            vendor_timestamp_ms=observed_at_ms,
        ),
        connected=True,
        connection_changed_at_ms=observed_at_ms,
        symbol_status=symbol_status,
    )


def _resume_bot(
    *,
    process_state: str = "EXITED",
    process_run_id: str | None = "run-prior",
    desired_state: str = "STOPPED",
    phase: str = "OFF_DUTY",
    checkpoint: ResumeCheckpointAdmissionFact | None = None,
    mode: str = "trade",
    terminal_evidence: TerminalEvidenceAdmissionFact = _READY_TERMINAL_EVIDENCE,
) -> ResumeRunFacts:
    return ResumeRunFacts(
        strategy_instance_id=_SID,
        proposed_run_id="run-resumed",
        prior_run_id="run-prior",
        configuration_hash="b" * 64,
        sealed_account_id="paper-account",
        mode=mode,
        program_build=_program_build(_NOW - 1_000),
        validation=_validation(_NOW - 1_000),
        runtime=StartRuntimeAdmissionFact(
            state="READY",
            observed_at_ms=_NOW - 1_000,
            explanation="The bot runtime is ready for Resume.",
        ),
        process=RunProcessAdmissionFact(
            state=process_state,
            run_id=process_run_id,
            process_identity=None,
            registry_generation="registry-1",
            observed_at_ms=_NOW - 1_000,
        ),
        market_data=MarketDataAdmissionFact(
            state="AVAILABLE",
            feed_id="alpaca-feed",
            observed_at_ms=_NOW - 1_000,
        ),
        market_liveness=_liveness(observed_at_ms=_NOW - 1_000),
        desired_state=desired_state,
        phase=phase,
        carryover_policy="ALLOW",
        carryover_account_policy_enabled=True,
        exposure_carryover_supported=True,
        checkpoint=checkpoint,
        terminal_evidence=terminal_evidence,
    )


def test_start_admission_allows_only_proven_absence_and_flat_custody() -> None:
    decision = evaluate_run_admission(_bot(), _clerk(), evaluated_at_ms=_NOW)

    assert decision.operation == "START"
    assert decision.allowed is True
    assert decision.reason_code == "START_ADMITTED"
    assert decision.strategy_instance_id == _SID
    assert decision.proposed_run_id == "run-new"
    assert decision.fact_ages_ms.model_dump() == {
        "program_build": 1_000,
        "runtime": 1_000,
        "process": 1_000,
        "market_data": 1_000,
        "market_liveness": 1_000,
        "clerk": 500,
    }
    assert "clerk:paper-account:7" in decision.evidence_refs


def test_start_admission_rejects_an_unproven_program_build_before_runtime() -> None:
    bot = _bot().model_copy(
        update={"program_build": _program_build(_NOW - 1_000, state="UNPROVEN")}
    )

    decision = evaluate_run_admission(bot, _clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "PROGRAM_BUILD_UNPROVEN"
    assert decision.explanation == "The running program is unproven."


def test_start_admission_blocks_a_different_sealed_account_before_effects() -> None:
    decision = evaluate_run_admission(
        _bot().model_copy(update={"sealed_account_id": "other-paper-account"}),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "SEALED_ACCOUNT_MISMATCH"


def test_start_admission_blocks_a_validation_proof_that_was_not_reverified() -> None:
    decision = evaluate_run_admission(
        _bot().model_copy(
            update={
                "validation": _validation(
                    _NOW - 1_000,
                    state="UNVERIFIED",
                )
            }
        ),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "STRATEGY_VALIDATION_UNVERIFIED"


def test_start_admission_blocks_fresh_market_wide_closed_evidence() -> None:
    decision = evaluate_run_admission(
        _bot(liveness_state="CLOSED"),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "MARKET_LIVENESS_CLOSED"


def test_start_admission_blocks_market_wide_closed_even_during_scheduled_rth() -> None:
    """#1671 AC4: scheduled RTH must never override fresh live evidence that
    the market is actually closed (e.g. an emergency early close) — the
    calendar and the broker's live clock can disagree, and live evidence
    wins for the admission decision."""
    decision = evaluate_run_admission(
        _bot(liveness_state="CLOSED", scheduled_phase="RTH"),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "MARKET_LIVENESS_CLOSED"


def test_start_admission_blocks_unknown_process_state() -> None:
    decision = evaluate_run_admission(_bot(process_state="UNKNOWN"), _clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "PROCESS_STATE_UNKNOWN"


def test_start_admission_blocks_incomplete_boot_recovery() -> None:
    decision = evaluate_run_admission(
        _bot(runtime_state="BOOT_RECOVERY_INCOMPLETE"),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "BOOT_RECOVERY_INCOMPLETE"


def test_start_admission_blocks_stale_market_data() -> None:
    decision = evaluate_run_admission(_bot(market_state="STALE"), _clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "MARKET_DATA_STALE"


def test_start_admission_keeps_unprovable_custody_unknown() -> None:
    decision = evaluate_run_admission(
        _bot(),
        _clerk(
            exposure_state="unknown",
            reconciliation_state="stale",
            reconciliation_fresh=False,
        ),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "CLERK_CUSTODY_UNPROVABLE"
    assert "zero" not in decision.explanation.lower()


def test_start_admission_refuses_existing_attributed_exposure() -> None:
    clerk = _clerk().model_copy(
        update={
            "exposure": CustodyExposureFact(
                state="non_zero",
                positions={"SPY": 1.0},
            )
        }
    )

    decision = evaluate_run_admission(_bot(), clerk, evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "START_REQUIRES_FLAT_CUSTODY"
    assert decision.next_step == "Use Resume for approved carryover, or flatten through the Clerk."


def test_start_admission_refuses_future_dated_authority_facts() -> None:
    decision = evaluate_run_admission(_bot(observed_at_ms=_NOW + 1), _clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "AUTHORITY_CLOCK_INVALID"


def test_start_admission_fact_age_boundary_is_explicit() -> None:
    below = evaluate_run_admission(_bot(observed_at_ms=_NOW - 4_999), _clerk(), evaluated_at_ms=_NOW)
    at_boundary = evaluate_run_admission(_bot(observed_at_ms=_NOW - 5_000), _clerk(), evaluated_at_ms=_NOW)
    above = evaluate_run_admission(_bot(observed_at_ms=_NOW - 5_001), _clerk(), evaluated_at_ms=_NOW)

    assert below.allowed is True
    assert at_boundary.allowed is True
    assert above.allowed is False
    assert above.reason_code == "AUTHORITY_FACT_STALE"


def test_resume_admission_allows_terminal_flat_instance_and_mints_proposed_run() -> None:
    decision = evaluate_run_admission(_resume_bot(), _clerk(), evaluated_at_ms=_NOW)

    assert decision.operation == "RESUME"
    assert decision.allowed is True
    assert decision.reason_code == "RESUME_ADMITTED"
    assert decision.proposed_run_id == "run-resumed"


def test_resume_admission_blocks_without_terminal_prior_process() -> None:
    decision = evaluate_run_admission(
        _resume_bot(process_state="EXITED", process_run_id="run-other"),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "RESUME_PROCESS_NOT_TERMINAL"


@pytest.mark.parametrize(
    ("kwargs", "reason_code"),
    [
        ({"phase": "RETIRED"}, "BOT_RETIRED"),
        ({"desired_state": "PAUSED"}, "RESUME_REQUIRES_STOPPED_INSTANCE"),
    ],
)
def test_resume_admission_refuses_invalid_instance_lifecycle(
    kwargs: dict[str, str],
    reason_code: str,
) -> None:
    decision = evaluate_run_admission(_resume_bot(**kwargs), _clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == reason_code


def test_resume_admission_refuses_unreadable_terminal_evidence() -> None:
    """PRD #1716 FR-3: an unreadable receipt denies before any custody gate."""
    unreadable = TerminalEvidenceAdmissionFact(
        state="UNREADABLE",
        evidence_ref="terminal-evidence:run-prior:receipt-corrupt",
        explanation="The terminal receipt for run 'run-prior' could not be read: boom",
        next_step="This requires engineering investigation; Refresh to check for updated evidence.",
    )

    decision = evaluate_run_admission(
        _resume_bot(terminal_evidence=unreadable),
        _clerk(),
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == "TERMINAL_EVIDENCE_UNREADABLE"
    assert decision.explanation == unreadable.explanation
    assert decision.next_step == unreadable.next_step


def test_resume_admission_decision_carries_the_terminal_evidence_reference() -> None:
    """The concurrency token (hashed from evidence_refs) must move when the
    underlying receipt or summary content changes."""
    decision = evaluate_run_admission(_resume_bot(), _clerk(), evaluated_at_ms=_NOW)

    assert _READY_TERMINAL_EVIDENCE.evidence_ref in decision.evidence_refs


def test_resume_admission_requires_exact_approved_carryover_checkpoint() -> None:
    clerk = _clerk().model_copy(
        update={
            "exposure": CustodyExposureFact(
                state="non_zero",
                positions={"SPY": 1.0},
            )
        }
    )
    checkpoint = ResumeCheckpointAdmissionFact(
        account_id="paper-account",
        stopped_run_id="run-prior",
        configuration_hash="b" * 64,
        exposure={"SPY": 1.0},
        approved=True,
        evidence_ref="carryover-checkpoint:run-prior",
    )

    allowed = evaluate_run_admission(
        _resume_bot(checkpoint=checkpoint),
        clerk,
        evaluated_at_ms=_NOW,
    )
    changed = evaluate_run_admission(
        _resume_bot(
            checkpoint=checkpoint.model_copy(update={"exposure": {"SPY": 2.0}})
        ),
        clerk,
        evaluated_at_ms=_NOW,
    )

    assert allowed.allowed is True
    assert "carryover-checkpoint:run-prior" in allowed.evidence_refs
    assert changed.allowed is False
    assert changed.reason_code == "RESUME_CHECKPOINT_MISMATCH"


@pytest.mark.parametrize(
    ("carryover_supported", "carryover_policy", "account_policy", "checkpoint", "reason_code"),
    [
        (False, "ALLOW", True, None, "RESUME_CARRYOVER_UNSUPPORTED"),
        (True, "FORBID", True, None, "RESUME_CARRYOVER_NOT_ALLOWED"),
        (True, "ALLOW", False, None, "RESUME_CARRYOVER_NOT_ALLOWED"),
        (True, "ALLOW", True, None, "RESUME_CHECKPOINT_MISSING"),
    ],
)
def test_resume_admission_refuses_unproven_exposure_carryover(
    carryover_supported: bool,
    carryover_policy: str,
    account_policy: bool,
    checkpoint: ResumeCheckpointAdmissionFact | None,
    reason_code: str,
) -> None:
    clerk = _clerk().model_copy(
        update={
            "exposure": CustodyExposureFact(state="non_zero", positions={"SPY": 1.0})
        }
    )
    bot = _resume_bot(checkpoint=checkpoint).model_copy(
        update={
            "exposure_carryover_supported": carryover_supported,
            "carryover_policy": carryover_policy,
            "carryover_account_policy_enabled": account_policy,
        }
    )

    decision = evaluate_run_admission(bot, clerk, evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == reason_code


@pytest.mark.parametrize(
    ("checkpoint_update", "reason_code"),
    [
        ({"account_id": "other-account"}, "RESUME_CHECKPOINT_MISMATCH"),
        ({"stopped_run_id": "run-other"}, "RESUME_CHECKPOINT_MISMATCH"),
        ({"configuration_hash": "c" * 64}, "RESUME_CHECKPOINT_MISMATCH"),
        ({"exposure": {"SPY": 2.0}}, "RESUME_CHECKPOINT_MISMATCH"),
    ],
)
def test_resume_checkpoint_requires_every_identity_leg(
    checkpoint_update: dict[str, object],
    reason_code: str,
) -> None:
    clerk = _clerk().model_copy(
        update={
            "exposure": CustodyExposureFact(state="non_zero", positions={"SPY": 1.0})
        }
    )
    checkpoint = ResumeCheckpointAdmissionFact(
        account_id="paper-account",
        stopped_run_id="run-prior",
        configuration_hash="b" * 64,
        exposure={"SPY": 1.0},
        approved=True,
        evidence_ref="carryover-checkpoint:run-prior",
    ).model_copy(update=checkpoint_update)

    decision = evaluate_run_admission(
        _resume_bot(checkpoint=checkpoint),
        clerk,
        evaluated_at_ms=_NOW,
    )

    assert decision.allowed is False
    assert decision.reason_code == reason_code


def test_resume_checkpoint_accepts_only_float_round_trip_noise() -> None:
    clerk = _clerk().model_copy(
        update={
            "exposure": CustodyExposureFact(state="non_zero", positions={"SPY": 1.0})
        }
    )
    exact = ResumeCheckpointAdmissionFact(
        account_id="paper-account",
        stopped_run_id="run-prior",
        configuration_hash="b" * 64,
        exposure={"SPY": 1.0},
        approved=True,
        evidence_ref="carryover-checkpoint:run-prior",
    )
    within_ulp = exact.model_copy(
        update={"exposure": {"SPY": math.nextafter(1.0, math.inf)}}
    )
    changed = exact.model_copy(update={"exposure": {"SPY": 1.000_001}})

    assert evaluate_run_admission(_resume_bot(checkpoint=exact), clerk, evaluated_at_ms=_NOW).allowed
    assert evaluate_run_admission(_resume_bot(checkpoint=within_ulp), clerk, evaluated_at_ms=_NOW).allowed
    assert not evaluate_run_admission(_resume_bot(checkpoint=changed), clerk, evaluated_at_ms=_NOW).allowed


# ── #1702: mode-tiered admission ────────────────────────────────────────
#
# Dry Run makes no broker contact and holds no custody, so every custody and
# evidence gate below is "not applicable" to it per the PRD's gate table.
# Every case is paired: the same custody/evidence fact that admits Dry Run
# must still deny trade — the guard in evaluate_run_admission is additive
# only, never a relaxation of trade/paper strictness.


def test_dry_run_admitted_despite_market_liveness_halted_trade_still_denied() -> None:
    dry_run = evaluate_run_admission(
        _bot(liveness_state="HALTED", mode="dry_run"), _clerk(), evaluated_at_ms=_NOW
    )
    trade = evaluate_run_admission(
        _bot(liveness_state="HALTED", mode="trade"), _clerk(), evaluated_at_ms=_NOW
    )

    assert dry_run.allowed is True
    assert dry_run.reason_code == "START_ADMITTED"
    assert trade.allowed is False
    assert trade.reason_code == "MARKET_LIVENESS_HALTED"


def test_dry_run_admitted_despite_unreconciled_custody_trade_still_denied() -> None:
    clerk = _clerk(reconciliation_state="stale", reconciliation_fresh=False)

    dry_run = evaluate_run_admission(_bot(mode="dry_run"), clerk, evaluated_at_ms=_NOW)
    trade = evaluate_run_admission(_bot(mode="trade"), clerk, evaluated_at_ms=_NOW)

    assert dry_run.allowed is True
    assert trade.allowed is False
    assert trade.reason_code == "CLERK_CUSTODY_UNPROVABLE"


def test_dry_run_admitted_despite_clerk_freeze_trade_still_denied() -> None:
    clerk = _clerk().model_copy(
        update={"freeze": _active_freeze()}
    )

    dry_run = evaluate_run_admission(_bot(mode="dry_run"), clerk, evaluated_at_ms=_NOW)
    trade = evaluate_run_admission(_bot(mode="trade"), clerk, evaluated_at_ms=_NOW)

    assert dry_run.allowed is True
    assert trade.allowed is False
    assert trade.reason_code == "ACCOUNT_STATE_UNPROVABLE"


def test_dry_run_admitted_despite_clerk_hold_trade_still_denied() -> None:
    clerk = _clerk().model_copy(
        update={"hold": HoldState(active=True, reason_code="UNEXPLAINED_ORDER_HOLD")}
    )

    dry_run = evaluate_run_admission(_bot(mode="dry_run"), clerk, evaluated_at_ms=_NOW)
    trade = evaluate_run_admission(_bot(mode="trade"), clerk, evaluated_at_ms=_NOW)

    assert dry_run.allowed is True
    assert trade.allowed is False
    assert trade.reason_code == "UNEXPLAINED_ORDER_HOLD"


def test_dry_run_admitted_despite_unknown_exposure_trade_still_denied() -> None:
    clerk = _clerk(exposure_state="unknown")

    dry_run = evaluate_run_admission(_bot(mode="dry_run"), clerk, evaluated_at_ms=_NOW)
    trade = evaluate_run_admission(_bot(mode="trade"), clerk, evaluated_at_ms=_NOW)

    assert dry_run.allowed is True
    assert trade.allowed is False
    assert trade.reason_code == "CLERK_EXPOSURE_UNKNOWN"


def test_dry_run_admitted_despite_non_zero_start_exposure_trade_still_denied() -> None:
    clerk = _clerk().model_copy(
        update={"exposure": CustodyExposureFact(state="non_zero", positions={"SPY": 1.0})}
    )

    dry_run = evaluate_run_admission(_bot(mode="dry_run"), clerk, evaluated_at_ms=_NOW)
    trade = evaluate_run_admission(_bot(mode="trade"), clerk, evaluated_at_ms=_NOW)

    assert dry_run.allowed is True
    assert trade.allowed is False
    assert trade.reason_code == "START_REQUIRES_FLAT_CUSTODY"


def test_dry_run_admitted_despite_unresolved_clerk_work_trade_still_denied() -> None:
    clerk = _clerk().model_copy(update={"working_orders": _count("non_zero")})

    dry_run = evaluate_run_admission(_bot(mode="dry_run"), clerk, evaluated_at_ms=_NOW)
    trade = evaluate_run_admission(_bot(mode="trade"), clerk, evaluated_at_ms=_NOW)

    assert dry_run.allowed is True
    assert trade.allowed is False
    assert trade.reason_code == "CLERK_WORK_REMAINS"


def test_dry_run_resume_admitted_despite_unapproved_carryover_exposure() -> None:
    """Defense-in-depth: the request schema already forbids
    ``carryover_policy=="ALLOW"`` for dry_run, so this block is moot in
    practice — but the guard covers it explicitly to match the gate table's
    "forbidden" cell precisely, not merely by upstream convention."""
    clerk = _clerk().model_copy(
        update={"exposure": CustodyExposureFact(state="non_zero", positions={"SPY": 1.0})}
    )
    bot = _resume_bot(mode="dry_run").model_copy(
        update={"exposure_carryover_supported": False, "carryover_policy": "FORBID"}
    )

    decision = evaluate_run_admission(bot, clerk, evaluated_at_ms=_NOW)

    assert decision.allowed is True
    assert decision.reason_code == "RESUME_ADMITTED"


def test_dry_run_still_denied_for_stale_authority_facts_and_process_conflicts() -> None:
    """Checks unrelated to custody/evidence strictness stay unconditional in
    every mode, including dry_run: stale authority facts, an already-active
    process, and market-data readiness are integrity checks, not gates that
    relax with strictness tier."""
    stale = evaluate_run_admission(
        _bot(mode="dry_run", observed_at_ms=_NOW - 5_001), _clerk(), evaluated_at_ms=_NOW
    )
    already_active = evaluate_run_admission(
        _bot(mode="dry_run", process_state="EXITED"), _clerk(), evaluated_at_ms=_NOW
    )
    no_market_data = evaluate_run_admission(
        _bot(mode="dry_run", market_state="UNAVAILABLE"), _clerk(), evaluated_at_ms=_NOW
    )

    assert stale.allowed is False
    assert stale.reason_code == "AUTHORITY_FACT_STALE"
    assert already_active.allowed is False
    assert already_active.reason_code == "STRATEGY_INSTANCE_ALREADY_EXISTS"
    assert no_market_data.allowed is False
    assert no_market_data.reason_code == "MARKET_DATA_UNAVAILABLE"


def test_trade_mode_admission_is_byte_identical_to_pre_1702_behavior() -> None:
    """#1702 regression sentinel: mode="trade" must reproduce every gating
    decision this file asserted before mode-tiering existed. Each helper
    factory now defaults to ``mode="trade"``, so this reruns the exact
    scenarios above under an explicit ``mode="trade"`` to make that claim
    visible in the diff, not merely implicit in unchanged assertions."""
    cases: tuple[tuple[object, object, bool, str], ...] = (
        (_bot(liveness_state="HALTED", mode="trade"), _clerk(), False, "MARKET_LIVENESS_HALTED"),
        (
            _bot(mode="trade"),
            _clerk(reconciliation_state="stale", reconciliation_fresh=False),
            False,
            "CLERK_CUSTODY_UNPROVABLE",
        ),
        (
            _bot(mode="trade"),
            _clerk().model_copy(
                update={"freeze": _active_freeze()}
            ),
            False,
            "ACCOUNT_STATE_UNPROVABLE",
        ),
        (
            _bot(mode="trade"),
            _clerk().model_copy(update={"hold": HoldState(active=True, reason_code="UNEXPLAINED_ORDER_HOLD")}),
            False,
            "UNEXPLAINED_ORDER_HOLD",
        ),
        (_bot(mode="trade"), _clerk(exposure_state="unknown"), False, "CLERK_EXPOSURE_UNKNOWN"),
        (
            _bot(mode="trade"),
            _clerk().model_copy(update={"exposure": CustodyExposureFact(state="non_zero", positions={"SPY": 1.0})}),
            False,
            "START_REQUIRES_FLAT_CUSTODY",
        ),
        (
            _bot(mode="trade"),
            _clerk().model_copy(update={"working_orders": _count("non_zero")}),
            False,
            "CLERK_WORK_REMAINS",
        ),
        (_bot(mode="trade"), _clerk(), True, "START_ADMITTED"),
    )
    for bot, clerk, expected_allowed, expected_reason_code in cases:
        decision = evaluate_run_admission(bot, clerk, evaluated_at_ms=_NOW)
        assert decision.allowed is expected_allowed
        assert decision.reason_code == expected_reason_code


# ── #1729 AC4: canary allowlist composed into evaluate_run_admission ─────────
#
# `_bot()`/`_clerk()` default `program_build` to "NOT_APPLICABLE", so none of
# the cases above ever exercise the canary gate -- these helpers build a
# proven Signal-Program pairing (`mode="trade"`, `program_build.state ==
# "PROVEN"`) that *is* canary-shaped, so the allowlist becomes the deciding
# factor. Every test below monkeypatches
# `app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS`
# locally; the shipped constant stays empty (see
# `tests/services/test_canary_admission.py::test_canary_allowlist_ships_empty`).


def _canary_program_build(
    *,
    state: str = "PROVEN",
    program_key: str = "ema_crossover_signal",
    observed_at_ms: int = _NOW - 1_000,
) -> ProgramBuildAdmissionFact:
    return ProgramBuildAdmissionFact(
        state=state,
        program_key=program_key,
        verified_at_ms=observed_at_ms,
        explanation=(
            "The running Signal Program build matches its golden qualification receipt."
            if state == "PROVEN"
            else "The running program is unproven."
        ),
    )


def _canary_bot(
    *,
    program_key: str = "ema_crossover_signal",
    program_build_state: str = "PROVEN",
    sealed_account_id: str = "paper-account",
    validation_state: str = "VERIFIED",
    runtime_state: str = "READY",
    observed_at_ms: int = _NOW - 1_000,
) -> StartRunFacts:
    return _bot(observed_at_ms=observed_at_ms, runtime_state=runtime_state).model_copy(
        update={
            "sealed_account_id": sealed_account_id,
            "program_build": _canary_program_build(
                state=program_build_state, program_key=program_key, observed_at_ms=observed_at_ms
            ),
            "validation": _validation(observed_at_ms, state=validation_state),
        }
    )


def _canary_resume_bot(
    *,
    program_key: str = "ema_crossover_signal",
    program_build_state: str = "PROVEN",
    sealed_account_id: str = "paper-account",
) -> ResumeRunFacts:
    """A Resume attempt shaped like the one that follows a canary rollback:
    the prior process is proven EXITED and a fresh run id is proposed."""
    return _resume_bot().model_copy(
        update={
            "sealed_account_id": sealed_account_id,
            "program_build": _canary_program_build(state=program_build_state, program_key=program_key),
        }
    )


def _canary_clerk(*, account_id: str = "paper-account", **kwargs: object) -> ClerkCustodySnapshot:
    return _clerk(**kwargs).model_copy(update={"account_id": account_id})


def test_canary_admission_refuses_an_account_not_on_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "an-account-someone-else-enabled")}),
    )

    decision = evaluate_run_admission(_canary_bot(), _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_PAIRING_NOT_ALLOWLISTED"


def test_canary_admission_refuses_a_program_not_on_the_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("sma_crossover_signal", "paper-account")}),
    )

    decision = evaluate_run_admission(_canary_bot(), _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_PAIRING_NOT_ALLOWLISTED"


def test_canary_admission_refuses_the_right_program_with_the_wrong_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1729 AC4: the allowlist is exact by (program, account) -- an
    allowlisted program alone is not enough; the account must match too."""
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )
    bot = _canary_bot(sealed_account_id="a-different-canary-account")
    clerk = _canary_clerk(account_id="a-different-canary-account")

    decision = evaluate_run_admission(bot, clerk, evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_PAIRING_NOT_ALLOWLISTED"


def test_canary_admission_admits_the_exact_allowlisted_pairing_with_every_proof(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )

    decision = evaluate_run_admission(_canary_bot(), _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is True
    assert decision.reason_code == "START_ADMITTED"


def test_canary_admission_refuses_when_build_is_unproven_even_if_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )
    bot = _canary_bot(program_build_state="UNPROVEN")

    decision = evaluate_run_admission(bot, _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "PROGRAM_BUILD_UNPROVEN"


def test_canary_admission_refuses_when_validation_is_unverified_even_if_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )
    bot = _canary_bot(validation_state="UNVERIFIED")

    decision = evaluate_run_admission(bot, _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "STRATEGY_VALIDATION_UNVERIFIED"


def test_canary_admission_refuses_when_replay_recovery_is_not_ready_even_if_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )
    bot = _canary_bot(runtime_state="RECOVERY_UNCERTAIN")

    decision = evaluate_run_admission(bot, _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "RECOVERY_UNCERTAIN"


def test_canary_admission_refuses_when_clerk_custody_is_unprovable_even_if_allowlisted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )
    clerk = _canary_clerk(reconciliation_state="stale", reconciliation_fresh=False)

    decision = evaluate_run_admission(_canary_bot(), clerk, evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "CLERK_CUSTODY_UNPROVABLE"


def test_canary_resume_after_rollback_refuses_without_a_fresh_allowlist_entry() -> None:
    """#1729 AC10: a rollback leaves no cached admission to replay -- Resume
    is re-gated by the same allowlist as any other admission. The shipped
    empty allowlist (no monkeypatch here) refuses it, exactly like it would
    have refused the original Start."""
    decision = evaluate_run_admission(_canary_resume_bot(), _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is False
    assert decision.reason_code == "CANARY_PAIRING_NOT_ALLOWLISTED"


def test_canary_resume_after_rollback_mints_a_genuinely_new_admitted_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#1729 AC10: once an operator re-enables the exact pairing, Resume
    mints a fresh run rather than reviving the stopped one -- no process is
    hot-swapped."""
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", "paper-account")}),
    )

    decision = evaluate_run_admission(_canary_resume_bot(), _canary_clerk(), evaluated_at_ms=_NOW)

    assert decision.allowed is True
    assert decision.reason_code == "RESUME_ADMITTED"
    assert decision.proposed_run_id == "run-resumed"
    assert decision.proposed_run_id != "run-prior"
