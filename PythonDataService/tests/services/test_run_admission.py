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
    ResumeCheckpointAdmissionFact,
    ResumeRunFacts,
    RunProcessAdmissionFact,
    StartRunFacts,
    StartRuntimeAdmissionFact,
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
        mode=mode,
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
        mode=mode,
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
        "runtime": 1_000,
        "process": 1_000,
        "market_data": 1_000,
        "market_liveness": 1_000,
        "clerk": 500,
    }
    assert "clerk:paper-account:7" in decision.evidence_refs


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
