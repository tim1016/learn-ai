"""Compatibility projections for surfaces retained across SQLite cutover."""

from __future__ import annotations

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.diagnosis import CustodyDiagnosis
from app.broker.alpaca.clerk.models import ClerkStatus
from app.broker.alpaca.clerk.sqlite.projection_models import ClerkProjection
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_projection_guidance,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.utils.timestamps import now_ms_utc


def active_sqlite_facade(broker: str = "alpaca") -> SqliteAlpacaClerkFacade | None:
    if broker != "alpaca":
        return None
    runtime = get_active_clerk_runtime()
    if (
        runtime is None
        or runtime.authority_kind != "sqlite"
        or not isinstance(runtime.clerk, SqliteAlpacaClerkFacade)
    ):
        return None
    return runtime.clerk


def sqlite_projection(
    *,
    account_id: str,
    strategy_instance_id: str | None,
) -> ClerkProjection | None:
    facade = active_sqlite_facade()
    if facade is None:
        return None
    if facade.account_id != account_id:
        raise ValueError("Requested account is not the active SQLite authority")
    reader = SqliteClerkProjectionReader.from_repository(facade.repository)
    try:
        projection = (
            reader.account_snapshot()
            if strategy_instance_id is None
            else reader.bot_snapshot(strategy_instance_id)
        )
    finally:
        reader.close()
    if projection is None:
        raise ValueError("Requested bot is not registered with the SQLite authority")
    return projection


def failed_sqlite_projection(
    *,
    account_id: str,
    strategy_instance_id: str | None,
) -> ClerkProjection | None:
    """Expose typed account-wide impact when an activated authority fails boot.

    This projection deliberately contains no fabricated durable state. Offline
    rebuild/reset capabilities are present but unavailable until their external
    proof is verified after the service process is stopped.
    """
    runtime = get_active_clerk_runtime()
    if runtime is None or runtime.authority_kind != "unavailable":
        return None
    failure = runtime.startup_failure
    if (
        failure is None
        or not failure.activation_detected
        or failure.account_id != account_id
    ):
        return None
    now_ms = now_ms_utc()
    authority_generation = failure.authority_generation or 0
    db_identity_token = failure.db_identity_token or "unverified-activation"
    context = RecoveryPolicyContext(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        authority_health="failed",
        authority_health_reason=failure.recovery,
        control_revision=0,
        now_ms=now_ms,
        runs=(),
        current_orders=(),
        positions=(),
        uncertainties=(),
        latest_account_reconciliation=None,
    )
    return ClerkProjection(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        authority_generation=authority_generation,
        db_identity_token=db_identity_token,
        authority_health="failed",
        authority_health_reason=failure.recovery,
        control_revision=0,
        custody_owner="ACCOUNT_CLERK",
        runs=(),
        commands=(),
        operations=(),
        positions=(),
        holds=(),
        uncertainties=(),
        latest_reconciliation=None,
        terminal_receipts=(),
        guidance=build_projection_guidance(context),
        recovery_actions=build_recovery_catalog(context),
        generated_at_ms=now_ms,
    )


def sqlite_clerk_status(projection: ClerkProjection) -> ClerkStatus:
    hold = projection.holds[0] if projection.holds else None
    unresolved = sum(
        operation.state in {"accepted", "in_progress", "unknown"}
        for operation in projection.operations
    )
    latest = projection.latest_reconciliation
    if projection.uncertainties:
        verdict = "stale"
    elif hold is not None:
        verdict = "unexplained_order"
    else:
        verdict = "clean"
    return ClerkStatus(
        broker="alpaca",
        account_id=projection.account_id,
        hold={
            "active": hold is not None,
            "reason_code": hold.reason_code if hold is not None else None,
            "reason": projection.guidance.impact if hold is not None else None,
            "since_ms": hold.opened_at_ms if hold is not None else None,
        },
        latest_reconciliation=(
            {"verdict": verdict, "recorded_at_ms": latest.attempted_at_ms}
            if latest is not None
            else None
        ),
        outstanding_intents=unresolved,
        observed_at_ms=projection.generated_at_ms,
        channel_healths=None,
        authority_kind="sqlite",
        generic_hold_clear_available=False,
        generic_hold_clear_explanation=(
            "SQLite custody holds close only when fresh evidence satisfies their "
            "typed resolution policy. Use the recovery actions shown by the Clerk."
        ),
    )


def sqlite_custody_diagnosis(projection: ClerkProjection) -> CustodyDiagnosis:
    divergent = bool(projection.holds or projection.uncertainties)
    evidence_refs = tuple(
        dict.fromkeys(
            reference
            for item in (*projection.holds, *projection.uncertainties)
            for reference in item.evidence_refs
        )
    )
    divergences = ()
    if divergent:
        divergences = (
            {
                "kind": "needs_review",
                "state": "needs_review",
                "explanation": projection.guidance.explanation,
                "possible_causes": (projection.guidance.impact,),
                "prerequisite_detail": projection.guidance.next_step,
                "evidence_refs": evidence_refs,
            },
        )
    return CustodyDiagnosis(
        broker="alpaca",
        account_id=projection.account_id,
        in_sync=not divergent,
        observed_at_ms=projection.generated_at_ms,
        snapshot_version=(
            f"sqlite:{projection.authority_generation}:{projection.control_revision}:"
            f"{projection.db_identity_token}"
        ),
        resolvable=False,
        blocked_reason=(
            None
            if not divergent
            else "Use the SQLite Clerk's typed, evidence-bound recovery actions."
        ),
        authority_kind="sqlite",
        divergences=divergences,
        resolution_plan=(),
    )
