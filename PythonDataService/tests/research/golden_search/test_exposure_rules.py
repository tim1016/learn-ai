"""Exposure state and the claim it can support."""

from __future__ import annotations

import pytest

from app.research.golden_search.exposure_rules import claim_for, exposure_state


@pytest.mark.parametrize(
    ("ledger", "outside", "state", "claim"),
    [
        (0, 0, "not_opened", "confirmatory"),
        (0, 3, "history_unknown", "exploratory"),
        (1, 0, "previously_used", "exploratory"),
        (2, 5, "previously_used", "exploratory"),
    ],
)
def test_exposure_state_ledger_reuse_outranks_unknown_history(ledger: int, outside: int, state: str, claim: str) -> None:
    observed = exposure_state(ledger_overlaps=ledger, outside_activity_overlaps=outside)

    assert observed == state
    assert claim_for(observed) == claim


def test_exposure_state_refuses_negative_counts() -> None:
    with pytest.raises(ValueError, match="negative"):
        exposure_state(ledger_overlaps=-1, outside_activity_overlaps=0)
