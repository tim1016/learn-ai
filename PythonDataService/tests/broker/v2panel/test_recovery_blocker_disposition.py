"""Disposition authoring for unavailable recovery capabilities (#1778, S17).

The operator-blocker contract says `wait` renders no move: there is nothing
for the operator to do but wait. Every unavailable recovery capability was
authored `wait` regardless of cause -- including stale evidence, whose cure
*is* an operator action. The blocker therefore rendered no move at all for
the one condition an operator could actually fix.

The frontend contract is not what changed here. `wait` correctly renders no
move; the authoring was violating its own contract.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.program_leg import LegRefusal, ProgramLegPolicy
from app.broker.alpaca.clerk.recovery_reduction import flatten_send_verdict
from app.broker.alpaca.clerk.sqlite.projection_models import RecoveryCapability
from app.services.broker_v2_panel.sqlite_panel_adapter import (
    BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR,
    _capability_blocker,
    _panel_action,
)
from app.services.session_authority import SessionAuthorityState
from app.utils.timestamps import to_ms_utc


def _capability(
    *,
    freshness: str,
    reason_code: str = "RECOVERY_EVIDENCE_STALE",
) -> RecoveryCapability:
    return RecoveryCapability(
        action_id="recover_exact_execution_evidence",
        label="Recover exact execution evidence",
        explanation="Re-read the broker's execution record for this run.",
        available=False,
        unavailable_reason_code=reason_code,
        unavailable_reason="The recorded execution evidence is no longer fresh.",
        scope="CUSTODY_SUBJECT",
        freshness=freshness,  # type: ignore[arg-type]
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Reconcile the account, then retry.",
        concurrency_token="token-1",
        execution_ref=None,
        mutation=True,
        primary=False,
    )


def test_stale_evidence_is_fixable_here_and_offers_the_reconcile_move() -> None:
    blocker = _capability_blocker(_capability(freshness="stale"))

    assert blocker.disposition == "fix_here"
    assert blocker.primary_move is not None
    # The move is backend-authored; the frontend never infers a cure from a
    # reason code.
    assert blocker.primary_move.label


def test_a_condition_the_operator_cannot_cure_still_offers_no_move() -> None:
    """Contract regression the other way: `wait` must stay move-less.

    If this ever starts rendering a move, the disposition contract has been
    broken from the authoring side again.
    """
    blocker = _capability_blocker(_capability(freshness="unavailable"))

    assert blocker.disposition == "wait"
    assert blocker.primary_move is None


# ── #2007: the unpriced flatten button outside the regular session ────────────


def _available(action_id: str) -> RecoveryCapability:
    return RecoveryCapability(
        action_id=action_id,
        label="Execute safe flatten",
        explanation="Submit the prepared reduction.",
        available=True,
        unavailable_reason_code=None,
        unavailable_reason=None,
        scope="CUSTODY_SUBJECT",
        freshness="fresh",
        evidence=(),
        reduction_plan=None,
        confirmation=None,
        next_step="Execute the flatten.",
        concurrency_token="token-1",
        execution_ref=None,
        mutation=True,
        primary=False,
    )


_ET = ZoneInfo("America/New_York")
_WEDNESDAY = date(2026, 9, 2)
# The Alpaca paper/live authority declares the 04:00-20:00 window; a ``sim:``
# authority (or any broker declaring none) trades the regular session only.
_WINDOWED = ProgramLegPolicy(window=ALPACA_EXTENDED_HOURS_WINDOW, allowances=None)
_NO_WINDOW = ProgramLegPolicy.regular_only()


def _verdict(hour: int, minute: int, policy: ProgramLegPolicy) -> SessionAuthorityState | LegRefusal:
    """The Clerk's own answer at that ET wall-clock instant on a full-session Wednesday."""
    now_ms = to_ms_utc(
        datetime(_WEDNESDAY.year, _WEDNESDAY.month, _WEDNESDAY.day, hour, minute, tzinfo=_ET)
    )
    return flatten_send_verdict(now_ms=now_ms, policy=policy)


@pytest.mark.parametrize(("hour", "minute"), [(7, 0), (16, 1)], ids=["PRE", "POST"])
def test_in_extended_hours_the_unpriced_flatten_button_moves_to_the_priced_plan(
    hour: int, minute: int
) -> None:
    action = _panel_action(
        _available("execute_safe_flatten"), 7, flatten_verdict=_verdict(hour, minute, _WINDOWED)
    )

    assert action.enabled is False
    (blocker,) = action.blockers
    assert blocker.condition.id == "EXTENDED_HOURS_FLATTEN_NEEDS_A_LIMIT"
    assert blocker.disposition == "fix_here"
    assert blocker.primary_move is not None
    assert blocker.primary_move.action.kind == "confirm_in_form"
    assert blocker.primary_move.action.anchor == BOT_COCKPIT_SAFE_FLATTEN_PREPARE_ANCHOR


def test_with_no_session_open_the_flatten_button_waits_with_no_move() -> None:
    verdict = _verdict(21, 0, _WINDOWED)
    assert isinstance(verdict, LegRefusal)

    action = _panel_action(_available("execute_safe_flatten"), 7, flatten_verdict=verdict)

    assert action.enabled is False
    (blocker,) = action.blockers
    assert blocker.condition.id == "NO_SESSION_OPEN"
    assert blocker.headline == verdict.explanation
    assert blocker.disposition == "wait"
    assert blocker.primary_move is None


def test_after_the_close_on_an_authority_with_no_window_the_button_says_what_the_clerk_says() -> None:
    """At 16:01 after-hours IS open; this authority just cannot price in it (#2440 review).

    The page used to judge the session itself and say no session was open. It
    now shows the Clerk's own refusal, at the Clerk's send instant.
    """
    verdict = _verdict(16, 1, _NO_WINDOW)
    assert isinstance(verdict, LegRefusal)
    assert verdict.reason_code == "EXTENDED_HOURS_PRICING_UNAVAILABLE"

    action = _panel_action(_available("execute_safe_flatten"), 7, flatten_verdict=verdict)

    assert action.enabled is False
    (blocker,) = action.blockers
    assert blocker.condition.id == "EXTENDED_HOURS_PRICING_UNAVAILABLE"
    assert blocker.headline == verdict.explanation
    assert blocker.detail == verdict.next_step
    assert "No trading session would be open" not in blocker.headline
    assert blocker.disposition == "wait"
    assert blocker.primary_move is None
    assert blocker.condition.evidence == {"available_at_ms": verdict.available_at_ms}


def test_inside_the_regular_session_the_flatten_button_is_unchanged() -> None:
    action = _panel_action(
        _available("execute_safe_flatten"), 7, flatten_verdict=_verdict(11, 0, _NO_WINDOW)
    )

    assert action.enabled is True
    assert action.blockers == []


def test_an_unanswered_session_blocks_the_unpriced_flatten_too() -> None:
    """The regular session is the only one this button can succeed in, so it is
    the only one an unanswered session may be assumed to be (CodeRabbit 2026-09-19)."""
    action = _panel_action(_available("execute_safe_flatten"), 7, flatten_verdict=None)

    assert action.enabled is False
    (blocker,) = action.blockers
    assert blocker.condition.id == "FLATTEN_SESSION_UNKNOWN"
    assert blocker.disposition == "fix_here"


def test_the_session_gates_no_other_recovery_action() -> None:
    action = _panel_action(
        _available("reconcile_now"), 7, flatten_verdict=_verdict(21, 0, _WINDOWED)
    )

    assert action.enabled is True
