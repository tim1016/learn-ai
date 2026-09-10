"""The supervised arming ceremony: observe, plan, apply, disarm (ADR 0059 D3).

``plan_arming`` is strictly read-only. ``apply_arming`` accepts no force mode:
it verifies the plan's own content hash and the operator's quoted token, checks
the confirmation window, re-observes every input, refuses any drift, and only
then appends the sealed record. ``disarm`` is the closed direction and takes no
plan at all.

Nothing here contacts a broker. The four inputs (design R8) are settings, the
shadow activation proof, and the instance's sealed binding; a current shadow
receipt is recorded when one exists -- all durable evidence already on disk.
Mode agreement against the broker stays the runtime's job at boot, and (slice 7)
at admission.

An arming record is a permission, never an authority. On an ungraduated
account it grants nothing at all; on a graduated one (slice 7) it is what
lets the live authority's arming gate admit that instance's ENTER — and only
that instance's, and only while the record still stands.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Literal

from app.broker.alpaca.clerk.account_authority import (
    custody_account_id_for,
    custody_account_ids_for,
    is_shadow_account_id,
    live_account_id_for_shadow_account,
    require_real_account_id,
)
from app.broker.alpaca.clerk.ceremony import (
    DEFAULT_CONFIRMATION_TTL_MS,
    plan_content_token,
    plan_payload,
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
    ArmingStatus,
    LiveArmingRecord,
    LiveArmingRefused,
    LiveDisarmRecord,
    RehearsalPredecessor,
    arming_status,
    instance_ids,
    latest_arming,
)
from app.broker.alpaca.clerk.live_arming_ledger import LiveArmingLedger
from app.broker.alpaca.clerk.live_envelope import LiveEnvelopeIncomplete, LiveEnvelopeValues
from app.broker.alpaca.clerk.shadow_activation import ShadowActivationInvalid, ShadowActivationStore
from app.broker.alpaca.clerk.shadow_receipt import ShadowReceiptInvalid, ShadowReceiptStore
from app.broker.alpaca.clerk.sqlite.activation import ActivationRecordInvalid, ActivationStore
from app.broker.alpaca.clerk.sqlite.database_verification import (
    DatabaseVerificationFailed,
    verify_database,
)
from app.broker.alpaca.clerk.sqlite.repository import DB_FILENAME
from app.broker.alpaca.clerk.sqlite.writes import confined_account_file
from app.broker.alpaca.config import AlpacaSettings
from app.schemas.account_authority import CustodyWorld
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
    shadow_receipt_sha256: str | None
    envelope: LiveEnvelopeValues
    max_sessions: int
    predecessor: RehearsalPredecessor | None = None


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
    shadow_receipt_sha256: str | None
    envelope_values: dict[str, float | int]
    envelope_sha256: str
    max_sessions: int
    predecessor: RehearsalPredecessor | None = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> LiveArmingPlan:
        """Rebuild nested predecessor evidence from a JSON plan payload."""
        raw_predecessor = payload.get("predecessor")
        predecessor = (
            None
            if raw_predecessor is None
            else RehearsalPredecessor(**raw_predecessor)
        )
        return cls(**{**payload, "predecessor": predecessor})


def _refused(reason_code: str) -> Callable[[str], LiveArmingRefused]:
    """Adapt this module's coded refusal to the ceremony's message-only seam."""
    return lambda message: LiveArmingRefused(reason_code, message)


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
    return live_account_id_for_shadow_account(shadow_ids[0])


def live_account_id_for_instance(
    *, strategy_instance_id: str, artifacts_root: Path, live_state_root: Path
) -> str:
    """Resolve one exact sealed instance to the account its ceremony may arm.

    A Shadow-bound binding retains the original activation-fence rule. A
    Live-bound binding instead proves its own account through the cutover
    activation record and its sealed cutover artifacts. This lets a graduated
    account arm a new Live instance without requiring an unrelated Shadow
    activation record, while preserving the refusal for an unsealed, foreign,
    or unverifiable instance.
    """
    binding = live_state_binding_repository(live_state_root).read(strategy_instance_id)
    if binding is None or binding.sealed_program is None or binding.sealed_account_id is None:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"{strategy_instance_id} has no sealed alpaca binding",
        )
    account_id = binding.sealed_account_id
    if is_shadow_account_id(account_id):
        return live_account_id_for(artifacts_root)
    return _verified_live_account_id(
        account_id=account_id,
        artifacts_root=artifacts_root,
        failure_detail=f"{strategy_instance_id} is not sealed to a verified Live account",
    )


def _verified_live_account_id(
    *,
    account_id: str,
    artifacts_root: Path,
    failure_detail: str,
) -> str:
    """Verify one graduated account's activation, artifacts, and database."""
    try:
        if not live_account_activation_is_verified(
            live_account_id=account_id,
            artifacts_root=artifacts_root,
        ):
            raise ActivationRecordInvalid("no live activation record names this account")
    except ActivationRecordInvalid as exc:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"{failure_detail}: {exc}",
        ) from exc
    return account_id


