"""Extended-hours runs are admitted only when the active authority can clock and price them."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.schemas.run_admission import ExtendedHoursAdmissionFact
from app.services.bot_start_admission import extended_hours_admission_fact
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("10"))


@pytest.mark.parametrize(
    ("use_rth", "policy", "state"),
    [
        (True, ProgramLegPolicy.regular_only(), "NOT_REQUESTED"),
        # #2440 owner decisions 2026-09-25: a regular-hours run's last-bar exit
        # is an after-hours limit priced from the exit allowance, so a paper or
        # sim authority (a declared window, no allowance) has its own state —
        # refused on Start and on a flat Resume, never on a holding one.
        (True, ProgramLegPolicy(window=_WINDOW, allowances=None), "NOT_REQUESTED"),
        (True, ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES), "NOT_REQUESTED"),
        (False, ProgramLegPolicy.regular_only(), "UNSUPPORTED"),
        (False, ProgramLegPolicy(window=_WINDOW, allowances=None), "ALLOWANCE_UNSET"),
        (False, ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES), "READY"),
    ],
)
def test_extended_hours_admission_fact(use_rth: bool, policy: ProgramLegPolicy, state: str) -> None:
    fact = extended_hours_admission_fact(exit_terms=DEPLOY_EXIT_TERMS, use_rth=use_rth, policy=policy, observed_at_ms=1_788_361_200_000)
    assert fact.state == state
    assert fact.observed_at_ms == 1_788_361_200_000


@pytest.mark.parametrize("state", ["READY", "NOT_REQUESTED"])
def test_an_admitted_fact_cannot_carry_a_refusal(state: str) -> None:
    from pydantic import ValidationError

    from app.broker.alpaca.clerk.program_leg import EXTENDED_HOURS_ALLOWANCE_UNSET

    with pytest.raises(ValidationError, match="cannot carry a refusal"):
        ExtendedHoursAdmissionFact(state=state, observed_at_ms=1_000, refusal=EXTENDED_HOURS_ALLOWANCE_UNSET)


@pytest.mark.parametrize('point,allowed', [('pre', True), ('rth', True), ('close', False), ('night', False), ('holiday', False)])
@pytest.mark.parametrize('mode', ['trade', 'dry_run'])
def test_calendar_window_is_shared_by_readiness_and_start(point, allowed, mode):
    from datetime import date

    from app.services.broker_v2_panel import paper_deploy_service
    from app.services.run_admission import evaluate_run_admission
    from app.services.session_authority import scheduled_extended_session_bounds
    from tests.services.test_run_admission import _bot, _clerk

    bounds = scheduled_extended_session_bounds(date(2026, 11, 27))  # early close
    holiday = scheduled_extended_session_bounds(date(2026, 11, 25))
    now = {'pre': bounds.open_ms, 'rth': bounds.rth_open_ms, 'close': bounds.rth_close_ms,
           'night': bounds.open_ms - 1, 'holiday': holiday.open_ms + 86_400_000}[point]
    fact = extended_hours_admission_fact(exit_terms=DEPLOY_EXIT_TERMS, use_rth=True, policy=ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES), observed_at_ms=now)
    bot = _bot(observed_at_ms=now, mode=mode, liveness_state='CLOSED' if point == 'pre' else 'TRADABLE').model_copy(update={'extended_hours': fact, 'start_window': paper_deploy_service.deploy_window(now)})
    decision = evaluate_run_admission(bot, _clerk(observed_at_ms=now), evaluated_at_ms=now)
    assert decision.allowed is (allowed or mode == 'dry_run'), decision.explanation
    assert (paper_deploy_service.deploy_window(now).state != "CLOSED") is allowed
    if not allowed and mode == 'trade':
        assert decision.reason_code == 'DEPLOY_WINDOW_CLOSED'
        assert 'ET' in decision.next_step
        assert bot.start_window.next_open_ms > now


def test_explicit_exit_terms_do_not_invent_an_extended_entry_allowance():
    fact = extended_hours_admission_fact(use_rth=False, policy=ProgramLegPolicy(window=_WINDOW, allowances=None),
        observed_at_ms=1_788_361_200_000, exit_terms=DEPLOY_EXIT_TERMS)
    assert fact.state == "ALLOWANCE_UNSET"


def test_new_start_needs_explicit_exit_terms_even_with_account_defaults():
    fact = extended_hours_admission_fact(use_rth=True, policy=ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES),
        observed_at_ms=1_788_361_200_000, exit_terms=None)
    assert fact.state == "EXIT_ALLOWANCE_UNSET"
