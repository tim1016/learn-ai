"""Debounce table for the stream-health account hold (#1777 WP4).

The timing promises live here rather than in the scheduler so they are
provable without a clock, a repository, or a broker: raise after 2
consecutive *fresh* unhealthy observations, release on 1 fresh healthy
one, and never let a replayed observation move either.
"""

from __future__ import annotations

from app.broker.alpaca.clerk.hold_debounce import (
    HoldDebounceState,
    HoldSample,
    advance_hold_debounce,
)


def _unhealthy(observed_at_ms: int) -> HoldSample:
    return HoldSample(verdict="unhealthy", observed_at_ms=observed_at_ms)


def _healthy(observed_at_ms: int) -> HoldSample:
    return HoldSample(verdict="healthy", observed_at_ms=observed_at_ms)


def test_one_unhealthy_observation_does_not_raise_the_hold() -> None:
    """The 5 s blip that froze entries account-wide (S10) must not raise."""
    step = advance_hold_debounce(HoldDebounceState(), _unhealthy(1_000))

    assert step.action == "none"
    assert step.state.consecutive_unhealthy == 1


def test_two_consecutive_fresh_unhealthy_observations_raise_the_hold() -> None:
    first = advance_hold_debounce(HoldDebounceState(), _unhealthy(1_000))
    second = advance_hold_debounce(first.state, _unhealthy(16_000))

    assert second.action == "raise"


def test_a_replayed_observation_never_advances_the_debounce() -> None:
    """Sample identity: only a strictly newer observation is new evidence.

    A provider that keeps handing back the same stale reading must never
    accumulate its way to a raise.
    """
    first = advance_hold_debounce(HoldDebounceState(), _unhealthy(1_000))

    replay = advance_hold_debounce(first.state, _unhealthy(1_000))
    older = advance_hold_debounce(replay.state, _unhealthy(500))

    assert replay.action == "none"
    assert older.action == "none"
    assert older.state.consecutive_unhealthy == 1


def test_one_fresh_healthy_observation_releases_the_hold() -> None:
    raised = advance_hold_debounce(
        advance_hold_debounce(HoldDebounceState(), _unhealthy(1_000)).state,
        _unhealthy(16_000),
    )

    released = advance_hold_debounce(raised.state, _healthy(31_000))

    assert released.action == "release"
    assert released.state.consecutive_unhealthy == 0


def test_an_unknown_sample_neither_raises_nor_releases() -> None:
    """A provider that cannot produce a fresh observation proves nothing.

    Absence of evidence is not evidence of health -- nor of failure.
    """
    unknown = HoldSample(verdict="unknown", observed_at_ms=None)

    from_clear = advance_hold_debounce(HoldDebounceState(), unknown)
    mid_run = advance_hold_debounce(
        advance_hold_debounce(HoldDebounceState(), _unhealthy(1_000)).state,
        unknown,
    )

    assert from_clear.action == "none"
    assert mid_run.action == "none"
    assert mid_run.state.consecutive_unhealthy == 1


def test_a_healthy_observation_resets_the_unhealthy_run() -> None:
    """Recovery must cost a fresh pair of failures to raise again."""
    one_bad = advance_hold_debounce(HoldDebounceState(), _unhealthy(1_000))
    recovered = advance_hold_debounce(one_bad.state, _healthy(16_000))

    next_bad = advance_hold_debounce(recovered.state, _unhealthy(31_000))

    assert next_bad.action == "none", "a single failure after recovery must not re-raise"


def test_a_persisting_outage_keeps_asking_for_the_raise() -> None:
    """Idempotency lives in the repository, not here.

    Every fresh unhealthy sample past the threshold re-asserts "raise"; the
    ledger's own append-on-change-only check is what stops the churn. That
    split keeps this table stateless about what was already written.
    """
    state = HoldDebounceState()
    actions = []
    for observed_at_ms in (1_000, 16_000, 31_000, 46_000):
        step = advance_hold_debounce(state, _unhealthy(observed_at_ms))
        state, _ = step.state, actions.append(step.action)

    assert actions == ["none", "raise", "raise", "raise"]
