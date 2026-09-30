"""The Start window's words and the Deploy exit steps' words, in the owner's ET (#2665).

Both name their instants through ``app.utils.timestamps.et_when_words``, so
they read like a bot's end: ``Thu Oct 1, 04:00 ET``, with the year when it is
not now's. Every clock is ``int64 ms UTC`` built from an ET wall clock through
the NY zone; the session times come from the canonical calendar.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from app.services.broker_v2_panel.paper_deploy_service import exit_steps_summary
from app.services.deploy_window import deploy_window, start_window_next_step
from app.services.run_admission import evaluate_run_admission
from app.utils.timestamps import to_ms_utc
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.services.test_run_admission import _bot, _clerk

_ET = ZoneInfo("America/New_York")


def _et(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return to_ms_utc(datetime(year, month, day, hour, minute, tzinfo=_ET))


@pytest.mark.parametrize(
    ("now_ms", "next_step"),
    [
        pytest.param(
            _et(2026, 9, 30, 20, 30), "Next Start or Resume window opens Thu Oct 1, 04:00 ET.", id="tomorrow",
        ),
        # New Year's Day 2027 is a Friday holiday: the next session is Monday, in another year.
        pytest.param(
            _et(2026, 12, 31, 20, 30), "Next Start or Resume window opens Mon Jan 4 2027, 04:00 ET.", id="next-year",
        ),
    ],
)
def test_start_window_next_step_names_the_next_open_in_the_owners_et_words(now_ms: int, next_step: str) -> None:
    window = deploy_window(now_ms)

    assert window.state == "CLOSED"
    assert start_window_next_step(window, now_ms=now_ms) == next_step


def test_a_closed_window_admission_words_the_next_open_as_of_its_own_evaluation() -> None:
    """The year rule reads the admission's ``evaluated_at_ms``, never a fresh clock."""
    now = _et(2026, 12, 31, 20, 30)
    bot = _bot(observed_at_ms=now).model_copy(update={"start_window": deploy_window(now)})

    decision = evaluate_run_admission(bot, _clerk(observed_at_ms=now), evaluated_at_ms=now)

    assert decision.reason_code == "DEPLOY_WINDOW_CLOSED"
    assert decision.next_step == "Next Start or Resume window opens Mon Jan 4 2027, 04:00 ET."


def test_start_window_next_step_while_the_window_is_open() -> None:
    now = _et(2026, 9, 30, 10)

    assert start_window_next_step(deploy_window(now), now_ms=now) == "Start is allowed in the current session."


def test_exit_steps_summary_names_each_step_in_the_owners_et_words() -> None:
    summary = exit_steps_summary(DEPLOY_EXIT_TERMS, _et(2026, 9, 21, 9, 53))

    assert summary == (
        "Regular close (Mon Sep 21, 16:00 ET): limit at decision close minus 20 bps. "
        "After-hours ends Mon Sep 21, 20:00 ET. "
        "Next pre-market (Tue Sep 22, 04:00 ET): limit at bid minus 20 bps; hold if the spread exceeds 50 bps. "
        "Next regular open (Tue Sep 22, 09:30 ET): cancel the unfilled Clerk-priced limit, confirm cancellation, "
        "then sell the remaining quantity at market. A confirmed halt holds exits."
    )
