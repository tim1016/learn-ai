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
        ({"entry": {"anchor": "open", "offset_ms": 0}}, "an entry at the open, which no minute closes at"),
        ({"exit": {"anchor": "close", "offset_ms": 0}}, "an exit at the close, with no minute left to fill in"),
        ({"exit": {"anchor": "open", "offset_ms": 5 * MS_PER_MINUTE}}, "an exit at the entry time"),
        ({"exit": {"anchor": "open", "offset_ms": 4 * MS_PER_MINUTE}}, "an exit before the entry, both from the open"),
        (
            {
                "entry": {"anchor": "close", "offset_ms": 30 * MS_PER_MINUTE},
                "exit": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
            },
            "an exit before the entry, both from the close",
        ),
        ({"entry": {"anchor": "open", "offset_ms": 1440 * MS_PER_MINUTE}}, "a time a whole day from its anchor"),
        (
            {
                "entry": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
                "exit": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
            },
            "an exit at the entry time, both from the close",
        ),
        ({"entry": {"anchor": "close", "offset_ms": MS_PER_MINUTE}}, "an entry that leaves no minute for an exit"),
        (
            {"entry": {"anchor": "close", "offset_ms": 0}, "exit": {"anchor": "open", "offset_ms": 30 * MS_PER_MINUTE}},
            "an entry at the close",
        ),
        ({"exit": {"anchor": "open", "offset_ms": MS_PER_MINUTE}}, "an exit with no minute for an entry before it"),
        ({"slippage_percent": "100"}, "slippage that takes the whole price"),
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
        ({"momentum": {"kind": "percent_down", "value": "100"}}, "a fall that leaves nothing"),
        ({"contracts": 1001}, "more contracts than a leg may have"),
        ({"leg_id": "a" * 33}, "a leg id longer than a leg id is"),
        ({"strike": {"kind": "atm_offset", "steps": 51}}, "more steps than the money has"),
        ({"expiry": {"kind": "min_days", "days": 731}}, "an expiry further off than is listed"),
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


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"legs": [_leg(leg_id=f"leg_{n}") for n in range(8)]}, "as many legs as a setup holds"),
        ({"entry": {"anchor": "open", "offset_ms": MS_PER_MINUTE}}, "the first minute an entry can be read at"),
        ({"exit": {"anchor": "close", "offset_ms": MS_PER_MINUTE}}, "the last minute an exit can fill after"),
        ({"exit": {"anchor": "open", "offset_ms": 6 * MS_PER_MINUTE}}, "an exit a minute after the entry"),
        (
            {
                "entry": {"anchor": "close", "offset_ms": 47 * MS_PER_MINUTE},
                "exit": {"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
            },
            "an entry a minute before the exit, both from the close",
        ),
        (
            {"no_reentry_after": {"anchor": "close", "offset_ms": 1439 * MS_PER_MINUTE}},
            "the furthest a time may be from its anchor",
        ),
        (
            {
                "entry": {"anchor": "close", "offset_ms": 2 * MS_PER_MINUTE},
                "exit": {"anchor": "close", "offset_ms": MS_PER_MINUTE},
            },
            "the last minute an entry can be read at",
        ),
        (
            {
                "entry": {"anchor": "open", "offset_ms": MS_PER_MINUTE},
                "exit": {"anchor": "open", "offset_ms": 2 * MS_PER_MINUTE},
            },
            "the first minute an exit can be read at",
        ),
        ({"no_reentry_after": {"anchor": "open", "offset_ms": 0}}, "a cutoff at the open, so no stop earns a re-entry"),
        ({"slippage_percent": "0"}, "no slippage"),
        ({"slippage_percent": "99.9999"}, "the most slippage a fill can bear"),
    ],
)
def test_a_setup_at_the_edge_of_what_can_run_is_taken(changes: dict[str, Any], reason: str) -> None:
    OptionsBacktestSetup.model_validate(_setup(**changes))


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"contracts": 1000}, "as many contracts as a leg may have"),
        ({"leg_id": "a" * 32}, "the longest leg id"),
        ({"stop_loss": {"kind": "percent", "value": "99.9999"}}, "the deepest stop in percent"),
        ({"stop_loss": {"kind": "points", "value": "100"}}, "a stop in points is not a percent"),
        ({"momentum": {"kind": "percent_down", "value": "99.9999"}}, "the deepest fall in percent"),
        ({"momentum": {"kind": "percent_up", "value": "100"}}, "a premium may double"),
        ({"momentum": {"kind": "points_down", "value": "100"}}, "a fall in points is not a percent"),
        (
            {"stop_loss": {"kind": "percent", "value": "25"}, "reentry_on_stop": {"kind": "at_once", "count": 20}},
            "as many re-entries as a leg may have",
        ),
        ({"strike": {"kind": "atm_offset", "steps": 50}}, "the furthest strike above the money"),
        ({"strike": {"kind": "atm_offset", "steps": -50}}, "the furthest strike below the money"),
        ({"expiry": {"kind": "min_days", "days": 0}}, "an expiry that may be today"),
        ({"expiry": {"kind": "min_days", "days": 730}}, "the furthest expiry"),
    ],
)
def test_a_leg_at_the_edge_of_what_can_trade_is_taken(changes: dict[str, Any], reason: str) -> None:
    OptionsBacktestSetup.model_validate(_setup(legs=[_leg(**changes)]))


