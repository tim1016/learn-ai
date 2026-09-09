"""The supervised arming ceremony: observe, plan, apply, disarm (ADR 0059 D3).

``plan_arming`` is strictly read-only. ``apply_arming`` accepts no force mode:
it verifies the plan's own content hash and the operator's quoted token, checks
the confirmation window, re-observes every input, refuses any drift, and only
then appends the sealed record. ``disarm`` is the closed direction and takes no
plan at all.

Nothing here contacts a broker. The four inputs (design R8) are settings, the
shadow activation proof, the instance's sealed binding, and a current shadow
receipt -- all durable evidence already on disk. Mode agreement against the
broker stays the runtime's job at boot, and (slice 7) at admission.

An arming record grants nothing in this slice: no path submits a real-money
order until slice 7 opens one.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.account_authority import (
    SHADOW_ACCOUNT_PREFIX,
    shadow_account_id_for_live_account,
)
from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    plan_content_token,
    require_confirmation_ttl_ms,
    require_plan_token,
    require_unexpired,
)
from app.broker.alpaca.clerk.live_arming import (
    LIVE_ARMING_INPUTS_CHANGED,
    LIVE_ARMING_INSTANCE_UNSEALED,
    LIVE_ARMING_NOT_ARMED,
    LIVE_ARMING_PLAN_EXPIRED,
    LIVE_ARMING_TOKEN_INVALID,
    LIVE_ARMING_TTL_INVALID,
    LIVE_ENVELOPE_MISSING,
    LIVE_SHADOW_INCOMPLETE,
    ArmingStatus,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    arming_status,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptStore
from app.broker.alpaca.config import AlpacaSettings
from app.services.bot_binding_repository import live_state_binding_repository
from app.utils.timestamps import Clock, now_ms_utc

logger = logging.getLogger(__name__)

_LABEL = "live arming"


@dataclass(frozen=True)
class InstanceSeal:
    """One instance's two hashes: what arming binds to, and what the receipt binds to."""

    seal_hash: str
    configured_signal_hash: str


@dataclass(frozen=True)
class ArmingInputs:
    """Everything one arming ceremony observed, at one instant."""

    live_account_id: str
    strategy_instance_id: str
    seal_hash: str
    configured_signal_hash: str
    shadow_receipt_sha256: str
    envelope: LiveEnvelopeValues
    max_sessions: int


type ArmingObserver = Callable[..., ArmingInputs]


@dataclass(frozen=True)
class LiveArmingPlan:
    """A read-only proposal whose content hash is its own confirmation token."""

    schema_version: int
    plan_id: str
    confirmation_token: str
    created_at_ms: int
    expires_at_ms: int
    live_account_id: str
    strategy_instance_id: str
    seal_hash: str
    configured_signal_hash: str
    shadow_receipt_sha256: str
    envelope_values: dict[str, float | int]
    envelope_sha256: str
    max_sessions: int


def _refused(reason_code: str) -> Callable[[str], LiveArmingRefused]:
    """Adapt this module's coded refusal to the ceremony's message-only seam."""
    return lambda message: LiveArmingRefused(reason_code, message)


def custody_account_ids_for(live_account_id: str) -> frozenset[str]:
    """The account ids a binding sealed on this live account may carry.

    Under the Shadow Account Authority custody is ``shadow:<live_account_id>``,
    so an instance rehearsing on this account seals the shadow id; slice 7's
    ``real_live`` custody will seal the live id itself. Both are the same
    account to an operator, and arming has to admit either.
    """
    return frozenset({live_account_id, shadow_account_id_for_live_account(live_account_id)})


def live_account_id_for(artifacts_root: Path) -> str:
    """The live account the shadow gate was run against -- observed, not supplied.

    Taking the account from the operator would let an arming name an account
    that was never shadowed. The activation fence is the evidence that one was.
    """
    shadow_ids = ShadowActivationStore(artifacts_root).account_ids()
    if not shadow_ids:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"no shadow activation proof under {artifacts_root}; run "
            "scripts.manage_alpaca_shadow activate before arming",
        )
    if len(shadow_ids) > 1:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the artifacts root names more than one shadowed live account "
            f"({', '.join(sorted(shadow_ids))}); arming cannot choose between them",
        )
    return shadow_ids[0].removeprefix(SHADOW_ACCOUNT_PREFIX)


def instance_seal_hashes(*, live_account_id: str, live_state_root: Path) -> dict[str, InstanceSeal]:
    """Every sealed Alpaca instance bound to this live account, by instance id.

    A binding with no v2 program seal is skipped rather than refused: it is a
    legacy record that cannot be armed, and its presence must not stop a sealed
    sibling from arming.
    """
    admissible = custody_account_ids_for(live_account_id)
    seals: dict[str, InstanceSeal] = {}
    for binding in live_state_binding_repository(live_state_root).list_for_broker("alpaca"):
        seal = binding.sealed_program
        if seal is None or binding.sealed_account_id not in admissible:
            continue
        seals[binding.strategy_instance_id] = InstanceSeal(
            seal_hash=seal.bot_configuration_hash,
            configured_signal_hash=seal.configured_signal_hash,
        )
    return seals


