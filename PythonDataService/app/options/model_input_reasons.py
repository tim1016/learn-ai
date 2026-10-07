"""Why one of the option model's inputs is not known: the code beside the sentence (#2913).

The rate and the declared dividends are each known or not, and each says why
when it is not. Every process words the reason in a sentence for the owner
and names it with one of these codes for a refusal to carry. The codes are
the same wherever the inputs are read: in a process that asks the vendors
itself, and on a fleet Clerk that asks the coordinator.
"""

from __future__ import annotations

from enum import StrEnum


class WhyNotKnown(StrEnum):
    """Why the rate is only the default, or why the dividends are not known."""

    FRED_GAVE_NO_RATE = "fred_gave_no_rate"
    NO_POLYGON_KEY = "no_polygon_key"
    POLYGON_DID_NOT_ANSWER = "polygon_did_not_answer"
    POLYGON_ANSWER_UNREADABLE = "polygon_answer_unreadable"
    # A lookup that had not answered when a read stopped waiting for it.
    LOOKUP_TOO_SLOW = "lookup_too_slow"
    # A lookup that failed and gave no code of its own.
    LOOKUP_FAILED = "lookup_failed"
    # What a Clerk says of its own ask of the coordinator. The coordinator never answers with these.
    COORDINATOR_NOT_CONFIGURED = "coordinator_not_configured"
    COORDINATOR_ADDRESS_REFUSED = "coordinator_address_refused"
    COORDINATOR_NOT_ASKED = "coordinator_not_asked"
    COORDINATOR_UNREACHABLE = "coordinator_unreachable"
    COORDINATOR_TIMED_OUT = "coordinator_timed_out"
    COORDINATOR_REFUSED = "coordinator_refused"
    COORDINATOR_FAILED = "coordinator_failed"
    COORDINATOR_ANSWER_UNREADABLE = "coordinator_answer_unreadable"


__all__ = ["WhyNotKnown"]
