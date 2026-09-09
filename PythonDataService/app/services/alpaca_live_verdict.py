"""Compose the Alpaca live verdict from settings and clerk selection (ADR 0059 D8).

Pure: no broker I/O, no clock of its own. The router supplies ``now_ms`` and,
for a live boot, the ``shadow_state`` that ``observe_shadow_state`` read from
the durable shadow evidence — the one function here that touches the disk, and
it only reads.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple

from app.broker.alpaca.clerk.account_authority import live_account_id_for_shadow_account
from app.broker.alpaca.clerk.active_authority import SQLITE_FACADE_AUTHORITIES, ActiveClerkRuntime
from app.broker.alpaca.clerk.live_arming import LIVE_MODE_DISAGREEMENT, LiveArmingInvalid
from app.broker.alpaca.clerk.live_arming_ceremony import account_arming
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.broker.alpaca.clerk.shadow_sessions import ShadowSessionLedger
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import LIVE_ENVELOPE_LOSS_HOLD_REASON_CODE
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.account_authority import CustodyWorld
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

_UNOBSERVED_REASONS = frozenset({"BROKER_ACCOUNT_UNAVAILABLE"})

_WHY_UNKNOWN: dict[str, str] = {
    "disagreed": "the configured mode and the observed account disagree",
    "unobserved": "the account has not been observed yet",
}

# The copy is a table keyed by situation, never arms inside two expressions:
# a new situation is a new row here. Live custody (the real-live authority,
# slice 7) earns its own rows because what an armed instance's ENTER does
# there -- submitted, or held -- is the sentence an operator reads first.
LiveSituation = Literal["live_armed", "live_held", "live_unarmed", "armed", "shadow", "bare"]


class _HoldSuffix(NamedTuple):
    """What a row appends when the account is in loss hold: headline, then detail."""

    headline: str
    detail: str


# Empty, for a row whose own sentences already say the account is held -- and,
# at the call site, for an account that is not held at all.
_NO_HOLD_SUFFIX = _HoldSuffix("", "")
_HOLD_SUFFIX = _HoldSuffix(
    " — loss hold",
    " The account is in loss hold: every ENTER is refused until an operator "
    "clears it with POST /api/brokers/alpaca/live-envelope/loss-hold/clear; "
    "exits still run.",
)


class _LiveCopy(NamedTuple):
    """One situation's whole sentence, hold suffix included.

    The suffix is row data because "does this row already mention the hold?"
    is a fact about the copy, not about the call site: keeping it here is what
    lets the caller append unconditionally instead of naming one row.
    """

    headline: str
    detail: str
    hold: _HoldSuffix


_LIVE_COPY: dict[LiveSituation, _LiveCopy] = {
    "live_armed": _LiveCopy(
        "LIVE account {account} — {instances} armed, real-money submission open",
        "This is a real-money Alpaca account custodied by its live authority, and an operator has "
        "armed {instances} on it. An armed instance's ENTER passes the arming gate and is submitted "
        "to Alpaca when the Clerk's holds and the risk envelope admit it; every other instance's "
        "ENTER is refused.",
        _HOLD_SUFFIX,
    ),
    "live_held": _LiveCopy(
        "LIVE account {account} — {instances} armed, real-money submission held by the loss hold",
        "This is a real-money Alpaca account custodied by its live authority, and an operator has "
        "armed {instances} on it, but the account is in loss hold: every ENTER is refused until an "
        "operator clears it with POST /api/brokers/alpaca/live-envelope/loss-hold/clear; exits and "
        "operator reduce-only actions still run.",
        _NO_HOLD_SUFFIX,
    ),
    "live_unarmed": _LiveCopy(
        "LIVE account {account} — real-money authority installed, no instance armed",
        "This is a real-money Alpaca account custodied by its live authority. No sealed instance "
        "is armed, so every ENTER is refused; EXITs and operator reduce-only actions still run. "
        "Arming is the supervised ceremony (ADR 0059 D3); a shadow rehearsal is optional.",
        _HOLD_SUFFIX,
    ),
    "armed": _LiveCopy(
        "LIVE account {account} — {instances} armed under the shadow authority, nothing submitted",
        "This is a real-money Alpaca account read by its shadow authority, and an operator has "
        "armed {instances} on it. Nothing is submitted under the shadow authority: every fill is "
        "synthesized. The live cutover (graduation) is what installs the authority that submits.",
        _HOLD_SUFFIX,
    ),
    "shadow": _LiveCopy(
        "LIVE account {account} — shadow authority active, no instance armed",
        "This is a real-money Alpaca account. Its shadow authority reads it and synthesizes "
        "every fill; nothing is submitted under the shadow authority. Arming is the supervised "
        "ceremony (ADR 0059 D3); a shadow rehearsal is optional.",
        _HOLD_SUFFIX,
    ),
    "bare": _LiveCopy(
        "LIVE account {account} — real money, no instance armed",
        "This is a real-money Alpaca account. No sealed instance is armed, so every order path "
        "refuses. Arming is the supervised ceremony (ADR 0059 D3); a shadow rehearsal is optional.",
        _HOLD_SUFFIX,
    ),
}


def _situation(
    *, live_custody: bool, live_armed: bool, held: bool, shadow_active: bool
) -> LiveSituation:
    """Which row of :data:`_LIVE_COPY` this account's state names.

    One dispatch over the four facts, so the table's keys and the conditions
    that select them are read together instead of a ternary inside each arm.
    """
    if not live_custody:
        return "armed" if live_armed else "shadow" if shadow_active else "bare"
    if not live_armed:
        return "live_unarmed"
    return "live_held" if held else "live_armed"

# R11 requires each non-armed instance to be named with its reason code, and
# the banner renders that sentence verbatim in a tooltip. One closed map, here,
# gives the code a phrase an operator can read without a lookup table -- the
# Frontend copy map stays untouched because this is backend-authored prose.
_NOT_ARMED_WHY: dict[str, str] = {
    "LIVE_ARMING_LAPSED": "its sessions are spent",
    "LIVE_ARMING_REVOKED": "it was disarmed",
    "LIVE_ARMING_SEAL_CHANGED": "its seal changed",
    "LIVE_ENVELOPE_DISAGREEMENT": "the envelope changed",
    "LIVE_ARMING_FUTURE_DATED": "its record is dated after the clock",
}


def _not_armed(strategy_instance_id: str, reason_code: str) -> str:
    """One named instance, its reason code, and why in a short phrase."""
    why = _NOT_ARMED_WHY.get(reason_code)
    named = reason_code if why is None else f"{reason_code}: {why}"
    return f"{strategy_instance_id} ({named})"


def _clerk_authority(runtime: ActiveClerkRuntime | None) -> ClerkAuthority:
    if runtime is None:
        return "not_installed"
    return runtime.authority_kind


def _mode_agreement(
    *,
    account_id: str | None,
    refusal: str | None,
) -> ModeAgreement:
    if refusal == LIVE_MODE_DISAGREEMENT:
        return "disagreed"
    if account_id is None or refusal in _UNOBSERVED_REASONS:
        return "unobserved"
    return "agreed"


def _mid_session_disagreement(runtime: ActiveClerkRuntime | None) -> bool:
    """Whether the live authority's arming gate stands invalid under the mode-disagreement code.

    The sync's 15 s account read is the one reader that sees the broker's
    mode move after boot (design R2). It holds the gate under
    ``LIVE_MODE_DISAGREEMENT`` until a read agrees again; the verdict reads
    that fault as the disagreement it is, not as an unobserved envelope.
    """
    clerk = runtime.clerk if runtime is not None else None
    gate = None if clerk is None else clerk.live_arming
    return gate is not None and gate.invalid_reason_code == LIVE_MODE_DISAGREEMENT


def observe_shadow_state(runtime: ActiveClerkRuntime | None, artifacts_root: Path) -> ShadowState:
    """What the durable shadow evidence says for the installed shadow authority (read-only).

    Only the receipt store and the session journal are consulted: no database
    is opened and the broker is never contacted. A store that refuses its own
    rows raises rather than answering ``"none"`` — a corrupt proof is not the
    absence of one.
    """
    if runtime is None or runtime.authority_kind != "shadow" or runtime.selected_account_id is None:
        return "none"
    live_account_id = live_account_id_for_shadow_account(runtime.selected_account_id)
    if ShadowReceiptStore(artifacts_root).any_for_account(live_account_id):
        return "complete"
    if ShadowSessionLedger(artifacts_root=artifacts_root, account_id=runtime.selected_account_id).has_rows():
        return "in_progress"
    return "none"


def _is_live_custody(runtime: ActiveClerkRuntime | None) -> bool:
    """Whether this runtime is the real-live authority — the one predicate, written once.

    Three surfaces here ask it (the facade test below, the arming read's
    custody world, and the verdict's own situation), and three spellings of
    one question is three things that can drift apart.
    """
    return runtime is not None and runtime.selected_account_authority_kind == "real_live"


def _live_custody_facade(runtime: ActiveClerkRuntime | None) -> bool:
    """A facade authority that custodies a live account: the shadow authority, or the sqlite one on ``real_live``.

    The ``sqlite`` authority kind is shared by the real-paper and the real-live
    authority (slice 7, Task 4); only the latter reads a live account. Every
    shadow authority reads a live account by construction.
    """
    if runtime is None or runtime.authority_kind not in SQLITE_FACADE_AUTHORITIES:
        return False
    return runtime.authority_kind != "sqlite" or _is_live_custody(runtime)


def observe_loss_hold(runtime: ActiveClerkRuntime | None) -> LossHoldState:
    """Whether the durable loss hold stands on the installed authority (read-only)."""
    if not _live_custody_facade(runtime):
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
    live_state_root: Callable[[], Path],
    *,
    settings: AlpacaSettings,
    now_ms: int,
) -> ArmingObservation:
    """Count this live account's armed instances from durable evidence (read-only).

    Reads on the shadow authority and on the real-live authority (slice 7);
    paper and unavailable runtimes report nothing.

    No database is opened and the broker is never contacted: the arming
    ledger, the runner's sealed bindings and the configured envelope are the
    only inputs. Fails closed -- a ledger that will not verify counts no
    instance and says so in the detail, rather than reporting an account as
    unarmed for a reason nobody can see.

    ``live_state_root`` is a callable, not a path, because resolving the
    runner's root reads legacy ``IbkrSettings`` and can therefore refuse. An
    argument is evaluated before this function can take its paper /
    absent-authority early return, so an eagerly resolved root turned an
    invalid legacy IBKR environment into a 500 on a paper verdict that reads
    no arming evidence at all. Nothing under the root is touched unless the
    shadow branch below is taken.
    """
    if not _live_custody_facade(runtime) or runtime.selected_account_id is None or settings.is_paper:
        return ArmingObservation.none()
    live_account_id = live_account_id_for_shadow_account(runtime.selected_account_id)
    # On the graduated account the rehearsal's ``shadow:``-sealed bindings and
    # their slice-6 arming records are still on disk (design R15). They are
    # foreign to the live authority and refused on every Start, so counting
    # them here would publish "N instances armed, real-money submission open"
    # for instances that can never submit.
    custody_world: CustodyWorld | None = "real_live" if _is_live_custody(runtime) else None
    try:
        # One read of the ledger answers every question below: the count, the
        # envelope state and the not-armed prose all come from the same
        # snapshot, so a concurrent ``apply`` cannot make one verdict describe
        # two.
        arming = account_arming(
            live_account_id=live_account_id,
            artifacts_root=artifacts_root,
            live_state_root=live_state_root(),
            configured_envelope=LiveEnvelopeValues.from_settings(settings),
            custody_world=custody_world,
            now_ms=now_ms,
        )
    except (LiveArmingInvalid, LiveEnvelopeIncomplete) as exc:
        # "cannot be judged", not "cannot be read": the two faults caught here
        # are a ledger that will not verify *and* an environment whose envelope
        # is incomplete, and telling an operator the ledger is at fault when it
        # is the .env sends them to the wrong file. The exception names which.
        logger.error(
            "the arming evidence cannot be judged; the verdict counts no armed instance",
            extra={
                "action": "live_arming_ledger_invalid",
                "account_id": live_account_id,
                "why": str(exc),
            },
        )
        return ArmingObservation(
            armed_instance_count=0,
            envelope_state="configured_unsealed",
            detail=f" The arming evidence cannot be judged ({exc}); no instance is counted as armed.",
        )
    not_armed = [
        _not_armed(strategy_instance_id, status.reason_code)
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

    if _mid_session_disagreement(runtime):
        agreement: ModeAgreement = "disagreed"
        refusal = LIVE_MODE_DISAGREEMENT
    else:
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
    named_account_id = (
        live_account_id_for_shadow_account(account_id) if account_id is not None else account_id
    )
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
    live_armed = armed >= 1 and _live_custody_facade(runtime)
    instances = f"{armed} instance{'' if armed == 1 else 's'}"
    copy = _LIVE_COPY[
        _situation(
            live_custody=_is_live_custody(runtime),
            live_armed=live_armed,
            held=held,
            shadow_active=shadow_active,
        )
    ]
    hold_suffix = copy.hold if held else _NO_HOLD_SUFFIX
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
        headline=copy.headline.format(account=named_account_id, instances=instances)
        + hold_suffix.headline,
        detail=copy.detail.format(instances=instances) + observed_arming.detail + hold_suffix.detail,
        observed_at_ms=now_ms,
    )