def configured_envelope(settings: AlpacaSettings) -> LiveEnvelopeValues:
    """The environment's current envelope, or this ceremony's own refusal.

    Both the mode gate and the completeness gate answer ``LIVE_ENVELOPE_MISSING``
    because they are the same question to an operator: the environment does not
    describe a live account this ceremony could arm. The operator CLI calls this
    rather than restating the rule -- a transport module deciding an admission
    policy is how the two sentences drift apart.
    """
    if settings.mode != "live":
        raise LiveArmingRefused(
            LIVE_ENVELOPE_MISSING,
            f"ALPACA_MODE={settings.mode}; only a live account can be armed (ADR 0059 D3).",
        )
    try:
        return LiveEnvelopeValues.from_settings(settings)
    except LiveEnvelopeIncomplete as exc:
        raise LiveArmingRefused(LIVE_ENVELOPE_MISSING, str(exc)) from exc


def observe_arming_inputs(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
) -> ArmingInputs:
    """Read the four inputs R8 names, refusing by code when any one is absent."""
    envelope = configured_envelope(settings)
    live_account_id = live_account_id_for(artifacts_root)
    seal = instance_seal_hashes(
        live_account_id=live_account_id, live_state_root=live_state_root
    ).get(strategy_instance_id)
    if seal is None:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"{strategy_instance_id} has no sealed alpaca binding on {live_account_id}",
        )
    receipt = ShadowReceiptStore(artifacts_root).current(
        strategy_instance_id,
        configured_signal_hash=seal.configured_signal_hash,
        required_sessions=envelope.shadow_sessions,
    )
    if receipt is None:
        raise LiveArmingRefused(
            LIVE_SHADOW_INCOMPLETE,
            f"{strategy_instance_id} has no current shadow receipt for this seal over "
            f"{envelope.shadow_sessions} session(s)",
        )
    return ArmingInputs(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        seal_hash=seal.seal_hash,
        configured_signal_hash=seal.configured_signal_hash,
        shadow_receipt_sha256=receipt.receipt_sha256,
        envelope=envelope,
        max_sessions=envelope.arming_max_sessions,
    )


def _plan_payload(plan: LiveArmingPlan) -> dict[str, Any]:
    """The plan's content, from which its two ids are derived."""
    payload = asdict(plan)
    del payload["plan_id"], payload["confirmation_token"]
    return payload


def plan_arming(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
    confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS,
    clock: Clock = now_ms_utc,
    observe: ArmingObserver = observe_arming_inputs,
) -> LiveArmingPlan:
    """Read and content-address every input without writing anything."""
    now = clock()
    require_confirmation_ttl_ms(confirmation_ttl_ms, refused=_refused(LIVE_ARMING_TTL_INVALID))
    inputs = observe(
        strategy_instance_id=strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
    )
    draft = LiveArmingPlan(
        schema_version=1,
        plan_id="",
        confirmation_token="",
        created_at_ms=now,
        expires_at_ms=now + confirmation_ttl_ms,
        live_account_id=inputs.live_account_id,
        strategy_instance_id=inputs.strategy_instance_id,
        seal_hash=inputs.seal_hash,
        configured_signal_hash=inputs.configured_signal_hash,
        shadow_receipt_sha256=inputs.shadow_receipt_sha256,
        envelope_values=inputs.envelope.to_mapping(),
        envelope_sha256=inputs.envelope.sha,
        max_sessions=inputs.max_sessions,
    )
    token = plan_content_token(_plan_payload(draft))
    return replace(draft, plan_id=token, confirmation_token=token)


# The facts a plan named and an apply must find unchanged. An allowlist rather
# than a whole-object comparison, so the two ids, the clock stamps and the
# expanded envelope values -- every one of which is derived from these -- cannot
# make a same-input re-observation look like drift.
_DRIFTABLE: tuple[str, ...] = (
    "live_account_id",
    "strategy_instance_id",
    "seal_hash",
    "configured_signal_hash",
    "shadow_receipt_sha256",
    "envelope_sha256",
    "max_sessions",
)


def _require_no_drift(plan: LiveArmingPlan, current: ArmingInputs) -> None:
    observed: dict[str, Any] = {
        "live_account_id": current.live_account_id,
        "strategy_instance_id": current.strategy_instance_id,
        "seal_hash": current.seal_hash,
        "configured_signal_hash": current.configured_signal_hash,
        "shadow_receipt_sha256": current.shadow_receipt_sha256,
        "envelope_sha256": current.envelope.sha,
        "max_sessions": current.max_sessions,
    }
    for name in _DRIFTABLE:
        if observed[name] != getattr(plan, name):
            raise LiveArmingRefused(
                LIVE_ARMING_INPUTS_CHANGED, f"{name} changed after arming planning"
            )


