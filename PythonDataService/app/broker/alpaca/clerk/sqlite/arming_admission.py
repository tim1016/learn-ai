"""Per-instance arming at ENTER — the third gate, between admission and the envelope (ADR 0059 D11, slice 7).

Order of refusals, each fail-closed: a ledger the last refresh could not
verify; no fresh snapshot; then the instance's own state — never armed,
lapsed, or disarmed under its reason code. EXIT is never subject to this
(Decisions 3 and 4). Nothing here touches a file, a broker or a clock: the
sync published the snapshot, and the caller's ``now_ms`` — the repository
clock — decides freshness and lapse.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_REQUIRED,
    LIVE_ARMING_UNOBSERVED,
)
from app.broker.alpaca.clerk.live_arming_gate import ArmingGate
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
)


def _refuse(reason_code: str, why: str) -> AdmissionBlockedError:
    return AdmissionBlockedError(
        CapabilityDecision(
            allowed=False,
            capability=Capability.NEW_EXPOSURE,
            reason_code=reason_code,
            why=why,
        )
    )


def require_arming_admission(gate: ArmingGate, *, strategy_instance_id: str, now_ms: int) -> None:
    """Admit one ENTER only for an instance the ledger says is armed right now.

    Returns nothing: the whole answer is whether it raised. No caller has ever
    read a status back from here, and a return value nobody reads is a second
    thing to keep true.
    """
    reason_code = gate.invalid_reason_code
    if reason_code is not None:
        raise _refuse(
            reason_code,
            f"The live authority admits no ENTER while its arming evidence cannot be judged "
            f"({gate.invalid_why}); restore it and let the sync observe again.",
        )
    snapshot = gate.fresh_snapshot(now_ms)
    if snapshot is None:
        raise _refuse(
            LIVE_ARMING_UNOBSERVED,
            "No fresh arming snapshot exists; the live authority cannot tell whether this instance is armed.",
        )
    status = snapshot.status_for(strategy_instance_id, now_ms=now_ms)
    if status.state == "armed":
        return
    if status.state == "unarmed":
        raise _refuse(
            LIVE_ARMING_REQUIRED,
            f"{strategy_instance_id} has never been armed on {snapshot.live_account_id}; "
            "run scripts.manage_alpaca_arming plan, then apply.",
        )
    raise _refuse(
        status.reason_code or LIVE_ARMING_REQUIRED,
        f"{strategy_instance_id} is {status.state} on {snapshot.live_account_id} "
        f"({status.reason_code}); re-arm before its next ENTER.",
    )


__all__ = ["require_arming_admission"]
