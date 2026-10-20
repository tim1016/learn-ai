"""Debounce table for the stream-health account hold (#1777 WP4).

The timing promises live here rather than in the scheduler so they are
provable without a clock, a repository, or a broker: raise after 2
consecutive unhealthy observations, release on 1 healthy one.

There is no replay case to test. Every observation is a fresh pull (see
the sampling contract in ``hold_debounce``'s module docstring), so the
table carries no sample identity to defend.
"""

from __future__ import annotations

import pytest

from app.broker.alpaca.clerk.hold_debounce import (
    HoldDebounceState,
    HoldSampleVerdict,
    HoldSyncAction,
    advance_hold_debounce,
)


@pytest.mark.parametrize(
    ("verdicts", "expected_actions", "expected_unhealthy_run"),
    [
        # The 5 s blip that froze entries account-wide (S10) must not raise.
        pytest.param(("unhealthy",), ("none",), 1, id="one unhealthy observation does not raise the hold"),
        pytest.param(
            ("unhealthy", "unhealthy"),
            ("none", "raise"),
            2,
            id="two consecutive unhealthy observations raise the hold",
        ),
        pytest.param(
            ("unhealthy", "unhealthy", "healthy"),
            ("none", "raise", "release"),
            0,
            id="one healthy observation releases the hold",
        ),
        # A provider that could not produce an observation proves nothing.
        # Absence of evidence is not evidence of health -- nor of failure.
        pytest.param(("unknown",), ("none",), 0, id="an unknown sample from clear neither raises nor releases"),
        pytest.param(
            ("unhealthy", "unknown"),
            ("none", "none"),
            1,
            id="an unknown sample mid run neither raises nor releases",
        ),
        # Recovery must cost a fresh pair of failures to raise again.
        pytest.param(
            ("unhealthy", "healthy", "unhealthy"),
            ("none", "release", "none"),
            1,
            id="a healthy observation resets the unhealthy run",
        ),
        # A momentarily absent provider must not reset progress toward a raise:
        # `unknown` holds the line, it does not undo what was already observed.
        pytest.param(
            ("unhealthy", "unknown", "unhealthy"),
            ("none", "none", "raise"),
            2,
            id="an unknown sample does not break an unhealthy run",
        ),
        # Idempotency lives in the repository, not here. Every unhealthy sample
        # past the threshold re-asserts "raise"; the ledger's own
        # append-on-change-only check is what stops the churn. That split keeps
        # this table stateless about what was already written.
        pytest.param(
            ("unhealthy", "unhealthy", "unhealthy", "unhealthy"),
            ("none", "raise", "raise", "raise"),
            4,
            id="a persisting outage keeps asking for the raise",
        ),
    ],
)
def test_the_hold_debounce_table(
    verdicts: tuple[HoldSampleVerdict, ...], expected_actions: tuple[HoldSyncAction, ...], expected_unhealthy_run: int
) -> None:
    state = HoldDebounceState()
    actions = []
    for verdict in verdicts:
        step = advance_hold_debounce(state, verdict)
        state = step.state
        actions.append(step.action)

    assert tuple(actions) == expected_actions, "the action taken at each observation"
    assert state.consecutive_unhealthy == expected_unhealthy_run, "the unhealthy run left after the last observation"
