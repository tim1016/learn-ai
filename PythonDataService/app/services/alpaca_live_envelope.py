"""Operator actions on the live envelope (ADR 0059 D4).

``clear_loss_hold`` is the ADR 0011 §6 shape: it re-reads the fact the
hold was raised on and refuses while the breach still stands. The hold
never clears on a timer or at session rollover; this is the only release.

The limit it re-reads against is the envelope **sealed at arming** wherever
an arming record exists (ADR 0059 D3): ``sync.observe()`` re-reads the
arming ledger before it re-reads the account, so raising
``ALPACA_LIVE_LOSS_USD`` in the environment and restarting cannot release a
standing hold. Only a re-arm moves that number. An account no ceremony has
ever armed has nothing sealed, and falls back to the configured values.

Re-reading is not free of side effects: ``sync.observe()`` is the same call
the background tap makes, so after ruling R-A′ it re-publishes the gate's
observation only when the reading is judgeable AND not breached, and
withdraws it otherwise -- unknown or breached alike.
"""

from __future__ import annotations

from typing import Literal

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.sqlite.live_envelope_sync import EnvelopeReading
from app.broker.alpaca.clerk.sqlite.uncertainty import resolve_account_hold
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
        day_pnl_usd=None if day_pnl is None or not day_pnl.known else day_pnl.total_usd,
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
        reading = await sync.observe()
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
    if day_pnl is None or not day_pnl.known or loss_limit_usd is None:
        return _from_reading(
            reading,
            outcome="refused",
            reason_code=LIVE_ENVELOPE_UNOBSERVED,
            detail=_unjudgeable_detail(reading),
        )
    limit_source = _SEALED_LIMIT if sync.envelope.in_force_is_sealed else _CONFIGURED_LIMIT
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
    released = resolve_account_hold(
        repo,
        reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        summary_code=LIVE_ENVELOPE_LOSS_HOLD_CLEARED,
    )
    if not released:
        return _from_reading(
            reading,
            outcome="no_hold",
            reason_code=None,
            detail="The hold was already released.",
        )
    return _from_reading(
        reading,
        outcome="cleared",
        reason_code=None,
        detail=(
            f"Loss hold cleared: day P&L {day_pnl.total_usd:.2f} USD is above the "
            f"{loss_limit_usd:.2f} USD loss limit {limit_source}. New entries are "
            "admitted again."
        ),
    )


__all__ = [
    "LIVE_ENVELOPE_LOSS_HOLD_CLEARED",
    "LIVE_ENVELOPE_LOSS_HOLD_STANDS",
    "LiveEnvelopeNotInstalled",
    "clear_loss_hold",
]