def live_account_activation_is_verified(
    *,
    live_account_id: str,
    artifacts_root: Path,
) -> bool:
    """Whether one account has a fully verified Live authority activation.

    A present activation row is not enough: its cutover artifacts and the
    identity of its confined Clerk database must still verify. All malformed
    evidence leaves through ``ActivationRecordInvalid`` so account resolution
    and operator admission reporting share one failure contract.
    """
    try:
        account_id = require_real_account_id(live_account_id)
        activation_store = ActivationStore(artifacts_root / "accounts" / "alpaca")
        activation = activation_store.latest(account_id)
        if activation is None:
            return False
        activation_store.validate_artifacts(activation, artifacts_root=artifacts_root)
        verify_database(
            confined_account_file(artifacts_root, account_id, DB_FILENAME),
            expected_account_id=account_id,
            expected_generation=activation.authority_generation,
            expected_db_identity=activation.db_identity_token,
        )
    except ActivationRecordInvalid:
        raise
    except (ValueError, DatabaseVerificationFailed) as exc:
        raise ActivationRecordInvalid(f"Live authority cannot be verified: {exc}") from exc
    return True


def live_account_id_for_status(
    *,
    strategy_instance_id: str | None,
    artifacts_root: Path,
    live_state_root: Path,
) -> str:
    """Resolve the exact account whose arming status can be reported.

    A named instance ordinarily uses its sealed binding. If that binding or a
    Shadow binding's activation fence has disappeared after arming, its unique
    account-rooted ledger row is enough for the read-only status command to
    report the recorded state; a never-armed instance still refuses instead of
    inheriting another account.

    List status retains the Shadow activation fence when present. A directly
    graduated account has no such fence, so its durable Live activation is the
    account evidence instead. Multiple candidates remain ambiguous and refuse.
    """
    if strategy_instance_id is not None:
        binding = live_state_binding_repository(live_state_root).read(strategy_instance_id)
        shadow_resolution_failure: LiveArmingRefused | ShadowActivationInvalid | None = None
        if (
            binding is not None
            and binding.sealed_program is not None
            and binding.sealed_account_id is not None
        ):
            try:
                return live_account_id_for_instance(
                    strategy_instance_id=strategy_instance_id,
                    artifacts_root=artifacts_root,
                    live_state_root=live_state_root,
                )
            except LiveArmingRefused as exc:
                if (
                    not is_shadow_account_id(binding.sealed_account_id)
                    or exc.reason_code != LIVE_ARMING_INSTANCE_UNSEALED
                ):
                    raise
                shadow_resolution_failure = exc
            except ShadowActivationInvalid as exc:
                if not is_shadow_account_id(binding.sealed_account_id):
                    raise
                shadow_resolution_failure = exc
        try:
            discovered = LiveArmingLedger.discover(
                artifacts_root,
                strategy_instance_id=strategy_instance_id,
            )
        except LiveArmingRefused:
            raise
        except ValueError as exc:
            raise LiveArmingRefused(
                LIVE_ARMING_INSTANCE_UNSEALED,
                f"the arming ledger cannot be discovered safely: {exc}",
            ) from exc
        if discovered is not None:
            return discovered.live_account_id
        if isinstance(shadow_resolution_failure, LiveArmingRefused):
            raise shadow_resolution_failure
        if shadow_resolution_failure is not None:
            raise LiveArmingRefused(
                LIVE_ARMING_INSTANCE_UNSEALED,
                f"{strategy_instance_id}'s Shadow activation proof cannot be verified: "
                f"{shadow_resolution_failure}",
            ) from shadow_resolution_failure
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"{strategy_instance_id} has no sealed alpaca binding",
        )

    shadow_ids = ShadowActivationStore(artifacts_root).account_ids()
    try:
        live_ids = ActivationStore(artifacts_root / "accounts" / "alpaca").account_ids()
    except ActivationRecordInvalid as exc:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"the Live activation ledger cannot be verified: {exc}",
        ) from exc
    shadow_live_ids = tuple(live_account_id_for_shadow_account(account_id) for account_id in shadow_ids)
    candidates = tuple(dict.fromkeys((*shadow_live_ids, *live_ids)))
    if not candidates:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            f"no Shadow or Live activation proof under {artifacts_root}",
        )
    if len(candidates) > 1:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the Shadow and Live activation evidence names more than one account "
            f"({', '.join(sorted(candidates))}); status cannot choose between them",
        )
    if not live_ids:
        return live_account_id_for(artifacts_root)
    return _verified_live_account_id(
        account_id=candidates[0],
        artifacts_root=artifacts_root,
        failure_detail="the Live activation is not verified",
    )


