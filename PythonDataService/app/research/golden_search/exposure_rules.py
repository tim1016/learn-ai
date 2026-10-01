"""Whether a final-test interval is still unseen in recorded research, and the claim it can support.

A final interval is ``previously_used`` when any exposure-ledger row for the
symbol overlaps it, from any study, strategy or revision; otherwise
``history_unknown`` when other recorded research activity (grid searches,
walk-forward studies, saved backtests, other studies' evaluations) overlaps
it, because that activity was never instrumented for exposure; otherwise
``not_opened``. Only ``not_opened`` supports a confirmatory claim. The
application cannot prove the owner never saw the market outside it, so the
strongest state means "not opened in recorded research", never "unseen".
Reference: PRD https://github.com/tim1016/learn-ai/issues/2696 "Open one final test".
Canonical implementation: this file.
Validated against: tests/research/golden_search/test_exposure_rules.py.
"""

from __future__ import annotations

from typing import Literal

ExposureState = Literal["not_opened", "previously_used", "history_unknown"]
Claim = Literal["confirmatory", "exploratory"]

EXPOSURE_EXPLANATIONS: dict[ExposureState, str] = {
    "not_opened": "Not opened in recorded research: no study has used this interval for this symbol.",
    "previously_used": "Previously used: a final test on this symbol already opened part of this interval.",
    "history_unknown": (
        "History unknown: other research on this symbol covered part of this interval, so it cannot count as fresh."
    ),
}


def exposure_state(*, ledger_overlaps: int, outside_activity_overlaps: int) -> ExposureState:
    """Ledger reuse outranks unknown outside activity; only an interval touched by neither is not opened."""
    if ledger_overlaps < 0 or outside_activity_overlaps < 0:
        raise ValueError("overlap counts cannot be negative")
    if ledger_overlaps > 0:
        return "previously_used"
    if outside_activity_overlaps > 0:
        return "history_unknown"
    return "not_opened"


def claim_for(state: ExposureState) -> Claim:
    """A final test is confirmatory only over an interval not opened in recorded research."""
    return "confirmatory" if state == "not_opened" else "exploratory"