def apply_arming(
    *,
    plan: LiveArmingPlan,
    confirmation_token: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
    clock: Clock = now_ms_utc,
    observe: ArmingObserver = observe_arming_inputs,
) -> LiveArmingRecord:
    """Recheck the plan, re-observe every input, then append the sealed record."""
    now = clock()
    if plan.schema_version != 1:
        raise LiveArmingRefused(LIVE_ARMING_TOKEN_INVALID, f"{_LABEL} plan content hash does not verify")
    require_plan_token(
        _plan_payload(plan),
        plan_id=plan.plan_id,
        confirmation_token=plan.confirmation_token,
        supplied_token=confirmation_token,
        refused=_refused(LIVE_ARMING_TOKEN_INVALID),
        label=_LABEL,
    )
    require_unexpired(
        now_ms=now,
        expires_at_ms=plan.expires_at_ms,
        refused=_refused(LIVE_ARMING_PLAN_EXPIRED),
        label=_LABEL,
    )
    current = observe(
        strategy_instance_id=plan.strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        settings=settings,
    )
    _require_no_drift(plan, current)
    record = LiveArmingRecord.create(
        live_account_id=current.live_account_id,
        strategy_instance_id=current.strategy_instance_id,
        seal_hash=current.seal_hash,
        configured_signal_hash=current.configured_signal_hash,
        shadow_receipt_sha256=current.shadow_receipt_sha256,
        envelope=current.envelope,
        armed_at_ms=now,
        max_sessions=current.max_sessions,
    )
    LiveArmingLedger(artifacts_root, live_account_id=record.live_account_id).append(record)
    logger.warning(
        "live instance armed",
        extra={
            "action": "live_arming_applied",
            "account_id": record.live_account_id,
            "strategy_instance_id": record.strategy_instance_id,
            "max_sessions": record.max_sessions,
            "record_sha256": record.record_sha256,
        },
    )
    return record


def disarm(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    clock: Clock = now_ms_utc,
) -> LiveDisarmRecord:
    """Revoke one instance's arming: one append, no plan, no confirmation (R4).

    Disarming is the closed direction, so it reads no settings, no binding and
    no receipt: an operator must be able to revoke an arming whose evidence has
    already gone. It needs only the account the shadow gate ran against and the
    record it revokes.
    """
    live_account_id = live_account_id_for(artifacts_root)
    ledger = LiveArmingLedger(artifacts_root, live_account_id=live_account_id)
    latest = ledger.latest(strategy_instance_id)
    if not isinstance(latest, LiveArmingRecord):
        raise LiveArmingRefused(
            LIVE_ARMING_NOT_ARMED,
            f"{strategy_instance_id} has no arming record to revoke on {live_account_id}",
        )
    record = LiveDisarmRecord.create(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        revokes_record_sha256=latest.record_sha256,
        disarmed_at_ms=clock(),
    )
    ledger.append(record)
    logger.warning(
        "live instance disarmed",
        extra={
            "action": "live_arming_revoked",
            "account_id": live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "revokes_record_sha256": latest.record_sha256,
        },
    )
    return record


def account_arming_statuses(
    *,
    live_account_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    configured_envelope: LiveEnvelopeValues,
    now_ms: int,
    strategy_instance_ids: Sequence[str] | None = None,
) -> dict[str, ArmingStatus]:
    """Every named instance's arming status, reading the ledger and bindings once.

    ``strategy_instance_ids=None`` means "every instance with a row in the
    ledger" -- the set an operator's ``status`` and the live verdict both want.
    An instance whose sealed binding has gone gets ``seal_hash=None``, which
    ``arming_status`` reads as a change from what was armed.
    """
    ledger = LiveArmingLedger(artifacts_root, live_account_id=live_account_id)
    records = ledger.records()
    seals = instance_seal_hashes(live_account_id=live_account_id, live_state_root=live_state_root)
    wanted = ledger.instance_ids() if strategy_instance_ids is None else tuple(strategy_instance_ids)
    statuses: dict[str, ArmingStatus] = {}
    for sid in wanted:
        seal = seals.get(sid)
        statuses[sid] = arming_status(
            records,
            live_account_id=live_account_id,
            strategy_instance_id=sid,
            seal_hash=None if seal is None else seal.seal_hash,
            configured_envelope=configured_envelope,
            now_ms=now_ms,
        )
    return statuses


__all__ = [
    "ArmingInputs",
    "ArmingObserver",
    "InstanceSeal",
    "LiveArmingPlan",
    "account_arming_statuses",
    "apply_arming",
    "configured_envelope",
    "custody_account_ids_for",
    "disarm",
    "instance_seal_hashes",
    "live_account_id_for",
    "observe_arming_inputs",
    "plan_arming",
]
