"""How a run ended, in the owner's words: the one outcome vocabulary (#2615).

The bot panel's outcome card and History's "How it ended" read these words
and no others. A run's recorded end is a kind (``BotDutyOutcomeKind``, worded
by the panel vocabulary's ``OPERATOR_COPY``) and a reason code, and a reason
is one of two sorts:

* **An end's own cause** (``END_REASON_COPY``) -- the scheduled end, a feed
  refusal, a crash on market data, a service shutdown, a failed launch. It
  says more than its kind, so it is the words on both surfaces.
* **A custody proof** (``CUSTODY_PROOF_COPY``) -- what the Clerk proved a
  stopped trade-mode bot holds, and so the panel's next step. It says nothing
  of who ended the run. A plain stop reads as its proof on the panel; any
  other end keeps its own words there and adds the proof's (#2667), and
  History, which says how a run ended, never words a proof.
"""

from __future__ import annotations

from app.broker.v2panel.vocabulary import copy_for, duty_outcome_copy_key
from app.marketdata.feed import IMPOSSIBLE_SOURCE_BAR
from app.schemas.bot_lifecycle import BotDutyOutcomeKind
from app.services.bot_end import SCHEDULED_END_REASON_CODE
from app.services.bot_run_evidence import ACTIVATION_FAILED_STOP_REASON_CODE, PROVISIONAL_STOP_REASON_CODE
from app.services.broker_v2_panel.feed_continuity_projection import WARMUP_REFUSAL_COPY

#: Each end whose cause says more than its kind: (label, explanation).
END_REASON_COPY: dict[str, tuple[str, str]] = {
    SCHEDULED_END_REASON_CODE: (
        "Ended at its scheduled time",
        "The Clerk stopped the bot at the end you set. Its end shows whether it sells or keeps its shares.",
    ),
    IMPOSSIBLE_SOURCE_BAR: (
        "Refused: impossible source bar",
        "The market-data feed delivered a bar that cannot be real -- a non-finite or "
        "non-positive price, a high below its low, a print outside the bar's range, or a "
        "negative volume -- so the run was stopped rather than allowed to decide on it. "
        "This is a data-quality refusal, not a market verdict: nothing about the strategy "
        "changed. Check IB Gateway's connection and market-data farm health, then deploy again "
        "once its bars arrive clean.",
    ),
    **WARMUP_REFUSAL_COPY,
    # A crash the market-data feed caused says so (hurdle H29): the generic
    # crash copy disclaims any market-data verdict, which here is the cause.
    "FEED_DEATH": (
        "Crashed: market data stopped",
        "The IBKR market-data feed stopped delivering bars, so the run ended rather "
        "than decide without them.",
    ),
    "SERVICE_SHUTDOWN": (
        "Stopped when the service shut down",
        "The service shut down while the bot was running, which ended this run.",
    ),
    # Every Stop records this first and replaces it once its custody is
    # proven; one never proven keeps it, and it says nothing of who stopped.
    PROVISIONAL_STOP_REASON_CODE: (
        "Stopped",
        "The run stopped. How it ended, and what it still holds, is not recorded yet.",
    ),
    # A failed launch recorded before #2667 is a stop with this reason: the
    # compensation runs through the normal Stop, but nobody stopped it (#2559).
    ACTIVATION_FAILED_STOP_REASON_CODE: (
        "Failed to launch",
        "The launch failed partway through, so the service ended the run. "
        "Nobody stopped it, and nothing is running.",
    ),
}

#: Each custody proof a trade-mode stop records (``StopCustodyOutcome``): (label, explanation).
CUSTODY_PROOF_COPY: dict[str, tuple[str, str]] = {
    "STOPPED_FLAT": (
        "Stopped flat",
        "The runtime is stopped and the Clerk proved zero attributed exposure.",
    ),
    "STOPPED_WITH_APPROVED_ATTRIBUTED_EXPOSURE": (
        "Stopped with approved carryover",
        "The runtime is stopped and exact attributed exposure is preserved by a durable checkpoint.",
    ),
    "STOP_REQUIRES_FLATTEN": (
        "Stopped; flatten required",
        "The runtime is stopped with attributed exposure. Use Flatten to resolve that exposure.",
    ),
    "STOPPED_CUSTODY_UNPROVABLE": (
        "Stopped; custody unprovable",
        "The runtime is stopped, but the Clerk could not prove a terminal flat or carryover outcome.",
    ),
}

_FLATTENED_HEADLINE = "Stopped and flattened"


def outcome_headline(kind: BotDutyOutcomeKind, reason_code: str, *, flattened: bool) -> str:
    """One run's end in History: its cause's words, else its kind's.

    ``flattened`` is History's own fact: the owner's flatten sold what the
    stopped run held (``clerk.sqlite.bot_history``).
    """
    if kind == "STOPPED" and flattened:
        return _FLATTENED_HEADLINE
    words = END_REASON_COPY.get(reason_code)
    return words[0] if words is not None else copy_for(duty_outcome_copy_key(kind)).label


def outcome_card_copy(kind: BotDutyOutcomeKind, reason_code: str) -> tuple[str, str]:
    """One run's end on the panel's outcome card: (label, explanation), see module doc."""
    words = END_REASON_COPY.get(reason_code)
    if words is not None:
        return words
    copy = copy_for(duty_outcome_copy_key(kind))
    proof = CUSTODY_PROOF_COPY.get(reason_code)
    if proof is None:
        return copy.label, copy.explanation
    if kind == "STOPPED":
        return proof
    return copy.label, f"{copy.explanation} {proof[1]}"


__all__ = ["CUSTODY_PROOF_COPY", "END_REASON_COPY", "outcome_card_copy", "outcome_headline"]
