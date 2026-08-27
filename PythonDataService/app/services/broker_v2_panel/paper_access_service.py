"""Account-scoped application service for sealed-program Paper access."""

from __future__ import annotations

from app.schemas.canary_admission import CanaryActivationPlan, CanaryAdmissionEvent
from app.services.broker_v2_panel.panel_scope import validate_account
from app.services.canary_admission import (
    CanaryActivationRefused,
    apply_canary_activation,
    plan_canary_activation,
)


async def prepare_paper_access(
    *,
    broker: str,
    account_id: str,
    program_key: str,
    actor: str,
    reason: str,
) -> CanaryActivationPlan:
    """Validate account scope and prepare a read-only exact-pairing plan."""
    await validate_account(broker, account_id)
    return plan_canary_activation(
        program_key=program_key,
        account_id=account_id,
        actor=actor,
        reason=reason,
    )


async def confirm_paper_access(
    *,
    broker: str,
    account_id: str,
    program_key: str,
    plan: CanaryActivationPlan,
    confirmation_token: str,
) -> CanaryAdmissionEvent:
    """Apply only the plan bound to this route's exact account and program."""
    await validate_account(broker, account_id)
    if plan.account_id != account_id or plan.program_key != program_key:
        raise CanaryActivationRefused(
            "the reviewed Paper-access plan does not match this strategy and account"
        )
    return apply_canary_activation(
        plan=plan,
        confirmation_token=confirmation_token,
    )
