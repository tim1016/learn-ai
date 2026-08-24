"""Exact-pairing admission gate and Clerk-proved rollback boundary for the
guarded Alpaca Paper canary path.

Issue #1729 (PRD Slice 3 — ``docs/prds/sealed-signal-program-to-governed-alpaca-bot.md``
Sec 23). Two responsibilities, both additive to machinery that already
exists — this module invents no new proof of its own:

* ``CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS`` gates every real Alpaca Paper
  (``mode="trade"``) admission of a Signal-Program-backed program to one
  exact ``(program_key, account_id)`` pairing. ``app.services.run_admission``
  already composes seal + build
  (``signal_program_admission.prove_running_program_build``, which also
  proves the sealed provider identity is present and unchanged), validation,
  replay/boot-recovery readiness, and Clerk custody for every Start/Resume —
  see ``evaluate_run_admission``'s existing ``PROGRAM_BUILD_UNPROVEN``,
  ``STRATEGY_VALIDATION_*``, runtime, and Clerk custody gates. This module
  adds only the missing exact-pairing check and is composed into that same
  gate sequence, following the established pattern rather than inventing a
  parallel one.

* ``evaluate_canary_rollback`` classifies whether an existing Stop custody
  outcome (``app.services.bot_carryover.prove_stop_outcome``, itself backed
  by the Clerk's ``prove_instance_custody``) is a *safe* boundary to roll a
  canary back at. Resuming after a rollback is not a separate mechanism: it
  re-enters ``BotResumeAdmission`` and is re-gated by the same allowlist,
  seal, build, validation, replay, and custody checks as any other Resume —
  there is no cached or partial admission to replay, and no process is
  hot-swapped (Resume only admits once the prior process is proven
  ``EXITED``; see ``evaluate_run_admission``'s ``RESUME_PROCESS_NOT_TERMINAL``
  gate).

SAFETY (issue #1729): ``CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS`` ships EMPTY.
Operational activation is a two-step, content-addressed human decision stored
in the append-only local ledger below. It is admitted only while that ledger's
hash chain and external head checkpoint are intact, and can be revoked without
editing source. Do not add a source entry, a default, a dev-convenience
shortcut, or an env-var fallback.
"""

from __future__ import annotations

import hmac
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.schemas.canary_admission import (
    CanaryActivationEvidence,
    CanaryActivationPlan,
    CanaryAdmissionCheckpoint,
    CanaryAdmissionEvent,
    CanaryAdmissionLedger,
    CanaryRollbackDecision,
)
from app.schemas.signal_program_seal import semantic_payload_hash
from app.services.bot_carryover import StopCustodyOutcome
from app.services.signal_program_admission import (
    DEFAULT_QUALIFICATION_MANIFEST,
    ProgramBuildQualificationManifest,
    running_artifact_digest,
)
from app.services.strategy_validation_admission import current_strategy_validation_fact
from app.utils.advisory_lock import advisory_file_lock
from app.utils.atomic_file import atomic_write_bytes
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_SERVICE_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CANARY_ADMISSION_LEDGER_PATH = _SERVICE_ROOT / "artifacts/canary_admission/events.json"
_MAX_CONFIRMATION_TTL_MS = 5 * 60_000

# SAFETY: exact by (program_key, account_id) — never a prefix, a program-only
# match, or an account-only match. Ships empty; see module docstring.
CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS: frozenset[tuple[str, str]] = frozenset()


class CanaryActivationRefused(ValueError):
    """The requested operational change failed a safety precondition."""


class CanaryAdmissionLedgerError(ValueError):
    """The durable canary admission history is unreadable or invalid."""


@dataclass(frozen=True)
class _ValidationBinding:
    strategy_key: str
    evidence_override: object | None = None


# The only two Stop outcomes `bot_carryover.prove_stop_outcome` can produce
# that represent a Clerk-proved safe position: no exposure at all, or an
# exposure explicitly approved for carryover. The other two outcomes
# (`STOP_REQUIRES_FLATTEN`, `STOPPED_CUSTODY_UNPROVABLE`) are honest records
# of an unsafe or unprovable boundary — a rollback refuses at those rather
# than proceeding.
_SAFE_ROLLBACK_STOP_OUTCOMES: frozenset[StopCustodyOutcome] = frozenset(
    {"STOPPED_FLAT", "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE"}
)