def instance_seal_hashes(
    *,
    live_account_id: str,
    live_state_root: Path,
    custody_world: CustodyWorld | None = None,
) -> dict[str, InstanceSeal]:
    """Every sealed Alpaca instance bound to this live account, by instance id.

    A binding with no v2 program seal is skipped rather than refused: it is a
    legacy record that cannot be armed, and its presence must not stop a sealed
    sibling from arming.

    ``custody_world`` narrows the admissible custody ids to that world's own
    -- whatever ``custody_account_id_for`` answers for it, so ``real_live``
    means the live id alone. After graduation the rehearsal's ``shadow:``-sealed
    bindings stay on disk (design R15) and their slice-6 arming records stay
    in the same account-rooted ledger, so a reader that counts them would
    report instances the live authority does not custody -- and refuses on
    every Start -- as armed under it. ``None`` (the ceremony, the operator
    CLI) keeps both ids: arming a shadow-sealed instance under a graduated
    account grants nothing and is refused at Start anyway.
    """
    # The world-to-custody-id rule is ``custody_account_id_for``'s, so naming a
    # world here narrows to that world's own id for *any* world rather than
    # re-spelling the ``real_live`` half of the table in a feature module.
    admissible = (
        custody_account_ids_for(live_account_id)
        if custody_world is None
        else frozenset({custody_account_id_for(custody_world, live_account_id)})
    )
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
    predecessor_strategy_instance_id: str | None = None,
) -> ArmingInputs:
    """Read the four inputs R8 names, refusing by code when any one is absent."""
    envelope = configured_envelope(settings)
    live_account_id = live_account_id_for_instance(
        strategy_instance_id=strategy_instance_id,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
    )
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
    # Shadow is a mode, not a requirement (owner decision 2026-09-09): a
    # current receipt for this instance on THIS account is recorded; its
    # absence arms nothing less. A receipt sealed for another account is not
    # this account's rehearsal and is not recorded either.
    shadow_receipt_sha256 = (
        receipt.receipt_sha256 if receipt is not None and receipt.live_account_id == live_account_id else None
    )
    predecessor = _predecessor_for(
        predecessor_strategy_instance_id=predecessor_strategy_instance_id,
        live_account_id=live_account_id,
        target_strategy_instance_id=strategy_instance_id,
        target_seal_hash=seal.seal_hash,
        target_configured_signal_hash=seal.configured_signal_hash,
        artifacts_root=artifacts_root,
        live_state_root=live_state_root,
        required_sessions=envelope.shadow_sessions,
    )
    return ArmingInputs(
        live_account_id=live_account_id,
        strategy_instance_id=strategy_instance_id,
        seal_hash=seal.seal_hash,
        configured_signal_hash=seal.configured_signal_hash,
        shadow_receipt_sha256=shadow_receipt_sha256,
        envelope=envelope,
        max_sessions=envelope.arming_max_sessions,
        predecessor=predecessor,
    )


