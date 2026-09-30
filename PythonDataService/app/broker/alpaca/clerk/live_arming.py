"""The sealed historical arming record and the reason codes it was refused under (ADR 0059 D3).

Read-only history. No production writer mints a grant, and nothing judges
permission against one any more: an ENTER is admitted only for a budgeted
deployment (#2553), and the arming gate, its per-tick refresh, the envelope
seal and the Start-time arming fact are retired (#2629). What stays is what a
reader of the past still needs:

* the record shapes, verified exactly as they were sealed, so an old
  ``live_arming.jsonl`` still loads (``live_arming_ledger.py``) -- the one-time
  exit-terms upgrade prices a legacy bot from its own newest arming
  (``sqlite/runtime.py::SqliteAlpacaClerkFacade.upgrade_legacy_exit_terms``),
  and ``program_leg._sealed_allowances`` still reads the newest arming's
  entry allowance;
* the reason codes, so ``blocked`` receipts recorded under them still read as
  transient refusals (``sqlite/uncertainty.py``) rather than as drift.

Nothing here touches a broker, a database, a file or a clock.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_DISAGREEMENT,
    LiveEnvelopeValues,
    envelope_domain_violation,
)
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256, verify_sealed_record
from app.utils.session_anchors import MAX_TIMESTAMP_MS

# The codes the retired ENTER-time arming check refused under (slice 7, ADR
# 0059 D11, retired by #2553). No ENTER is judged by arming any more, but
# ``blocked`` receipts already recorded under these codes stay readable:
# ``uncertainty.py`` keeps them transient, so the replay proof still
# recognises them rather than calling them drift. The ceremony's own codes
# were CLI refusals and log actions, never recorded, and went with it (#2629).
LIVE_ARMING_REQUIRED = "LIVE_ARMING_REQUIRED"
LIVE_ARMING_UNOBSERVED = "LIVE_ARMING_UNOBSERVED"
LIVE_ARMING_LEDGER_INVALID = "LIVE_ARMING_LEDGER_INVALID"
LIVE_ARMING_LAPSED = "LIVE_ARMING_LAPSED"
LIVE_ARMING_REVOKED = "LIVE_ARMING_REVOKED"
LIVE_ARMING_SEAL_CHANGED = "LIVE_ARMING_SEAL_CHANGED"
LIVE_ARMING_FUTURE_DATED = "LIVE_ARMING_FUTURE_DATED"
# The adapter's own refusal (`BrokerAccountModeDisagreement.reason_code`),
# restated only because the retired arming gate refused ENTERs under it; a
# test pins the two strings equal.
LIVE_MODE_DISAGREEMENT = "LIVE_MODE_DISAGREEMENT"

ARMING_ADMISSION_REASON_CODES: frozenset[str] = frozenset(
    {
        LIVE_ARMING_REQUIRED,
        LIVE_ARMING_UNOBSERVED,
        LIVE_ARMING_LEDGER_INVALID,
        LIVE_ARMING_LAPSED,
        LIVE_ARMING_REVOKED,
        LIVE_ARMING_SEAL_CHANGED,
        LIVE_ARMING_FUTURE_DATED,
        LIVE_ENVELOPE_DISAGREEMENT,
        LIVE_MODE_DISAGREEMENT,
    }
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL = "live arming"


class LiveArmingInvalid(ValueError):
    """An arming or disarm row cannot be trusted."""


@dataclass(frozen=True)
class RehearsalPredecessor:
    """The immutable Shadow evidence reviewed for a new Live instance."""

    strategy_instance_id: str
    seal_hash: str
    receipt_sha256: str


@dataclass(frozen=True)
class LiveArmingRecord:
    """One instance armed on one live account, under one sealed envelope (R1);
    ``shadow_receipt_sha256`` is null when the instance holds no receipt."""

    kind: Literal["armed"]
    schema_version: int
    live_account_id: str
    strategy_instance_id: str
    seal_hash: str
    configured_signal_hash: str
    shadow_receipt_sha256: str | None
    envelope_values: dict[str, float | int]
    envelope_sha256: str
    armed_at_ms: int
    max_sessions: int
    record_sha256: str
    predecessor: RehearsalPredecessor | None = None
    originating_plan_id: str | None = None
    exit_terms: dict[str, Any] | None = None

    @classmethod
    def create(
        cls,
        *,
        live_account_id: str,
        strategy_instance_id: str,
        seal_hash: str,
        configured_signal_hash: str,
        shadow_receipt_sha256: str | None,
        envelope: LiveEnvelopeValues,
        armed_at_ms: int,
        max_sessions: int,
        predecessor: RehearsalPredecessor | None = None,
        originating_plan_id: str | None = None,
        exit_terms: dict[str, Any] | None = None,
    ) -> LiveArmingRecord:
        unsigned: dict[str, Any] = {
            "kind": "armed",
            "schema_version": 3 if exit_terms is not None else (2 if predecessor is not None else 1),
            "live_account_id": live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "seal_hash": seal_hash,
            "configured_signal_hash": configured_signal_hash,
            "shadow_receipt_sha256": shadow_receipt_sha256,
            "envelope_values": envelope.to_mapping(),
            "envelope_sha256": envelope.sha,
            "armed_at_ms": armed_at_ms,
            "max_sessions": max_sessions,
        }
        if predecessor is not None:
            unsigned["predecessor"] = asdict(predecessor)
            unsigned["originating_plan_id"] = originating_plan_id
        if exit_terms is not None:
            unsigned["exit_terms"] = exit_terms
            unsigned["predecessor"] = None if predecessor is None else asdict(predecessor)
            unsigned["originating_plan_id"] = originating_plan_id
        record = cls(
            **{
                **unsigned,
                "predecessor": predecessor,
                "originating_plan_id": originating_plan_id,
            },
            record_sha256=canonical_sha256(unsigned),
        )
        _validate_armed(record)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LiveArmingRecord:
        try:
            raw_predecessor = payload.get("predecessor")
            predecessor = (
                None
                if raw_predecessor is None
                else RehearsalPredecessor(**raw_predecessor)
            )
            record = cls(**{**payload, "predecessor": predecessor})
            _validate_armed(record)
            unsigned = asdict(record)
            del unsigned["record_sha256"]
            # Version 1 rows predate predecessor evidence. They remain valid
            # exactly as sealed instead of being rewritten during a read.
            unsigned = arming_version_payload(unsigned, record.schema_version)
            if record.record_sha256 != canonical_sha256(unsigned):
                raise LiveArmingInvalid("live arming record digest does not verify")
        except LiveArmingInvalid:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise LiveArmingInvalid("live arming record has an invalid shape") from exc
        return record

    @property
    def envelope(self) -> LiveEnvelopeValues:
        """The envelope this record sealed, rebuilt from its own values."""
        return LiveEnvelopeValues(**self.envelope_values)


@dataclass(frozen=True)
class LiveDisarmRecord:
    """One operator revocation, the closed direction of the ceremony (R4)."""

    kind: Literal["disarmed"]
    schema_version: int
    live_account_id: str
    strategy_instance_id: str
    revokes_record_sha256: str
    disarmed_at_ms: int
    record_sha256: str

    @classmethod
    def create(
        cls,
        *,
        live_account_id: str,
        strategy_instance_id: str,
        revokes_record_sha256: str,
        disarmed_at_ms: int,
    ) -> LiveDisarmRecord:
        unsigned: dict[str, Any] = {
            "kind": "disarmed",
            "schema_version": 1,
            "live_account_id": live_account_id,
            "strategy_instance_id": strategy_instance_id,
            "revokes_record_sha256": revokes_record_sha256,
            "disarmed_at_ms": disarmed_at_ms,
        }
        record = cls(**unsigned, record_sha256=canonical_sha256(unsigned))
        _validate_disarmed(record)
        return record

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> LiveDisarmRecord:
        return _verified(cls, payload, _validate_disarmed)


LedgerRecord = LiveArmingRecord | LiveDisarmRecord


def _verified[RecordT: LedgerRecord](
    cls: type[RecordT], payload: Mapping[str, Any], validate: Callable[[RecordT], None]
) -> RecordT:
    """One row of this ledger, on the shared sealed-record reading discipline."""
    return verify_sealed_record(
        cls,
        payload,
        validate=validate,
        digest_field="record_sha256",
        invalid=LiveArmingInvalid,
        label=_LABEL,
    )


def _require_real(account_id: str) -> None:
    try:
        require_real_account_id(account_id)
    except ValueError as exc:
        raise LiveArmingInvalid("live arming record names a reserved account identity as real") from exc


def _is_int(value: object) -> bool:
    """Whether ``value`` is an ``int`` and nothing that merely behaves like one.

    A frozen dataclass does not enforce its annotations, so a row read off disk
    -- or a ``create`` call -- can carry ``1.0`` or ``True`` where this record
    promises an integer, and a re-sealed row's digest verifies over exactly the
    value it carries. Neither an ``isinstance`` test (``True`` is an ``int``)
    nor a range check (``1.0`` compares equal to ``1``) excludes them, so the
    type is asked for by name before any bound is applied.
    """
    return type(value) is int


def _require_kind(
    kind: str, expected: str, schema_version: int, *, allowed_versions: tuple[int, ...]
) -> None:
    """A row of the wrong shape is named as such, not as a bad number."""
    if kind != expected or not _is_int(schema_version) or schema_version not in allowed_versions:
        raise LiveArmingInvalid("live arming record has an invalid kind or schema version")


def _require_hashes(*values: str) -> None:
    if any(not isinstance(value, str) or _SHA256.match(value) is None for value in values):
        raise LiveArmingInvalid("live arming record has invalid hash facts")


def _validate_armed(record: LiveArmingRecord) -> None:
    _require_real(record.live_account_id)
    _require_kind(record.kind, "armed", record.schema_version, allowed_versions=(1, 2, 3))
    if (
        not record.strategy_instance_id
        or not _is_int(record.max_sessions)
        or not _is_int(record.armed_at_ms)
        or record.max_sessions < 1
        or not 0 <= record.armed_at_ms <= MAX_TIMESTAMP_MS
    ):
        raise LiveArmingInvalid("live arming record has invalid integer or identity facts")
    _require_hashes(record.seal_hash, record.configured_signal_hash, record.envelope_sha256)
    # Shadow is a mode, not a requirement (owner decision 2026-09-09): a
    # receipt is recorded when the instance holds one, and null otherwise.
    if record.shadow_receipt_sha256 is not None:
        _require_hashes(record.shadow_receipt_sha256)
    try:
        sealed = LiveEnvelopeValues(**record.envelope_values)
    except TypeError as exc:
        raise LiveArmingInvalid("live arming record's sealed envelope has an invalid shape") from exc
    # ``LiveEnvelopeValues`` is a plain frozen dataclass with no field validation
    # of its own, so a re-sealed row can carry ``1.0`` or ``True`` for either
    # integer field and still verify -- the same gap ``_is_int`` closed above
    # for the record's own four integers.
    if not _is_int(sealed.shadow_sessions) or not _is_int(sealed.arming_max_sessions):
        raise LiveArmingInvalid("live arming record's sealed envelope has invalid integer facts")
    # The sealed values bound real money -- the loss limit the hold is raised on
    # and cleared against, the allowance an extended leg is priced from -- and
    # they arrive off disk without ever passing through ``AlpacaSettings``. The
    # domain is the envelope's own (``live_envelope._ENVELOPE_DOMAINS``), asked
    # for here rather than restated.
    violation = envelope_domain_violation(sealed)
    if violation is not None:
        raise LiveArmingInvalid(f"live arming record's sealed {violation}")
    if sealed.sha != record.envelope_sha256:
        raise LiveArmingInvalid("live arming record's envelope sha does not match its sealed values")
    # The lapse count is the sealed envelope's, not a second number beside it.
    # ``arming_status`` counts off ``max_sessions`` while the operator confirmed
    # -- and the sync publishes -- ``arming_max_sessions``, so a self-consistent
    # row could otherwise stay armed for 100 sessions while its sealed envelope
    # said 20. The envelope hash cannot catch that: both fields are inside it.
    if record.max_sessions != sealed.arming_max_sessions:
        raise LiveArmingInvalid("live arming record's max_sessions disagrees with the sealed envelope")
    if record.schema_version == 1 and record.predecessor is not None:
        raise LiveArmingInvalid("version 1 live arming records cannot name a predecessor")
    if record.schema_version == 2 and record.predecessor is None:
        raise LiveArmingInvalid(
            "live arming record has an invalid kind or schema version: "
            "version 2 live arming records must name a predecessor"
        )
    if record.predecessor is not None:
        if not record.predecessor.strategy_instance_id:
            raise LiveArmingInvalid("live arming record predecessor has no strategy instance")
        _require_hashes(record.predecessor.seal_hash, record.predecessor.receipt_sha256)
    if record.schema_version == 1 and record.originating_plan_id is not None:
        raise LiveArmingInvalid("version 1 live arming records cannot name an originating plan")
    if record.schema_version == 3:
        from app.schemas.exit_terms import ExitTerms

        if record.exit_terms is None:
            raise LiveArmingInvalid("version 3 requires sealed exit terms")
        ExitTerms.model_validate(record.exit_terms)
    elif record.exit_terms is not None:
        raise LiveArmingInvalid("historical arming schemas cannot carry exit terms")
    if record.schema_version in (2, 3):
        if record.originating_plan_id is None:
            raise LiveArmingInvalid("version 2 live arming records must name an originating plan")
        _require_hashes(record.originating_plan_id)


def _validate_disarmed(record: LiveDisarmRecord) -> None:
    _require_real(record.live_account_id)
    _require_kind(record.kind, "disarmed", record.schema_version, allowed_versions=(1,))
    if (
        not record.strategy_instance_id
        or not _is_int(record.disarmed_at_ms)
        or not 0 <= record.disarmed_at_ms <= MAX_TIMESTAMP_MS
    ):
        raise LiveArmingInvalid("live arming record has invalid integer or identity facts")
    _require_hashes(record.revokes_record_sha256)


def latest_arming(records: Sequence[LedgerRecord]) -> LiveArmingRecord | None:
    """The newest arming record among ``records``, ignoring revocations (R10).

    A disarm row carries no envelope, which is why it is skipped: both of its
    readers -- the exit-terms upgrade and ``program_leg._sealed_allowances`` --
    want the newest sealed values.
    """
    armings = [row for row in records if isinstance(row, LiveArmingRecord)]
    return armings[-1] if armings else None


__all__ = [
    "ARMING_ADMISSION_REASON_CODES",
    "LIVE_ARMING_FUTURE_DATED",
    "LIVE_ARMING_LAPSED",
    "LIVE_ARMING_LEDGER_INVALID",
    "LIVE_ARMING_REQUIRED",
    "LIVE_ARMING_REVOKED",
    "LIVE_ARMING_SEAL_CHANGED",
    "LIVE_ARMING_UNOBSERVED",
    "LIVE_ENVELOPE_DISAGREEMENT",
    "LIVE_MODE_DISAGREEMENT",
    "LedgerRecord",
    "LiveArmingInvalid",
    "LiveArmingRecord",
    "LiveDisarmRecord",
    "RehearsalPredecessor",
    "arming_version_payload",
    "latest_arming",
]


def arming_version_payload(payload: dict[str, object], version: int) -> dict[str, object]:
    """Omit fields that were absent from this historical arming wire version."""
    result = dict(payload)
    if version < 3:
        result.pop("exit_terms", None)
    if version == 1:
        result.pop("predecessor", None)
        result.pop("originating_plan_id", None)
    return result