def canary_gate_applies(*, mode: str, program_build_state: str) -> bool:
    """True only once a real Alpaca Paper trade-mode build is already proven.

    Dry Run uses its own isolated synthetic authority and is never subject to
    the canary allowlist (PRD Sec 15). A program with no registered Signal
    Program (``program_build_state == "NOT_APPLICABLE"``) has no seal to
    compose a canary proof from, so it is out of this gate's scope entirely —
    the legacy event-handler strategies keep working exactly as before.

    Deliberately ``== "PROVEN"`` rather than ``!= "NOT_APPLICABLE"``: an
    unproven build is already refused by the pre-existing
    ``PROGRAM_BUILD_UNPROVEN`` gate regardless of allowlist membership, and
    scoping to the proven case keeps this check from ever pre-empting that
    established gate's own reason code — the allowlist is the deciding
    factor only once every other proof would otherwise pass.
    """
    return mode == "trade" and program_build_state == "PROVEN"


def canary_pairing_admitted(
    *,
    program_key: str,
    account_id: str,
    ledger_path: Path | None = None,
) -> bool:
    """Return exact-pair admission, failing closed on ledger corruption.

    The source set remains as a compatibility seam for existing tests, but
    ships empty. Real local activation is always reconstructed from the
    verified append-only ledger.
    """
    pair = (program_key, account_id)
    if pair in CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS:
        return True
    try:
        return pair in active_canary_pairings(ledger_path=ledger_path)
    except CanaryAdmissionLedgerError as exc:
        logger.error("Canary admission failed closed: %s", exc)
        return False


def active_canary_pairings(*, ledger_path: Path | None = None) -> frozenset[tuple[str, str]]:
    """Derive the currently active exact pairings from verified history."""
    resolved_path = _resolve_ledger_path(ledger_path)
    ledger = _read_ledger(resolved_path)
    return _active_pairs_from_ledger(ledger)


def plan_canary_activation(
    *,
    program_key: str,
    account_id: str,
    actor: str,
    reason: str,
    ledger_path: Path | None = None,
    qualification_manifest_path: Path | None = None,
    confirmation_ttl_ms: int = 120_000,
    clock: Callable[[], int] = now_ms_utc,
) -> CanaryActivationPlan:
    """Build a read-only activation intent bound to current proof bytes.

    Planning never creates or changes the ledger. The returned content hash
    must be supplied back to :func:`apply_canary_activation` before expiry.
    """
    program_key = _required_operator_value("program_key", program_key)
    account_id = _required_operator_value("account_id", account_id)
    actor = _required_operator_value("actor", actor)
    reason = _required_operator_value("reason", reason)
    if not 1 <= confirmation_ttl_ms <= _MAX_CONFIRMATION_TTL_MS:
        raise CanaryActivationRefused(
            f"confirmation TTL must be between 1 and {_MAX_CONFIRMATION_TTL_MS} ms"
        )

    resolved_path = _resolve_ledger_path(ledger_path)
    ledger = _read_ledger(resolved_path)
    if (program_key, account_id) in _active_pairs_from_ledger(ledger):
        raise CanaryActivationRefused("the exact program/account pairing is already active")

    created_at_ms = clock()
    evidence = _prove_activation_evidence(
        program_key=program_key,
        observed_at_ms=created_at_ms,
        qualification_manifest_path=qualification_manifest_path,
    )
    payload = {
        "schema_version": 1,
        "program_key": program_key,
        "account_id": account_id,
        "actor": actor,
        "reason": reason,
        "created_at_ms": created_at_ms,
        "expires_at_ms": created_at_ms + confirmation_ttl_ms,
        "ledger_path": _ledger_identity(resolved_path),
        "expected_ledger_head_hash": _ledger_head_hash(ledger),
        "evidence": evidence.model_dump(mode="json"),
    }
    token = semantic_payload_hash(payload)
    return CanaryActivationPlan(plan_id=token, confirmation_token=token, **payload)