def _predecessor_for(
    *,
    predecessor_strategy_instance_id: str | None,
    live_account_id: str,
    target_strategy_instance_id: str,
    target_seal_hash: str,
    target_configured_signal_hash: str,
    artifacts_root: Path,
    live_state_root: Path,
    required_sessions: int,
) -> RehearsalPredecessor | None:
    """Verify an explicitly selected Shadow predecessor for a Live successor."""
    if predecessor_strategy_instance_id is None:
        return None
    if predecessor_strategy_instance_id == target_strategy_instance_id:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "a Live instance cannot name itself as its Shadow predecessor",
        )
    bindings = live_state_binding_repository(live_state_root)
    predecessor_binding = bindings.read(predecessor_strategy_instance_id)
    target_binding = bindings.read(target_strategy_instance_id)
    if (
        predecessor_binding is None
        or predecessor_binding.sealed_program is None
        or target_binding is None
        or target_binding.sealed_program is None
        or predecessor_binding.sealed_account_id
        != custody_account_id_for("shadow", live_account_id)
    ):
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the selected Shadow predecessor is not a sealed instance on this Live account",
        )
    predecessor_seal = predecessor_binding.sealed_program
    target_seal = target_binding.sealed_program
    if (
        target_binding.sealed_account_id != live_account_id
        or target_seal.bot_configuration_hash != target_seal_hash
        or target_seal.configured_signal_hash != target_configured_signal_hash
        or predecessor_seal.configured_signal_hash != target_seal.configured_signal_hash
        or predecessor_seal.action_plan != target_seal.action_plan
        or predecessor_seal.quantity != target_seal.quantity
        or predecessor_seal.carryover_policy != target_seal.carryover_policy
        or predecessor_binding.use_rth != target_binding.use_rth
    ):
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the selected Shadow predecessor does not match this sealed Live instance",
        )
    try:
        receipt = ShadowReceiptStore(artifacts_root).current(
            predecessor_strategy_instance_id,
            configured_signal_hash=predecessor_seal.configured_signal_hash,
            required_sessions=required_sessions,
        )
    except ShadowReceiptInvalid as exc:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the selected Shadow predecessor receipt does not verify",
        ) from exc
    if receipt is None or receipt.live_account_id != live_account_id:
        raise LiveArmingRefused(
            LIVE_ARMING_INSTANCE_UNSEALED,
            "the selected Shadow predecessor has no current receipt for this Live account",
        )
    return RehearsalPredecessor(
        strategy_instance_id=predecessor_strategy_instance_id,
        seal_hash=predecessor_seal.bot_configuration_hash,
        receipt_sha256=receipt.receipt_sha256,
    )


