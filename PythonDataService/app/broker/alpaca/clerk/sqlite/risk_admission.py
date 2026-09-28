"""Rejudge account risk immediately before committing new exposure.

A fresh broker observation is only a price/cash input, not durable permission.
Execution and fee evidence can change between cadence ticks. Budget and ENTER
call this check inside their commit coordinator; no network occurs here.
The normal custody folds continue accepting evidence and reductions even when
this check withdraws new-exposure permission.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    AccountObservation,
    LiveEnvelopeGate,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import observed_day_pnl, risk_evidence_ready, risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
    raise_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, LossHoldCause


@dataclass(frozen=True)
class RiskReadiness:
    observation: AccountObservation | None = None
    reason_code: str | None = None
    detail: str = "Current account risk evidence permits new exposure."
    breach_cause: LossHoldCause | None = None

    @property
    def allowed(self) -> bool:
        return self.reason_code is None


def current_risk_readiness(
    repo: ClerkSqliteRepository, *, envelope: LiveEnvelopeGate, now_ms: int,
) -> RiskReadiness:
    """Read the current admission judgement without withdrawing or writing a hold.

    Consumers retain the same writer fence through any related cash/hold reads.
    This read is advisory: actual commitment rechecks and records breaches.
    """
    with repo._write_lock:
        if envelope.agreement == "disagreed":
            return RiskReadiness(reason_code=LIVE_ENVELOPE_DISAGREEMENT, detail="The effective account risk configuration disagrees. Review Configuration.")
        if repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is not None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, detail="The account loss hold stands. Review it in Configuration.")
        observation = envelope.fresh_observation(now_ms)
        policy = repo.account_risk_policy()
        revision = None if policy is None else policy.revision
        if observation is None or observation.risk_revision != revision or observation.last_equity_usd is None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, detail="Fresh account evidence for the current risk limits is unavailable. Refresh account evidence.")
        if repo.account_id.startswith(("sim:", "shadow:")) and (
            observation.simulation_session_start_ms != et_day_window_ms(now_ms)[0]
            or (observation.simulation_marks_valid_until_ms is not None and now_ms > observation.simulation_marks_valid_until_ms)
        ):
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, detail="Refresh the simulation session baseline and current market prices before deploying.")
        if observation.risk_fill_sequence != risk_fill_sequence(repo):
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, detail="Executions changed after the account observation. Refresh account evidence before deploying.")
        synthetic = repo.account_id.startswith("sim:")
        values = policy if policy is not None else (envelope.in_force if envelope.values is not None else None)
        if values is None and not synthetic:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, detail="Apply account risk limits in Configuration before deploying.")
        if observation.equity_usd is None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, detail="Current account equity is unavailable. Refresh account evidence.")
        pnl = observed_day_pnl(observation=observation, now_ms=now_ms)
        if not pnl.known or not math.isfinite(observation.last_equity_usd) or not risk_evidence_ready(repo, now_ms=now_ms):
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, detail="Current account evidence is incomplete. Refresh transfers and fees and reconcile executions before deploying.")
        # Dry Run retains its explicit daily-loss-policy exemption while money,
        # fees, execution evidence and ordinary custody holds still apply.
        if not synthetic:
            limit = loss_limit_usd(values, last_equity_usd=observation.last_equity_usd)
            if loss_breached(day_pnl_usd=pnl.total_usd, loss_limit_usd=limit):
                cause = LossHoldCause(pnl.day_start_ms, pnl.total_usd, limit, observation.last_equity_usd, observation.observed_at_ms, revision)
                return RiskReadiness(reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                    detail="The current account loss limit is breached. Review the loss hold in Configuration.", breach_cause=cause)
        return RiskReadiness(observation=observation)


def require_current_risk_admission(
    repo: ClerkSqliteRepository, *, envelope: LiveEnvelopeGate, now_ms: int,
) -> AccountObservation:
    """Commit-time wrapper: retain the writer fence and record any new breach."""
    with repo._write_lock:
        decision = current_risk_readiness(repo, envelope=envelope, now_ms=now_ms)
        if decision.allowed:
            assert decision.observation is not None
            return decision.observation
        envelope.withdraw()
        if decision.breach_cause is not None:
            cause = decision.breach_cause
            raise_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                evidence_refs=[f"day-pnl:{cause.day_start_ms}"], cause_facts=cause.to_mapping())
        raise AdmissionBlockedError(CapabilityDecision(
            allowed=False, capability=Capability.NEW_EXPOSURE, reason_code=decision.reason_code, why=decision.detail,
        ))
