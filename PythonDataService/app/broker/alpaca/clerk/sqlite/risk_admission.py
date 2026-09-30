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
    ENVELOPE_SYNC_INTERVAL_S,
    LIVE_ENVELOPE_UNOBSERVED,
    AccountObservation,
    LiveEnvelopeGate,
    loss_breached,
    loss_limit_usd,
)
from app.broker.alpaca.clerk.sqlite.day_pnl import observed_day_pnl, reading_covers_executions, risk_evidence_ready
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty import (
    AdmissionBlockedError,
    Capability,
    CapabilityDecision,
    raise_account_hold,
)
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, LossHoldCause

# The envelope cadence re-reads the account on its own; there is no manual
# refresh, so the copy says when the next reading comes instead of asking for one.
_NEXT_READING = f"The account is read again every {ENVELOPE_SYNC_INTERVAL_S:g} seconds."


class AccountReadingBehindExecutions(AdmissionBlockedError):
    """New exposure is refused because executions postdate the last account reading.

    The refusal a newer reading can lift, so an ENTER refused this way waits
    for one instead of being dropped (#2623, owner decision 2026-09-29). It is
    judged before the equity, day P&L, fee, loss-limit and cash rules, so it
    says nothing about them: the ENTER is judged whole again on the new reading.
    """


@dataclass(frozen=True)
class RiskReadiness:
    observation: AccountObservation | None = None
    reason_code: str | None = None
    detail: str = "Current account risk evidence permits new exposure."
    breach_cause: LossHoldCause | None = None
    # No limit is set at all: the fix is setting one, not waiting for evidence.
    limit_missing: bool = False
    # The one refusal a newer account reading can lift (#2623).
    reading_behind_executions: bool = False

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
        if repo.active_uncertainty(scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None) is not None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, detail="The account loss hold stands. Review it in Settings.")
        policy = repo.account_risk_policy()
        revision = None if policy is None else policy.revision
        synthetic = repo.account_id.startswith("sim:")
        values = policy if policy is not None else envelope.values
        # A missing limit is judged before any observation: without one there
        # is nothing to observe against, and naming stale evidence instead sent
        # the owner looking for a refresh that could never help (#2566, H6/H7).
        if values is None and not synthetic:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, limit_missing=True,
                detail="No daily loss limit is set for this account, so new entries are refused. Set one in Settings.")
        observation = envelope.fresh_observation(now_ms)
        if observation is None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED,
                detail=f"Alpaca has not confirmed this account's cash and equity recently, so new entries wait. {_NEXT_READING}")
        if observation.risk_revision != revision:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED,
                detail=f"The daily loss limit changed after the last account reading, so new entries wait. {_NEXT_READING}")
        if observation.last_equity_usd is None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED,
                detail=f"Alpaca has not reported this account's starting equity for the day, so the loss limit cannot be judged. {_NEXT_READING}")
        if repo.account_id.startswith(("sim:", "shadow:")) and (
            observation.simulation_session_start_ms != et_day_window_ms(now_ms)[0]
            or (observation.simulation_marks_valid_until_ms is not None and now_ms > observation.simulation_marks_valid_until_ms)
        ):
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED,
                detail=f"The simulated account's session baseline or current market prices are out of date, so new entries wait. {_NEXT_READING}")
        if not reading_covers_executions(repo, observation):
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED, reading_behind_executions=True,
                detail=f"Executions changed after the last account reading, so new entries wait. {_NEXT_READING}")
        if observation.equity_usd is None:
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED,
                detail=f"Alpaca has not reported this account's current equity, so new entries wait. {_NEXT_READING}")
        pnl = observed_day_pnl(observation=observation, now_ms=now_ms)
        if not pnl.known or not math.isfinite(observation.last_equity_usd) or not risk_evidence_ready(repo, now_ms=now_ms):
            return RiskReadiness(reason_code=LIVE_ENVELOPE_UNOBSERVED,
                detail="Current account evidence is incomplete: some transfers, fees or executions are not accounted for yet, so new entries wait.")
        # Dry Run retains its explicit daily-loss-policy exemption while money,
        # fees, execution evidence and ordinary custody holds still apply.
        if not synthetic:
            limit = loss_limit_usd(values, last_equity_usd=observation.last_equity_usd)
            if loss_breached(day_pnl_usd=pnl.total_usd, loss_limit_usd=limit):
                cause = LossHoldCause(pnl.day_start_ms, pnl.total_usd, limit, float(observation.last_equity_usd), observation.observed_at_ms, revision)
                return RiskReadiness(reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                    detail="The current account loss limit is breached. Review the loss hold in Settings.", breach_cause=cause)
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
        # A reading behind executions stays published (#2623): every other
        # ENTER, and a budget deploy, must meet the same refusal -- the one
        # that waits -- not an "unobserved" one that drops it. Leaving it
        # cannot admit anything, because every commitment re-runs this whole
        # judgement, the execution watermark included, and that reading fails
        # it until a newer one replaces it.
        if not decision.reading_behind_executions:
            envelope.withdraw()
        if decision.breach_cause is not None:
            cause = decision.breach_cause
            raise_account_hold(repo, reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
                evidence_refs=[f"day-pnl:{cause.day_start_ms}"], cause_facts=cause.to_mapping())
        refusal = AccountReadingBehindExecutions if decision.reading_behind_executions else AdmissionBlockedError
        raise refusal(CapabilityDecision(
            allowed=False, capability=Capability.NEW_EXPOSURE, reason_code=decision.reason_code, why=decision.detail,
        ))