def _plan_payload(plan: LiveArmingPlan) -> dict[str, Any]:
    if plan.schema_version not in (1, 2):
        raise LiveArmingRefused(LIVE_ARMING_TOKEN_INVALID, f"{_LABEL} plan content hash does not verify")
    return plan_payload(
        plan,
        schema_version=plan.schema_version,
        refused=_refused(LIVE_ARMING_TOKEN_INVALID),
        label=_LABEL,
    )


def plan_arming(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    settings: AlpacaSettings,
    confirmation_ttl_ms: int = DEFAULT_CONFIRMATION_TTL_MS,
    predecessor_strategy_instance_id: str | None = None,
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
        predecessor_strategy_instance_id=predecessor_strategy_instance_id,
    )
    draft = LiveArmingPlan(
        schema_version=2 if inputs.predecessor is not None else 1,
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
        predecessor=inputs.predecessor,
    )
    token = plan_content_token(_plan_payload(draft))
    return replace(draft, plan_id=token, confirmation_token=token)


def _require_no_drift(plan: LiveArmingPlan, current: ArmingInputs) -> None:
    """Refuse if any fact the plan named has changed since it was written.

    The compared set is built from ``ArmingInputs`` itself rather than a
    hand-kept list, so a field added to the observation cannot be missed here
    at apply time. It is still narrower than a whole-object comparison: the two
    ids, the clock stamps and the expanded envelope values on the plan are all
    *derived* from these facts, and comparing them would read a same-input
    re-observation as drift. The envelope is compared by its sha, which is the
    one name both sides carry.
    """
    observed: dict[str, Any] = {
        name: value for name, value in asdict(current).items() if name != "envelope"
    }
    observed["predecessor"] = current.predecessor
    observed["envelope_sha256"] = current.envelope.sha
    for name, value in observed.items():
        if value != getattr(plan, name):
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
        predecessor_strategy_instance_id=(
            None if plan.predecessor is None else plan.predecessor.strategy_instance_id
        ),
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
        predecessor=current.predecessor,
        originating_plan_id=plan.plan_id if current.predecessor is not None else None,
    )
    record = LiveArmingLedger(artifacts_root, live_account_id=record.live_account_id).append_once_for_plan(record)
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


def _disarm_ledger(*, strategy_instance_id: str, artifacts_root: Path) -> LiveArmingLedger:
    """The ledger holding this instance's arming -- activation proof or not.

    The activation fence is the ordinary answer: it is the evidence that a
    shadow gate ran on this account, and it is what every *opening* step reads.
    But disarming is the closed direction, and that fence can be deleted,
    damaged or made ambiguous after an arming -- during exactly the incident a
    revocation exists for. Blocking the revocation then is the wrong failure,
    and it is avoidable: an arming row already names its own live account, so
    the arming tree itself can be asked.

    The fallback is taken only when the fence cannot name one account. Any
    other refusal propagates.
    """
    try:
        return LiveArmingLedger(artifacts_root, live_account_id=live_account_id_for(artifacts_root))
    except (LiveArmingRefused, ShadowActivationInvalid) as exc:
        if isinstance(exc, LiveArmingRefused) and exc.reason_code != LIVE_ARMING_INSTANCE_UNSEALED:
            raise
        logger.warning(
            "the shadow activation proof cannot name one live account; disarm is reading "
            "the arming ledgers themselves",
            extra={
                "action": "live_arming_disarm_discovery",
                "strategy_instance_id": strategy_instance_id,
                "why": str(exc),
            },
        )
    discovered = LiveArmingLedger.discover(artifacts_root, strategy_instance_id=strategy_instance_id)
    if discovered is None:
        raise LiveArmingRefused(
            LIVE_ARMING_NOT_ARMED,
            f"no arming ledger under {artifacts_root} names {strategy_instance_id}",
        )
    return discovered


