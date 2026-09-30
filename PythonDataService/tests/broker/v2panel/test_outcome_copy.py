"""The one outcome vocabulary: how a run ended, on the panel and in History (#2615, #2667)."""

from __future__ import annotations

from typing import get_args

import pytest

from app.broker.v2panel.vocabulary import DutyOutcomeKind
from app.marketdata.feed import FEED_REFUSAL_REASON_CODES
from app.schemas.bot_lifecycle import BotDutyOutcomeKind
from app.services.bot_carryover import StopCustodyOutcome
from app.services.broker_v2_panel.outcome_copy import (
    CUSTODY_PROOF_COPY,
    END_REASON_COPY,
    outcome_card_copy,
    outcome_headline,
)


def test_the_panel_admits_every_kind_a_run_can_end_with() -> None:
    """A kind the runner can record but the panel cannot name failed the
    panel's validation, and History's second copy map raised ``KeyError``."""
    assert set(get_args(DutyOutcomeKind)) == {*get_args(BotDutyOutcomeKind), "ON_DUTY"}


@pytest.mark.parametrize("kind", get_args(BotDutyOutcomeKind))
def test_every_kind_has_its_own_words_on_both_surfaces(kind: str) -> None:
    headline = outcome_headline(kind, "A_REASON_WITH_NO_WORDS", flattened=False)  # type: ignore[arg-type]
    label, explanation = outcome_card_copy(kind, "A_REASON_WITH_NO_WORDS")  # type: ignore[arg-type]

    assert headline == label and headline.strip() and explanation.strip()


def test_every_feed_refusal_and_custody_proof_has_its_words() -> None:
    assert END_REASON_COPY.keys() >= FEED_REFUSAL_REASON_CODES
    assert CUSTODY_PROOF_COPY.keys() == set(get_args(StopCustodyOutcome))


@pytest.mark.parametrize(
    ("kind", "reason", "flattened", "headline"),
    [
        ("STOPPED", "SCHEDULED_END", False, "Ended at its scheduled time"),
        # An operator's Stop in trade mode reads as before: the proof is not History's.
        ("STOPPED", "STOPPED_FLAT", False, "Stopped by you"),
        ("STOPPED", "STOP_REQUIRES_FLATTEN", False, "Stopped by you"),
        ("STOPPED", "STOPPED_FLAT", True, "Stopped and flattened"),
        ("STOPPED", "SERVICE_SHUTDOWN", False, "Stopped when the service shut down"),
        # A stop whose custody was never proven names no one who stopped it.
        ("STOPPED", "STOPPED_PENDING_CUSTODY_PROOF", False, "Stopped"),
        ("STOPPED", "WARMUP_HISTORY_UNAVAILABLE", False, "Refused: warmup history unavailable"),
        ("CRASHED", "FEED_DEATH", False, "Crashed: market data stopped"),
        ("CRASHED", "ValueError", False, "Crashed"),
        ("HALTED", "HALTED", False, "Halted"),
        # #2667: a trade-mode failed launch records its custody proof as its reason.
        ("FAILED_LAUNCH", "STOP_REQUIRES_FLATTEN", False, "Failed to launch"),
        ("FAILED_LAUNCH", "ACTIVATION_FAILED_AFTER_REGISTRATION", False, "Failed to launch"),
        # A failed launch recorded before #2667 is a stop with the activation code.
        ("STOPPED", "ACTIVATION_FAILED_AFTER_REGISTRATION", False, "Failed to launch"),
        ("EXITED_UNVERIFIED", "INTERRUPTED_BY_RESTART", False, "Ended without a clean exit"),
        ("RETIRED", "RETIRED", False, "Retired before its end was recorded"),
    ],
)
def test_each_way_a_run_ends_has_its_own_plain_words(kind: str, reason: str, flattened: bool, headline: str) -> None:
    assert outcome_headline(kind, reason, flattened=flattened) == headline  # type: ignore[arg-type]


def test_a_stop_reads_as_its_custody_proof_on_the_panel() -> None:
    assert outcome_card_copy("STOPPED", "STOP_REQUIRES_FLATTEN") == CUSTODY_PROOF_COPY["STOP_REQUIRES_FLATTEN"]


def test_a_failed_launch_keeps_its_own_words_and_the_proofs_next_step_on_the_panel() -> None:
    """#2667: who ended the run, and what the Clerk proved it left holding."""
    label, explanation = outcome_card_copy("FAILED_LAUNCH", "STOP_REQUIRES_FLATTEN")

    assert label == "Failed to launch"
    assert explanation.startswith("The launch failed partway through")
    assert explanation.endswith(CUSTODY_PROOF_COPY["STOP_REQUIRES_FLATTEN"][1])
    assert "Use Flatten" in explanation