def test_an_entry_from_the_open_and_an_exit_from_the_close_are_left_to_each_session() -> None:
    """Whether they cross depends on the session's length, which only the calendar knows."""
    setup = OptionsBacktestSetup.model_validate(
        _setup(
            entry={"anchor": "open", "offset_ms": 300 * MS_PER_MINUTE},
            exit={"anchor": "close", "offset_ms": 46 * MS_PER_MINUTE},
        )
    )

    full_day = setup.entry.at(session_open_ms=_OPEN_MS, session_close_ms=_CLOSE_MS) < setup.exit.at(
        session_open_ms=_OPEN_MS, session_close_ms=_CLOSE_MS
    )
    half_day = setup.entry.at(session_open_ms=_OPEN_MS, session_close_ms=_HALF_DAY_CLOSE_MS) < setup.exit.at(
        session_open_ms=_OPEN_MS, session_close_ms=_HALF_DAY_CLOSE_MS
    )

    assert (full_day, half_day) == (True, False)


@pytest.mark.parametrize(
    ("entry", "exit_", "close_ms", "fits", "reason"),
    [
        (("open", 5), ("close", 46), _CLOSE_MS, True, "the shipped window on a full day"),
        (("open", 5), ("close", 46), _HALF_DAY_CLOSE_MS, True, "the shipped window on a half day"),
        (("open", 1), ("close", 1), _CLOSE_MS, True, "the widest window a session holds"),
        (("open", 300), ("close", 46), _HALF_DAY_CLOSE_MS, False, "an entry after the exit on a half day"),
        (("open", 164), ("close", 46), _HALF_DAY_CLOSE_MS, False, "an entry at the exit on a half day"),
        (("open", 163), ("close", 46), _HALF_DAY_CLOSE_MS, True, "an entry a minute before the exit on a half day"),
        (("close", 390), ("close", 46), _CLOSE_MS, False, "an entry at the open, counted from the close"),
        (("close", 389), ("close", 46), _CLOSE_MS, True, "an entry a minute after the open, counted from the close"),
        (("open", 5), ("open", 390), _CLOSE_MS, False, "an exit at the close, counted from the open"),
        (("open", 5), ("open", 389), _CLOSE_MS, True, "an exit a minute before the close, counted from the open"),
        (("open", 5), ("open", 400), _CLOSE_MS, False, "an exit after the close"),
        (("close", 400), ("close", 46), _CLOSE_MS, False, "an entry before the open"),
    ],
)
def test_a_session_fits_a_window_that_leaves_a_minute_to_read_and_a_minute_to_fill(
    entry: tuple[str, int], exit_: tuple[str, int], close_ms: int, fits: bool, reason: str
) -> None:
    setup = OptionsBacktestSetup.model_validate(
        _setup(
            entry={"anchor": entry[0], "offset_ms": entry[1] * MS_PER_MINUTE},
            exit={"anchor": exit_[0], "offset_ms": exit_[1] * MS_PER_MINUTE},
        )
    )

    assert setup.window_fits(session_open_ms=_OPEN_MS, session_close_ms=close_ms) is fits


