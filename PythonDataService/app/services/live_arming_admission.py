"""The arming fact Start, Resume and the runner share (ADR 0059 D11, slice 7 R6).

Read-only: one ``account_arming`` read of the ledger and the runner's sealed
bindings, judged at the caller's instant. It answers ``None`` for every world
that has no arming to consult, so paper, shadow and Dry Run launches are
byte-for-byte what they were.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from pydantic import ValidationError

from app.broker.alpaca.clerk.active_authority import primary_custody_world
from app.broker.alpaca.clerk.live_arming import LIVE_ARMING_LEDGER_INVALID, LIVE_ARMING_REQUIRED, LiveArmingInvalid
from app.broker.alpaca.clerk.live_arming_ceremony import account_arming
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.models import ClerkCustodySnapshot
from app.broker.alpaca.config import AlpacaSettings, get_alpaca_settings
from app.broker.ibkr.config import live_artifacts_root
from app.schemas.account_authority import CustodyWorld
from app.schemas.run_admission import ARMING_NEXT_STEP, ArmingAdmissionFact
from app.services.bot_binding_repository import BrokerBotBinding

logger = logging.getLogger(__name__)

ArmingFactResolver = Callable[[BrokerBotBinding, ClerkCustodySnapshot, int], ArmingAdmissionFact | None]


def live_arming_admission_fact(
    binding: BrokerBotBinding,
    custody: ClerkCustodySnapshot,
    observed_at_ms: int,
    *,
    custody_world: CustodyWorld | None = None,
    settings: AlpacaSettings | None = None,
    artifacts_root: Path | None = None,
    live_state_root: Path | None = None,
) -> ArmingAdmissionFact | None:
    """The instance's arming state on the live account, or ``None`` where arming does not apply.

    The keyword seams exist for tests and for callers that already hold the
    values; production resolves each from its one owner.
    """
    world = primary_custody_world() if custody_world is None else custody_world
    if binding.mode == "dry_run" or custody.account_mode != "live" or world != "real_live":
        return None
    try:
        resolved = get_alpaca_settings() if settings is None else settings
        arming = account_arming(
            live_account_id=custody.account_id,
            artifacts_root=resolved.clerk_dir if artifacts_root is None else artifacts_root,
            live_state_root=live_artifacts_root() if live_state_root is None else live_state_root,
            configured_envelope=LiveEnvelopeValues.from_settings(resolved),
            now_ms=observed_at_ms,
            strategy_instance_ids=(binding.strategy_instance_id,),
            custody_world=world,
        )
    except (LiveArmingInvalid, LiveEnvelopeIncomplete, ValidationError) as exc:
        logger.warning(
            "live arming evidence unreadable; the launch is refused",
            extra={
                "action": "live_arming_admission_unreadable",
                "live_account_id": custody.account_id,
                "strategy_instance_id": binding.strategy_instance_id,
                "reason_code": LIVE_ARMING_LEDGER_INVALID,
                "error": str(exc),
            },
        )
        return ArmingAdmissionFact(
            state="UNREADABLE",
            reason_code=LIVE_ARMING_LEDGER_INVALID,
            explanation=(
                "The arming ledger or the ALPACA_LIVE_* environment for this account does not "
                "verify; the detail is in the service log."
            ),
            next_step="Restore the arming ledger and the ALPACA_LIVE_* environment, then retry.",
            observed_at_ms=observed_at_ms,
        )
    status = arming.statuses[binding.strategy_instance_id]
    if status.state == "armed":
        return ArmingAdmissionFact(
            state="ARMED",
            explanation=f"{binding.strategy_instance_id} is armed on {custody.account_id} with {status.sessions_remaining} session(s) remaining.",
            observed_at_ms=observed_at_ms,
        )
    return ArmingAdmissionFact(
        state="NOT_ARMED",
        reason_code=status.reason_code or LIVE_ARMING_REQUIRED,
        explanation=(
            f"{binding.strategy_instance_id} has never been armed on {custody.account_id}."
            if status.state == "unarmed"
            else f"{binding.strategy_instance_id} is {status.state} on {custody.account_id} ({status.reason_code})."
        ),
        next_step=ARMING_NEXT_STEP,
        observed_at_ms=observed_at_ms,
    )


__all__ = ["ArmingFactResolver", "live_arming_admission_fact"]
