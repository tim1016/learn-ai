"""The sealed arming record, its revocation, and the pure status rule (ADR 0059 D3).

Formula: ``sessions_used = trading_session_count(ET date of armed_at_ms, ET date
  of now_ms)`` over the canonical NYSE calendar, inclusive of both dates;
  lapsed iff ``sessions_used > max_sessions``. A record dated after the caller's
  clock (``now_ms < armed_at_ms``) never reaches that formula: it fails closed
  under its own reason code before the calendar is ever asked (controller
  ruling, ADR 0059 D3 addendum).
Reference: ADR 0059 Decision 3; design rulings R1, R2, R4, R5, R6, R12 in
  ``docs/superpowers/specs/2026-09-09-live-slice-6-arming-ceremony-design.md``.
Canonical implementation: this file. The ledger that stores these records is
  ``live_arming_ledger.py``; the ceremony that mints them is
  ``live_arming_ceremony.py``; the calendar is ``app/lean_sidecar/trading_calendar.py``.
Validated against: ``tests/broker/alpaca/clerk/test_live_arming.py``.

Nothing here touches a broker, a database, a file or a clock: every function is
a pure fact about records the caller already read, judged at the caller's
``now_ms``. Arming binds to the instance's *whole* sealed-program hash (R2), so
a change to its size, action plan or account disarms it just as a change to its
signal would.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any, Literal

from app.broker.alpaca.clerk.account_authority import require_real_account_id
from app.broker.alpaca.clerk.live_envelope import (
    LIVE_ENVELOPE_DISAGREEMENT,
    LIVE_ENVELOPE_MISSING,
    LiveEnvelopeValues,
)
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256, verify_sealed_record
from app.lean_sidecar.trading_calendar import trading_session_count
from app.utils.session_anchors import MAX_TIMESTAMP_MS, et_date_at_ms

LIVE_ARMING_LAPSED = "LIVE_ARMING_LAPSED"
LIVE_ARMING_REVOKED = "LIVE_ARMING_REVOKED"
LIVE_ARMING_SEAL_CHANGED = "LIVE_ARMING_SEAL_CHANGED"
LIVE_ARMING_INSTANCE_UNSEALED = "LIVE_ARMING_INSTANCE_UNSEALED"
LIVE_ARMING_TOKEN_INVALID = "LIVE_ARMING_TOKEN_INVALID"
LIVE_ARMING_PLAN_EXPIRED = "LIVE_ARMING_PLAN_EXPIRED"
LIVE_ARMING_INPUTS_CHANGED = "LIVE_ARMING_INPUTS_CHANGED"
LIVE_ARMING_NOT_ARMED = "LIVE_ARMING_NOT_ARMED"
LIVE_ARMING_TTL_INVALID = "LIVE_ARMING_TTL_INVALID"
# A record dated after the caller's clock fails closed under its own code
# (controller ruling): the calendar never sees a reversed range, and no
# caller can silently extend an arming by rolling its own clock back.
LIVE_ARMING_FUTURE_DATED = "LIVE_ARMING_FUTURE_DATED"
# ADR 0059 D2 names this refusal; slice 4 could only write it in prose because
# nothing read the receipt yet. This is the first code that does.
LIVE_SHADOW_INCOMPLETE = "LIVE_SHADOW_INCOMPLETE"

# Slice 7 (ADR 0059 D11): the admission-time codes. ``REQUIRED`` is an instance
# the ledger has never named; ``UNOBSERVED`` is a gate with no fresh snapshot;
# ``LEDGER_INVALID`` is a snapshot the last refresh could not verify. The
# fourth is ADR 0059 D8's own name for the *transition* a lost arming makes:
# it is the sync's once-per-transition log action, never a receipt code — the
# refused ENTER carries the instance's own arming code (owner decision
# 2026-09-09: the halt is the refusal, the warning and the verdict; no
# ``desired_state`` is written).
LIVE_ARMING_REQUIRED = "LIVE_ARMING_REQUIRED"
LIVE_ARMING_UNOBSERVED = "LIVE_ARMING_UNOBSERVED"
LIVE_ARMING_LEDGER_INVALID = "LIVE_ARMING_LEDGER_INVALID"
LIVE_VERDICT_TRANSITION_HALT = "LIVE_VERDICT_TRANSITION_HALT"
# The adapter's own refusal (`BrokerAccountModeDisagreement.reason_code`),
# restated so the gate can name it when the broker's mode moves mid-session
# (design R2); a test pins the two strings equal.
LIVE_MODE_DISAGREEMENT = "LIVE_MODE_DISAGREEMENT"

ARMING_REASON_CODES: frozenset[str] = frozenset(
    {
        LIVE_ARMING_LAPSED,
        LIVE_ARMING_REVOKED,
        LIVE_ARMING_SEAL_CHANGED,
        LIVE_ARMING_INSTANCE_UNSEALED,
        LIVE_ARMING_TOKEN_INVALID,
        LIVE_ARMING_PLAN_EXPIRED,
        LIVE_ARMING_INPUTS_CHANGED,
        LIVE_ARMING_NOT_ARMED,
        LIVE_ARMING_TTL_INVALID,
        LIVE_ARMING_FUTURE_DATED,
        LIVE_SHADOW_INCOMPLETE,
        LIVE_ENVELOPE_DISAGREEMENT,
        LIVE_ENVELOPE_MISSING,
        LIVE_ARMING_REQUIRED,
        LIVE_ARMING_UNOBSERVED,
        LIVE_ARMING_LEDGER_INVALID,
        LIVE_VERDICT_TRANSITION_HALT,
    }
)

# What ``sqlite/arming_admission.require_arming_admission`` may refuse with —
# the three codes above plus the per-instance states ``arming_status`` names.
# ``uncertainty.py`` classifies all of them transient: a lost arming refuses
# the next ENTER and retries on the next decision clock — it never pauses the
# instance and never halts it from inside admission.
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

ArmingState = Literal["unarmed", "armed", "lapsed", "disarmed"]

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_LABEL = "live arming"

# The domains ``AlpacaSettings`` enforces on the environment (``config.py``),
# re-asserted on the values a record *sealed*. The two must agree because the
# sealed envelope is what bounds real money: it is the loss limit the hold is
# raised on and cleared against, and the allowance an extended-session leg is
# priced from. ``LiveEnvelopeValues`` is a plain frozen dataclass with no field
# validation of its own, so a re-sealed row could otherwise carry a `loss_usd`
# of `inf` -- which makes every loss comparison False -- or an `xh_exit_bps`
# past 10 000, which floors an exit's anchor to zero and refuses the leg.
# Written as (field, admits, domain) so a new bound is a row, not a branch.
_SEALED_ENVELOPE_DOMAINS: tuple[tuple[str, Callable[[float], bool], str], ...] = (
    ("loss_fraction", lambda value: 0 < value < 1, "in (0, 1)"),
    # Excludes NaN and infinity as well as zero: `inf > 0` is True.
    ("loss_usd", lambda value: 0 < value < float("inf"), "positive and finite"),
    ("shadow_sessions", lambda value: value >= 1, "at least 1"),
    ("arming_max_sessions", lambda value: value >= 1, "at least 1"),
    ("xh_entry_bps", lambda value: 0 <= value < 10_000, "in [0, 10000)"),
    ("xh_exit_bps", lambda value: 0 <= value < 10_000, "in [0, 10000)"),
)


class LiveArmingInvalid(ValueError):
    """An arming or disarm row cannot be trusted."""


class LiveArmingRefused(ValueError):
    """The ceremony refused, under one named reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


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
    ) -> LiveArmingRecord:
        unsigned: dict[str, Any] = {
            "kind": "armed",
            "schema_version": 2 if predecessor is not None else 1,
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
            if record.schema_version == 1:
                del unsigned["predecessor"]
                del unsigned["originating_plan_id"]
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
    _require_kind(record.kind, "armed", record.schema_version, allowed_versions=(1, 2))
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
    for name, admits, domain in _SEALED_ENVELOPE_DOMAINS:
        if not admits(getattr(sealed, name)):
            raise LiveArmingInvalid(f"live arming record's sealed {name} is not {domain}")
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
    if record.schema_version == 2:
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


def sessions_used(*, armed_at_ms: int, now_ms: int) -> int:
    """Calendar NYSE sessions spent since the arming, both ET dates inclusive (R6).

    The arming session counts as one, so an arming at 15:59 ET spends a whole
    session on a minute -- disclosed, and what ``sessions_remaining`` is for. An
    arming on a non-trading ET date starts counting at the next session, which
    is exactly what the inclusive count over the calendar already says.

    A ``now_ms`` behind the arming is refused, not absorbed: this raises
    ``ValueError`` rather than returning 0, so the calendar is never asked to
    evaluate a reversed range and no caller can silently extend an arming by
    rolling its own clock back (controller ruling). ``arming_status`` checks
    for this case itself before ever calling here, and reports it under
    ``LIVE_ARMING_FUTURE_DATED``.
    """
    if now_ms < armed_at_ms:
        raise ValueError("now_ms precedes armed_at_ms; a record dated after the clock is not current")
    # No second, date-level guard: ``et_date_at_ms`` is monotonic, so the
    # millisecond comparison above already excludes every reversed range.
    return trading_session_count(et_date_at_ms(armed_at_ms), et_date_at_ms(now_ms))


@dataclass(frozen=True)
class ArmingStatus:
    """One instance's arming state and the evidence behind it."""

    state: ArmingState
    reason_code: str | None
    record: LiveArmingRecord | None
    sessions_used: int
    sessions_remaining: int


def latest_arming(records: Sequence[LedgerRecord]) -> LiveArmingRecord | None:
    """The account's newest arming record, ignoring revocations (R10).

    A disarm withdraws one instance's permission; it does not unseal the
    account's envelope, which stays whatever the last arming ceremony read out
    of the environment until another ceremony replaces it.
    """
    armings = [row for row in records if isinstance(row, LiveArmingRecord)]
    return armings[-1] if armings else None


def instance_ids(records: Sequence[LedgerRecord]) -> tuple[str, ...]:
    """Every instance with a row in ``records``, in first-appearance order."""
    return tuple(dict.fromkeys(row.strategy_instance_id for row in records))


def _live_state(
    latest: LiveArmingRecord,
    *,
    seal_hash: str | None,
    configured_envelope: LiveEnvelopeValues,
    future_dated: bool,
    used: int,
) -> tuple[ArmingState, str | None]:
    """The ordered rule R5 states, as five lines; ``arming_status`` says why."""
    if seal_hash is None or seal_hash != latest.seal_hash:
        return "disarmed", LIVE_ARMING_SEAL_CHANGED
    if latest.envelope_sha256 != configured_envelope.sha:
        return "disarmed", LIVE_ENVELOPE_DISAGREEMENT
    if future_dated:
        return "disarmed", LIVE_ARMING_FUTURE_DATED
    if used > latest.max_sessions:
        return "lapsed", LIVE_ARMING_LAPSED
    return "armed", None


def arming_status(
    records: Sequence[LedgerRecord],
    *,
    live_account_id: str,
    strategy_instance_id: str,
    seal_hash: str | None,
    configured_envelope: LiveEnvelopeValues,
    now_ms: int,
) -> ArmingStatus:
    """This instance's arming state, decided by its latest record alone (R5).

    The checks run in order -- ``REVOKED`` -> ``SEAL_CHANGED`` ->
    ``LIVE_ENVELOPE_DISAGREEMENT`` -> ``LIVE_ARMING_FUTURE_DATED`` -> ``LAPSED``
    -- and the first failure names the state. Order is meaning, not
    optimisation: an instance whose seal changed *and* whose arming has lapsed
    is reported as seal-changed, because re-sealing is what its operator has
    to do first. ``LIVE_ARMING_FUTURE_DATED`` is checked as a plain comparison
    before ``sessions_used`` is ever called, so a record dated after ``now_ms``
    can still be reported as seal-changed or in envelope disagreement without
    the calendar seeing a reversed range (controller ruling).

    ``seal_hash=None`` means no sealed binding for this instance exists on this
    account any more, which is a change from whatever was armed -- so it is
    ``LIVE_ARMING_SEAL_CHANGED``, never ``armed``.
    """
    latest: LedgerRecord | None = None
    for record in records:
        if record.live_account_id == live_account_id and record.strategy_instance_id == strategy_instance_id:
            latest = record
    if latest is None:
        return ArmingStatus(state="unarmed", reason_code=None, record=None, sessions_used=0, sessions_remaining=0)
    if isinstance(latest, LiveDisarmRecord):
        return ArmingStatus(
            state="disarmed", reason_code=LIVE_ARMING_REVOKED, record=None, sessions_used=0, sessions_remaining=0
        )

    future_dated = now_ms < latest.armed_at_ms
    if future_dated:
        used, remaining = 0, latest.max_sessions
    else:
        used = sessions_used(armed_at_ms=latest.armed_at_ms, now_ms=now_ms)
        remaining = max(0, latest.max_sessions - used)

    # ``remaining`` is already 0 wherever the rule says lapsed, because that
    # branch is reached only when ``used > max_sessions``.
    state, reason_code = _live_state(
        latest,
        seal_hash=seal_hash,
        configured_envelope=configured_envelope,
        future_dated=future_dated,
        used=used,
    )
    return ArmingStatus(
        state=state,
        reason_code=reason_code,
        record=latest,
        sessions_used=used,
        sessions_remaining=remaining,
    )


__all__ = [
    "ARMING_ADMISSION_REASON_CODES",
    "ARMING_REASON_CODES",
    "LIVE_ARMING_FUTURE_DATED",
    "LIVE_ARMING_INPUTS_CHANGED",
    "LIVE_ARMING_INSTANCE_UNSEALED",
    "LIVE_ARMING_LAPSED",
    "LIVE_ARMING_LEDGER_INVALID",
    "LIVE_ARMING_NOT_ARMED",
    "LIVE_ARMING_PLAN_EXPIRED",
    "LIVE_ARMING_REQUIRED",
    "LIVE_ARMING_REVOKED",
    "LIVE_ARMING_SEAL_CHANGED",
    "LIVE_ARMING_TOKEN_INVALID",
    "LIVE_ARMING_TTL_INVALID",
    "LIVE_ARMING_UNOBSERVED",
    "LIVE_ENVELOPE_DISAGREEMENT",
    "LIVE_ENVELOPE_MISSING",
    "LIVE_MODE_DISAGREEMENT",
    "LIVE_SHADOW_INCOMPLETE",
    "LIVE_VERDICT_TRANSITION_HALT",
    "ArmingState",
    "ArmingStatus",
    "LedgerRecord",
    "LiveArmingInvalid",
    "LiveArmingRecord",
    "LiveArmingRefused",
    "LiveDisarmRecord",
    "RehearsalPredecessor",
    "arming_status",
    "instance_ids",
    "latest_arming",
    "sessions_used",
]