# ── An underlying is one to five letters ─────────────────────────────────


@pytest.mark.parametrize("underlying", ["A", "SPY", "GOOGL"])
def test_an_underlying_of_one_to_five_letters_is_taken(underlying: str) -> None:
    OptionsBacktestSetup.model_validate(_setup(underlying=underlying))


@pytest.mark.parametrize("underlying", ["GOOGLE", "ABCDEF", ""])
def test_an_underlying_of_six_letters_is_refused_since_its_contracts_names_do_not_fit_the_lake(underlying: str) -> None:
    # A six-letter root makes a contract symbol of 21 characters, one more than the lake's catalog stores.
    with pytest.raises(ValidationError):
        OptionsBacktestSetup.model_validate(_setup(underlying=underlying))


# ── An entry from the open and an exit from the close ────────────────────


@pytest.mark.parametrize(
    ("entry_minutes", "exit_minutes", "taken"),
    [
        (700, 739, True),  # 1,439 minutes: one minute short of a day
        (700, 740, False),  # 1,440 minutes: a day
        (1, 1438, True),  # 1,439 minutes
        (1, 1439, False),  # 1,440 minutes
        (1439, 1, False),
        (1439, 2, False),
        (720, 720, False),
        (1000, 1000, False),
    ],
)
def test_an_entry_from_the_open_and_an_exit_from_the_close_that_add_up_to_a_day_fit_no_session(
    entry_minutes: int, exit_minutes: int, taken: bool
) -> None:
    written = _setup(
        entry={"anchor": "open", "offset_ms": entry_minutes * MS_PER_MINUTE},
        exit={"anchor": "close", "offset_ms": exit_minutes * MS_PER_MINUTE},
    )

    if taken:
        OptionsBacktestSetup.model_validate(written)
    else:
        with pytest.raises(ValidationError, match="add up to a day"):
            OptionsBacktestSetup.model_validate(written)


def test_an_entry_from_the_close_and_an_exit_from_the_open_are_left_to_each_session_whatever_they_add_up_to() -> None:
    # Counted that way round the entry is the earlier of the two only when they add up to more than the session, so a
    # large sum is what lets one fit. The rule above is not about this pair.
    setup = OptionsBacktestSetup.model_validate(
        _setup(
            entry={"anchor": "close", "offset_ms": 300 * MS_PER_MINUTE},
            exit={"anchor": "open", "offset_ms": 300 * MS_PER_MINUTE},
        )
    )

    assert setup.window_fits(session_open_ms=_OPEN_MS, session_close_ms=_CLOSE_MS) is True
    assert setup.window_fits(session_open_ms=_OPEN_MS, session_close_ms=_HALF_DAY_CLOSE_MS) is False


# ── A re-entry rule a stop can reach ─────────────────────────────────────

_STOP = {"kind": "percent", "value": "25"}
_RE_ENTER = {"kind": "at_once", "count": 1}