def apply_canary_activation(
    *,
    plan: CanaryActivationPlan,
    confirmation_token: str,
    ledger_path: Path | None = None,
    qualification_manifest_path: Path | None = None,
    clock: Callable[[], int] = now_ms_utc,
) -> CanaryAdmissionEvent:
    """Confirm a fresh plan and atomically append its activation decision."""
    expected_token = _activation_plan_hash(plan)
    if not hmac.compare_digest(plan.confirmation_token, expected_token) or not hmac.compare_digest(
        confirmation_token, expected_token
    ):
        raise CanaryActivationRefused("activation confirmation token does not match the plan")

    resolved_path = _resolve_ledger_path(ledger_path)
    if not hmac.compare_digest(plan.ledger_path, _ledger_identity(resolved_path)):
        raise CanaryActivationRefused(
            "activation plan was reviewed for a different canary admission ledger"
        )

    recorded_at_ms = clock()
    if recorded_at_ms < plan.created_at_ms:
        raise CanaryActivationRefused("activation plan is not valid before its creation time")
    if recorded_at_ms > plan.expires_at_ms:
        raise CanaryActivationRefused("activation plan has expired")

    current_evidence = _prove_activation_evidence(
        program_key=plan.program_key,
        observed_at_ms=recorded_at_ms,
        qualification_manifest_path=qualification_manifest_path,
    )
    if current_evidence != plan.evidence:
        raise CanaryActivationRefused(
            "activation evidence changed after planning; create and review a new plan"
        )

    with advisory_file_lock(resolved_path):
        ledger = _read_ledger(resolved_path)
        if _ledger_head_hash(ledger) != plan.expected_ledger_head_hash:
            raise CanaryActivationRefused(
                "the canary admission ledger changed after planning; create and review a new plan"
            )
        pair = (plan.program_key, plan.account_id)
        if pair in _active_pairs_from_ledger(ledger):
            raise CanaryActivationRefused("the exact program/account pairing is already active")
        event = _new_admission_event(
            ledger=ledger,
            action="activated",
            program_key=plan.program_key,
            account_id=plan.account_id,
            actor=plan.actor,
            reason=plan.reason,
            recorded_at_ms=recorded_at_ms,
            evidence=current_evidence,
        )
        _write_ledger(resolved_path, CanaryAdmissionLedger(events=(*ledger.events, event)))
    return event


def revoke_canary_pairing(
    *,
    program_key: str,
    account_id: str,
    actor: str,
    reason: str,
    ledger_path: Path | None = None,
    clock: Callable[[], int] = now_ms_utc,
) -> CanaryAdmissionEvent:
    """Atomically append a revocation; prior activation evidence is retained."""
    program_key = _required_operator_value("program_key", program_key)
    account_id = _required_operator_value("account_id", account_id)
    actor = _required_operator_value("actor", actor)
    reason = _required_operator_value("reason", reason)
    resolved_path = _resolve_ledger_path(ledger_path)
    with advisory_file_lock(resolved_path):
        ledger = _read_ledger(resolved_path)
        if (program_key, account_id) not in _active_pairs_from_ledger(ledger):
            raise CanaryActivationRefused("the exact program/account pairing is not active")
        event = _new_admission_event(
            ledger=ledger,
            action="revoked",
            program_key=program_key,
            account_id=account_id,
            actor=actor,
            reason=reason,
            recorded_at_ms=clock(),
            evidence=None,
        )
        _write_ledger(resolved_path, CanaryAdmissionLedger(events=(*ledger.events, event)))
    return event


def _prove_activation_evidence(
    *,
    program_key: str,
    observed_at_ms: int,
    qualification_manifest_path: Path | None,
) -> CanaryActivationEvidence:
    """Re-hash accepted validation and golden-build evidence for one program."""
    registration = _STRATEGY_REGISTRY.get(program_key)
    if (
        registration is None
        or registration.signal_program_factory is None
        or registration.signal_program_contract is None
    ):
        raise CanaryActivationRefused(
            "canary activation requires a registered, qualified Signal Program"
        )

    validation = current_strategy_validation_fact(
        _ValidationBinding(strategy_key=program_key),
        observed_at_ms,
    )
    if (
        validation.state != "VERIFIED"
        or validation.evidence_status != "accepted"
        or validation.event_id is None
        or validation.evidence_snapshot_sha256 is None
    ):
        raise CanaryActivationRefused(
            "canary activation requires a current accepted validation proof"
        )

    contract = registration.signal_program_contract
    manifest_path = qualification_manifest_path or DEFAULT_QUALIFICATION_MANIFEST
    try:
        digest = running_artifact_digest(contract)
        manifest = ProgramBuildQualificationManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
    except (OSError, ValidationError, ValueError) as exc:
        raise CanaryActivationRefused(
            f"program qualification evidence is unreadable: {type(exc).__name__}"
        ) from exc
    receipt = next(
        (
            candidate
            for candidate in manifest.receipts
            if candidate.program_key == program_key
            and candidate.program_version == contract.program_version
            and candidate.golden_trace_root == contract.golden_trace_root
            and candidate.artifact_digest == digest
        ),
        None,
    )
    if receipt is None:
        raise CanaryActivationRefused(
            "the running Signal Program bytes have no matching golden qualification receipt"
        )
    return CanaryActivationEvidence(
        validation_event_id=validation.event_id,
        validation_snapshot_sha256=validation.evidence_snapshot_sha256,
        program_version=contract.program_version,
        golden_trace_root=contract.golden_trace_root,
        running_artifact_digest=digest,
        qualification_receipt_hash=receipt.receipt_hash,
        qualification_suite=receipt.qualification_suite,
        qualified_at_ms=receipt.qualified_at_ms,
    )


