"""The bot page's one-line run summary is a pure function of its facts (#2794 R1)."""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.fills import FillRecord
from app.schemas.bot_page import HeldPositionFact, RunSummaryFacts
from app.services.broker_v2_panel.bot_page_projection import run_facts, run_summary_text

# Wed Sep 30 2026, 14:30 and 15:59 ET; the line is written that evening, or a year later.
_START = 1_790_793_000_000
_END = 1_790_798_340_000
_WRITTEN = _END + 3_600_000
_A_YEAR_LATER = _END + 366 * 86_400_000


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
        "authored_at_ms": _WRITTEN,
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
            {"ended_at_ms": _END + 86_400_000, "authored_at_ms": _A_YEAR_LATER},
            "Ran Wed Sep 30 2026, 14:30 ET – Thu Oct 1 2026, 15:59 ET · ended on schedule · 5 decisions, "
            "no trades · $800.00 back to the account.",
        ),
        (
            # A run whose end recorded no outcome claims no cause.
            {"ending": "ended"},
            "Ran Wed Sep 30, 14:30–15:59 ET · ended · 5 decisions, no trades · $800.00 back to the account.",
        ),
        (
            # A run past the Clerk's decision retention counts at least what it kept.
            {"decision_count": 1000, "decision_count_is_floor": True, "trade_count": 1234},
            "Ran Wed Sep 30, 14:30–15:59 ET · ended on schedule · at least 1,000 decisions, 1,234 trades · "
            "$800.00 back to the account.",
        ),
        ({"ending": "not_started", "started_at_ms": None, "ended_at_ms": None}, "Not started yet."),
        (
            {"ending": "unreadable", "started_at_ms": None, "ended_at_ms": None},
            "This bot's latest run record could not be read.",
        ),
    ],
)
def test_the_summary_line_is_written_from_its_facts(overrides: dict[str, object], text: str) -> None:
    facts = _facts(**overrides)

    assert run_summary_text(facts) == text
    # The same facts give the same line.
    assert run_summary_text(RunSummaryFacts.model_validate(facts.model_dump())) == text


def _fill(event_key: str, quantity: float) -> FillRecord:
    return FillRecord(
        account_id="PA1", sid="spy-bot", intent_id="intent-1", order_ref="alpaca/intent-1", event_key=event_key,
        symbol="SPY", side="buy", quantity=quantity, fill_price=100.0, filled_at_ms=_START, fee=None,
    )


def test_each_effective_fill_is_a_trade_as_history_counts_it() -> None:
    """Codex on #2804: an order filled in two parts is two trades, as Home and History count them."""
    facts = run_facts(
        None,
        run_fills=[_fill("execution:e-1", 0.5), _fill("execution:e-2", 0.5)],
        activity=None,
        budget=None,
        exit_in_progress=False,
    )

    assert facts.trade_count == 2