@pytest.mark.parametrize(
    ("square_off", "legs", "taken", "reason"),
    [
        ("partial", [_leg(reentry_on_stop=_RE_ENTER)], False, "a re-entry rule on a leg with no stop, alone"),
        ("complete", [_leg(reentry_on_stop=_RE_ENTER)], False, "a re-entry rule on a leg with no stop, alone, complete"),
        (
            "partial",
            [_leg(stop_loss=_STOP, reentry_on_stop=_RE_ENTER)],
            True,
            "a leg that can be stopped by its own stop",
        ),
        (
            "complete",
            [_leg(stop_loss=_STOP, reentry_on_stop=_RE_ENTER)],
            True,
            "a leg that can be stopped by its own stop, complete",
        ),
        (
            "partial",
            [_leg(reentry_on_stop=_RE_ENTER), _leg(leg_id="put", right="put", stop_loss=_STOP)],
            False,
            "another leg's stop closes only that leg under a partial square-off",
        ),
        (
            "complete",
            [_leg(reentry_on_stop=_RE_ENTER), _leg(leg_id="put", right="put", stop_loss=_STOP)],
            True,
            "another leg's stop closes this leg too under a complete square-off",
        ),
        (
            "complete",
            [_leg(reentry_on_stop=_RE_ENTER), _leg(leg_id="put", right="put")],
            False,
            "no leg has a stop, complete",
        ),
        (
            "partial",
            [_leg(reentry_on_stop=_RE_ENTER), _leg(leg_id="put", right="put", reentry_on_stop=_RE_ENTER)],
            False,
            "two legs that re-enter and neither can be stopped",
        ),
        (
            "complete",
            [_leg(reentry_on_stop=_RE_ENTER), _leg(leg_id="put", right="put", stop_loss=_STOP, reentry_on_stop=_RE_ENTER)],
            True,
            "the second leg's stop reaches the first, and its own reaches itself",
        ),
        ("partial", [_leg(), _leg(leg_id="put", right="put")], True, "no re-entry rule asks for no stop"),
    ],
)
def test_a_leg_with_a_re_entry_rule_needs_a_stop_that_can_reach_it(
    square_off: str, legs: list[dict[str, Any]], taken: bool, reason: str
) -> None:
    written = _setup(square_off=square_off, legs=legs)

    if taken:
        OptionsBacktestSetup.model_validate(written)
        return
    with pytest.raises(ValidationError) as caught:
        OptionsBacktestSetup.model_validate(written)
    message = str(caught.value)
    assert "nothing can stop it out" in message and "give it a stop loss" in message


def test_the_three_shipped_examples_still_validate() -> None:
    from app.options_backtest.saved_setups import EXAMPLE_SETUPS

    assert [example.setup is not None for example in EXAMPLE_SETUPS] == [True, True, True]
    for example in EXAMPLE_SETUPS:
        assert example.setup is not None
        assert OptionsBacktestSetup.model_validate(example.setup.model_dump(mode="json")) == example.setup


# ── A leg's side (#3105) ─────────────────────────────────────────────────

# What setups dumped to on master at 90937b3c, before a leg had a side, written out here as text. A bought leg writes
# no side, so these are what a setup of bought legs dumps to now, byte for byte: stored setups, stored runs and the
# equality check of a finished run depend on it.
_LEG_OF_THE_EXAMPLES = (
    '{{"leg_id":"{name}","right":"{name}","contracts":1,"expiry":{{"kind":"this_week"}},"strike":{{"kind":"atm"}},'
    '"stop_loss":{{"kind":"percent","value":"25"}},"momentum":{{"kind":"percent_up","value":"25"}},'
    '"reentry_on_stop":{reentry}}}'
)
_THE_COSTS = (
    '"slippage_percent":"0.3","price_step_usd":"0.01","fees":{"kind":"not_charged","per_contract_usd":null},'
    '"starting_cash_usd":"10000"}'
)
_ONCE = '{"kind":"at_once","count":1}'
_MASTER_BYTES: dict[str, str] = {
    "SPY premium momentum, no re-entry": (
        '{"version":1,"underlying":"SPY","entry":{"anchor":"open","offset_ms":300000},'
        '"exit":{"anchor":"close","offset_ms":2760000},"no_reentry_after":null,"square_off":"complete","legs":['
        + ",".join(_LEG_OF_THE_EXAMPLES.format(name=name, reentry="null") for name in ("call", "put"))
        + "],"
        + _THE_COSTS
    ),
    "SPY premium momentum, one re-entry": (
        '{"version":1,"underlying":"SPY","entry":{"anchor":"open","offset_ms":300000},'
        '"exit":{"anchor":"close","offset_ms":2760000},'
        '"no_reentry_after":{"anchor":"close","offset_ms":3600000},"square_off":"complete","legs":['
        + ",".join(_LEG_OF_THE_EXAMPLES.format(name=name, reentry=_ONCE) for name in ("call", "put"))
        + "],"
        + _THE_COSTS
    ),
}
_A_PLAIN_SETUP_ON_MASTER = (
    '{"version":1,"underlying":"SPY","entry":{"anchor":"open","offset_ms":300000},'
    '"exit":{"anchor":"close","offset_ms":2760000},"no_reentry_after":null,"square_off":"complete","legs":['
    '{"leg_id":"call","right":"call","contracts":1,"expiry":{"kind":"this_week"},"strike":{"kind":"atm"},'
    '"stop_loss":null,"momentum":null,"reentry_on_stop":null}],' + _THE_COSTS
)
_A_BUSY_SETUP_ON_MASTER = (
    '{"version":1,"underlying":"SPY","entry":{"anchor":"open","offset_ms":300000},'
    '"exit":{"anchor":"close","offset_ms":2760000},'
    '"no_reentry_after":{"anchor":"close","offset_ms":3600000},"square_off":"complete","legs":['
    '{"leg_id":"call","right":"call","contracts":1,"expiry":{"kind":"this_week"},"strike":{"kind":"atm"},'
    '"stop_loss":{"kind":"percent","value":"25"},"momentum":{"kind":"percent_up","value":"10"},'
    '"reentry_on_stop":{"kind":"at_once","count":2}},'
    '{"leg_id":"put","right":"put","contracts":3,"expiry":{"kind":"min_days","days":7},'
    '"strike":{"kind":"atm_offset","steps":-2},"stop_loss":null,"momentum":null,"reentry_on_stop":null}],'
    '"slippage_percent":"0.3","price_step_usd":"0.01","fees":{"kind":"per_contract","per_contract_usd":"0.65"},'
    '"starting_cash_usd":"10000"}'
)