def _activation_plan_hash(plan: CanaryActivationPlan) -> str:
    return semantic_payload_hash(
        plan.model_dump(mode="json", exclude={"plan_id", "confirmation_token"})
    )


def _new_admission_event(
    *,
    ledger: CanaryAdmissionLedger,
    action: Literal["activated", "revoked"],
    program_key: str,
    account_id: str,
    actor: str,
    reason: str,
    recorded_at_ms: int,
    evidence: CanaryActivationEvidence | None,
) -> CanaryAdmissionEvent:
    payload = {
        "schema_version": 1,
        "sequence": len(ledger.events) + 1,
        "action": action,
        "program_key": program_key,
        "account_id": account_id,
        "actor": actor,
        "reason": reason,
        "recorded_at_ms": recorded_at_ms,
        "evidence": None if evidence is None else evidence.model_dump(mode="json"),
        "previous_event_hash": _ledger_head_hash(ledger),
    }
    return CanaryAdmissionEvent(event_hash=semantic_payload_hash(payload), **payload)


def _resolve_ledger_path(ledger_path: Path | None) -> Path:
    return Path(ledger_path) if ledger_path is not None else DEFAULT_CANARY_ADMISSION_LEDGER_PATH


def _ledger_identity(path: Path) -> str:
    return str(path.resolve(strict=False))


def _read_ledger(path: Path) -> CanaryAdmissionLedger:
    checkpoint_path = _checkpoint_path(path)
    if path.is_symlink():
        raise CanaryAdmissionLedgerError("canary admission ledger cannot be a symbolic link")
    if not path.exists():
        if checkpoint_path.exists() or checkpoint_path.is_symlink():
            raise CanaryAdmissionLedgerError(
                "canary admission ledger is missing while its checkpoint exists"
            )
        return CanaryAdmissionLedger()
    try:
        ledger = CanaryAdmissionLedger.model_validate_json(path.read_text(encoding="utf-8"))
        _validate_ledger_history(ledger)
        _validate_ledger_checkpoint(checkpoint_path, ledger)
        return ledger
    except CanaryAdmissionLedgerError:
        raise
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise CanaryAdmissionLedgerError(
            f"canary admission ledger is invalid: {type(exc).__name__}"
        ) from exc


def _validate_ledger_history(ledger: CanaryAdmissionLedger) -> None:
    active: set[tuple[str, str]] = set()
    expected_previous: str | None = None
    for expected_sequence, event in enumerate(ledger.events, start=1):
        if event.sequence != expected_sequence:
            raise CanaryAdmissionLedgerError("canary admission ledger has an invalid event sequence")
        if event.previous_event_hash != expected_previous:
            raise CanaryAdmissionLedgerError("canary admission ledger has an invalid hash chain")
        pair = (event.program_key, event.account_id)
        if event.action == "activated":
            if pair in active:
                raise CanaryAdmissionLedgerError(
                    "canary admission ledger contains a duplicate active pairing"
                )
            active.add(pair)
        else:
            if pair not in active:
                raise CanaryAdmissionLedgerError(
                    "canary admission ledger revokes an inactive pairing"
                )
            active.remove(pair)
        expected_previous = event.event_hash


def _active_pairs_from_ledger(ledger: CanaryAdmissionLedger) -> frozenset[tuple[str, str]]:
    active: set[tuple[str, str]] = set()
    for event in ledger.events:
        pair = (event.program_key, event.account_id)
        if event.action == "activated":
            active.add(pair)
        else:
            active.remove(pair)
    return frozenset(active)


def _ledger_head_hash(ledger: CanaryAdmissionLedger) -> str | None:
    return None if not ledger.events else ledger.events[-1].event_hash


