from __future__ import annotations

from app.broker.alpaca.clerk.models import (
    AccountFreezeState,
    ClerkCustodySnapshot,
    CustodyCountFact,
    CustodyExposureFact,
    HoldState,
)
from app.schemas.run_admission import (
    MarketDataAdmissionFact,
    RunProcessAdmissionFact,
    StartRunFacts,
)
from app.services.run_admission import evaluate_run_admission

_NOW = 1_700_000_010_000
_SID = "alpaca-start-1"


def _bot(
    *,
    process_state: str = "ABSENT",
    market_state: str = "AVAILABLE",
    observed_at_ms: int = _NOW - 1_000,
) -> StartRunFacts:
    return StartRunFacts(
        strategy_instance_id=_SID,
        proposed_run_id="run-new",
        configuration_hash="a" * 64,
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
        ),
    )


def _count(state: str = "zero") -> CustodyCountFact:
    return CustodyCountFact(state=state, count=0 if state == "zero" else None)


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
        reason_code=(
            "CLERK_CUSTODY_PROVEN"
            if reconciliation_state == "clean"
            else "CLERK_CUSTODY_UNPROVABLE"
        ),
        evidence_refs=("clerk:paper-account:7",),
        observed_at_ms=observed_at_ms,
    )


def test_start_admission_allows_only_proven_absence_and_flat_custody() -> None:
    decision = evaluate_run_admission(_bot(), _clerk(), evaluated_at_ms=_NOW)

    assert decision.operation == "START"
    assert decision.allowed is True
    assert decision.reason_code == "START_ADMITTED"
    assert decision.strategy_instance_id == _SID
    assert decision.proposed_run_id == "run-new"
    assert decision.fact_ages_ms == {
        "process": 1_000,
        "market_data": 1_000,
        "clerk": 500,
    }
    assert "clerk:paper-account:7" in decision.evidence_refs


def test_start_admission_blocks_unknown_process_state() -> None:
    decision = evaluate_run_admission(
        _bot(process_state="UNKNOWN"), _clerk(), evaluated_at_ms=_NOW
    )

    assert decision.allowed is False
    assert decision.reason_code == "PROCESS_STATE_UNKNOWN"


def test_start_admission_blocks_stale_market_data() -> None:
    decision = evaluate_run_admission(
        _bot(market_state="STALE"), _clerk(), evaluated_at_ms=_NOW
    )

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
    decision = evaluate_run_admission(
        _bot(observed_at_ms=_NOW + 1), _clerk(), evaluated_at_ms=_NOW
    )

    assert decision.allowed is False
    assert decision.reason_code == "AUTHORITY_CLOCK_INVALID"


def test_start_admission_fact_age_boundary_is_explicit() -> None:
    below = evaluate_run_admission(
        _bot(observed_at_ms=_NOW - 4_999), _clerk(), evaluated_at_ms=_NOW
    )
    at_boundary = evaluate_run_admission(
        _bot(observed_at_ms=_NOW - 5_000), _clerk(), evaluated_at_ms=_NOW
    )
    above = evaluate_run_admission(
        _bot(observed_at_ms=_NOW - 5_001), _clerk(), evaluated_at_ms=_NOW
    )

    assert below.allowed is True
    assert at_boundary.allowed is True
    assert above.allowed is False
    assert above.reason_code == "AUTHORITY_FACT_STALE"