def _busy_setup() -> dict[str, Any]:
    return _setup(
        legs=[
            _leg(
                stop_loss={"kind": "percent", "value": "25"},
                momentum={"kind": "percent_up", "value": "10"},
                reentry_on_stop={"kind": "at_once", "count": 2},
            ),
            _leg(
                leg_id="put",
                right="put",
                contracts=3,
                expiry={"kind": "min_days", "days": 7},
                strike={"kind": "atm_offset", "steps": -2},
            ),
        ],
        no_reentry_after={"anchor": "close", "offset_ms": 60 * MS_PER_MINUTE},
        fees={"kind": "per_contract", "per_contract_usd": "0.65"},
    )


def test_the_two_shipped_examples_that_buy_dump_to_the_bytes_they_dumped_to_before_a_leg_had_a_side() -> None:
    from app.options_backtest.saved_setups import EXAMPLE_SETUPS

    # The third example sells, so it has no bytes from before a leg could be sold: only the sold leg writes a side.
    buying = [example for example in EXAMPLE_SETUPS if example.name in _MASTER_BYTES]
    assert len(buying) == 2
    for example in buying:
        assert example.setup is not None
        assert example.setup.model_dump_json() == _MASTER_BYTES[example.name], example.name


@pytest.mark.parametrize(
    ("written", "on_master"),
    [
        pytest.param(_setup(), _A_PLAIN_SETUP_ON_MASTER, id="the plain fixture setup"),
        pytest.param(_busy_setup(), _A_BUSY_SETUP_ON_MASTER, id="a setup with every rule and two kinds of expiry"),
        pytest.param(
            _setup(legs=[_leg(side="buy")]),
            _A_PLAIN_SETUP_ON_MASTER,
            id="a side written as buy is the default and is not written back",
        ),
    ],
)
def test_a_setup_of_bought_legs_dumps_to_the_bytes_it_dumped_to_before_a_leg_had_a_side(
    written: dict[str, Any], on_master: str
) -> None:
    setup = OptionsBacktestSetup.model_validate(written)

    assert setup.model_dump_json() == on_master
    assert "side" not in setup.model_dump(mode="json")["legs"][0]
    assert "side" not in setup.model_dump(mode="python")["legs"][0]


@pytest.mark.parametrize("stored", [*_MASTER_BYTES.values(), _A_PLAIN_SETUP_ON_MASTER, _A_BUSY_SETUP_ON_MASTER])
def test_a_setup_stored_before_legs_had_a_side_opens_as_it_was_and_writes_back_the_same_bytes(stored: str) -> None:
    opened = OptionsBacktestSetup.model_validate_json(stored)

    assert all(leg.side == "buy" for leg in opened.legs)
    assert opened.model_dump_json() == stored


