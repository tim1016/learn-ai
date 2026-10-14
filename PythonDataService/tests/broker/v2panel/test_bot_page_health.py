"""The bot page's "Last check against Alpaca" health line goes overdue with the periodic sweep (#3165).

The verdict comes from durable uncertainty rows, not from whether the
reconciliation sweep is alive, so a dead or backed-off sweep left a green
"Clean" beside a frozen time. The line ages by the sweep's own last pass, not
by the last check of any kind: a "Reconcile now" on a lane whose sweep is dead
is a fresh answer and still no periodic check. The line is built from the same
projection the panel serves: a ``ClerkStatus`` through ``build_clerk_card``.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk.models import ClerkStatus, HoldState, ReconciliationSummary
from app.broker.alpaca.clerk.sqlite.recovery_policy import SWEEP_LIVENESS_BOUND_MS
from app.schemas.bot_page import HealthLineView
from app.services.broker_v2_panel.bot_page_projection import health_groups
from app.services.broker_v2_panel.panel_projection_service import build_clerk_card

_NOW = 1_790_798_340_000
_LAST_CHECK = _NOW - 1_000


def _reconciliation_line(
    *, verdict: str | None, checked_at_ms: int | None, sweep_pass_at_ms: int | None
) -> HealthLineView:
    status = ClerkStatus(
        broker="alpaca",
        account_id="PA-TEST",
        hold=HoldState(active=False),
        latest_reconciliation=(
            None
            if verdict is None or checked_at_ms is None
            else ReconciliationSummary(verdict=verdict, recorded_at_ms=checked_at_ms)  # type: ignore[arg-type]
        ),
        outstanding_intents=0,
        observed_at_ms=_NOW,
    )
    panel = SimpleNamespace(
        clerk=build_clerk_card(status, _NOW),
        health=SimpleNamespace(running=True, last_decision_at_ms=None, decision_stale=False),
        feed_continuity=SimpleNamespace(
            provider_label="IBKR", state="continuous", state_label="Live", explanation="Bars are arriving."
        ),
    )
    run = SimpleNamespace(decision_count=0, decision_count_is_floor=False, started_at_ms=None, orders_sent=0)
    groups = health_groups(panel, run, sweep_pass_at_ms=sweep_pass_at_ms, now_ms=_NOW)  # type: ignore[arg-type]
    [line] = [line for line in groups.account if line.key == "reconciliation"]
    return line


def test_a_clean_check_with_a_pass_inside_the_sweep_liveness_bound_reads_ok() -> None:
    line = _reconciliation_line(
        verdict="clean", checked_at_ms=_LAST_CHECK, sweep_pass_at_ms=_NOW - SWEEP_LIVENESS_BOUND_MS
    )

    assert (line.state, line.value, line.note) == ("ok", "Clean", None)
    assert line.at_ms == _LAST_CHECK


def test_a_clean_check_whose_last_pass_is_older_than_the_bound_is_overdue_even_if_just_made() -> None:
    line = _reconciliation_line(
        verdict="clean", checked_at_ms=_LAST_CHECK, sweep_pass_at_ms=_NOW - SWEEP_LIVENESS_BOUND_MS - 1
    )

    assert (line.state, line.value) == ("attention", "Overdue")
    assert line.note is not None and "Clean" in line.note
    assert line.at_ms == _LAST_CHECK


def test_a_lane_with_no_periodic_pass_never_goes_overdue() -> None:
    """A Dry Run's own account has no sweep: it is checked at Start, at its end and on Reconcile now."""
    line = _reconciliation_line(
        verdict="clean", checked_at_ms=_NOW - 10 * SWEEP_LIVENESS_BOUND_MS, sweep_pass_at_ms=None
    )

    assert (line.state, line.value, line.note) == ("ok", "Clean", None)


def test_a_pass_stamped_after_now_is_not_old() -> None:
    line = _reconciliation_line(verdict="clean", checked_at_ms=_LAST_CHECK, sweep_pass_at_ms=_NOW + 5_000)

    assert line.state == "ok"


@pytest.mark.parametrize("verdict", ["stale", "unexplained_order", "missing_intent"])
def test_a_verdict_that_is_not_clean_keeps_its_own_words_however_old_the_pass(verdict: str) -> None:
    fresh = _reconciliation_line(verdict=verdict, checked_at_ms=_LAST_CHECK, sweep_pass_at_ms=_LAST_CHECK)
    old = _reconciliation_line(
        verdict=verdict, checked_at_ms=_LAST_CHECK, sweep_pass_at_ms=_NOW - 10 * SWEEP_LIVENESS_BOUND_MS
    )

    assert fresh.state == old.state == "attention"
    assert fresh.value == old.value != "Overdue"
    assert old.note is None


def test_a_check_never_made_stays_idle() -> None:
    line = _reconciliation_line(verdict=None, checked_at_ms=None, sweep_pass_at_ms=None)

    assert (line.state, line.value, line.at_ms) == ("idle", "Not checked yet", None)
