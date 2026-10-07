"""ActionPlan schema tests (PRD #593).

Slice 1A (#594) ships the empty-plan envelope.
Slice 1B (#595) extends the schema with stock ``ActionEntity`` entry
legs and ``close_leg`` ``ExitEntity`` references, plus the cross-language
JSON fixtures under ``tests/fixtures/action_plan/``. Option legs and
selectors land in Slice 1C (#596).

Prior art: ``tests/schemas/test_host_runner_deploy_request_sizing.py``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.action_plan import (
    ActionPlan,
    traded_instrument,
    traded_market_symbol,
    trades_the_signals_shares,
)


def test_unknown_top_level_key_rejected() -> None:
    """`extra="forbid"` pins the deploy-boundary invariant: an operator typo
    like ``on_entry`` (instead of ``on_enter``) fails validation instead of
    silently round-tripping a malformed plan into the ledger."""

    with pytest.raises(ValidationError, match=r"on_entry"):
        ActionPlan.model_validate({"on_enter": [], "on_exit": [], "on_entry": []})


# ---------------------------------------------------------------------------
# Slice 1B (#595) — stock entry legs + close_leg exit references.

_STOCK_LEG: dict = {
    "leg_id": "spy_long",
    "instrument": {"kind": "stock", "underlying": "SPY"},
    "position": "long",
    "qty_ratio": 1,
}


def test_single_stock_entry_leg_round_trips() -> None:
    plan = ActionPlan(on_enter=[_STOCK_LEG], on_exit=[])

    assert plan.model_dump() == {"on_enter": [_STOCK_LEG], "on_exit": []}


def test_stock_entry_leg_missing_underlying_rejected() -> None:
    """``instrument.underlying`` is mandatory on every leg — there is no
    implicit fallback from ``live_config.symbol`` (ADR 0012 §5). The plan
    is self-contained or it doesn't validate."""

    bad = {
        "leg_id": "spy_long",
        "instrument": {"kind": "stock"},  # underlying missing
        "position": "long",
        "qty_ratio": 1,
    }
    with pytest.raises(ValidationError, match=r"underlying"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_stock_entry_leg_qty_ratio_below_one_rejected() -> None:
    """``qty_ratio`` is a positive integer (>= 1) — Slice 1B declarative
    semantics. Composition lands in Slice 4."""

    bad = dict(_STOCK_LEG, qty_ratio=0)
    with pytest.raises(ValidationError, match=r"qty_ratio"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_stock_entry_leg_missing_leg_id_rejected() -> None:
    bad = {
        "instrument": {"kind": "stock", "underlying": "SPY"},
        "position": "long",
        "qty_ratio": 1,
    }
    with pytest.raises(ValidationError, match=r"leg_id"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_stock_entry_leg_malformed_leg_id_rejected() -> None:
    """``leg_id`` is constrained to ``^[a-z0-9_]{1,32}$`` so the future
    resolver / persistence layer can rely on it as a stable identifier
    without escaping (ADR 0012 §3)."""

    bad = dict(_STOCK_LEG, leg_id="UPPER-CASE!")
    with pytest.raises(ValidationError, match=r"leg_id"):
        ActionPlan(on_enter=[bad], on_exit=[])


def test_duplicate_leg_ids_within_a_plan_rejected() -> None:
    """Two entry legs cannot share a ``leg_id`` — exits would be
    ambiguous, and the future resolver's ``leg_id → conId`` map would
    collapse."""

    duplicate = dict(_STOCK_LEG)
    with pytest.raises(ValidationError, match=r"duplicate"):
        ActionPlan(on_enter=[_STOCK_LEG, duplicate], on_exit=[])


def test_close_leg_exit_round_trips_when_entry_id_matches() -> None:
    plan = ActionPlan(
        on_enter=[_STOCK_LEG],
        on_exit=[{"kind": "close_leg", "entry_leg_id": "spy_long"}],
    )

    assert plan.model_dump()["on_exit"] == [
        {"kind": "close_leg", "entry_leg_id": "spy_long"}
    ]


def test_orphan_close_leg_referencing_unknown_entry_id_rejected() -> None:
    """A ``close_leg`` referencing a ``leg_id`` that is not in
    ``on_enter`` is a hard schema error, NOT a parity warning (#597
    handles warnings). Future-resolver consumption would fail at runtime;
    catching it at the deploy boundary keeps malformed plans out of the
    ledger."""

    with pytest.raises(ValidationError, match=r"close_leg"):
        ActionPlan(
            on_enter=[_STOCK_LEG],
            on_exit=[{"kind": "close_leg", "entry_leg_id": "does_not_exist"}],
        )


# ---------------------------------------------------------------------------
# Option legs, their strike and expiry rules, and the option structure (#2913).

_OPTION_LEG: dict = {
    "leg_id": "spy_long_call",
    "instrument": {"kind": "option", "underlying": "SPY"},
    "position": "long",
    "qty_ratio": 1,
    "right": "call",
    "strike": {"selector": "atm"},
    "expiry": {"selector": "min_dte", "days": 14},
}

# 2026-12-18 at 16:00 New York time.
_EXPIRY_ANCHOR_MS = 1_797_627_600_000


def _leg(leg_id: str, **changes: object) -> dict:
    return dict(_OPTION_LEG, leg_id=leg_id, **changes)


def _structure(*legs: dict) -> ActionPlan:
    """A plan that opens ``legs`` and closes each of them once."""
    return ActionPlan(
        on_enter=list(legs),
        on_exit=[{"kind": "close_leg", "entry_leg_id": leg["leg_id"]} for leg in legs],
    )


def test_single_option_entry_leg_round_trips() -> None:
    plan = _structure(_OPTION_LEG)

    assert plan.model_dump()["on_enter"] == [_OPTION_LEG]


def test_option_entry_leg_missing_right_rejected() -> None:
    bad = dict(_OPTION_LEG)
    del bad["right"]
    with pytest.raises(ValidationError, match=r"right"):
        _structure(bad)


def test_option_entry_leg_missing_strike_rejected() -> None:
    bad = dict(_OPTION_LEG)
    del bad["strike"]
    with pytest.raises(ValidationError, match=r"strike"):
        _structure(bad)


def test_option_entry_leg_missing_expiry_rejected() -> None:
    bad = dict(_OPTION_LEG)
    del bad["expiry"]
    with pytest.raises(ValidationError, match=r"expiry"):
        _structure(bad)


def test_unknown_strike_selector_rejected() -> None:
    with pytest.raises(ValidationError, match=r"selector"):
        _structure(_leg("a", strike={"selector": "wild_guess"}))


@pytest.mark.parametrize(
    ("declared", "stored"),
    [
        pytest.param({"selector": "atm"}, {"selector": "atm"}, id="at the money"),
        pytest.param(
            {"selector": "atm_offset", "offset": -3}, {"selector": "atm_offset", "offset": -3}, id="steps from the money"
        ),
        pytest.param(
            {"selector": "absolute", "strike": 452.5}, {"selector": "absolute", "strike": 452.5}, id="a named strike"
        ),
        pytest.param(
            {"selector": "delta", "target": 0.3},
            {"selector": "delta", "target": 0.3, "tolerance_delta": 0.05},
            id="a delta target, within 0.05 unless told otherwise",
        ),
        pytest.param(
            {"selector": "percent", "percent": -2.0},
            {"selector": "percent", "percent": -2.0, "tolerance_percent": 0.5},
            id="percent from the price, within half a percent unless told otherwise",
        ),
    ],
)
def test_a_strike_rule_is_kept_with_its_tolerance(declared: dict, stored: dict) -> None:
    """A rule's tolerance is part of the plan, so it is written out even when the owner left the default."""
    plan = _structure(_leg("a", strike=declared))

    assert plan.model_dump()["on_enter"][0]["strike"] == stored


def test_a_strike_measured_from_another_leg_must_land_on_a_listed_strike_unless_told_otherwise() -> None:
    plan = _structure(_leg("near"), _leg("far", strike={"selector": "leg_distance", "leg_id": "near", "dollars": 5}))

    assert plan.model_dump()["on_enter"][1]["strike"] == {
        "selector": "leg_distance",
        "leg_id": "near",
        "dollars": 5.0,
        "tolerance_dollars": 0.0,
    }


@pytest.mark.parametrize(
    ("strike", "refused"),
    [
        pytest.param({"selector": "delta", "target": 0}, "target", id="a delta target of zero"),
        pytest.param({"selector": "delta", "target": 1}, "target", id="a delta target of one"),
        pytest.param(
            {"selector": "delta", "target": -0.3}, "target", id="a signed delta target; the leg's put gives the sign"
        ),
        pytest.param(
            {"selector": "delta", "target": 0.3, "tolerance_delta": 0}, "tolerance_delta", id="a delta tolerance of zero"
        ),
        pytest.param(
            {"selector": "delta", "target": 0.3, "tolerance_delta": 1}, "tolerance_delta", id="a delta tolerance of the whole range"
        ),
        pytest.param(
            {"selector": "delta", "target": 0.3, "tolerance": 0.01},
            "tolerance",
            id="a misspelt tolerance, which would otherwise take the default unnoticed",
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": float("nan")}, "dollars", id="a distance that is no number"
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": 2.5005}, "dollars", id="a distance finer than strikes are listed"
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": -99999.999},
            "dollars",
            id="a distance wider than the whole range of strikes",
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": 5, "tolerance_dollars": -1},
            "tolerance_dollars",
            id="a negative dollar tolerance",
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": 5, "tolerance_dollars": 100000},
            "tolerance_dollars",
            id="a dollar tolerance wider than the whole range of strikes",
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": 5, "tolerance_dollars": 0.0005},
            "tolerance_dollars",
            id="a dollar tolerance finer than strikes are listed",
        ),
        pytest.param({"selector": "percent", "percent": -100}, "percent", id="a strike of zero or less"),
        pytest.param({"selector": "percent", "percent": float("inf")}, "percent", id="an infinite percent"),
        pytest.param({"selector": "percent", "percent": 1000.1}, "percent", id="a strike eleven times the price"),
        pytest.param(
            {"selector": "percent", "percent": 2, "tolerance_percent": 100.1},
            "tolerance_percent",
            id="a percent tolerance wider than the price itself",
        ),
        pytest.param(
            {"selector": "percent", "percent": 2, "tolerance": 1}, "tolerance", id="a misspelt percent tolerance"
        ),
        pytest.param(
            {"selector": "leg_distance", "leg_id": "a", "dollars": 5, "tolerance": 1},
            "tolerance",
            id="a misspelt distance tolerance",
        ),
        pytest.param({"selector": "atm", "offset": -5}, "offset", id="a steps rule spelt as at the money"),
        pytest.param(
            {"selector": "absolute", "strike": 100, "tolerance_dollars": 1}, "tolerance_dollars", id="a tolerance on a named strike"
        ),
        pytest.param({"selector": "percent", "percent": float("nan")}, "percent", id="a percent that is no number"),
        pytest.param(
            {"selector": "percent", "percent": 2, "tolerance_percent": 0},
            "tolerance_percent",
            id="a percent tolerance of zero, which no listed strike meets",
        ),
        pytest.param(
            {"selector": "percent", "percent": 2, "tolerance_percent": float("nan")},
            "tolerance_percent",
            id="a percent tolerance that is no number",
        ),
        pytest.param({"selector": "absolute", "strike": 100.0005}, "strike", id="a named strike finer than strikes are listed"),
        pytest.param({"selector": "absolute", "strike": 100000}, "strike", id="a named strike no contract symbol can write"),
    ],
)
def test_a_strike_rule_no_listed_strike_could_satisfy_is_refused(strike: dict, refused: str) -> None:
    with pytest.raises(ValidationError) as error:
        _structure(_leg("a"), _leg("b", strike=strike))

    assert [failure["loc"][-1] for failure in error.value.errors()] == [refused]


@pytest.mark.parametrize(
    ("strike", "field"),
    [
        ({"selector": "leg_distance", "leg_id": "a", "dollars": -0.0}, "dollars"),
        ({"selector": "percent", "percent": -0.0}, "percent"),
    ],
    ids=["a distance", "a percent"],
)
def test_minus_zero_is_stored_as_zero(strike: dict, field: str) -> None:
    """One strike, one spelling: -0.0 would hash apart from 0.0."""
    plan = _structure(_leg("a"), _leg("b", right="put", strike=strike))

    assert str(plan.model_dump()["on_enter"][1]["strike"][field]) == "0.0"


@pytest.mark.parametrize(
    "expiry",
    [{"selector": "same_day", "days": 0}, {"selector": "nearest_weekly", "days": 30}],
    ids=["same day", "nearest weekly"],
)
def test_an_expiry_rule_with_a_key_it_does_not_have_is_refused(expiry: dict) -> None:
    """A rule that takes no days must not swallow them: the owner would believe they counted."""
    with pytest.raises(ValidationError, match="days"):
        _structure(_leg("a", expiry=expiry))


@pytest.mark.parametrize(
    "expiry",
    [
        {"selector": "min_dte", "days": 14},
        {"selector": "nearest_weekly"},
        {"selector": "same_day"},
        {"selector": "absolute", "expiration_ms": _EXPIRY_ANCHOR_MS},
    ],
    ids=["at least N days", "nearest weekly", "same day", "a named date"],
)
def test_an_expiry_rule_round_trips(expiry: dict) -> None:
    plan = _structure(_leg("a", expiry=expiry))

    assert plan.model_dump()["on_enter"][0]["expiry"] == expiry


def test_min_dte_days_below_one_rejected() -> None:
    with pytest.raises(ValidationError, match=r"days"):
        _structure(_leg("a", expiry={"selector": "min_dte", "days": 0}))


def test_absolute_expiry_missing_expiration_ms_rejected() -> None:
    with pytest.raises(ValidationError, match=r"expiration_ms"):
        _structure(_leg("a", expiry={"selector": "absolute"}))


@pytest.mark.parametrize(
    ("expiration_ms", "reason"),
    [
        pytest.param(1_797_552_000_000, "is not an option expiry", id="midnight UTC on 2026-12-18, not 16:00 New York"),
        pytest.param(10**20, "less than or equal to", id="an instant no calendar holds"),
        pytest.param(-97_200_000, "greater than 0", id="1969-12-30, on the anchor but before the first instant"),
        pytest.param(4_103_125_200_000, "two digits", id="2100-01-08, a year no contract symbol can write"),
    ],
)
def test_a_named_expiry_no_contract_could_carry_is_refused(expiration_ms: int, reason: str) -> None:
    """ADR 0022 (i): one instant per expiry date, so two plans naming one date cannot hash apart."""
    with pytest.raises(ValidationError, match=reason):
        _structure(_leg("a", expiry={"selector": "absolute", "expiration_ms": expiration_ms}))


_SHORT = {"position": "short"}
_PUT = {"right": "put"}


@pytest.mark.parametrize(
    "legs",
    [
        pytest.param([_leg("call")], id="a bought call"),
        pytest.param([_leg("put", **_PUT)], id="a bought put"),
        pytest.param(
            [_leg("bought"), _leg("sold", **_SHORT, strike={"selector": "atm_offset", "offset": 5})],
            id="a call spread",
        ),
        pytest.param(
            [
                _leg("call"),
                _leg("put", **_PUT, strike={"selector": "leg_distance", "leg_id": "call", "dollars": 0}),
            ],
            id="a straddle, the put at the call's strike",
        ),
        pytest.param(
            [
                _leg("bought", instrument={"kind": "option", "underlying": "spy"}),
                _leg("sold", **_SHORT, strike={"selector": "atm_offset", "offset": 5}),
            ],
            id="one underlying written in two cases",
        ),
        pytest.param(
            [
                _leg("low", strike={"selector": "atm_offset", "offset": -5}),
                _leg("body", **_SHORT, qty_ratio=2),
                _leg("high", strike={"selector": "atm_offset", "offset": 5}),
            ],
            id="a call butterfly: two sold calls, one bought on each side",
        ),
        pytest.param(
            [
                _leg("sold_put", **_SHORT, **_PUT, strike={"selector": "delta", "target": 0.2}),
                _leg("bought_put", **_PUT, strike={"selector": "leg_distance", "leg_id": "sold_put", "dollars": -5}),
                _leg("sold_call", **_SHORT, strike={"selector": "delta", "target": 0.2}),
                _leg("bought_call", strike={"selector": "leg_distance", "leg_id": "sold_call", "dollars": 5}),
            ],
            id="an iron condor by delta, wings five dollars out",
        ),
    ],
)
def test_a_structure_one_order_can_carry_is_accepted(legs: list[dict]) -> None:
    assert len(_structure(*legs).on_enter) == len(legs)


@pytest.mark.parametrize(
    ("legs", "reason"),
    [
        pytest.param(
            [_leg("a"), _leg("b"), _leg("c"), _leg("d"), _leg("e")],
            "one to 4 legs",
            id="five legs",
        ),
        pytest.param(
            [_leg("a"), _leg("b", instrument={"kind": "option", "underlying": "QQQ"})],
            "one underlying",
            id="legs on two underlyings",
        ),
        pytest.param(
            [_leg("a"), {"leg_id": "shares", "instrument": {"kind": "stock", "underlying": "SPY"}, "position": "long", "qty_ratio": 100}],
            "no stock leg",
            id="a stock leg beside an option leg",
        ),
        pytest.param([_leg("a", qty_ratio=2)], "lowest terms", id="one leg written as two"),
        pytest.param(
            [_leg("a", qty_ratio=2), _leg("b", **_SHORT, qty_ratio=2)],
            "lowest terms",
            id="a two-by-two spread, which is a one-by-one spread opened twice",
        ),
        pytest.param([_leg("a", **_SHORT)], "sells 1 call", id="a sold call nothing covers"),
        pytest.param(
            [_leg("a"), _leg("b", **_SHORT, qty_ratio=2)],
            "sells 2 call",
            id="two sold calls against one bought",
        ),
        pytest.param(
            [_leg("a"), _leg("b", **_SHORT, **_PUT)],
            "sells 1 put",
            id="a sold put covered only by a bought call",
        ),
        pytest.param(
            [_leg("a", strike={"selector": "leg_distance", "leg_id": "nobody", "dollars": 5})],
            "not a leg of this plan",
            id="a strike measured from a leg the plan does not have",
        ),
        pytest.param(
            [_leg("a", strike={"selector": "leg_distance", "leg_id": "a", "dollars": 5})],
            "a -> a",
            id="a strike measured from itself",
        ),
        pytest.param(
            [
                _leg("a", strike={"selector": "leg_distance", "leg_id": "b", "dollars": 5}),
                _leg("b", strike={"selector": "leg_distance", "leg_id": "a", "dollars": -5}),
            ],
            "a -> b -> a",
            id="two strikes measured from each other",
        ),
        pytest.param(
            [
                _leg("a", strike={"selector": "leg_distance", "leg_id": "b", "dollars": 5}),
                _leg("b", strike={"selector": "leg_distance", "leg_id": "c", "dollars": 5}),
                _leg("c", strike={"selector": "leg_distance", "leg_id": "b", "dollars": -5}),
            ],
            "b -> c -> b",
            id="a loop the first leg leads into but is not part of",
        ),
        pytest.param(
            [
                _leg("a", strike={"selector": "leg_distance", "leg_id": "d", "dollars": 5}),
                _leg("b", strike={"selector": "leg_distance", "leg_id": "c", "dollars": 5}),
                _leg("c", strike={"selector": "leg_distance", "leg_id": "b", "dollars": -5}),
                _leg("d", **_SHORT),
            ],
            "b -> c -> b",
            id="a loop the first leg has nothing to do with",
        ),
    ],
)
def test_a_structure_one_order_cannot_carry_is_refused_with_the_reason(legs: list[dict], reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        _structure(*legs)


@pytest.mark.parametrize(
    ("exits", "reason"),
    [
        pytest.param(["bought"], "'sold' is closed 0 times", id="a leg the exit leaves open"),
        pytest.param(["bought", "sold", "sold"], "'sold' is closed 2 times", id="a leg the exit closes twice"),
    ],
)
def test_an_exit_that_does_not_close_exactly_what_the_entry_opened_is_refused(exits: list[str], reason: str) -> None:
    """Asymmetric exits were once to be a warning (ADR 0012 §8). For an option structure they are a refusal."""
    with pytest.raises(ValidationError, match=reason):
        ActionPlan(
            on_enter=[_leg("bought"), _leg("sold", **_SHORT, strike={"selector": "atm_offset", "offset": 5})],
            on_exit=[{"kind": "close_leg", "entry_leg_id": leg_id} for leg_id in exits],
        )


def test_a_stock_plan_is_not_held_to_the_option_rules() -> None:
    """Stock plans keep the validation they had. This one breaks every option rule and still reads:
    six legs, two underlyings, ratios with a common divisor, a sold leg nothing covers, and no exit."""
    legs = [
        {
            "leg_id": f"leg_{index}",
            "instrument": {"kind": "stock", "underlying": "SPY" if index % 2 else "QQQ"},
            "position": "short" if index == 0 else "long",
            "qty_ratio": 2,
        }
        for index in range(6)
    ]

    assert len(ActionPlan(on_enter=legs, on_exit=[]).on_enter) == 6


# ---- the structure's name (#2913) -------------------------------------------


def test_a_named_structure_keeps_its_name_and_a_plan_without_one_is_written_as_before() -> None:
    """The name is in the plan when it has one, so two plans that differ only by name are two plans.
    A plan without one has no such key at all, so its hashes are what they were before names existed."""
    legs = [_leg("bought"), _leg("sold", **_SHORT, strike={"selector": "atm_offset", "offset": 5})]
    named = ActionPlan.model_validate(_structure(*legs).model_dump() | {"structure": "bull_call_spread"})
    custom = _structure(*legs)

    assert named.model_dump(mode="json")["structure"] == "bull_call_spread"
    assert ActionPlan.model_validate_json(named.model_dump_json()) == named
    assert "structure" not in custom.model_dump()
    assert "structure" not in custom.model_dump_json()
    assert ActionPlan.model_validate(custom.model_dump()) == custom


@pytest.mark.parametrize(
    ("name", "legs", "reason"),
    [
        pytest.param(
            "bull_call_spread",
            [_leg("one"), _leg("two", strike={"selector": "atm_offset", "offset": 5})],
            "a bull call spread is 1 bought call and 1 sold call",
            id="two bought calls called a spread",
        ),
        pytest.param(
            "bull_call_spread",
            [_leg("bought", **_PUT), _leg("sold", **_PUT, **_SHORT, strike={"selector": "atm_offset", "offset": -5})],
            "a bull call spread is 1 bought call and 1 sold call",
            id="a put spread called a call spread",
        ),
        pytest.param(
            "call_butterfly",
            [
                _leg("lower", strike={"selector": "atm_offset", "offset": -5}),
                _leg("body", **_SHORT),
                _leg("upper", strike={"selector": "atm_offset", "offset": 5}),
            ],
            "a call butterfly is 1 bought call and 2 sold calls and 1 bought call",
            id="a butterfly that sells its body once",
        ),
        pytest.param("long_call", [_leg("call"), _leg("put", **_PUT)], "a long call is 1 bought call", id="a straddle called a long call"),
    ],
)
def test_a_name_whose_legs_are_not_what_it_is_made_of_is_refused(name: str, legs: list[dict], reason: str) -> None:
    with pytest.raises(ValidationError, match=reason):
        ActionPlan.model_validate(_structure(*legs).model_dump() | {"structure": name})


def test_a_stock_plan_cannot_carry_a_structures_name() -> None:
    with pytest.raises(ValidationError, match="a plan with no option leg is not a long call"):
        ActionPlan(on_enter=[_STOCK_LEG], on_exit=[], structure="long_call")


# ---- what a plan trades (#2912) --------------------------------------------


def _stock_plan(underlying: str) -> ActionPlan:
    return ActionPlan.model_validate(
        {
            "on_enter": [
                {
                    "leg_id": "primary",
                    "instrument": {"kind": "stock", "underlying": underlying},
                    "position": "long",
                    "qty_ratio": 1,
                }
            ],
            "on_exit": [{"kind": "close_leg", "entry_leg_id": "primary"}],
        }
    )


def _option_leg(leg_id: str, underlying: str) -> dict[str, object]:
    return {
        "leg_id": leg_id,
        "instrument": {"kind": "option", "underlying": underlying},
        "position": "long",
        "qty_ratio": 1,
        "right": "call",
        "strike": {"selector": "atm"},
        "expiry": {"selector": "nearest_weekly"},
    }


def test_a_plan_in_the_signals_own_shares_is_not_trading_something_else() -> None:
    assert trades_the_signals_shares(_stock_plan("SPY"), "spy")
    assert not trades_the_signals_shares(_stock_plan("QQQ"), "SPY")


def test_options_on_the_signals_own_symbol_are_something_else() -> None:
    """Decided by the instrument's type and identity, not by comparing tickers."""
    plan = ActionPlan.model_validate(
        {"on_enter": [_option_leg("call", "SPY")], "on_exit": [{"kind": "close_leg", "entry_leg_id": "call"}]}
    )

    assert traded_instrument(plan).kind == "option"
    assert not trades_the_signals_shares(plan, "SPY")


def test_a_plan_whose_legs_name_two_instruments_has_no_one_answer() -> None:
    plan = ActionPlan.model_validate(
        {
            "on_enter": [
                {"leg_id": leg_id, "instrument": {"kind": "stock", "underlying": underlying}, "position": "long", "qty_ratio": 1}
                for leg_id, underlying in (("a", "SPY"), ("b", "QQQ"))
            ]
        }
    )

    with pytest.raises(ValueError, match="exactly one instrument"):
        traded_instrument(plan)


@pytest.mark.parametrize(
    ("traded", "listens_to", "second_symbol"),
    [("QQQ", "SPY", "QQQ"), ("qqq", "SPY", "QQQ"), ("SPY", "spy", None)],
    ids=["another-stock", "another-stock-in-lower-case", "its-own-shares"],
)
def test_the_second_symbol_a_bot_needs_live_is_the_one_its_orders_are_in(
    traded: str, listens_to: str, second_symbol: str | None
) -> None:
    assert traded_market_symbol(_stock_plan(traded), listens_to) == second_symbol


def test_options_on_the_symbol_a_bot_listens_to_need_no_second_symbol_live() -> None:
    """They are something else to trade, but the market they need live is the same one."""
    plan = ActionPlan.model_validate(
        {"on_enter": [_option_leg("call", "SPY")], "on_exit": [{"kind": "close_leg", "entry_leg_id": "call"}]}
    )

    assert traded_market_symbol(plan, "SPY") is None
