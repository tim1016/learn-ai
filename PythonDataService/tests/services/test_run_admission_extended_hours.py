"""Extended-hours runs are admitted only when the active authority can clock and price them."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.capabilities import ExtendedHoursWindow
from app.schemas.run_admission import ExtendedHoursAdmissionFact
from app.services.bot_start_admission import extended_hours_admission_fact

_WINDOW = ExtendedHoursWindow(open_minute_et=4 * 60, close_minute_et=20 * 60)
_ALLOWANCES = ExtendedHoursAllowances(entry_bps=Decimal("10"), exit_bps=Decimal("10"))


@pytest.mark.parametrize(
    ("use_rth", "policy", "state"),
    [
        (True, ProgramLegPolicy.regular_only(), "NOT_REQUESTED"),
        (False, ProgramLegPolicy.regular_only(), "UNSUPPORTED"),
        (False, ProgramLegPolicy(window=_WINDOW, allowances=None), "ALLOWANCE_UNSET"),
        (False, ProgramLegPolicy(window=_WINDOW, allowances=_ALLOWANCES), "READY"),
    ],
)
def test_extended_hours_admission_fact(use_rth: bool, policy: ProgramLegPolicy, state: str) -> None:
    fact = extended_hours_admission_fact(use_rth=use_rth, policy=policy, observed_at_ms=1_000)
    assert fact == ExtendedHoursAdmissionFact(state=state, observed_at_ms=1_000)
