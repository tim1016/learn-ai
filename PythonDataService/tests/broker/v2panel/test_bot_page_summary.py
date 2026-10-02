"""The bot page's one-line run summary is a pure function of its facts (#2794 R1)."""

from __future__ import annotations

import pytest

from app.schemas.bot_page import HeldPositionFact, RunSummaryFacts
from app.services.broker_v2_panel.bot_page_projection import run_summary_text

# Wed Sep 30 2026, 14:30 and 15:59 ET.
_START = 1_790_793_000_000
_END = 1_790_798_340_000


def _facts(**overrides: object) -> RunSummaryFacts:
    values: dict[str, object] = {
        "run_id": "run-1",
        "started_at_ms": _START,
        "ended_at_ms": _END,
        "scheduled_end_at_ms": None,
        "ending": "on_schedule",
        "decision_count": 5,
        "trade_count": 0,
        "set_aside_usd": None,
        "returned_usd": "800.00",
        "held": [],
        "exit_queued": False,
        "current_year": 2026,
    }
    return RunSummaryFacts.model_validate(values | overrides)


@pytest.mark.parametrize(
    ("overrides", "text"),
    [
        (
            {},
            "Ran Wed Sep 30, 14:30–15:59 ET · ended on schedule · 5 decisions, no trades · $800.00 back to the account.",
        ),
        (
            {"ending": "stopped", "decision_count": 1, "trade_count": 2, "returned_usd": None,
             "held": [HeldPositionFact(symbol="SPY", quantity="1")], "exit_queued": True},
            "Ran Wed Sep 30, 14:30–15:59 ET · stopped · 1 decision, 2 trades · holds 1 SPY · a sale is queued.",
        ),
        (
            {"ending": "running", "ended_at_ms": None, "scheduled_end_at_ms": _END, "returned_usd": None,
             "set_aside_usd": "800.00", "decision_count": 0},
            "Running since Wed Sep 30, 14:30 ET · ends 15:59 ET · no decisions, no trades · $800.00 set aside.",
        ),
        (
            # A stop that recorded no release says nothing about the money.
            {"ending": "crashed", "returned_usd": None},
            "Ran Wed Sep 30, 14:30–15:59 ET · crashed · 5 decisions, no trades.",
        ),
        (
            # A run that ended on a later day names that day; a run from another year names its year.
            {"ended_at_ms": _END + 86_400_000, "current_year": 2027},
            "Ran Wed Sep 30 2026, 14:30 ET – Thu Oct 1 2026, 15:59 ET · ended on schedule · 5 decisions, "
            "no trades · $800.00 back to the account.",
        ),
        ({"ending": "not_started", "started_at_ms": None, "ended_at_ms": None}, "Not started yet."),
    ],
)
def test_the_summary_line_is_written_from_its_facts(overrides: dict[str, object], text: str) -> None:
    facts = _facts(**overrides)

    assert run_summary_text(facts) == text
    # The same facts give the same line.
    assert run_summary_text(RunSummaryFacts.model_validate(facts.model_dump())) == text