def test_a_sold_leg_writes_its_side_and_reads_back_as_sold() -> None:
    sold = OptionsBacktestSetup.model_validate(
        _setup(legs=[_leg(leg_id="long"), _leg(leg_id="short", side="sell", strike={"kind": "atm_offset", "steps": 2})])
    )

    dumped = sold.model_dump(mode="json")

    assert [leg.get("side") for leg in dumped["legs"]] == [None, "sell"]
    assert OptionsBacktestSetup.model_validate_json(sold.model_dump_json()) == sold
    # The slippage ladder re-validates a dump taken in python mode, and the side must survive it.
    assert OptionsBacktestSetup.model_validate(sold.model_dump(mode="python")) == sold
    assert OptionsBacktestSetup.model_validate_json(sold.model_dump_json()).legs[1].side == "sell"


def test_two_setups_that_differ_only_in_a_legs_side_are_not_equal() -> None:
    bought = OptionsBacktestSetup.model_validate(_setup(legs=[_leg(leg_id="long"), _leg(leg_id="short")]))
    sold = OptionsBacktestSetup.model_validate(_setup(legs=[_leg(leg_id="long"), _leg(leg_id="short", side="sell")]))

    assert bought != sold
    assert bought.model_dump_json() != sold.model_dump_json()


@pytest.mark.parametrize("side", ["short", "BUY", "", None, 1])
def test_a_side_that_is_neither_buy_nor_sell_is_refused(side: Any) -> None:
    with pytest.raises(ValidationError):
        OptionsBacktestSetup.model_validate(_setup(legs=[_leg(side=side)]))


# Rule 2: a sold contract is covered by a bought contract of its kind, one for one.
_LONG_CALL = _leg(leg_id="long_call")
_SHORT_CALL = _leg(leg_id="short_call", side="sell")
_LONG_PUT = _leg(leg_id="long_put", right="put")
_SHORT_PUT = _leg(leg_id="short_put", right="put", side="sell")


