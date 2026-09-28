"""Read the installed account authority; never infer permission from old grants.

The verdict is an account-level observation, not a count of authorized bots.
Every new run still needs its own reviewed budget and, for Live, explicit consent.
"""
from __future__ import annotations

from app.broker.alpaca.clerk.account_authority import live_account_id_for_shadow_account
from app.broker.alpaca.clerk.active_runtime import ActiveClerkRuntime
from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT
from app.broker.alpaca.clerk.sqlite.risk_admission import current_risk_readiness
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.alpaca_live_verdict import (
    AlpacaLiveVerdict,
    DeploymentReadiness,
    LossHoldState,
    ModeAgreement,
)


def observe_loss_hold(runtime: ActiveClerkRuntime | None) -> LossHoldState:
    """Read the selected authority's standing hold, including simulation worlds."""
    repo = None if runtime is None else runtime.sqlite_repository
    if repo is None:
        return "not_applicable"
    active = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE,
        strategy_instance_id=None,
    )
    return "held" if active is not None else "clear"


def _readiness(runtime: ActiveClerkRuntime | None, *, held: bool) -> tuple[int, DeploymentReadiness]:
    repo = None if runtime is None else runtime.sqlite_repository
    if repo is None:
        return 0, "not_applicable"
    version = repo.budget_authority_version()
    if version < 2:
        return version, "upgrade_required"
    if held:
        return version, "loss_hold"
    sync = runtime.envelope_sync
    if sync is None:
        return version, "risk_not_observed"
    current = current_risk_readiness(repo, envelope=sync.envelope, now_ms=repo.clock())
    if not current.allowed:
        return version, ("loss_hold" if current.reason_code == LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE else "risk_not_observed")
    return version, "ready"


_READINESS_COPY: dict[DeploymentReadiness, str] = {
    "not_applicable": "Activate this account in Settings before deploying a bot.",
    "upgrade_required": "Switch this account to budgets in Settings before deploying a new bot.",
    "risk_not_observed": "Current risk evidence is unavailable. New entries remain blocked until it is observed.",
    "loss_hold": "The account is in loss hold. Clear it in Settings after recovery; reducing exits remain available.",
    "ready": "Budgeted Deploy is available. Every new run needs its own reviewed budget; Live also requires explicit consent.",
}


def alpaca_live_verdict(
    *, settings: AlpacaSettings | None, runtime: ActiveClerkRuntime | None,
    now_ms: int, loss_hold: LossHoldState | None = None,
) -> AlpacaLiveVerdict:
    """Describe current custody and readiness without opening files or contacting a broker."""
    failure = None if runtime is None else runtime.startup_failure
    account_id = None if runtime is None else runtime.selected_account_id
    if account_id is None and failure is not None:
        account_id = failure.account_id
    refusal = None if failure is None else failure.reason_code
    authority = "not_installed" if runtime is None else runtime.authority_kind
    gate = None if runtime is None or runtime.clerk is None else runtime.clerk.live_arming
    if ((gate is not None and gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT)
            or (runtime is not None and runtime.envelope_sync is not None and runtime.envelope_sync.account_mode_disagreed)):
        refusal = LIVE_MODE_DISAGREEMENT
    agreement: ModeAgreement = (
        "disagreed" if refusal == LIVE_MODE_DISAGREEMENT else
        "unobserved" if account_id is None or refusal == "BROKER_ACCOUNT_UNAVAILABLE" else "agreed"
    )
    hold = observe_loss_hold(runtime) if loss_hold is None else loss_hold
    version, readiness = _readiness(runtime, held=hold == "held")
    common = {
        "configured_mode": "unconfigured" if settings is None else settings.mode,
        "observed_account_id": account_id,
        "mode_agreement": agreement,
        "clerk_authority": authority,
        "clerk_refusal_reason_code": refusal,
        "budget_authority_version": version,
        "deployment_readiness": readiness,
        "loss_hold": hold,
        "observed_at_ms": now_ms,
    }
    if settings is None:
        return AlpacaLiveVerdict(**{**common, "deployment_readiness": "not_applicable"},
            final_verdict="unknown", headline="Alpaca is not configured",
            detail="No valid account configuration is loaded. Open Settings to select an account.")
    if agreement == "disagreed" or (settings.is_live and agreement != "agreed"):
        return AlpacaLiveVerdict(**{**common, "deployment_readiness": "not_applicable"},
            final_verdict="unknown", headline="Account mode is not verified",
            detail="The configured account mode has not been verified against the broker. New entries are blocked.")
    if settings.is_paper:
        return AlpacaLiveVerdict(**common, final_verdict="paper",
            headline="Paper account — no real money at risk",
            detail="Orders reach Alpaca's paper endpoint only. " + _READINESS_COPY[readiness])
    live_id = None if account_id is None else live_account_id_for_shadow_account(account_id)
    if authority == "shadow":
        return AlpacaLiveVerdict(**common, final_verdict="shadow",
            headline=f"LIVE account {live_id} — Shadow simulation, nothing submitted",
            detail="Every fill is simulated and no order is submitted to the live account. " + _READINESS_COPY[readiness])
    return AlpacaLiveVerdict(**common, final_verdict="live",
        headline=f"LIVE account {live_id} — real money",
        detail="Live orders can reach this account only through its installed Live authority. " + _READINESS_COPY[readiness])
