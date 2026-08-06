"""Shared deterministic helpers for SQLite Account Clerk tests."""

from __future__ import annotations

from app.broker.alpaca.clerk.sqlite.facts import AccountHoldRaisedFacts
from app.broker.alpaca.clerk.sqlite.models import TransitionInput


class _TestClock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def advance(self, delta_ms: int) -> None:
        self.value += delta_ms


def _clock_at(start_ms: int) -> _TestClock:
    return _TestClock(start_ms)


def _hold_transition(
    *,
    reason_code: str = "UNEXPLAINED_ORDER",
    evidence_refs: list[str] | None = None,
) -> TransitionInput:
    facts = AccountHoldRaisedFacts(
        reason_code=reason_code,
        evidence_refs=evidence_refs or ["bo-1"],
    )
    return TransitionInput(
        transition_kind="ACCOUNT_HOLD_RAISED",
        custody_owner="ACCOUNT_CLERK",
        execution_authority="ACCOUNT_CLERK",
        operation_state="succeeded",
        clerk_observed_at_ms=1,
        summary_code="ACCOUNT_HOLD_RAISED",
        facts_json=facts.to_facts_json(),
    )
