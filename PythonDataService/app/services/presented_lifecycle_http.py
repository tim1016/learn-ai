"""HTTP-boundary enforcement for signed lifecycle presentations.

Keeping this small facade out of the legacy live-instances router prevents the
router from becoming another safety-policy writer.  The facade only verifies
proof and translates a closed error contract; lifecycle state and daemon
actuation remain owned by their existing services.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import HTTPException, status

from app.config import settings
from app.schemas.account_safety_snapshot import (
    PresentedOperatorAction,
    PresentedOperatorActionRejection,
)
from app.schemas.presented_operator_action import (
    PresentedOperatorActionInvocation,
    PresentedOperatorActionTarget,
)
from app.services.account_directory import AccountDirectoryError, UnknownAccountError
from app.services.account_safety_access import account_safety_snapshot_service
from app.services.presented_lifecycle_actions import (
    LifecyclePresentedActionId,
    PresentedLifecycleActionRejectedError,
    PresentedLifecycleActionService,
)


def require_presented_lifecycle_action(
    artifacts_root: Path,
    account_id: str,
    strategy_instance_id: str,
    run_id: str | None,
    action_id: LifecyclePresentedActionId,
    invocation: PresentedOperatorActionInvocation | None,
) -> PresentedOperatorAction | None:
    """Require current proof for risk creation, but not for a safe stop control."""

    # The repository's existing router matrix runs in the explicitly
    # unauthenticated local-control mode. That mode is a test/development
    # harness, not a production transport: it cannot issue a meaningful
    # browser-authenticated envelope. Production has this flag disabled and
    # therefore fails closed below when the envelope is absent.
    if invocation is None and settings.DATA_PLANE_ALLOW_UNAUTHENTICATED_CONTROL:
        return None
    if not account_id or not strategy_instance_id:
        raise _rejection(
            "ACTION_TARGET_UNRESOLVED",
            "The account or bot identity is unavailable; refresh before continuing.",
        )
    snapshot = None
    try:
        snapshot = account_safety_snapshot_service(artifacts_root=artifacts_root).snapshot(
            account_id=account_id
        )
    except (AccountDirectoryError, UnknownAccountError) as exc:
        if action_id not in {"pause", "stop", "end_day"}:
            raise _rejection(
                "ACTION_PROOF_UNAVAILABLE",
                "Current account safety proof is unavailable; this action was not queued.",
            ) from exc
    target = PresentedOperatorActionTarget(
        account_id=account_id,
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
    )
    try:
        return PresentedLifecycleActionService().validate(
            action_id=action_id,
            target=target,
            invocation=invocation,
            snapshot=snapshot,
        )
    except PresentedLifecycleActionRejectedError as exc:
        raise _rejection(
            exc.reason_code,
            exc.message,
            snapshot_id=None if snapshot is None else snapshot.snapshot_id,
            snapshot_version=None if snapshot is None else snapshot.snapshot_version,
        ) from exc


def _rejection(
    reason_code: str,
    message: str,
    *,
    snapshot_id: str | None = None,
    snapshot_version: str | None = None,
) -> HTTPException:
    return HTTPException(
        status.HTTP_409_CONFLICT,
        detail=PresentedOperatorActionRejection(
            reason_code=reason_code,
            message=message,
            snapshot_id=snapshot_id,
            snapshot_version=snapshot_version,
        ).model_dump(mode="json"),
    )
