"""Compose the Alpaca live verdict from settings and clerk selection (ADR 0059 D8).

Pure: no broker I/O, no clock of its own. The router supplies ``now_ms`` and,
for a live boot, the ``shadow_state`` that ``observe_shadow_state`` read from
the durable shadow evidence — the one function here that touches the disk, and
it only reads.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from app.broker.alpaca.clerk.account_authority import SHADOW_ACCOUNT_PREFIX
from app.broker.alpaca.clerk.active_authority import SQLITE_FACADE_AUTHORITIES, ActiveClerkRuntime
from app.broker.alpaca.clerk.live_arming import LiveArmingInvalid
from app.broker.alpaca.clerk.live_arming_ceremony import account_arming
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.alpaca_live_verdict import (
    AlpacaLiveVerdict,
    ClerkAuthority,
    EnvelopeAgreement,
    EnvelopeState,
    LossHoldState,
    ModeAgreement,
    ShadowState,
)

logger = logging.getLogger(__name__)

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


def observe_loss_hold(runtime: ActiveClerkRuntime | None) -> LossHoldState:
    """Whether the durable loss hold stands on the installed authority (read-only)."""
    if runtime is None or runtime.authority_kind != "shadow":
        return "not_applicable"
    repo = runtime.sqlite_repository
    if repo is None:
        return "not_applicable"
    active = repo.active_uncertainty(
        scope="ACCOUNT_CLERK", reason_code=LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE, strategy_instance_id=None
    )
    return "held" if active is not None else "clear"


@dataclass(frozen=True)
class ArmingObservation:
    """What the durable arming ledger says for one live account (read-only).

    ``detail`` is a fragment of backend-authored operator prose, appended to
    the verdict's detail sentence. It names every instance the ledger knows
    that is *not* armed, with its reason code, because "0 armed" and "0
    armed, and here is the one that lapsed last Tuesday" are different
    operator situations.
    """

    armed_instance_count: int
    envelope_state: EnvelopeState
    detail: str

    @classmethod
    def none(cls) -> ArmingObservation:
        """No arming evidence was consulted, and none is claimed."""
        return cls(armed_instance_count=0, envelope_state="configured_unsealed", detail="")


def observe_arming(
    runtime: ActiveClerkRuntime | None,
    artifacts_root: Path,
    live_state_root: Path,
    *,
    settings: AlpacaSettings,
    now_ms: int,
) -> ArmingObservation:
    """Count this live account's armed instances from durable evidence (read-only).

    No database is opened and the broker is never contacted: the arming
    ledger, the runner's sealed bindings and the configured envelope are the
    only inputs. Fails closed -- a ledger that will not verify counts no
    instance and says so in the detail, rather than reporting an account as
    unarmed for a reason nobody can see.
    """
    if (
        runtime is None
        # This slice's only custody path is the shadow authority; slice 7's
        # real ``sqlite`` live authority is the widening point that lets this
        # gate open for a genuinely live account, not just a rehearsing one.
        or runtime.authority_kind != "shadow"
        or runtime.selected_account_id is None
        or settings.is_paper
    ):
        return ArmingObservation.none()
    live_account_id = runtime.selected_account_id.removeprefix(SHADOW_ACCOUNT_PREFIX)
    try:
        # One read of the ledger answers every question below: the count, the
        # envelope state and the not-armed prose all come from the same
        # snapshot, so a concurrent ``apply`` cannot make one verdict describe
        # two.
        arming = account_arming(
            live_account_id=live_account_id,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root,
            configured_envelope=LiveEnvelopeValues.from_settings(settings),
            now_ms=now_ms,
        )
    except (LiveArmingInvalid, LiveEnvelopeIncomplete) as exc:
        logger.error(
            "the arming ledger cannot be read; the verdict counts no armed instance",
            extra={
                "action": "live_arming_ledger_invalid",
                "account_id": live_account_id,
                "why": str(exc),
            },
        )
        return ArmingObservation(
            armed_instance_count=0,
            envelope_state="configured_unsealed",
            detail=f" The arming ledger cannot be read ({exc}); no instance is counted as armed.",
        )
    not_armed = [
        f"{strategy_instance_id} ({status.reason_code})"
        for strategy_instance_id, status in sorted(arming.statuses.items())
        if status.state != "armed" and status.reason_code is not None
    ]
    return ArmingObservation(
        armed_instance_count=arming.armed_instance_count,
        envelope_state=arming.envelope_state,
        detail="" if not not_armed else f" Not armed: {'; '.join(not_armed)}.",
    )


def alpaca_live_verdict(
    *,
    settings: AlpacaSettings | None,
    runtime: ActiveClerkRuntime | None,
    now_ms: int,
    shadow_state: ShadowState | None = None,
    loss_hold: LossHoldState | None = None,
    arming: ArmingObservation | None = None,
) -> AlpacaLiveVerdict:
    """Return the verdict for the current process state.

    ``shadow_state`` is the caller's observation of the durable shadow
    evidence. It is reported only where shadow can exist — an agreed live
    account; paper stays ``not_applicable`` and an unknown verdict keeps the
    empty state it already published.

    ``arming`` is the caller's observation of the durable arming ledger. It is
    read only on an agreed live account; paper and unknown verdicts keep the
    empty count they already published.
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
            envelope_agreement="not_applicable",
            loss_hold="not_applicable",
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
            envelope_agreement="not_applicable",
            loss_hold="not_applicable",
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
            envelope_agreement="not_applicable",
            loss_hold="not_applicable",
            shadow_state="not_applicable",
            final_verdict="paper",
            headline=f"Paper account{f' {account_id}' if account_id else ''} — no real money at risk",
            detail="ALPACA_MODE=paper. Orders reach Alpaca's paper endpoint only.",
            observed_at_ms=now_ms,
        )

    observed_shadow: ShadowState = shadow_state if shadow_state is not None else "none"
    shadow_active = authority == "shadow"
    # The copy names the LIVE account a human recognises. ``shadow:`` is the
    # runtime's custody namespace for the same account, not part of its number,
    # and it reads as a different account in a sentence beginning "LIVE account".
    named_account_id = account_id.removeprefix(SHADOW_ACCOUNT_PREFIX) if account_id is not None else account_id
    # ``not_applicable``, not ``unsealed``, when no envelope object exists: a
    # live boot the composition refused ``LIVE_ENVELOPE_MISSING`` has no
    # envelope at all, and ``unsealed`` reads as "configured, not yet sealed".
    envelope_agreement: EnvelopeAgreement = (
        runtime.clerk.live_envelope.agreement
        if runtime is not None and runtime.clerk is not None and runtime.clerk.live_envelope is not None
        else "not_applicable"
    )
    observed_loss_hold: LossHoldState = loss_hold if loss_hold is not None else "not_applicable"
    held = observed_loss_hold == "held"
    # ADR 0059 D8 / design R11: ``armed`` is a fact about durable arming
    # records, and ``live-armed`` additionally requires a Clerk that could hold
    # custody at all -- an armed record under a refused authority is a
    # permission nothing can act on, and must not read as the loudest state.
    observed_arming = arming if arming is not None else ArmingObservation.none()
    armed = observed_arming.armed_instance_count
    live_armed = armed >= 1 and authority in SQLITE_FACADE_AUTHORITIES
    instances = f"{armed} instance{'' if armed == 1 else 's'}"
    return AlpacaLiveVerdict(
        configured_mode="live",
        observed_account_id=account_id,
        mode_agreement="agreed",
        clerk_authority=authority,
        clerk_refusal_reason_code=refusal,
        armed_instance_count=armed,
        envelope_state=observed_arming.envelope_state,
        envelope_agreement=envelope_agreement,
        loss_hold=observed_loss_hold,
        shadow_state=observed_shadow,
        final_verdict="live-armed" if live_armed else "live-unarmed",
        headline=(
            f"LIVE account {named_account_id} — {instances} armed, nothing submitted yet"
            if live_armed
            else f"LIVE account {named_account_id} — shadow authority active, no instance armed"
            if shadow_active
            else f"LIVE account {named_account_id} — real money, no instance armed"
        )
        + (" — loss hold" if held else ""),
        detail=(
            "This is a real-money Alpaca account and an operator has armed "
            f"{instances} on it. No path submits a real-money order in this slice: the "
            "shadow authority still synthesizes every fill, and ADR 0059 slice 7 is what "
            "opens submission."
            if live_armed
            else "This is a real-money Alpaca account. Its shadow authority reads it and "
            "synthesizes every fill; nothing is submitted. Arming requires a completed "
            "shadow receipt and the supervised ceremony (ADR 0059)."
            if shadow_active
            else "This is a real-money Alpaca account. No sealed instance is armed, so "
            "every order path refuses. Arming requires a completed shadow receipt "
            "and the supervised ceremony (ADR 0059)."
        )
        + observed_arming.detail
        + (
            " The account is in loss hold: every ENTER is refused until an operator "
            "clears it with POST /api/brokers/alpaca/live-envelope/loss-hold/clear; "
            "exits still run."
            if held
            else ""
        ),
        observed_at_ms=now_ms,
    )
