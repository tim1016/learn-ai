"""Debounce table for the stream-health account hold (#1777 WP4).

The timing promises live here rather than in the scheduler so they are
provable without a clock, a repository, or a broker: raise after 2
consecutive unhealthy observations, release on 1 healthy one.

There is no replay case to test. Every observation is a fresh pull (see
the sampling contract in ``hold_debounce``'s module docstring), so the
table carries no sample identity to defend.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.hold_debounce import (
    HoldDebounceState,
    advance_hold_debounce,
)


def test_one_unhealthy_observation_does_not_raise_the_hold() -> None:
    """The 5 s blip that froze entries account-wide (S10) must not raise."""
    step = advance_hold_debounce(HoldDebounceState(), "unhealthy")

    assert step.action == "none"
    assert step.state.consecutive_unhealthy == 1


def test_two_consecutive_unhealthy_observations_raise_the_hold() -> None:
    first = advance_hold_debounce(HoldDebounceState(), "unhealthy")
    second = advance_hold_debounce(first.state, "unhealthy")

    assert second.action == "raise"


def test_one_healthy_observation_releases_the_hold() -> None:
    raised = advance_hold_debounce(
        advance_hold_debounce(HoldDebounceState(), "unhealthy").state,
        "unhealthy",
    )

    released = advance_hold_debounce(raised.state, "healthy")

    assert released.action == "release"
    assert released.state.consecutive_unhealthy == 0


def test_an_unknown_sample_neither_raises_nor_releases() -> None:
    """A provider that could not produce an observation proves nothing.

    Absence of evidence is not evidence of health -- nor of failure.
    """
    from_clear = advance_hold_debounce(HoldDebounceState(), "unknown")
    mid_run = advance_hold_debounce(
        advance_hold_debounce(HoldDebounceState(), "unhealthy").state,
        "unknown",
    )

    assert from_clear.action == "none"
    assert mid_run.action == "none"
    assert mid_run.state.consecutive_unhealthy == 1


def test_a_healthy_observation_resets_the_unhealthy_run() -> None:
    """Recovery must cost a fresh pair of failures to raise again."""
    one_bad = advance_hold_debounce(HoldDebounceState(), "unhealthy")
    recovered = advance_hold_debounce(one_bad.state, "healthy")

    next_bad = advance_hold_debounce(recovered.state, "unhealthy")

    assert next_bad.action == "none", "a single failure after recovery must not re-raise"


def test_an_unknown_sample_does_not_break_an_unhealthy_run() -> None:
    """A momentarily absent provider must not reset progress toward a raise.

    Two unhealthy observations either side of one `unknown` still raise:
    `unknown` holds the line, it does not undo what was already observed.
    """
    state = HoldDebounceState()
    actions = []
    for verdict in ("unhealthy", "unknown", "unhealthy"):
        step = advance_hold_debounce(state, verdict)
        state, _ = step.state, actions.append(step.action)

    assert actions == ["none", "none", "raise"]


def test_a_persisting_outage_keeps_asking_for_the_raise() -> None:
    """Idempotency lives in the repository, not here.

    Every unhealthy sample past the threshold re-asserts "raise"; the
    ledger's own append-on-change-only check is what stops the churn. That
    split keeps this table stateless about what was already written.
    """
    state = HoldDebounceState()
    actions = []
    for _ in range(4):
        step = advance_hold_debounce(state, "unhealthy")
        state, _ = step.state, actions.append(step.action)

    assert actions == ["none", "raise", "raise", "raise"]
