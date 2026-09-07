"""Compose the Alpaca live verdict from settings and clerk selection (ADR 0059 D8).

Pure: no broker I/O, no clock of its own. The router supplies ``now_ms``.
"""

from __future__ import annotations

from typing import Literal

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.alpaca_live_verdict import (
    AlpacaLiveVerdict,
    ClerkAuthority,
    ModeAgreement,
)

_DISAGREEMENT = "LIVE_MODE_DISAGREEMENT"
_UNOBSERVED_REASONS = frozenset({"BROKER_ACCOUNT_UNAVAILABLE"})

_WHY_UNKNOWN: dict[str, str] = {
    "disagreed": "the configured mode and the observed account disagree",
    "unobserved": "the account has not been observed yet",
}


def _clerk_authority(runtime: ActiveClerkRuntime | None) -> ClerkAuthority:
    if runtime is None:
        return "not_installed"
    return runtime.authority_kind


def _mode_agreement(
    *,
    account_id: str | None,
    refusal: str | None,
) -> ModeAgreement:
    if refusal == _DISAGREEMENT:
        return "disagreed"
    if account_id is None or refusal in _UNOBSERVED_REASONS:
        return "unobserved"
    return "agreed"


def alpaca_live_verdict(
    *,
    settings: AlpacaSettings | None,
    runtime: ActiveClerkRuntime | None,
    now_ms: int,
) -> AlpacaLiveVerdict:
    """Return the verdict for the current process state."""
    if settings is None:
        return AlpacaLiveVerdict(
            configured_mode="unconfigured",
            observed_account_id=None,
            mode_agreement="unobserved",
            clerk_authority=_clerk_authority(runtime),
            clerk_refusal_reason_code=None,
            armed_instance_count=0,
            envelope_state="not_applicable",
            shadow_state="not_applicable",
            final_verdict="unknown",
            headline="Alpaca is not configured",
            detail="No valid Alpaca settings are loaded, so the account mode cannot be stated.",
            observed_at_ms=now_ms,
        )

    failure = runtime.startup_failure if runtime is not None else None
    refusal = failure.reason_code if failure is not None else None
    account_id = runtime.selected_account_id if runtime is not None else None
    if account_id is None and failure is not None:
        account_id = failure.account_id
    authority = _clerk_authority(runtime)

    agreement = _mode_agreement(account_id=account_id, refusal=refusal)
    mode: Literal["paper", "live"] = "paper" if settings.is_paper else "live"

    # Fail closed (ADR 0011 / ADR 0059 D8): a disagreement is unknown under
    # either mode, and live is unknown until the account is positively
    # observed. A paper account cannot reach live funds, but a paper verdict
    # must not reassure while the Clerk is refusing the account.
    if agreement == "disagreed" or (mode == "live" and agreement != "agreed"):
        return AlpacaLiveVerdict(
            configured_mode=mode,
            observed_account_id=account_id,
            mode_agreement=agreement,
            clerk_authority=authority,
            clerk_refusal_reason_code=refusal,
            armed_instance_count=0,
            envelope_state="not_applicable" if mode == "paper" else "configured_unsealed",
            shadow_state="not_applicable" if mode == "paper" else "none",
            final_verdict="unknown",
            headline=f"{'Paper' if mode == 'paper' else 'Live'} mode configured — account state unknown",
            detail=(
                f"ALPACA_MODE={mode}, but {_WHY_UNKNOWN[agreement]}. No order path "
                "trusts this account while the verdict is unknown."
            ),
            observed_at_ms=now_ms,
        )

    if mode == "paper":
        return AlpacaLiveVerdict(
            configured_mode="paper",
            observed_account_id=account_id,
            mode_agreement=agreement,
            clerk_authority=authority,
            clerk_refusal_reason_code=refusal,
            armed_instance_count=0,
            envelope_state="not_applicable",
            shadow_state="not_applicable",
            final_verdict="paper",
            headline=f"Paper account{f' {account_id}' if account_id else ''} — no real money at risk",
            detail="ALPACA_MODE=paper. Orders reach Alpaca's paper endpoint only.",
            observed_at_ms=now_ms,
        )

    # Slice 1: no arming exists, so an agreed live account is always unarmed.
    return AlpacaLiveVerdict(
        configured_mode="live",
        observed_account_id=account_id,
        mode_agreement="agreed",
        clerk_authority=authority,
        clerk_refusal_reason_code=refusal,
        armed_instance_count=0,
        envelope_state="configured_unsealed",
        shadow_state="none",
        final_verdict="live-unarmed",
        headline=f"LIVE account {account_id} — real money, no instance armed",
        detail=(
            "This is a real-money Alpaca account. No sealed instance is armed, so "
            "every order path refuses. Arming requires a completed shadow receipt "
            "and the supervised ceremony (ADR 0059)."
        ),
        observed_at_ms=now_ms,
    )
