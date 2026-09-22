"""The clerk's durable confirmation evidence (audit 2026-09-13, finding 2).

The handover from "local acknowledgement" to "fleet confirmation" spans two
databases, and a process can die on either side of it. This module is the
additive, nonsecret evidence file the clerk keeps on its own volume: the
exact grant it *actually confirmed* — registry, clerk, volume, assignment
generation, effective tuple, binding generation, and the instance and epoch
that confirmed it — plus, since schema v2 (#2155), the last lifecycle the
lane knew itself to hold. A lane that learns it was drained re-authors the
file with ``lifecycle_state: draining``, and evidence in any non-provisioned
lifecycle vouches for nothing: the FR-066 offline boot that would otherwise
resurrect a drained binding is refused by the lane's own volume.

It is evidence of an existing grant, never an independent one: the fleet
registry remains the single authority for account ownership, and a local
checkpoint cannot prove the original writer is offline or transfer anything
(audit 2026-09-13, finding 2). Its two uses are exactly the crash protocol's
two reconciliations:

- after a lost confirmation reply, the restarted clerk re-confirms the same
  identity instead of minting a new assignment;
- with the coordinator unreachable, a recovered binding matching this
  evidence may boot last-effective (FR-066) while every new assignment,
  changed binding and browser command fails closed — unless the evidence
  itself says the lane was drained, in which case it boots nothing.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from pathlib import Path

from app.broker.fleet.errors import FleetControlError
from app.broker.fleet.records import StoredLifecycleState
from app.utils.session_anchors import MAX_TIMESTAMP_MS

EVIDENCE_FILENAME = "confirmation.json"
EVIDENCE_DIRECTORY = "fleet"
_SCHEMA_VERSION = 2

_V1_FIELDS = frozenset(
    {
        "schema_version",
        "clerk_id",
        "volume_id",
        "registry_id",
        "assignment_generation",
        "canonical_account_id",
        "binding_generation",
        "effective_profile_id",
        "effective_revision",
        "confirmed_at_ms",
        "agent_instance_id",
        "routing_epoch",
    }
)
_V2_FIELDS = _V1_FIELDS | {"lifecycle_state"}
_EVIDENCE_LIFECYCLES = frozenset(state.value for state in StoredLifecycleState)


class ConfirmationEvidenceError(FleetControlError):
    """The confirmation evidence is unreadable or contradicts its volume.

    Internal family: boot fails closed — an unreadable checkpoint cannot
    vouch for any binding, and rewriting it is a host recovery ceremony.
    """

    reason = "confirmation_evidence_invalid"
    status_code = 409


@dataclass(frozen=True, slots=True)
class ConfirmationEvidence:
    """The exact grant one clerk confirmed, as nonsecret evidence.

    ``lifecycle_state`` is the last lifecycle the lane knew itself to hold:
    ``provisioned`` at every confirmation (a draining or retired clerk
    confirms nothing), re-authored to ``draining`` the moment the lane learns
    its drain (#2155). Evidence in any other lifecycle is a tombstone, not a
    voucher — it stays on the volume as the auditable reason the lane is
    down, and ``evidence_vouches_for`` refuses it.
    """

    clerk_id: str
    volume_id: str
    registry_id: str
    assignment_generation: int
    canonical_account_id: str
    binding_generation: int
    effective_profile_id: str | None
    effective_revision: int | None
    confirmed_at_ms: int
    agent_instance_id: str
    routing_epoch: int
    lifecycle_state: str = StoredLifecycleState.PROVISIONED.value

    def tuple_key(self) -> tuple[str | None, int | None, str]:
        """The effective tuple this evidence vouches for."""
        return (self.effective_profile_id, self.effective_revision, self.canonical_account_id)


def confirmation_evidence_path(clerk_root: Path) -> Path:
    """Where the evidence lives: inside the clerk volume, never beside it."""
    return clerk_root / EVIDENCE_DIRECTORY / EVIDENCE_FILENAME


def read_confirmation_evidence(clerk_root: Path) -> ConfirmationEvidence | None:
    """Parse the evidence if present; ``None`` only means "none written yet"."""
    target = confirmation_evidence_path(clerk_root)
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} is unreadable: {exc}",
            next_step="Restore the evidence from the volume's backup or repeat the "
            "fleet enrolment ceremony; an unreadable checkpoint vouches for "
            "nothing.",
        ) from exc
    if not isinstance(raw, dict) or set(raw) not in (_V1_FIELDS, _V2_FIELDS):
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} does not match schema "
            f"version {_SCHEMA_VERSION}.",
            next_step="Run the build that wrote it, or repeat the enrolment ceremony.",
        )
    version = raw["schema_version"]
    if not isinstance(version, int) or version not in (1, _SCHEMA_VERSION):
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} is schema_version="
            f"{version!r}; this build reads 1 and {_SCHEMA_VERSION}.",
            next_step="Run the build that provisioned this volume, or re-enrol it.",
        )
    if version == 1 and set(raw) != _V1_FIELDS:
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} claims schema version 1 "
            "while carrying schema-2 fields.",
            next_step="Run the build that wrote it, or repeat the enrolment ceremony.",
        )
    # Schema v1 predates the lifecycle field; every v1 file was written at a
    # confirmation, and only a provisioned clerk confirms, so v1 reads back
    # as provisioned — the legacy volume keeps its FR-066 story untouched
    # (#2155's residual window is a v1 file that was never re-authored, not
    # a v1 file this reader refuses).
    lifecycle_state = StoredLifecycleState.PROVISIONED.value
    if version == _SCHEMA_VERSION:
        lifecycle_state = raw["lifecycle_state"]
        if lifecycle_state not in _EVIDENCE_LIFECYCLES:
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid "
                f"lifecycle_state: {lifecycle_state!r}.",
            )
    for field in ("clerk_id", "volume_id", "registry_id", "canonical_account_id", "agent_instance_id"):
        if not isinstance(raw[field], str) or not raw[field]:
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid {field}.",
            )
    for field in ("assignment_generation", "binding_generation", "routing_epoch"):
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid {field}.",
            )
    confirmed_at_ms = raw["confirmed_at_ms"]
    if (
        isinstance(confirmed_at_ms, bool)
        or not isinstance(confirmed_at_ms, int)
        or not 0 <= confirmed_at_ms <= MAX_TIMESTAMP_MS
    ):
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} carries an invalid confirmed_at_ms.",
        )
    for field in ("effective_profile_id", "effective_revision"):
        value = raw[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int if field == "effective_revision" else str)):
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid {field}.",
            )
    if (raw["effective_profile_id"] is None) != (raw["effective_revision"] is None):
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} carries an incomplete effective tuple.",
        )
    return ConfirmationEvidence(
        clerk_id=raw["clerk_id"],
        volume_id=raw["volume_id"],
        registry_id=raw["registry_id"],
        assignment_generation=raw["assignment_generation"],
        canonical_account_id=raw["canonical_account_id"],
        binding_generation=raw["binding_generation"],
        effective_profile_id=raw["effective_profile_id"],
        effective_revision=raw["effective_revision"],
        confirmed_at_ms=raw["confirmed_at_ms"],
        agent_instance_id=raw["agent_instance_id"],
        routing_epoch=raw["routing_epoch"],
        lifecycle_state=lifecycle_state,
    )


def write_confirmation_evidence(clerk_root: Path, evidence: ConfirmationEvidence) -> None:
    """Persist the evidence atomically; identity fields never migrate.

    The clerk identity and volume identity are immutable once written — a
    later file naming a different clerk or volume is a mis-mounted root, not
    an update. The grant fields (generation, tuple, session) move forward
    with each re-confirmation.
    """
    target = confirmation_evidence_path(clerk_root)
    existing = read_confirmation_evidence(clerk_root)
    if existing is not None and (
        existing.clerk_id != evidence.clerk_id or existing.volume_id != evidence.volume_id
    ):
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} names clerk "
            f"{existing.clerk_id}/volume {existing.volume_id}; refusing to "
            f"overwrite it with {evidence.clerk_id}/{evidence.volume_id}.",
            next_step="Mount this clerk's own volume; an evidence file never "
            "migrates onto another lane.",
        )
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "clerk_id": evidence.clerk_id,
        "volume_id": evidence.volume_id,
        "registry_id": evidence.registry_id,
        "assignment_generation": evidence.assignment_generation,
        "canonical_account_id": evidence.canonical_account_id,
        "binding_generation": evidence.binding_generation,
        "effective_profile_id": evidence.effective_profile_id,
        "effective_revision": evidence.effective_revision,
        "confirmed_at_ms": evidence.confirmed_at_ms,
        "agent_instance_id": evidence.agent_instance_id,
        "routing_epoch": evidence.routing_epoch,
        "lifecycle_state": evidence.lifecycle_state,
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.tmp-{os.getpid()}")
    # Durability to the same bar as the registry's FULL-sync SQLite: the
    # evidence is the FR-066 offline story, and a rename that outlived its
    # data on power loss would vouch for a grant that no longer exists.
    with open(temporary, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def mark_confirmation_evidence_draining(clerk_root: Path) -> bool:
    """Re-author the volume's evidence into its drained tombstone (#2155).

    Called the moment a lane learns it is draining — from a heartbeat's
    lifecycle answer or from the coordinator's typed registration refusal —
    and before anything else the lane does with that knowledge. The grant
    fields are preserved verbatim (the tombstone is also the audit record of
    what was drained); only the lifecycle flips, under the writer's own
    identity and durability rules. Idempotent: evidence already drained, and
    a volume that never confirmed anything, are both fine to call again.
    Returns whether this call left the file in the draining state.
    """
    existing = read_confirmation_evidence(clerk_root)
    if existing is None:
        return False
    if existing.lifecycle_state == StoredLifecycleState.DRAINING.value:
        return True
    write_confirmation_evidence(
        clerk_root, replace(existing, lifecycle_state=StoredLifecycleState.DRAINING.value)
    )
    return True


def evidence_vouches_for(
    evidence: ConfirmationEvidence | None,
    *,
    canonical_account_id: str,
    effective_profile_id: str | None,
    effective_revision: int | None,
    binding_generation: int | None = None,
) -> bool:
    """Whether a recovered binding is the exact grant the evidence confirms.

    This is the FR-066 test: only the already-confirmed exact last-effective
    tuple may boot with the coordinator unavailable. Anything else — a
    changed account, profile or revision — waits for the coordinator. When
    the caller pins the current binding generation, it must equal the
    evidence's: a tuple that changed away and back carries a newer
    generation, and that grant has not been confirmed.

    Drained evidence vouches for nothing regardless of the tuple (#2155):
    the lane marked its own file when it learned the drain, and that mark —
    not the coordinator's availability — is what a restart answers to.
    """
    if evidence is None:
        return False
    if evidence.lifecycle_state != StoredLifecycleState.PROVISIONED.value:
        return False
    if evidence.tuple_key() != (
        effective_profile_id,
        effective_revision,
        canonical_account_id,
    ):
        return False
    return binding_generation is None or evidence.binding_generation == binding_generation


__all__ = [
    "ConfirmationEvidence",
    "ConfirmationEvidenceError",
    "confirmation_evidence_path",
    "evidence_vouches_for",
    "mark_confirmation_evidence_draining",
    "read_confirmation_evidence",
    "write_confirmation_evidence",
]
