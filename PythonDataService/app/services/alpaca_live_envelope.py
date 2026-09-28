"""Guarded account loss-hold clearance for both Paper and Live.

Re-observe current evidence, then prove the current policy and the hold's
retained period/threshold under the custody writer fence. A rollover, larger
baseline, looser Apply or legacy re-arm cannot clear a standing hold.
"""

from __future__ import annotations

from typing import Literal

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import EnvelopeReading
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.contract.errors import BrokerError
from app.schemas.alpaca_live_envelope import LossHoldClearOutcome

LIVE_ENVELOPE_LOSS_HOLD_STANDS = "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
LIVE_ENVELOPE_LOSS_HOLD_CLEARED = "LIVE_ENVELOPE_LOSS_HOLD_CLEARED"
# Which envelope decided, said in the operator's own sentence. An operator who
# has just raised a limit in the environment and is watching the hold refuse
# anyway needs to read that the number is the armed one, not the edited one.
_SEALED_LIMIT = "sealed at arming"
_CONFIGURED_LIMIT = "configured in the environment"

_ClearOutcome = Literal["cleared", "no_hold", "refused"]


class LiveEnvelopeNotInstalled(Exception):
    """The active authority carries no live envelope (paper, synthetic, or none)."""


def _unobserved(
    now_ms: int,
    *,
    outcome: _ClearOutcome,
    reason_code: str | None,
    detail: str,
) -> LossHoldClearOutcome:
    """An outcome with no observation behind it: there are no figures to report."""
    return LossHoldClearOutcome(
        outcome=outcome,
        reason_code=reason_code,
        day_pnl_usd=None,
        loss_limit_usd=None,
        observed_at_ms=now_ms,
        detail=detail,
    )


def _unjudgeable_detail(reading: EnvelopeReading) -> str:
    """Why the account cannot be judged, in the operator's own sentence.

    One predicate, two sentences, so the prose cannot drift from the diagnosis
    the sync logs beside it (``_unknown_detail``'s
    ``sealed_envelope_readable``). An operator sent to debug the broker feed
    over a corrupt ``live_arming.jsonl`` loses the incident.
    """
    if not reading.seal_readable:
        return (
            "This account's arming inputs could not be read, so there is no sealed "
            "loss limit to judge against. Repair the arming ledger, then clear again. "
            "The hold stands."
        )
    return (
        "Day P&L is unknown (an external order was seen today, or the broker "
        "reported no previous-close equity, or a risk figure the broker "
        "reported was not a finite number). The hold stands."
    )


def _from_reading(
    reading: EnvelopeReading,
    *,
    outcome: _ClearOutcome,
    reason_code: str | None,
    detail: str,
) -> LossHoldClearOutcome:
    """An outcome stamped with the reading that decided it.

    ``day_pnl_usd`` is the one rule this module owns, expressed once: an
    unknown day is ``None``, never a number (plan R5). The operator reads
    this screen while deciding whether a real-money account may take
    exposure again, and a figure beside "the day is unknown" is a lie.
    """
    day_pnl = reading.day_pnl
    return LossHoldClearOutcome(
        outcome=outcome,
        reason_code=reason_code,
        day_pnl_usd=None if reading.breached is None else day_pnl.total_usd,
        loss_limit_usd=reading.loss_limit_usd,
        observed_at_ms=reading.observation.observed_at_ms,
        detail=detail,
    )


async def clear_loss_hold(runtime: ActiveClerkRuntime, *, now_ms: int) -> LossHoldClearOutcome:
    repo = runtime.sqlite_repository
    sync = runtime.envelope_sync
    if repo is None or sync is None:
        raise LiveEnvelopeNotInstalled("no live envelope is installed on the active authority")
    if (
        repo.active_uncertainty(
            scope="ACCOUNT_CLERK",
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
            strategy_instance_id=None,
        )
        is None
    ):
        return _unobserved(
            now_ms,
            outcome="no_hold",
            reason_code=None,
            detail="The account is not in loss hold.",
        )
    try:
        reading, quiet = await sync.observe_loss_clearance()
    except BrokerError as exc:
        return _unobserved(
            now_ms,
            outcome="refused",
            reason_code=LIVE_ENVELOPE_UNOBSERVED,
            detail=f"The account could not be re-observed: {exc}. The hold stands.",
        )
    day_pnl, loss_limit_usd = reading.day_pnl, reading.loss_limit_usd
    # Exactly ``reading.breached is None``, spelled out so both figures are
    # known to exist below: the day is unjudgeable precisely when one is absent.
    if reading.breached is None:
        return _from_reading(
            reading,
            outcome="refused",
            reason_code=LIVE_ENVELOPE_UNOBSERVED,
            detail=_unjudgeable_detail(reading),
        )
    limit_source = "effective account policy" if reading.policy_revision is not None else (_SEALED_LIMIT if sync.envelope.in_force_is_sealed else _CONFIGURED_LIMIT)
    if reading.breached:
        return _from_reading(
            reading,
            outcome="refused",
            reason_code=LIVE_ENVELOPE_LOSS_HOLD_STANDS,
            detail=(
                f"Day P&L {day_pnl.total_usd:.2f} USD is still at or below the "
                f"{loss_limit_usd:.2f} USD loss limit {limit_source}. The hold stands."
            ),
        )
    outcome, detail = sync.clear_observed_loss_hold(reading, quiet=quiet)
    return _from_reading(
        reading,
        outcome="refused" if outcome in {"unknown", "held"} else outcome,
        reason_code=(LIVE_ENVELOPE_UNOBSERVED if outcome == "unknown" else
                     LIVE_ENVELOPE_LOSS_HOLD_STANDS if outcome == "held" else None),
        detail=detail,
    )


__all__ = [
    "LIVE_ENVELOPE_LOSS_HOLD_CLEARED",
    "LIVE_ENVELOPE_LOSS_HOLD_STANDS",
    "LiveEnvelopeNotInstalled",
    "clear_loss_hold",
]
