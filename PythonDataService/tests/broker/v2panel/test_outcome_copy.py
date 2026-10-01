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


def test_a_stop_reads_as_its_custody_proof_on_the_panel() -> None:
    assert outcome_card_copy("STOPPED", "STOP_REQUIRES_FLATTEN") == CUSTODY_PROOF_COPY["STOP_REQUIRES_FLATTEN"]
