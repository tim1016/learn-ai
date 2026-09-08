"""Compose the Alpaca live verdict from settings and clerk selection (ADR 0059 D8).

Pure: no broker I/O, no clock of its own. The router supplies ``now_ms`` and,
for a live boot, the ``shadow_state`` that ``observe_shadow_state`` read from
the durable shadow evidence — the one function here that touches the disk, and
it only reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.account_authority import SHADOW_ACCOUNT_PREFIX
from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.alpaca_live_verdict import (
    AlpacaLiveVerdict,
    ClerkAuthority,
    ModeAgreement,
    ShadowState,
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


def observe_shadow_state(runtime: ActiveClerkRuntime | None, artifacts_root: Path) -> ShadowState:
    """What the durable shadow evidence says for the installed shadow authority (read-only).

    Only the receipt store and the session journal are consulted: no database
    is opened and the broker is never contacted. A store that refuses its own
    rows raises rather than answering ``"none"`` — a corrupt proof is not the
    absence of one.
    """
    if runtime is None or runtime.authority_kind != "shadow" or runtime.selected_account_id is None:
        return "none"
    live_account_id = runtime.selected_account_id.removeprefix(SHADOW_ACCOUNT_PREFIX)
    if ShadowReceiptStore(artifacts_root).any_for_account(live_account_id):
        return "complete"
    if ShadowSessionLedger(artifacts_root=artifacts_root, account_id=runtime.selected_account_id).has_rows():
        return "in_progress"
    return "none"


def alpaca_live_verdict(
    *,
    settings: AlpacaSettings | None,
    runtime: ActiveClerkRuntime | None,
    now_ms: int,
    shadow_state: ShadowState | None = None,
) -> AlpacaLiveVerdict:
    """Return the verdict for the current process state.

    ``shadow_state`` is the caller's observation of the durable shadow
    evidence. It is reported only where shadow can exist — an agreed live
    account; paper stays ``not_applicable`` and an unknown verdict keeps the
    empty state it already published.
    """
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
    observed_shadow: ShadowState = shadow_state if shadow_state is not None else "none"
    shadow_active = authority == "shadow"
    # The copy names the LIVE account a human recognises. ``shadow:`` is the
    # runtime's custody namespace for the same account, not part of its number,
    # and it reads as a different account in a sentence beginning "LIVE account".
    named_account_id = account_id.removeprefix(SHADOW_ACCOUNT_PREFIX) if account_id is not None else account_id
    return AlpacaLiveVerdict(
        configured_mode="live",
        observed_account_id=account_id,
        mode_agreement="agreed",
        clerk_authority=authority,
        clerk_refusal_reason_code=refusal,
        armed_instance_count=0,
        envelope_state="configured_unsealed",
        shadow_state=observed_shadow,
        final_verdict="live-unarmed",
        headline=(
            f"LIVE account {named_account_id} — shadow authority active, no instance armed"
            if shadow_active
            else f"LIVE account {named_account_id} — real money, no instance armed"
        ),
        detail=(
            "This is a real-money Alpaca account. Its shadow authority reads it and "
            "synthesizes every fill; nothing is submitted. Arming requires a completed "
            "shadow receipt and the supervised ceremony (ADR 0059)."
            if shadow_active
            else "This is a real-money Alpaca account. No sealed instance is armed, so "
            "every order path refuses. Arming requires a completed shadow receipt "
            "and the supervised ceremony (ADR 0059)."
        ),
        observed_at_ms=now_ms,
    )