@pytest.mark.parametrize(
    ("legs", "kind", "reason"),
    [
        ([_SHORT_CALL], "call", "a sold call and nothing bought"),
        ([_LONG_PUT, _SHORT_CALL], "call", "a bought put does not cover a sold call"),
        ([_LONG_CALL, _leg(leg_id="short_call", side="sell", contracts=2)], "call", "two sold against one bought"),
        (
            [_leg(leg_id="long_call", contracts=3), _SHORT_CALL, _leg(leg_id="more", side="sell", contracts=3)],
            "call",
            "sold legs of one kind add up",
        ),
        ([_LONG_CALL, _SHORT_PUT], "put", "a bought call does not cover a sold put"),
        (
            [_LONG_CALL, _SHORT_CALL, _LONG_PUT, _leg(leg_id="short_put", right="put", side="sell", contracts=2)],
            "put",
            "the put side alone is short",
        ),
    ],
)
def test_a_setup_that_sells_more_contracts_of_a_kind_than_it_buys_is_refused_and_the_sentence_names_the_kind(
    legs: list[dict[str, Any]], kind: str, reason: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        OptionsBacktestSetup.model_validate(_setup(legs=legs))

    message = str(caught.value)
    assert f"sell more {kind} contracts than they buy" in message
    assert f"a sold {kind} needs a bought {kind} to cover it" in message


@pytest.mark.parametrize(
    ("legs", "reason"),
    [
        ([_LONG_CALL, _SHORT_CALL], "one bought call covers one sold call"),
        ([_leg(leg_id="long_call", contracts=2), _SHORT_CALL], "more bought than sold"),
        ([_LONG_CALL, _SHORT_CALL, _LONG_PUT, _SHORT_PUT], "a pair of each kind"),
        ([_LONG_PUT, _SHORT_PUT, _LONG_CALL], "a bought leg with nothing sold of its kind"),
        (
            [_leg(leg_id="a", contracts=1), _leg(leg_id="b", contracts=1), _leg(leg_id="s", contracts=2, side="sell")],
            "two bought legs together cover a sold leg of two",
        ),
        ([_LONG_CALL, _LONG_PUT], "nothing sold at all"),
    ],
)
def test_a_setup_that_buys_at_least_as_many_contracts_of_each_kind_as_it_sells_is_taken(
    legs: list[dict[str, Any]], reason: str
) -> None:
    OptionsBacktestSetup.model_validate(_setup(legs=legs))


# Rule 3: a setup with a sold leg carries one expiry rule on every leg.
_SAME_DAY = {"kind": "same_day"}


@pytest.mark.parametrize(
    ("legs", "reason"),
    [
        ([_LONG_CALL, _leg(leg_id="short_call", side="sell", expiry=_SAME_DAY)], "a sold leg on another rule"),
        ([_leg(leg_id="long_call", expiry=_SAME_DAY), _SHORT_CALL], "the bought leg on another rule"),
        (
            [
                _leg(leg_id="long_call", expiry={"kind": "min_days", "days": 3}),
                _leg(leg_id="short_call", side="sell", expiry={"kind": "min_days", "days": 4}),
            ],
            "one rule with two day counts",
        ),
        (
            [_LONG_CALL, _SHORT_CALL, _leg(leg_id="long_put", right="put", expiry=_SAME_DAY)],
            "a third leg that is not on the rule",
        ),
    ],
)
def test_a_setup_with_a_sold_leg_whose_legs_do_not_all_carry_one_expiry_rule_is_refused(
    legs: list[dict[str, Any]], reason: str
) -> None:
    with pytest.raises(ValidationError) as caught:
        OptionsBacktestSetup.model_validate(_setup(legs=legs))

    assert "one expiry rule" in str(caught.value)


@pytest.mark.parametrize(
    ("legs", "reason"),
    [
        ([_LONG_CALL, _SHORT_CALL], "the same rule"),
        (
            [
                _leg(leg_id="long_call", expiry={"kind": "min_days", "days": 3}),
                _leg(leg_id="short_call", side="sell", expiry={"kind": "min_days", "days": 3}),
            ],
            "the same rule with the same day count",
        ),
        (
            [_leg(leg_id="a", expiry=_SAME_DAY), _leg(leg_id="b", right="put", expiry={"kind": "this_week"})],
            "a setup that only buys may mix expiry rules, as it always could",
        ),
    ],
)
def test_one_expiry_rule_on_every_leg_is_taken_and_a_setup_that_only_buys_is_left_alone(
    legs: list[dict[str, Any]], reason: str
) -> None:
    OptionsBacktestSetup.model_validate(_setup(legs=legs))


# Rule 4: a percent stop is under 100 on a bought leg only.
@pytest.mark.parametrize("value", ["100", "100.0001", "150", "1000", "99999999"])
def test_a_percent_stop_of_a_hundred_or_more_is_refused_on_a_bought_leg_where_it_was_before(value: str) -> None:
    with pytest.raises(ValidationError) as caught:
        OptionsBacktestSetup.model_validate(_setup(legs=[_leg(stop_loss={"kind": "percent", "value": value})]))

    [error] = caught.value.errors()
    # The refusal stays at the stop loss, where the page puts it, though the check is the leg's.
    assert error["loc"] == ("legs", 0, "stop_loss")
    assert "under 100" in error["msg"]


@pytest.mark.parametrize("value", ["100", "150", "1000", "99999999"])
def test_a_percent_stop_of_a_hundred_or_more_is_taken_on_a_sold_leg(value: str) -> None:
    setup = OptionsBacktestSetup.model_validate(
        _setup(legs=[_LONG_CALL, _leg(leg_id="short_call", side="sell", stop_loss={"kind": "percent", "value": value})])
    )

    assert setup.legs[1].stop_loss is not None and setup.legs[1].stop_loss.value == Decimal(value)


@pytest.mark.parametrize("side", ["buy", "sell"])
@pytest.mark.parametrize("stop", [{"kind": "points", "value": "250"}, {"kind": "percent", "value": "99.9999"}])
def test_a_stop_in_points_and_a_percent_below_a_hundred_are_taken_on_either_side(
    side: str, stop: dict[str, Any]
) -> None:
    setup = OptionsBacktestSetup.model_validate(
        _setup(legs=[_LONG_CALL, _leg(leg_id="other", side=side, stop_loss=stop)])
    )

    assert setup.legs[1].stop_loss is not None


def test_a_percent_stop_is_judged_by_the_side_whatever_order_the_leg_is_written_in() -> None:
    stop = {"kind": "percent", "value": "150"}

    OptionsBacktestSetup.model_validate(
        _setup(legs=[_LONG_CALL, {"stop_loss": stop, "side": "sell", **_leg(leg_id="short_call")}])
    )
    with pytest.raises(ValidationError):
        OptionsBacktestSetup.model_validate(
            _setup(legs=[{"stop_loss": stop, "side": "buy", **_leg(leg_id="long_call")}])
        )


# Rule 5: a sold leg's re-entry rule is reachable through its own stop, through a complete square-off with another
# leg's stop, or through a stop on a bought leg of its kind.
_STOP = {"kind": "percent", "value": "25"}
_RE_ENTER = {"kind": "at_once", "count": 1}


@pytest.mark.parametrize(
    ("square_off", "legs", "taken", "reason"),
    [
        (
            "partial",
            [_LONG_CALL, _leg(leg_id="short_call", side="sell", reentry_on_stop=_RE_ENTER)],
            False,
            "no stop anywhere",
        ),
        (
            "partial",
            [_LONG_CALL, _leg(leg_id="short_call", side="sell", stop_loss=_STOP, reentry_on_stop=_RE_ENTER)],
            True,
            "its own stop",
        ),
        (
            "complete",
            [
                _leg(leg_id="long_call", stop_loss=_STOP),
                _leg(leg_id="short_call", side="sell", reentry_on_stop=_RE_ENTER),
            ],
            True,
            "another leg's stop under a complete square-off",
        ),
        (
            "partial",
            [
                _leg(leg_id="long_call", stop_loss=_STOP),
                _leg(leg_id="short_call", side="sell", reentry_on_stop=_RE_ENTER),
            ],
            True,
            "a stop on the bought leg of its kind: it leaves with its protection",
        ),
        (
            "partial",
            [
                _leg(leg_id="long_put", right="put", stop_loss=_STOP),
                _LONG_CALL,
                _leg(leg_id="short_call", side="sell", reentry_on_stop=_RE_ENTER),
            ],
            False,
            "a stop on a bought leg of the other kind does not reach it",
        ),
        (
            "partial",
            [
                _leg(leg_id="long_call", contracts=2),
                _leg(leg_id="other_short", side="sell", stop_loss=_STOP),
                _leg(leg_id="short_call", side="sell", reentry_on_stop=_RE_ENTER),
            ],
            False,
            "a stop on another sold leg does not reach it",
        ),
        (
            "partial",
            [
                _leg(leg_id="long_call", reentry_on_stop=_RE_ENTER),
                _leg(leg_id="short_call", side="sell", stop_loss=_STOP),
            ],
            False,
            "a bought leg is not reached through a sold leg of its kind, as before",
        ),
    ],
)
def test_a_re_entry_rule_on_a_sold_leg_needs_a_stop_that_can_reach_it(
    square_off: str, legs: list[dict[str, Any]], taken: bool, reason: str
) -> None:
    written = _setup(square_off=square_off, legs=legs)

    if taken:
        OptionsBacktestSetup.model_validate(written)
        return
    with pytest.raises(ValidationError) as caught:
        OptionsBacktestSetup.model_validate(written)
    assert "nothing can stop it out" in str(caught.value)


def test_a_sold_legs_unreachable_re_entry_says_it_can_be_reached_through_a_bought_leg_of_its_kind() -> None:
    written = _setup(legs=[_LONG_PUT, _leg(leg_id="short_put", right="put", side="sell", reentry_on_stop=_RE_ENTER)])

    with pytest.raises(ValidationError) as caught:
        OptionsBacktestSetup.model_validate(written)

    assert "or a bought put, a stop loss" in str(caught.value)
