"""Unit coverage for app.services.broker_capability_service.extended_phase_proven_at_ms (#1671)."""

from __future__ import annotations

import pytest

import app.services.broker_capability_service as broker_capability_service
from app.services.broker_capability_service import extended_phase_proven_at_ms
from app.services.session_authority import SessionAuthorityState

_NOW = 1_700_000_000_000


def _session(
    *,
    phase: str,
    extended_phase_proven: bool,
    permits_strategy_activity: bool = False,
) -> SessionAuthorityState:
    return SessionAuthorityState(
        phase=phase,
        permits_strategy_activity=permits_strategy_activity,
        next_transition_ms=None,
        timezone="America/New_York",
        as_of_ms=_NOW,
        source="ibkr_capability",
        extended_phase_proven=extended_phase_proven,
    )


@pytest.mark.parametrize("phase", ["PRE", "POST", "OVERNIGHT"])
def test_proven_extended_phase_returns_true(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    monkeypatch.setattr(
        broker_capability_service,
        "session_state_at_ms",
        lambda **_kwargs: _session(phase=phase, extended_phase_proven=True),
    )

    assert extended_phase_proven_at_ms(now_ms=_NOW, symbol="SPY", account_id="ibkr-acct") is True


def test_provenance_true_but_phase_closed_does_not_count_as_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: a fresh, matching capability snapshot (extended_phase_proven
    True) can still resolve to CLOSED — e.g. an emergency RTH closure, or an
    instant that falls outside every known window in the snapshot. That
    provenance flag alone must never be read as "extended hours proven," or
    a running use_rth=False bot could override genuinely CLOSED evidence."""
    monkeypatch.setattr(
        broker_capability_service,
        "session_state_at_ms",
        lambda **_kwargs: _session(phase="CLOSED", extended_phase_proven=True),
    )

    assert extended_phase_proven_at_ms(now_ms=_NOW, symbol="SPY", account_id="ibkr-acct") is False


def test_provenance_true_but_phase_rth_does_not_count_as_extended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        broker_capability_service,
        "session_state_at_ms",
        lambda **_kwargs: _session(phase="RTH", extended_phase_proven=True),
    )

    assert extended_phase_proven_at_ms(now_ms=_NOW, symbol="SPY", account_id="ibkr-acct") is False


def test_no_account_id_returns_false_without_a_capability_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every caller resolves account_id from something that can come up
    empty (a feed with no capability identity, a panel rendered before an
    account is known) — this must fail closed on None without even
    resolving session_state_at_ms, so a caller can pass its raw, possibly
    absent id through directly instead of re-guarding it first."""

    def _fail(**_kwargs: object) -> SessionAuthorityState:
        raise AssertionError("session_state_at_ms must not be called without an account_id")

    monkeypatch.setattr(broker_capability_service, "session_state_at_ms", _fail)

    assert extended_phase_proven_at_ms(now_ms=_NOW, symbol="SPY", account_id=None) is False


def test_unproven_extended_phase_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    """The bare NYSE calendar can resolve PRE/POST/OVERNIGHT-shaped phases
    too in principle, but without a matching capability snapshot the
    provenance flag stays False and this must not treat the calendar alone
    as sufficient proof of extended-session availability."""
    monkeypatch.setattr(
        broker_capability_service,
        "session_state_at_ms",
        lambda **_kwargs: _session(phase="PRE", extended_phase_proven=False),
    )

    assert extended_phase_proven_at_ms(now_ms=_NOW, symbol="SPY", account_id="ibkr-acct") is False
