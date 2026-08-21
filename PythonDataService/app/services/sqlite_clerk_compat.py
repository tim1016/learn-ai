"""Compatibility projections for surfaces retained across SQLite cutover."""

from __future__ import annotations

import logging
from collections.abc import Sequence

from app.broker.alpaca.clerk.active_authority import get_active_clerk_runtime
from app.broker.alpaca.clerk.models import ChannelHealth, ClerkStatus
from app.broker.alpaca.clerk.sqlite.account_operator_posture import (
    AccountOperatorPostureContext,
    build_account_operator_posture,
)
from app.broker.alpaca.clerk.sqlite.order_projection import (
    ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES,
)
from app.broker.alpaca.clerk.sqlite.projection_models import (
    ClerkProjection,
    ProjectedOperation,
)
from app.broker.alpaca.clerk.sqlite.projections import SqliteClerkProjectionReader
from app.broker.alpaca.clerk.sqlite.recovery_policy import (
    RecoveryPolicyContext,
    build_projection_guidance,
    build_recovery_catalog,
)
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerAccountSnapshot
from app.schemas.clerk_custody import CustodyDiagnosis
from app.services.broker_v2_panel.panel_projection_service import (
    ChannelHealthEvaluation,
    evaluate_channel_health,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)


def active_sqlite_facade(broker: str = "alpaca") -> SqliteAlpacaClerkFacade | None:
    """Return the selected Alpaca Broker V2 authority, not host-runner health.

    ``HostRunnerHealth.clerks`` is a separate process inventory and can list a
    Clerk for another account. Alpaca status and custody decisions must resolve
    through this selector so that another broker's Clerk is never mistaken for
    the Alpaca SQLite authority.
    """
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


def sqlite_clerk_status(
    projection: ClerkProjection,
    *,
    channel_healths: Sequence[ChannelHealth] | None = None,
    account: BrokerAccountSnapshot | None = None,
) -> ClerkStatus:
    """Compose the Clerk status, including the #1664 canonical operator posture.

    ``account`` is the same-request Alpaca account observation. It is
    optional only because a live broker read can fail independently of the
    Clerk's own custody folds; when it is unavailable the posture reports an
    explicit ``alpaca_account_evidence_unavailable`` condition instead of
    silently skipping the paper-mode/active-status checks (#1664 scope).

    If ``account`` names a different account than ``projection`` — e.g. the
    registered Alpaca port's credentials were repointed while this SQLite
    Clerk authority is still bound to the original account — its facts are
    never attributed to this projection's account (2026-08-20 review): the
    posture reports an explicit ``alpaca_account_identity_mismatch``
    condition instead of silently blending two accounts' evidence.
    """
    hold = projection.holds[0] if projection.holds else None
    unresolved = sum(
        _operation_requires_reconciliation(operation)
        for operation in projection.operations
    )
    latest = projection.latest_reconciliation
    if projection.uncertainties:
        verdict = "stale"
    elif hold is not None:
        verdict = "unexplained_order"
    else:
        verdict = "clean"
    channel_evaluation = evaluate_channel_health(channel_healths, projection.generated_at_ms)
    identity_mismatch = account is not None and account.account_id != projection.account_id
    if identity_mismatch:
        logger.warning(
            "Clerk status account read named a different account than the "
            "active projection; reporting identity-mismatch posture",
            extra={
                "projection_account_id": projection.account_id,
                "observed_account_id": account.account_id if account is not None else None,
            },
        )
    account_usable = account is not None and not identity_mismatch
    posture = build_account_operator_posture(
        AccountOperatorPostureContext(
            authority_health=projection.authority_health,
            uncertainty_count=len(projection.uncertainties),
            guidance=projection.guidance,
            recovery_actions=projection.recovery_actions,
            # None together exactly when the account snapshot is
            # unavailable or names the wrong account — never defaulted to a
            # fake eligible shape. See AccountOperatorPostureContext's
            # docstring (#1664 review).
            account_mode=account.account_mode if account_usable else None,
            account_status=account.account_status if account_usable else None,
            trading_blocked=account.trading_blocked if account_usable else None,
            account_blocked=account.account_blocked if account_usable else None,
            account_identity_mismatch=identity_mismatch,
            outstanding_intents=unresolved,
            channels_ready=channel_evaluation.ready,
            channels_detail=_channel_evaluation_detail(channel_evaluation),
        )
    )
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
        channel_healths=(
            list(channel_healths) if channel_healths is not None else None
        ),
        authority_kind="real_paper",
        operator_posture=posture,
    )


def _channel_evaluation_detail(evaluation: ChannelHealthEvaluation) -> str | None:
    if evaluation.ready:
        return None
    parts: list[str] = []
    if evaluation.missing:
        parts.append(f"missing: {', '.join(evaluation.missing)}")
    if evaluation.stale:
        parts.append(f"stale: {', '.join(evaluation.stale)}")
    if evaluation.unhealthy:
        parts.append(f"unhealthy: {', '.join(evaluation.unhealthy)}")
    return "Clerk submission channels are not ready (" + "; ".join(parts) + ")."


def _operation_requires_reconciliation(operation: ProjectedOperation) -> bool:
    """Mirror the SQLite authority's broker-facing nonterminal predicate."""
    if operation.state in {"succeeded", "failed", "rejected"}:
        return False
    if operation.state in {"accepted", "unknown"} or operation.kind == "EXIT":
        return True
    return not operation.orders or any(
        order.broker_state is None
        or order.broker_state.lower() not in ACCOUNT_EXPOSURE_TERMINAL_ORDER_STATUSES
        for order in operation.orders
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
        authority_kind="real_paper",
        divergences=divergences,
        resolution_plan=(),
    )
