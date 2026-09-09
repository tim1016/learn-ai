"""Operator actions on the live envelope (ADR 0059 D4).

``clear_loss_hold`` is the ADR 0011 §6 shape: it re-reads the fact the
hold was raised on and refuses while the breach still stands. The hold
never clears on a timer or at session rollover; this is the only release.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_envelope import LIVE_ENVELOPE_UNOBSERVED
from app.broker.alpaca.clerk.sqlite.uncertainty import resolve_account_hold
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.contract.errors import BrokerError
from app.schemas.alpaca_live_envelope import LossHoldClearOutcome

LIVE_ENVELOPE_LOSS_HOLD_STANDS = "LIVE_ENVELOPE_LOSS_HOLD_STANDS"
LIVE_ENVELOPE_LOSS_HOLD_CLEARED = "LIVE_ENVELOPE_LOSS_HOLD_CLEARED"


class LiveEnvelopeNotInstalled(Exception):
    """The active authority carries no live envelope (paper, synthetic, or none)."""


async def clear_loss_hold(runtime: ActiveClerkRuntime, *, now_ms: int) -> LossHoldClearOutcome:
    repo = runtime.sqlite_repository
    sync = runtime.envelope_sync
    if repo is None or sync is None:
        raise LiveEnvelopeNotInstalled("no live envelope is installed on the active authority")
    if repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is None:
        return LossHoldClearOutcome(outcome="no_hold", reason_code=None, day_pnl_usd=None, loss_limit_usd=None, observed_at_ms=now_ms, detail="The account is not in loss hold.")
    try:
        reading = await sync.observe()
    except BrokerError as exc:
        return LossHoldClearOutcome(outcome="refused", reason_code=LIVE_ENVELOPE_UNOBSERVED, day_pnl_usd=None, loss_limit_usd=None, observed_at_ms=now_ms, detail=f"The account could not be re-observed: {exc}. The hold stands.")
    day_pnl = None if reading.day_pnl is None else reading.day_pnl.total_usd
    if reading.breached is None:
        return LossHoldClearOutcome(outcome="refused", reason_code=LIVE_ENVELOPE_UNOBSERVED, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail="Day P&L is unknown (an external order was seen today, or the broker reported no previous-close equity). The hold stands.")
    if reading.breached:
        return LossHoldClearOutcome(outcome="refused", reason_code=LIVE_ENVELOPE_LOSS_HOLD_STANDS, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail=f"Day P&L {day_pnl:.2f} USD is still at or below the {reading.loss_limit_usd:.2f} USD loss limit. The hold stands.")
    released = resolve_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, summary_code=LIVE_ENVELOPE_LOSS_HOLD_CLEARED)
    if not released:
        return LossHoldClearOutcome(outcome="no_hold", reason_code=None, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail="The hold was already released.")
    return LossHoldClearOutcome(outcome="cleared", reason_code=None, day_pnl_usd=day_pnl, loss_limit_usd=reading.loss_limit_usd, observed_at_ms=reading.observation.observed_at_ms, detail=f"Loss hold cleared: day P&L {day_pnl:.2f} USD is above the {reading.loss_limit_usd:.2f} USD loss limit. New entries are admitted again.")


__all__ = ["LIVE_ENVELOPE_LOSS_HOLD_CLEARED", "LIVE_ENVELOPE_LOSS_HOLD_STANDS", "LiveEnvelopeNotInstalled", "clear_loss_hold"]
