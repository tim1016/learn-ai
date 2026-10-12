"""What an options backtest setup accepts and refuses."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app.options_backtest.setup import MS_PER_MINUTE, OptionsBacktestSetup, SessionTime

# 2026-10-09, a full session: 09:30 to 16:00 New York time.
_OPEN_MS = 1_791_552_600_000
_CLOSE_MS = _OPEN_MS + 390 * MS_PER_MINUTE
# A half day closes at 13:00.
_HALF_DAY_CLOSE_MS = _OPEN_MS + 210 * MS_PER_MINUTE


def _leg(**changes: Any) -> dict[str, Any]:
    return {
        "leg_id": "call",
        "right": "call",
        "contracts": 1,
        "expiry": {"kind": "this_week"},
        "strike": {"kind": "atm"},
        **changes,
    }


def _setup(**changes: Any) -> dict[str, Any]:
    return {
        "underlying": "SPY",
        "entry": {"anchor": "open", "offset_ms": 5 * MS_PER_MINUTE},
        "exit": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
        "square_off": "complete",
        "legs": [_leg()],
        "fees": {"kind": "not_charged"},
        "starting_cash_usd": "10000",
        **changes,
    }


def test_a_plain_setup_is_taken_with_its_stated_defaults() -> None:
    setup = OptionsBacktestSetup.model_validate(_setup())

    assert setup.version == 1
    assert setup.slippage_percent == Decimal("0.3")
    assert setup.price_step_usd == Decimal("0.01")
    assert setup.no_reentry_after is None
    assert setup.legs[0].stop_loss is None


def test_a_setup_reads_back_as_it_was_written() -> None:
    written = _setup(
        legs=[
            _leg(
                stop_loss={"kind": "percent", "value": "25"},
                momentum={"kind": "percent_up", "value": "25"},
                reentry_on_stop={"kind": "on_momentum", "count": 1},
            )
        ],
        no_reentry_after={"anchor": "close", "offset_ms": 60 * MS_PER_MINUTE},
    )

    setup = OptionsBacktestSetup.model_validate(written)

    assert OptionsBacktestSetup.model_validate_json(setup.model_dump_json()) == setup


def test_times_keep_their_distance_from_the_open_and_the_close_on_a_half_day() -> None:
    entry = SessionTime(anchor="open", offset_ms=5 * MS_PER_MINUTE)
    exit_ = SessionTime(anchor="close", offset_ms=46 * MS_PER_MINUTE)

    assert entry.at(session_open_ms=_OPEN_MS, session_close_ms=_HALF_DAY_CLOSE_MS) == _OPEN_MS + 5 * MS_PER_MINUTE
    assert exit_.at(session_open_ms=_OPEN_MS, session_close_ms=_CLOSE_MS) == _CLOSE_MS - 46 * MS_PER_MINUTE
    assert (
        exit_.at(session_open_ms=_OPEN_MS, session_close_ms=_HALF_DAY_CLOSE_MS)
        == _HALF_DAY_CLOSE_MS - 46 * MS_PER_MINUTE
    )


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"legs": []}, "no leg"),
        ({"legs": [_leg(), _leg()]}, "two legs with one id"),
        ({"legs": [_leg(leg_id=f"leg_{n}") for n in range(9)]}, "more legs than a setup holds"),
        ({"underlying": "spy"}, "an underlying that is not an option root"),
        ({"entry": {"anchor": "open", "offset_ms": 90_000}}, "a time that is not a whole minute"),
        ({"entry": {"anchor": "open", "offset_ms": -MS_PER_MINUTE}}, "a time before its anchor"),
        ({"slippage_percent": "-0.1"}, "slippage in the trader's favour"),
        ({"price_step_usd": "0"}, "no price step"),
        ({"starting_cash_usd": "0"}, "no cash"),
        ({"fees": {"kind": "per_contract"}}, "a per-contract policy with no fee"),
        ({"fees": {"kind": "not_charged", "per_contract_usd": "0.05"}}, "a fee on a policy that charges none"),
        ({"square_off": "everything"}, "an unknown square-off"),
        ({"dates": "2026"}, "a field a setup does not have"),
    ],
)
def test_a_setup_that_cannot_run_is_refused(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValidationError):
        OptionsBacktestSetup.model_validate(_setup(**changes))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"contracts": 0}, "no contracts"),
        ({"right": "straddle"}, "neither a call nor a put"),
        ({"stop_loss": {"kind": "percent", "value": "100"}}, "a stop below nothing"),
        ({"stop_loss": {"kind": "points", "value": "0"}}, "a stop of nothing"),
        ({"momentum": {"kind": "percent_up", "value": "-5"}}, "momentum of less than nothing"),
        ({"reentry_on_stop": {"kind": "at_once", "count": 0}}, "a re-entry that never happens"),
        ({"reentry_on_stop": {"kind": "at_once", "count": 21}}, "more re-entries than a leg may have"),
        ({"reentry_on_stop": {"kind": "on_momentum", "count": 1}}, "re-entry on momentum with no momentum rule"),
        ({"strike": {"kind": "atm_offset", "steps": 0}}, "steps from the money that go nowhere"),
        ({"expiry": {"kind": "min_days", "days": -1}}, "an expiry before the trading date"),
        ({"expiry": {"kind": "monthly"}}, "an expiry rule the backtest does not have"),
    ],
)
def test_a_leg_that_cannot_trade_is_refused(changes: dict[str, Any], reason: str) -> None:
    with pytest.raises(ValidationError):
        OptionsBacktestSetup.model_validate(_setup(legs=[_leg(**changes)]))


def test_a_leg_may_re_enter_at_once_without_a_momentum_rule() -> None:
    setup = OptionsBacktestSetup.model_validate(
        _setup(legs=[_leg(stop_loss={"kind": "percent", "value": "25"}, reentry_on_stop={"kind": "at_once", "count": 2})])
    )

    assert setup.legs[0].reentry_on_stop is not None
    assert setup.legs[0].reentry_on_stop.count == 2