def disarm(
    *,
    strategy_instance_id: str,
    artifacts_root: Path,
    clock: Clock = now_ms_utc,
) -> LiveDisarmRecord:
    """Revoke one instance's arming: one append, no plan, no confirmation (R4).

    Disarming is the closed direction, so it reads no settings, no binding and
    no receipt -- and, when the activation fence cannot answer, no activation
    proof either: an operator must be able to revoke an arming whose evidence
    has already gone. It needs only the ledger holding the record it revokes.
    """
    ledger = _disarm_ledger(strategy_instance_id=strategy_instance_id, artifacts_root=artifacts_root)
    # The ledger owns the read-and-revoke transaction: what is being revoked
    # and the row that revokes it are decided under one lock acquisition, so a
    # concurrent re-arm cannot make ``revokes_record_sha256`` name a stale
    # record.
    record = ledger.revoke_latest(strategy_instance_id, disarmed_at_ms=clock())
    logger.warning(
        "live instance disarmed",
        extra={
            "action": "live_arming_revoked",
            "account_id": ledger.live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "revokes_record_sha256": record.revokes_record_sha256,
        },
    )
    return record


@dataclass(frozen=True)
class AccountArming:
    """One live account's arming evidence, from a single read of its ledger.

    Every fact R11 publishes about an account -- the per-instance statuses, the
    record that sealed its envelope, the armed count and the envelope state --
    is derived here from the same ``records()`` tuple. Two readers of one
    verdict can therefore never describe two different snapshots of a file an
    operator may be appending to while the verdict is being composed.
    """

    statuses: dict[str, ArmingStatus]
    sealed: LiveArmingRecord | None

    @property
    def armed_instance_count(self) -> int:
        """How many of the named instances are armed at the judged instant (R11)."""
        return sum(1 for status in self.statuses.values() if status.state == "armed")

    @property
    def envelope_state(self) -> Literal["configured_unsealed", "sealed"]:
        """Whether any arming record has sealed this account's envelope (R11)."""
        return "configured_unsealed" if self.sealed is None else "sealed"


def account_arming(
    *,
    live_account_id: str,
    artifacts_root: Path,
    live_state_root: Path,
    configured_envelope: LiveEnvelopeValues,
    now_ms: int,
    strategy_instance_ids: Sequence[str] | None = None,
    custody_world: CustodyWorld | None = None,
) -> AccountArming:
    """This account's arming evidence, reading the ledger file exactly once.

    ``strategy_instance_ids=None`` means "every instance with a row in the
    ledger" -- the set an operator's ``status`` and the live verdict both want.
    An instance whose sealed binding has gone gets ``seal_hash=None``, which
    ``arming_status`` reads as a change from what was armed.

    The runner's bindings are read only when the ledger actually knows one of
    the wanted instances: nothing under ``live_state_root`` can change the
    answer for an instance that was never armed, so a ``status`` on a
    never-armed account touches no bindings root at all -- which on a fresh
    installation may not exist yet.

    ``custody_world`` passes straight through to :func:`instance_seal_hashes`:
    a caller that knows it reads the ``real_live`` world counts only
    live-sealed instances.
    """
    records = LiveArmingLedger(artifacts_root, live_account_id=live_account_id).records()
    known = instance_ids(records)
    wanted = known if strategy_instance_ids is None else tuple(strategy_instance_ids)
    seals = (
        instance_seal_hashes(
            live_account_id=live_account_id,
            live_state_root=live_state_root,
            custody_world=custody_world,
        )
        if any(sid in known for sid in wanted)
        else {}
    )
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
    return AccountArming(statuses=statuses, sealed=latest_arming(records))


__all__ = [
    "AccountArming",
    "ArmingInputs",
    "ArmingObserver",
    "InstanceSeal",
    "LiveArmingPlan",
    "account_arming",
    "apply_arming",
    "configured_envelope",
    "disarm",
    "instance_seal_hashes",
    "live_account_activation_is_verified",
    "live_account_id_for",
    "live_account_id_for_instance",
    "live_account_id_for_status",
    "observe_arming_inputs",
    "plan_arming",
]
