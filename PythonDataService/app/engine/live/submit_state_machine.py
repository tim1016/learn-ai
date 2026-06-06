"""Module D — submit state machine (deep, pure). ADR-0008 §4, PRD #446.

Given the current WAL status, an acknowledgement outcome, and a broker-probe
discriminator, return the next action verdict. The discriminator
(``PRESENT`` / ``PROVABLY_ABSENT`` / ``NOT_PROVABLE``) is computed by the I/O
layer and handed in, so "provably absent" is *never* implementer judgment
inside this module — and so this stays a pure function.

The submit lifecycle (ADR-0008 §3–§4):
    PENDING_INTENT --clean ack--------------> SUBMITTED
    PENDING_INTENT --raise/timeout----------> ACK_FAILED_UNCERTAIN
    ACK_FAILED_UNCERTAIN --probe PRESENT----> SUBMITTED_RECOVERED   (adopt)
    ACK_FAILED_UNCERTAIN --probe ABSENT-----> INTENT_NOT_ACCEPTED   (retry once)
    ACK_FAILED_UNCERTAIN --probe NOT_PROV.--> SUBMIT_UNCERTAIN_HALTED (halt)

``RETRY_CAP = 1``: a provably-absent intent may be retried at most once, reusing
the SAME ``intent_id``/``order_ref``. A second uncertain ack on the retried
intent halts. Blind retry is structurally unrepresentable — ``RETRY_ONCE`` is
emitted only for ``PROVABLY_ABSENT`` with ``retry_count < RETRY_CAP``.
"""

from __future__ import annotations

from enum import StrEnum

from app.engine.live.intent_events import IntentEventType

RETRY_CAP = 1


class AckOutcome(StrEnum):
    CLEAN_ACK = "CLEAN_ACK"
    RAISED_OR_TIMEOUT = "RAISED_OR_TIMEOUT"


class BrokerProbe(StrEnum):
    """The resolution-phase discriminator (ADR-0008 §4)."""

    PRESENT = "PRESENT"
    PROVABLY_ABSENT = "PROVABLY_ABSENT"
    NOT_PROVABLE = "NOT_PROVABLE"


class SubmitVerdict(StrEnum):
    RECORD_SUBMITTED = "RECORD_SUBMITTED"
    RECORD_ACK_FAILED_UNCERTAIN = "RECORD_ACK_FAILED_UNCERTAIN"
    RECOVER_ADOPT = "RECOVER_ADOPT"
    RETRY_ONCE = "RETRY_ONCE"
    HALT = "HALT"


class IllegalSubmitTransition(ValueError):
    """Raised for an input combination the lifecycle cannot produce."""


def next_action(
    *,
    current_status: IntentEventType,
    ack_outcome: AckOutcome | None = None,
    probe: BrokerProbe | None = None,
    retry_count: int = 0,
) -> SubmitVerdict:
    """Pure transition. ``current_status`` is the intent's latest WAL state."""
    if current_status is IntentEventType.PENDING_INTENT:
        if probe is not None:
            raise IllegalSubmitTransition("ack phase takes no broker probe")
        if ack_outcome is None:
            raise IllegalSubmitTransition("PENDING_INTENT requires an ack_outcome")
        if ack_outcome is AckOutcome.CLEAN_ACK:
            return SubmitVerdict.RECORD_SUBMITTED
        return SubmitVerdict.RECORD_ACK_FAILED_UNCERTAIN

    if current_status is IntentEventType.ACK_FAILED_UNCERTAIN:
        if ack_outcome is not None:
            raise IllegalSubmitTransition("resolution phase takes no ack_outcome")
        if probe is None:
            raise IllegalSubmitTransition("ACK_FAILED_UNCERTAIN requires a broker probe")
        if retry_count < 0:
            # A negative count must never satisfy `< RETRY_CAP` and slip a second
            # retry through — that would be a double-submit path.
            raise IllegalSubmitTransition(f"retry_count must be non-negative, got {retry_count}")
        if probe is BrokerProbe.PRESENT:
            return SubmitVerdict.RECOVER_ADOPT
        if probe is BrokerProbe.PROVABLY_ABSENT:
            # The cap is the only thing standing between a recoverable miss and
            # a double-submit loop. A second uncertain ack halts.
            if retry_count >= RETRY_CAP:
                return SubmitVerdict.HALT
            return SubmitVerdict.RETRY_ONCE
        return SubmitVerdict.HALT  # NOT_PROVABLE — never guess

    raise IllegalSubmitTransition(f"no transition defined from status {current_status}")


def verdict_to_event_type(verdict: SubmitVerdict) -> IntentEventType:
    """The WAL event a verdict records. ``RETRY_ONCE`` records
    ``INTENT_NOT_ACCEPTED`` (proven absent); the re-place then appends a fresh
    ``PENDING_INTENT`` reusing the same ``intent_id``/``order_ref``."""
    return {
        SubmitVerdict.RECORD_SUBMITTED: IntentEventType.SUBMITTED,
        SubmitVerdict.RECORD_ACK_FAILED_UNCERTAIN: IntentEventType.ACK_FAILED_UNCERTAIN,
        SubmitVerdict.RECOVER_ADOPT: IntentEventType.SUBMITTED_RECOVERED,
        SubmitVerdict.RETRY_ONCE: IntentEventType.INTENT_NOT_ACCEPTED,
        SubmitVerdict.HALT: IntentEventType.SUBMIT_UNCERTAIN_HALTED,
    }[verdict]


__all__ = [
    "RETRY_CAP",
    "AckOutcome",
    "BrokerProbe",
    "IllegalSubmitTransition",
    "SubmitVerdict",
    "next_action",
    "verdict_to_event_type",
]