def _checkpoint_path(ledger_path: Path) -> Path:
    return ledger_path.with_name(f"{ledger_path.name}.checkpoint")


def _validate_ledger_checkpoint(
    checkpoint_path: Path,
    ledger: CanaryAdmissionLedger,
) -> None:
    if checkpoint_path.is_symlink():
        raise CanaryAdmissionLedgerError(
            "canary admission checkpoint cannot be a symbolic link"
        )
    if not checkpoint_path.exists():
        raise CanaryAdmissionLedgerError("canary admission checkpoint is missing")
    try:
        checkpoint = CanaryAdmissionCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
        )
    except (OSError, UnicodeDecodeError, ValidationError, ValueError) as exc:
        raise CanaryAdmissionLedgerError(
            f"canary admission checkpoint is invalid: {type(exc).__name__}"
        ) from exc
    if (
        checkpoint.event_count != len(ledger.events)
        or checkpoint.ledger_head_hash != _ledger_head_hash(ledger)
    ):
        raise CanaryAdmissionLedgerError(
            "canary admission ledger does not match its monotonic checkpoint"
        )


def _write_ledger(path: Path, ledger: CanaryAdmissionLedger) -> None:
    encoded = (
        json.dumps(
            ledger.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(path, encoded)
    checkpoint = CanaryAdmissionCheckpoint(
        event_count=len(ledger.events),
        ledger_head_hash=_ledger_head_hash(ledger),
    )
    checkpoint_bytes = (
        json.dumps(
            checkpoint.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    atomic_write_bytes(_checkpoint_path(path), checkpoint_bytes)


def _required_operator_value(name: str, value: str) -> str:
    if not value or not value.strip():
        raise CanaryActivationRefused(f"{name} must be non-empty")
    if value != value.strip():
        raise CanaryActivationRefused(f"{name} cannot have surrounding whitespace")
    return value


def evaluate_canary_rollback(
    *,
    strategy_instance_id: str,
    stop_outcome: StopCustodyOutcome,
    evaluated_at_ms: int,
) -> CanaryRollbackDecision:
    """Refuse a canary rollback unless the Clerk proves a safe stop boundary.

    ``stop_outcome`` is the exact classification
    ``bot_carryover.prove_stop_outcome`` already derives from a fresh
    ``InstanceCustodyProof``: freeze inactive, reconciliation clean, and no
    working orders or unresolved intents remain, then either no exposure
    (``STOPPED_FLAT``) or an approved carried position
    (``STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE``). An ordinary Stop always
    records that outcome honestly, even when it is ``STOP_REQUIRES_FLATTEN``
    or ``STOPPED_CUSTODY_UNPROVABLE``. Rollback is stricter: it is refused
    outright at either of those two boundaries instead of proceeding on an
    unprovable or policy-violating position.
    """
    allowed = stop_outcome in _SAFE_ROLLBACK_STOP_OUTCOMES
    if allowed:
        reason_code = "CANARY_ROLLBACK_ADMITTED"
        explanation = "The Clerk proves a safe boundary to stop the canary at."
        next_step = None
    elif stop_outcome == "STOP_REQUIRES_FLATTEN":
        reason_code = "CANARY_ROLLBACK_REQUIRES_FLATTEN"
        explanation = (
            "The Clerk proves attributed exposure remains and this instance's "
            "carryover policy does not approve carrying it through rollback."
        )
        next_step = "Flatten the exact Clerk-attributed exposure before rollback."
    else:
        reason_code = "CANARY_ROLLBACK_BOUNDARY_UNPROVABLE"
        explanation = "The Clerk cannot currently prove a safe custody boundary to roll back at."
        next_step = "Reconcile the account through the Clerk, then retry rollback."
    return CanaryRollbackDecision(
        strategy_instance_id=strategy_instance_id,
        allowed=allowed,
        reason_code=reason_code,
        explanation=explanation,
        next_step=next_step,
        stop_outcome=stop_outcome,
        evaluated_at_ms=evaluated_at_ms,
    )


__all__ = [
    "CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
    "DEFAULT_CANARY_ADMISSION_LEDGER_PATH",
    "CanaryActivationRefused",
    "CanaryAdmissionLedgerError",
    "active_canary_pairings",
    "apply_canary_activation",
    "canary_gate_applies",
    "canary_pairing_admitted",
    "evaluate_canary_rollback",
    "plan_canary_activation",
    "revoke_canary_pairing",
]
