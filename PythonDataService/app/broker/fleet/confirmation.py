"""The clerk's durable confirmation evidence (audit 2026-09-13, finding 2).

The handover from "local acknowledgement" to "fleet confirmation" spans two
databases, and a process can die on either side of it. This module is the
additive, nonsecret evidence file the clerk keeps on its own volume: the
exact grant it *actually confirmed* — registry, clerk, volume, assignment
generation, effective tuple, binding generation, and the instance and epoch
that confirmed it.

It is evidence of an existing grant, never an independent one: the fleet
registry remains the single authority for account ownership, and a local
checkpoint cannot prove the original writer is offline or transfer anything
(audit 2026-09-13, finding 2). Its two uses are exactly the crash protocol's
two reconciliations:

- after a lost confirmation reply, the restarted clerk re-confirms the same
  identity instead of minting a new assignment;
- with the coordinator unreachable, a recovered binding matching this
  evidence may boot last-effective (FR-066) while every new assignment,
  changed binding and browser command fails closed.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.broker.fleet.errors import FleetControlError

EVIDENCE_FILENAME = "confirmation.json"
EVIDENCE_DIRECTORY = "fleet"
_SCHEMA_VERSION = 1


class ConfirmationEvidenceError(FleetControlError):
    """The confirmation evidence is unreadable or contradicts its volume.

    Internal family: boot fails closed — an unreadable checkpoint cannot
    vouch for any binding, and rewriting it is a host recovery ceremony.
    """

    reason = "confirmation_evidence_invalid"
    status_code = 409


@dataclass(frozen=True, slots=True)
class ConfirmationEvidence:
    """The exact grant one clerk confirmed, as nonsecret evidence."""

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
    if not isinstance(raw, dict) or set(raw) != {
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
    }:
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} does not match schema "
            f"version {_SCHEMA_VERSION}.",
            next_step="Run the build that wrote it, or repeat the enrolment ceremony.",
        )
    if not isinstance(raw["schema_version"], int) or raw["schema_version"] != _SCHEMA_VERSION:
        raise ConfirmationEvidenceError(
            f"The confirmation evidence at {target} is schema_version="
            f"{raw['schema_version']!r}; this build reads {_SCHEMA_VERSION}.",
            next_step="Run the build that provisioned this volume, or re-enrol it.",
        )
    for field in ("clerk_id", "volume_id", "registry_id", "canonical_account_id", "agent_instance_id"):
        if not isinstance(raw[field], str) or not raw[field]:
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid {field}.",
            )
    for field in ("assignment_generation", "binding_generation", "confirmed_at_ms", "routing_epoch"):
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid {field}.",
            )
    for field in ("effective_profile_id", "effective_revision"):
        value = raw[field]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int if field == "effective_revision" else str)):
            raise ConfirmationEvidenceError(
                f"The confirmation evidence at {target} carries an invalid {field}.",
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
    """
    if evidence is None:
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
    "read_confirmation_evidence",
    "write_confirmation_evidence",
]
