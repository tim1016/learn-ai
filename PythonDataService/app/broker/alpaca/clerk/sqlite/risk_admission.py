"""Rejudge account risk immediately before committing new exposure.

A fresh broker observation is only a price/cash input, not durable permission.
Execution and fee evidence can change between cadence ticks. Budget and ENTER
call this check inside their commit coordinator; no network occurs here.
The normal custody folds continue accepting evidence and reductions even when
this check withdraws new-exposure permission.
"""
from __future__ import annotations

import math

from app.broker.alpaca.clerk.et_day import et_day_window_ms
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_UNOBSERVED,
    AccountObservation,
    LiveEnvelopeGate,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_at, risk_fill_sequence
from app.broker.alpaca.clerk.sqlite.economic_projection import SqliteEconomicProjectionReader
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
    raise_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, LossHoldCause


def _refuse(envelope: LiveEnvelopeGate, reason: str, detail: str) -> AdmissionBlockedError:
    envelope.withdraw()
    return AdmissionBlockedError(CapabilityDecision(
        allowed=False, capability=Capability.NEW_EXPOSURE, reason_code=reason, why=detail,
    ))


def require_current_risk_admission(
    repo: ClerkSqliteRepository, *, envelope: LiveEnvelopeGate, now_ms: int,
) -> AccountObservation:
    """Return the fresh observation only if current custody risk is proven safe.

    Caller retains ``repo._write_lock`` through its own new-exposure commit.
    This also holds it for direct callers, so a refusal's durable loss cause and
    observation withdrawal cannot race another risk policy writer.
    """
    with repo._write_lock:
        if envelope.agreement == "disagreed":
            raise _refuse(envelope, LIVE_ENVELOPE_DISAGREEMENT, "The effective account risk configuration disagrees. Review Configuration.")
        observation = envelope.fresh_observation(now_ms)
        policy = repo.account_risk_policy()
        revision = None if policy is None else policy.revision
        if observation is None or observation.risk_revision != revision or observation.last_equity_usd is None:
            raise _refuse(envelope, LIVE_ENVELOPE_UNOBSERVED, "Fresh account evidence for the current risk limits is unavailable. Refresh account evidence.")
        if repo.account_id.startswith(("sim:", "shadow:")) and (
            observation.simulation_session_start_ms != et_day_window_ms(now_ms)[0]
            or (observation.simulation_marks_valid_until_ms is not None and now_ms > observation.simulation_marks_valid_until_ms)
        ):
            raise _refuse(envelope, LIVE_ENVELOPE_UNOBSERVED, "Refresh the simulation session baseline and current market prices before deploying.")
        if observation.risk_fill_sequence != risk_fill_sequence(repo):
            raise _refuse(envelope, LIVE_ENVELOPE_UNOBSERVED, "Executions changed after the account observation. Refresh account evidence before deploying.")
        synthetic = repo.account_id.startswith("sim:")
        values = policy if policy is not None else (envelope.in_force if envelope.values is not None else None)
        if values is None and not synthetic:
            raise _refuse(envelope, LIVE_ENVELOPE_UNOBSERVED, "Apply account risk limits in Configuration before deploying.")
        if repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is not None:
            raise _refuse(envelope, LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, "The account loss hold stands. Review it in Configuration.")
        reader = SqliteEconomicProjectionReader.from_repository(repo)
        try:
            pnl = day_pnl_at(reader, repo, observation=observation, now_ms=now_ms)
        finally:
            reader.close()
        if not pnl.known or not math.isfinite(observation.last_equity_usd):
            raise _refuse(envelope, LIVE_ENVELOPE_UNOBSERVED, "Current account loss evidence is incomplete. Refresh fees and reconcile executions before deploying.")
        # Dry Run retains its explicit account-loss-policy exemption. Its
        # private cash, fees, execution evidence and custody holds still gate.
        if synthetic:
            return observation
        limit = loss_limit_usd(values, last_equity_usd=observation.last_equity_usd)
        if loss_breached(day_pnl_usd=pnl.total_usd, loss_limit_usd=limit):
            cause = LossHoldCause(pnl.day_start_ms, pnl.total_usd, limit, observation.last_equity_usd, observation.observed_at_ms, revision)
            envelope.withdraw()
            raise_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                evidence_refs=[f"day-pnl:{cause.day_start_ms}"], cause_facts=cause.to_mapping())
            raise _refuse(envelope, LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, "The current account loss limit is breached. Review the loss hold in Configuration.")
        return observation
