"""The Deploy form's two option reads on a Clerk: the named-structure catalogue and the preview (#2913).

The routes are asked through the app, as the builder will ask them. What a
preview works out is proven in ``tests/services/test_option_structure_preview.py``;
here it is the request and the answer on the wire.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.exceptions import RequestValidationError
from httpx import ASGITransport
from pydantic import TypeAdapter

from app.options import order_terms
from app.options.bounds import Bounds
from app.options.deadlines import CloseTerms
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, DEFAULT_LEG_GAP_LIMIT_PERCENT
from app.options.expiry import option_expiry_anchor_ms
from app.options.named_structures import NamedStructure
from app.options.structure_catalogue import order_term_starts
from app.schemas.action_plan import ActionPlan, ExpirySelector, StrikeSelector
from app.schemas.option_preview import OptionStructurePreview, OptionStructurePreviewRequest
from app.services.broker_v2_panel import option_structure_panel
from app.services.option_search import get_option_searches
from app.utils.error_handlers import request_validation_exception_handler
from tests.broker.v2panel.fixtures import ACCT

# A Clerk's option quotes on a stand-in Gateway, and a bull call spread to ask about.
from tests.services.test_option_structure_preview import _BULL_CALL_SPREAD, _KNOWN, FRIDAY, Desk

_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"
_PREVIEW = f"{_BOTS}/option-structure-preview"
_SPREAD = _BULL_CALL_SPREAD.model_dump(mode="json")


async def _get(app, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


async def _post(app, body: dict, path: str = _PREVIEW) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(path, json=body)


def _on(desk: Desk, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the route at ``desk``: its supervisor, its watch list, its clock and its window's reach."""
    monkeypatch.setattr(option_structure_panel, "get_option_quotes", lambda: desk.status)
    monkeypatch.setattr(option_structure_panel, "get_option_watch_list", lambda: desk.watch)
    monkeypatch.setattr(option_structure_panel, "now_ms_utc", lambda: desk.ibkr.now)
    monkeypatch.setattr(option_structure_panel, "_model_inputs", lambda *_: _KNOWN)
    monkeypatch.setattr(option_structure_panel, "_preview_searches", lambda: desk.searches)
    monkeypatch.setattr(
        option_structure_panel, "ibkr_settings", lambda: SimpleNamespace(option_window_strikes_each_side=desk.reach)
    )


# ---- the catalogue ---------------------------------------------------------------


async def test_the_catalogue_offers_every_named_structure_once_in_the_builders_groups(deploy_app) -> None:
    app, _registry = deploy_app

    response = await _get(app, f"{_BOTS}/option-structures")

    assert response.status_code == 200, response.text
    catalogue = response.json()
    assert [len(group["structures"]) for group in catalogue["groups"]] == [2, 6, 4]
    offered = [start["structure"] for group in catalogue["groups"] for start in group["structures"]]
    assert sorted(offered) == sorted(NamedStructure)
    assert all(group["label"] for group in catalogue["groups"])
    assert all(start["label"] and start["description"] for group in catalogue["groups"] for start in group["structures"])
    assert catalogue["max_legs"] == 4


async def test_every_start_the_catalogue_gives_is_a_structure_the_preview_takes_as_it_is(deploy_app) -> None:
    """The builder posts a start's legs back unchanged. Each must be a structure that can be deployed."""
    app, _registry = deploy_app
    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()

    for group in catalogue["groups"]:
        for start in group["structures"]:
            body = {"preview_key": "page-0001", "underlying": "SPY", "structure": start["structure"], "legs": start["legs"]}
            seen = (await _post(app, body)).json()

            assert seen["verdict"]["can_be_deployed"] is True, start["structure"]
            assert (seen["state"], seen["structure"]) == ("no_quote_source", start["structure"])


# What an owner might put in a box, by the unit the catalogue gives the box. A date is its 16:00 New York
# anchor, never a date's text; a leg is another leg's id.
_TYPED = {
    "delta": 0.30,
    "strikes": -2,
    "dollars": 5.0,
    "percent": 1.5,
    "days": 7,
    "date": option_expiry_anchor_ms(FRIDAY),
    "leg": "other_leg",
}


def _from_the_catalogue_alone(rule: dict, *, tolerance_said: bool = True) -> dict:
    """A leg's strike or expiry rule, built from nothing but what the catalogue says of it: its selector,
    something typed into each box it names, and its "Within" left at the default it gives."""
    built = {"selector": rule["selector"]}
    for box in ("value", "measured_from"):
        if rule.get(box) is not None:
            built[rule[box]["field"]] = _TYPED[rule[box]["unit"]]
    if tolerance_said and rule.get("tolerance") is not None:
        built[rule["tolerance"]["field"]] = rule["tolerance"]["default"]
    return built


async def test_the_catalogue_offers_the_strike_rules_the_builder_lists_each_with_the_action_plans_own_tolerance(
    deploy_app,
) -> None:
    """A rule's "Within" starts at the tolerance the Action Plan gives the rule when none is said, in the rule's
    own unit. The catalogue reads it there and keeps no second copy."""
    app, _registry = deploy_app
    as_a_strike_rule = TypeAdapter(StrikeSelector)

    rules = (await _get(app, f"{_BOTS}/option-structures")).json()["strike_rules"]

    assert [rule["selector"] for rule in rules] == ["delta", "atm_offset", "leg_distance", "percent"]
    assert [None if rule["tolerance"] is None else rule["tolerance"]["unit"] for rule in rules] == [
        "delta", None, "dollars", "percent",
    ]
    # Only a distance is measured from another leg, and the catalogue says which field names that leg.
    assert [None if rule["measured_from"] is None else rule["measured_from"]["unit"] for rule in rules] == [
        None, None, "leg", None,
    ]
    for rule in rules:
        assert rule["label"] and rule["value"]["unit_label"]
        assert rule["measured_from"] is None or rule["measured_from"]["unit_label"]
        left_unsaid = as_a_strike_rule.validate_python(_from_the_catalogue_alone(rule, tolerance_said=False))
        if rule["tolerance"] is None:
            continue
        assert rule["tolerance"]["unit_label"]
        # Left unsaid, the plan's own default is what the catalogue said the box starts at.
        assert getattr(left_unsaid, rule["tolerance"]["field"]) == rule["tolerance"]["default"]
        assert as_a_strike_rule.validate_python(_from_the_catalogue_alone(rule)) == left_unsaid


async def test_the_catalogue_offers_the_expiry_rules_the_builder_lists_and_each_makes_a_rule_the_plan_takes(
    deploy_app,
) -> None:
    app, _registry = deploy_app
    as_an_expiry_rule = TypeAdapter(ExpirySelector)

    rules = (await _get(app, f"{_BOTS}/option-structures")).json()["expiry_rules"]

    assert [rule["selector"] for rule in rules] == ["min_dte", "nearest_weekly", "same_day", "absolute"]
    assert [None if rule["value"] is None else rule["value"]["unit"] for rule in rules] == ["days", None, None, "date"]
    for rule in rules:
        assert rule["label"]
        assert as_an_expiry_rule.validate_python(_from_the_catalogue_alone(rule)).selector == rule["selector"]
    # A fixed date is sent as its 16:00 New York anchor, never as a date's text.
    assert _TYPED["date"] == _BULL_CALL_SPREAD.legs[0].expiry.expiration_ms


async def test_every_rule_the_catalogue_offers_makes_a_leg_the_action_plan_takes_from_the_catalogues_own_fields_alone(
    deploy_app,
) -> None:
    """The builder knows a rule only by what the catalogue says of it. Each strike rule with each expiry rule,
    with something in every box the catalogue names and nothing else, is a leg of a plan the Action Plan takes:
    the catalogue leaves the builder nothing a rule requires to know by itself. A distance needs the leg it is
    measured from, and the catalogue names that field too."""
    app, _registry = deploy_app
    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()

    def leg(leg_id: str, strike: dict, expiry: dict) -> dict:
        return {
            "leg_id": leg_id,
            "instrument": {"kind": "option", "underlying": "SPY"},
            "position": "long",
            "qty_ratio": 1,
            "right": "call",
            "strike": _from_the_catalogue_alone(strike),
            "expiry": _from_the_catalogue_alone(expiry),
        }

    stands_alone = next(rule for rule in catalogue["strike_rules"] if rule["measured_from"] is None)
    taken = []
    for strike in catalogue["strike_rules"]:
        for expiry in catalogue["expiry_rules"]:
            legs = [leg(_TYPED["leg"], stands_alone, expiry), leg("this_leg", strike, expiry)]
            plan = ActionPlan.model_validate(
                {"on_enter": legs, "on_exit": [{"kind": "close_leg", "entry_leg_id": each["leg_id"]} for each in legs]}
            )
            built = plan.on_enter[1]
            taken.append((built.strike.selector, built.expiry.selector))
            # Every field the plan's own rule has, beside its selector, is one the catalogue named.
            assert set(type(built.strike).model_fields) == set(legs[1]["strike"]), strike["selector"]
            assert set(type(built.expiry).model_fields) == set(legs[1]["expiry"]), expiry["selector"]

    assert taken == [
        (strike["selector"], expiry["selector"]) for strike in catalogue["strike_rules"] for expiry in catalogue["expiry_rules"]
    ]
    assert len(taken) == 16


async def test_the_catalogue_gives_the_order_terms_a_deploy_starts_from_each_where_the_code_holds_it(deploy_app) -> None:
    app, _registry = deploy_app

    terms = (await _get(app, f"{_BOTS}/option-structures")).json()["order_terms"]

    assert set(terms) == {"gap_limit_percent", "entry_wait_seconds", "cost_limit_percent", "expiry_close_lead_minutes"}
    assert all(term["label"] and term["unit_label"] and term["help"] for term in terms.values())
    assert Decimal(terms["gap_limit_percent"]["default"]) == DEFAULT_LEG_GAP_LIMIT_PERCENT
    assert Decimal(terms["cost_limit_percent"]["default"]) == DEFAULT_COST_LIMIT_PERCENT
    # The cost limit a preview is held to when the page says none is the one the catalogue shows.
    assert OptionStructurePreviewRequest.model_fields["cost_limit_percent"].default == DEFAULT_COST_LIMIT_PERCENT
    held_to = CloseTerms()
    assert terms["entry_wait_seconds"]["default"] * 1_000 == held_to.entry_wait_ms
    assert terms["expiry_close_lead_minutes"]["default"] * 60_000 == held_to.expiry_close_lead_ms
    # What the catalogue shows is a limit a preview takes as it is.
    asked = {**_SPREAD, **{name: terms[name]["default"] for name in ("gap_limit_percent", "cost_limit_percent")}}
    assert (await _post(app, asked)).status_code == 200


def test_an_order_term_follows_the_close_terms_it_is_read_from_and_is_never_shown_rounded() -> None:
    twenty = order_term_starts(CloseTerms(entry_wait_ms=20_000, expiry_close_lead_ms=30 * 60_000))

    assert (twenty["entry_wait_seconds"].default, twenty["expiry_close_lead_minutes"].default) == (20, 30)
    with pytest.raises(ValueError):
        order_term_starts(CloseTerms(entry_wait_ms=20_500))
    with pytest.raises(ValueError):
        order_term_starts(CloseTerms(expiry_close_lead_ms=90_000))


# ---- the preview -----------------------------------------------------------------


async def test_a_preview_answers_in_the_published_shape_with_money_in_cents_and_prices_as_exact_decimals(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app
    desk = Desk(monkeypatch)
    _on(desk, monkeypatch)
    await _post(app, _SPREAD)
    await desk.settle()
    desk.spy()
    await desk.settle()
    desk.quote_every_line()
    desk.quote("call", 585, "4.08", "4.12")
    desk.quote("call", 590, "2.18", "2.22")
    desk.ibkr.now += 400

    response = await _post(app, _SPREAD)

    assert response.status_code == 200, response.text
    seen = response.json()
    assert set(seen) == set(OptionStructurePreview.model_fields)
    assert (seen["state"], seen["structure"], seen["structures"]) == ("resolved", "bull_call_spread", 1)
    assert (seen["quotes_read_ms"], seen["the_money"]) == (desk.ibkr.now, "585.11")
    bought = seen["legs"][0]
    assert bought["contract"] == {
        "occ_symbol": "SPY261009C00585000",
        "underlying": "SPY",
        "expiry_ms": _BULL_CALL_SPREAD.legs[0].expiry.expiration_ms,
        "right": "call",
        "strike": "585",
    }
    assert bought["quote"] == {"bid": "4.08", "bid_age_ms": 400, "ask": "4.12", "ask_age_ms": 400}
    assert isinstance(bought["delta"], float)
    money = seen["money"]
    assert (money["net_price"], money["premium_is"], money["net_entry_premium_cents"]) == ("1.94", "paid", 19_400)
    assert (money["broker_requirement_cents"], money["fee_estimate_cents"], money["entry_commitment_cents"]) == (0, 10, 19_410)
    assert (seen["at_expiry"]["loss_cents"], seen["at_expiry"]["gain_cents"]) == (19_400, 30_600)
    assert seen["at_expiry"]["gain_holds"] == [{"from_price": "590", "to_price": None}]
    assert seen["at_expiry"]["break_even_prices"] == ["586.94"]
    assert (seen["lean"]["direction"], seen["verdict"]["red"]) == ("up", False)
    assert OptionStructurePreview.model_validate(seen).state == "resolved"


async def _resolved(app, desk: Desk, body: dict) -> dict:
    """Ask until the spread's lines are quoted, then once more: the answer with figures."""
    await _post(app, body)
    await desk.settle()
    desk.spy()
    await desk.settle()
    desk.quote_every_line()
    desk.quote("call", 585, "4.08", "4.12")
    desk.quote("call", 590, "2.18", "2.22")
    desk.ibkr.now += 400
    response = await _post(app, body)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize(
    ("written", "read_as"),
    [
        pytest.param("1e1", "10", id="an exponent"),
        pytest.param("10.000000", "10", id="trailing zeros"),
        pytest.param("7.50", "7.5", id="one trailing zero"),
        pytest.param("0.000001", "0.000001", id="a millionth of a percent, the finest there is"),
    ],
)
async def test_a_limit_is_carried_back_and_said_as_the_owner_would_write_it(
    deploy_app, monkeypatch: pytest.MonkeyPatch, written: str, read_as: str
) -> None:
    """``1e1`` is ten percent. It is never printed as ``1E+1%``."""
    app, _registry = deploy_app
    desk = Desk(monkeypatch)
    _on(desk, monkeypatch)

    seen = await _resolved(app, desk, {**_SPREAD, "cost_limit_percent": written, "gap_limit_percent": written})

    assert (seen["state"], seen["cost_limit_percent"], seen["gap_limit_percent"]) == ("resolved", read_as, read_as)
    assert seen["money"]["cost_limit_percent"] == read_as
    assert f"{read_as}%" in seen["money"]["cost_sentence"] and "E" not in seen["money"]["cost_sentence"].split("limit")[1]


@pytest.mark.parametrize("written", ["-0", "-0.000", "0.0", "0"])
async def test_a_cost_limit_of_nothing_is_carried_back_and_said_as_zero_whatever_sign_it_was_written_with(
    deploy_app, monkeypatch: pytest.MonkeyPatch, written: str
) -> None:
    app, _registry = deploy_app
    desk = Desk(monkeypatch)
    _on(desk, monkeypatch)

    seen = await _resolved(app, desk, {**_SPREAD, "cost_limit_percent": written})

    assert (seen["state"], seen["cost_limit_percent"], seen["money"]["cost_limit_percent"]) == ("resolved", "0", "0")
    assert "-0" not in seen["money"]["cost_sentence"]


async def test_on_a_process_with_no_option_quote_source_the_preview_says_so_and_shows_no_figure(deploy_app) -> None:
    app, _registry = deploy_app

    response = await _post(app, _SPREAD)

    assert response.status_code == 200, response.text
    seen = response.json()
    assert set(seen) == set(OptionStructurePreview.model_fields)
    assert (seen["state"], seen["reason_code"]) == ("no_quote_source", "OPTION_PREVIEW_NO_QUOTE_SOURCE")
    assert (seen["legs"], seen["money"], seen["at_expiry"], seen["lean"]) == ([], None, None, None)


async def test_a_structure_the_plan_refuses_is_an_answer_and_not_an_error(deploy_app) -> None:
    app, _registry = deploy_app
    uncovered = {**_SPREAD, "structure": None, "legs": [_SPREAD["legs"][1] | {"strike": {"selector": "atm"}}]}

    response = await _post(app, uncovered)

    assert response.status_code == 200, response.text
    assert (response.json()["state"], response.json()["verdict"]["can_be_deployed"]) == ("plan_refused", False)


_BY_DELTA = _SPREAD["legs"][0] | {"strike": {"selector": "delta", "target": 0.30}}


def _leg_free(strike: dict, expiry: dict) -> dict:
    return {"leg_id": "only", "position": "long", "qty_ratio": 1, "right": "call", "strike": strike, "expiry": expiry}

_OUT_OF_RANGE = "OPTION_PREVIEW_VALUE_OUT_OF_RANGE"


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"preview_key": None}, id="no key to hold its quotes under"),
        pytest.param({"underlying": "not a ticker"}, id="an underlying that is no ticker"),
        pytest.param({"cost_limit_percent": "NaN"}, id="a cost limit that is no number"),
        pytest.param({"cost_limit_percent": "ten"}, id="a cost limit that is a word"),
        pytest.param({"structures": [3]}, id="a list where a count belongs"),
        pytest.param({"structure": "short_straddle"}, id="a name that is no named structure"),
        pytest.param({"what_else": 1}, id="a field nobody defined"),
        pytest.param({"legs": [_SPREAD["legs"][0] | {"strike": {"selector": "by_feel"}}]}, id="a strike rule nobody defined"),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"expiry": {"selector": "absolute", "expiration_ms": 1_791_302_400_001}}]},
            id="a fixed date that is inside its range and is no expiry",
        ),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"expiry": {"selector": "absolute", "expiration_ms": True}}]},
            id="a truth value where a date belongs, which reads as the first millisecond there is",
        ),
        # One fault that is a range's and one that is not: the request is malformed, and stays so.
        pytest.param({"structures": 0, "what_else": 1}, id="a number out of range beside a field nobody defined"),
    ],
)
async def test_a_request_that_is_not_a_preview_request_is_refused_as_malformed(deploy_app, broken: dict) -> None:
    app, _registry = deploy_app

    response = await _post(app, {**_SPREAD, **broken})

    assert response.status_code == 422, response.text


async def test_a_gap_limit_left_out_is_the_one_the_catalogue_starts_from_and_a_gap_limit_of_null_is_malformed(
    deploy_app,
) -> None:
    """A request says a number or leaves the field out. Left out, a leg's gap is judged by the limit an owner
    starts from, as the catalogue gives it. Null is neither: it was once "not judged", and is no longer taken."""
    app, _registry = deploy_app
    starts_at = (await _get(app, f"{_BOTS}/option-structures")).json()["order_terms"]["gap_limit_percent"]["default"]
    left_out = {name: value for name, value in _SPREAD.items() if name != "gap_limit_percent"}

    taken = await _post(app, left_out)
    with_null = await _post(app, {**_SPREAD, "gap_limit_percent": None})

    assert "gap_limit_percent" not in left_out and taken.status_code == 200, taken.text
    assert taken.json()["gap_limit_percent"] == taken.json()["order_terms"]["gap_limit_percent"] == starts_at == "8"
    assert with_null.status_code == 422, with_null.text


@pytest.mark.parametrize("no_number", ["NaN", "Infinity", "-Infinity"])
async def test_a_number_that_is_no_number_is_malformed_wherever_it_is_typed_and_is_never_an_error(
    deploy_app, no_number: str
) -> None:
    """A page can send what JSON has no word for. It is not a number outside a range: it is no number. The
    service's own handler answers such a request, as it does on every route, and is given it untouched."""
    app, _registry = deploy_app
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    as_sent = (
        '{"preview_key": "page-0001", "underlying": "SPY", "legs": [{"leg_id": "only", "position": "long", '
        '"qty_ratio": 1, "right": "call", "strike": {"selector": "delta", "target": '
        + no_number
        + '}, "expiry": {"selector": "nearest_weekly"}}]}'
    )

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(_PREVIEW, content=as_sent, headers={"content-type": "application/json"})
    as_text = await _post(app, {**_SPREAD, "gap_limit_percent": no_number})

    assert (response.status_code, as_text.status_code) == (422, 422), (response.text, as_text.text)


@pytest.mark.parametrize(
    ("typed", "said_of", "legs_named"),
    [
        pytest.param({"structures": 0}, ["0", "1 to 100"], [], id="no structures"),
        pytest.param({"cost_limit_percent": "-1"}, ["-1", "0 to 100"], [], id="a cost limit below zero"),
        pytest.param({"cost_limit_percent": "3.3333333"}, ["3.3333333", "0.000001"], [], id="a cost limit finer than it can be compared in"),
        pytest.param({"cost_limit_percent": "1e-7"}, ["1e-7", "0.000001"], [], id="a cost limit finer than that, written with an exponent"),
        # Refused by the bound on its value, before anything writes it out.
        pytest.param({"cost_limit_percent": "1e999999999"}, ["1e999999999", "0 to 100"], [], id="a cost limit too large to write out"),
        pytest.param({"gap_limit_percent": "1e999000"}, ["1e999000", "100"], [], id="a gap limit that would write out to a megabyte"),
        pytest.param({"gap_limit_percent": "7.1234567"}, ["7.1234567", "0.000001"], [], id="a gap limit as fine"),
        pytest.param({"gap_limit_percent": "0"}, ["above 0"], [], id="a gap limit of nothing"),
        pytest.param({"entry_wait_seconds": 0}, ["1 to 30", "50 seconds"], [], id="an entry that waits for nothing"),
        pytest.param({"entry_wait_seconds": 31}, ["31", "1 to 30", "50 seconds"], [], id="a wait a second longer than an owner may choose"),
        pytest.param({"entry_wait_seconds": 300}, ["300", "1 to 30"], [], id="the longest wait the Clerk itself would take"),
        pytest.param({"entry_wait_seconds": 2.5}, ["2.5", "whole number"], [], id="an entry wait that is no whole number"),
        pytest.param({"expiry_close_lead_minutes": 391}, ["391", "0 to 390"], [], id="a close before the session opens"),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"strike": {"selector": "delta", "target": 1.5}}]},
            ["Leg 1", "1.5", "below 1"], ["long_call"], id="a delta above one",
        ),
        pytest.param(
            {"legs": [_SPREAD["legs"][0], _SPREAD["legs"][1] | {"qty_ratio": 0}]},
            ["Leg 2", "1 to 1,000"], ["short_call"], id="a second leg of no contracts",
        ),
        pytest.param({"legs": [_BY_DELTA] * 9}, ["9", "1 to 4"], [], id="nine legs"),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"strike": {"selector": "atm_offset", "offset": 1.5}}]},
            ["Leg 1", "1.5", "whole number"], ["long_call"], id="a step and a half from the money",
        ),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"qty_ratio": 1.5}]}, ["Leg 1", "1.5", "whole number"], ["long_call"],
            id="a contract and a half",
        ),
        # More digits than the decimal context keeps, in a box with no top. Rounded to 28 digits first, it
        # read as a whole number inside its range, and the request was refused with nothing to read.
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"strike": {"selector": "atm_offset", "offset": "12345678901234567890123456789.5"}}]},
            ["Leg 1", "31 characters", "whole number"], ["long_call"], id="thirty digits of steps and a half",
        ),
        pytest.param(
            {"legs": [_SPREAD["legs"][0], _SPREAD["legs"][1] | {"strike": {"selector": "leg_distance", "leg_id": "long_call", "dollars": 1e308}}]},
            ["Leg 2", "99,999.999"], ["short_call"], id="a distance no two strikes are apart",
        ),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"expiry": {"selector": "absolute", "expiration_ms": 10**30}}]},
            ["Leg 1", "2000", "2099"], ["long_call"], id="a date past any a symbol can write",
        ),
        pytest.param(
            {"structures": 101, "legs": [_SPREAD["legs"][0] | {"expiry": {"selector": "min_dte", "days": 0}}]},
            ["Leg 1", "101", "1 or more"], ["long_call"], id="two numbers out of range at once",
        ),
    ],
)
async def test_a_number_outside_its_range_is_answered_in_words_as_what_cannot_be_deployed(
    deploy_app, typed: dict, said_of: list[str], legs_named: list[str]
) -> None:
    """It was a 422 with nothing an owner could read. Now the page is told which box, what was typed and what the
    box takes, in the shape of every other answer."""
    app, _registry = deploy_app

    response = await _post(app, {**_SPREAD, **typed})

    assert response.status_code == 200, response.text
    seen = response.json()
    assert set(seen) == set(OptionStructurePreview.model_fields)
    assert OptionStructurePreview.model_validate(seen).state == "plan_refused"
    assert (seen["reason_code"], seen["verdict"]["deploy_reason_code"]) == (_OUT_OF_RANGE, _OUT_OF_RANGE)
    assert (seen["verdict"]["can_be_deployed"], seen["verdict"]["red"], seen["ask_again_in_ms"]) == (False, True, None)
    assert all(part in seen["notice"] for part in said_of), seen["notice"]
    assert seen["notice"] in seen["verdict"]["deploy_sentence"]
    assert (seen["leg_ids"], seen["verdict"]["leg_ids"]) == (legs_named, legs_named)
    assert not any(f"'{leg_id}'" in seen["notice"] for leg_id in ("long_call", "short_call"))
    # Nothing is resolved, and the rules and terms it was asked with are not said back as if they stood.
    assert (seen["legs"], seen["money"], seen["legs_asked"], seen["order_terms"]) == ([], None, [], None)


@pytest.mark.parametrize(
    "legs",
    [
        pytest.param([_leg_free({"selector": "atm_offset", "offset": 10**400}, {"selector": "nearest_weekly"})], id="steps beyond counting"),
        pytest.param([_leg_free({"selector": "atm_offset", "offset": -(10**400)}, {"selector": "nearest_weekly"})], id="steps below, beyond counting"),
        pytest.param([_leg_free({"selector": "atm"}, {"selector": "min_dte", "days": 10**400})], id="days beyond counting"),
        pytest.param([_leg_free({"selector": "delta", "target": 5e-324}, {"selector": "same_day"})], id="the smallest delta there is"),
        pytest.param(
            [_leg_free({"selector": "delta", "target": 0.9999999999999999, "tolerance_delta": 5e-324}, {"selector": "same_day"})],
            id="the largest delta below one, with no room at all",
        ),
        pytest.param([_leg_free({"selector": "percent", "percent": 1000, "tolerance_percent": 100}, {"selector": "nearest_weekly"})], id="ten times the price"),
        pytest.param([_leg_free({"selector": "percent", "percent": -99.99999999999999}, {"selector": "nearest_weekly"})], id="all but nothing"),
    ],
)
async def test_a_number_at_the_far_end_of_what_is_taken_is_answered_and_is_never_an_error(
    deploy_app, monkeypatch: pytest.MonkeyPatch, legs: list[dict]
) -> None:
    """Each is a number the plan takes. What its rule comes to on a real chain is the resolver's to say."""
    app, _registry = deploy_app
    desk = Desk(monkeypatch)
    _on(desk, monkeypatch)
    body = {"preview_key": "page-0001", "underlying": "SPY", "legs": legs, "structures": 100}

    await _post(app, body)
    await desk.settle()
    desk.spy()
    await desk.settle()
    # A cent or two bid and offered on every line the window opened, however far from the money it reached.
    for line in sorted(desk.ibkr.open_option_lines()):
        desk.ibkr.tick(line, bid=0.01, ask=0.03)
    desk.ibkr.now += 400
    response = await _post(app, body)

    assert response.status_code == 200, response.text
    seen = response.json()
    assert seen["verdict"]["can_be_deployed"] is True
    assert OptionStructurePreview.model_validate(seen).legs_asked[0].sentence


async def test_a_refused_number_is_said_back_as_the_default_and_every_other_part_as_it_was_asked(deploy_app) -> None:
    app, _registry = deploy_app

    seen = (await _post(app, {**_SPREAD, "structures": 3, "cost_limit_percent": "150", "gap_limit_percent": "2.50"})).json()

    assert (seen["state"], seen["structures"], seen["gap_limit_percent"]) == ("plan_refused", 3, "2.5")
    assert seen["cost_limit_percent"] == str(DEFAULT_COST_LIMIT_PERCENT) and "150" in seen["notice"]
    assert (seen["structure"], seen["structure_label"], seen["underlying"]) == ("bull_call_spread", "Bull call spread", "SPY")


async def test_a_number_out_of_range_gives_back_the_quote_lines_its_page_held(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app
    desk = Desk(monkeypatch)
    _on(desk, monkeypatch)
    await _resolved(app, desk, _SPREAD)
    held = desk.ibkr.open_option_lines()

    refused = await _post(app, {**_SPREAD, "structures": 0})
    await desk.settle()

    assert refused.json()["reason_code"] == _OUT_OF_RANGE
    assert len(held) == 10 and desk.ibkr.open_option_lines() == set()


async def test_a_number_out_of_range_on_someone_elses_account_is_not_answered(deploy_app) -> None:
    app, _registry = deploy_app

    elsewhere = await _post(
        app, {**_SPREAD, "structures": 0}, "/api/brokers/alpaca/accounts/someone-else/bots/option-structure-preview"
    )
    another_broker = await _post(app, {**_SPREAD, "structures": 0}, f"/api/brokers/ibkr/accounts/{ACCT}/bots/option-structure-preview")

    assert (elsewhere.status_code, another_broker.status_code) == (404, 422)


# ---- every range the catalogue serves is the one the server holds ---------------------------------

_A_HAIR = Decimal("0.000000001")


def _either_side(bounds: dict[str, Any]) -> tuple[list[Decimal], list[Decimal]]:
    """Numbers just inside a served range and just outside it, at each end it has: by one step where the range
    has a step, and by a hair where it has none. One finer than the step is outside too."""
    step = None if bounds["step"] is None else Decimal(bounds["step"])
    by = step or _A_HAIR
    inside, outside = [], []
    for edge, included, outward in ((bounds["min"], bounds["min_included"], -1), (bounds["max"], bounds["max_included"], 1)):
        if edge is None:
            continue
        end = Decimal(edge)
        inside.append(end if included else end - outward * by)
        outside.append(end + outward * by if included else end)
    if step is not None and inside:
        outside.append(inside[0] + step / 10)
    return inside, outside


def _as_json(number: Decimal | str, *, text: bool) -> Any:
    """A percent an owner sets travels as text. Every other number travels as a number. A number handed in as
    text is sent as that text, whatever the box: a page can send one, and JSON has no number that large."""
    if isinstance(number, str):
        return number
    if text:
        return str(number)
    return int(number) if number == number.to_integral_value() and abs(number) < 10**15 else float(number)


def _leg_with(strike: dict[str, Any], expiry: dict[str, Any], *, leg_id: str = "this_leg", qty: Any = 1) -> dict[str, Any]:
    return {"leg_id": leg_id, "position": "long", "qty_ratio": qty, "right": "call", "strike": strike, "expiry": expiry}


async def _boxes(app, *, with_the_date: bool = False) -> list[tuple[str, dict[str, Any], Any]]:
    """Every number the catalogue gives a range for: what to call it, its range, and how to ask with it typed in.
    The fixed date is a box too, and is left out unless asked for: its range is two moments, not two numbers."""
    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()
    weekly = {"selector": "nearest_weekly"}
    by_delta = {"selector": "delta", "target": 0.30}
    stands_alone = _leg_with(by_delta, weekly, leg_id=_TYPED["leg"])

    def asking(**parts: Any) -> dict[str, Any]:
        return {"preview_key": "page-0001", "underlying": "SPY", "legs": [stands_alone], **parts}

    boxes: list[tuple[str, dict[str, Any], Any]] = []
    for rule in catalogue["strike_rules"]:
        for box in ("value", "tolerance"):
            if rule[box] is None:
                continue

            def typed(number: Decimal, rule=rule, box=box) -> dict[str, Any]:
                strike = _from_the_catalogue_alone(rule) | {rule[box]["field"]: _as_json(number, text=False)}
                return asking(legs=[stands_alone, _leg_with(strike, weekly)])

            boxes.append((f"{rule['selector']} {box}", rule[box]["bounds"], typed))
    for rule in catalogue["expiry_rules"]:
        if rule["value"] is None or (rule["value"]["unit"] == "date" and not with_the_date):
            continue

        def typed(number: Decimal, rule=rule) -> dict[str, Any]:
            expiry = {"selector": rule["selector"], rule["value"]["field"]: _as_json(number, text=False)}
            return asking(legs=[stands_alone, _leg_with(by_delta, expiry)])

        boxes.append((f"{rule['selector']} value", rule["value"]["bounds"], typed))
    boxes.append(
        (
            "leg quantity",
            catalogue["leg_quantity_bounds"],
            # Beside a leg of one contract, so that the two are in their lowest terms whatever the number.
            lambda number: asking(legs=[stands_alone, _leg_with(by_delta, weekly, qty=_as_json(number, text=False))]),
        )
    )
    boxes.append(("structures", catalogue["structures_bounds"], lambda number: asking(structures=_as_json(number, text=False))))
    for name, term in catalogue["order_terms"].items():
        boxes.append((name, term["bounds"], lambda number, name=name: asking(**{name: _as_json(number, text="percent" in name)})))
    return boxes


async def test_a_number_just_inside_each_range_the_catalogue_serves_is_taken_and_one_just_outside_is_refused_in_words(
    deploy_app,
) -> None:
    """The catalogue says what each number may be so that a form can refuse one before asking. What it says is
    held to what the server does, at each end of every range: a number a step inside is a structure the plan
    takes, and one a step outside, or finer than the step, is refused as out of range, by name. Neither is ever
    an error, and neither is a refusal with nothing to read."""
    app, _registry = deploy_app
    boxes = await _boxes(app)
    tried = {"inside": 0, "outside": 0}

    for what, bounds, typed in boxes:
        inside, outside = _either_side(bounds)
        for number in inside:
            response = await _post(app, typed(number))
            assert response.status_code == 200, (what, number, response.text)
            seen = response.json()
            # A structure the plan takes, on a Clerk with no quote source to resolve it against.
            assert (seen["state"], seen["verdict"]["can_be_deployed"]) == ("no_quote_source", True), (what, number, seen["notice"])
            tried["inside"] += 1
        for number in outside:
            response = await _post(app, typed(number))
            assert response.status_code == 200, (what, number, response.text)
            seen = response.json()
            assert (seen["state"], seen["reason_code"]) == ("plan_refused", _OUT_OF_RANGE), (what, number, seen["notice"])
            assert seen["notice"] and seen["verdict"]["red"], (what, number)
            tried["outside"] += 1

    # Fourteen boxes: seven of the strike rules', one of the expiry rules', a leg's quantity, the structures, four terms.
    assert len(boxes) == 14
    assert tried["inside"] >= 20 and tried["outside"] >= 25


_A_SPREAD_OF_NUMBERS = [
    Decimal(number)
    for number in (
        "-1000000000000", "-100000", "-99999.999", "-99999.9985", "-100", "-1", "-0.5", "0", "0.0000001", "0.0005",
        "0.5", "1", "1.5", "45", "100", "100.5", "300", "301", "390", "391", "1000", "1000.5", "1001", "99999.998",
        "99999.9985", "99999.999", "100000", "1000000000000",
    )
]


def _as_served(bounds: dict[str, Any]) -> Bounds:
    def exact(end: str | None) -> Decimal | None:
        return None if end is None else Decimal(end)

    return Bounds(exact(bounds["min"]), bounds["min_included"], exact(bounds["max"]), bounds["max_included"], exact(bounds["step"]))


async def test_what_the_catalogue_says_of_a_number_is_what_the_server_does_with_it_whatever_the_number(deploy_app) -> None:
    """Twenty-eight numbers, from far below nothing to far above any strike, typed into each of the fourteen
    boxes. Where the range the catalogue served takes the number, the server takes it. Where the range refuses
    it, the server refuses it in words. A bound the server holds and the catalogue left out would show here
    as a request refused with nothing to read; one the catalogue made up, as a number taken that it called
    refused."""
    app, _registry = deploy_app
    taken = refused = 0

    for what, bounds, typed in await _boxes(app):
        served = _as_served(bounds)
        for number in _A_SPREAD_OF_NUMBERS:
            response = await _post(app, typed(number))
            assert response.status_code == 200, (what, number, response.text)
            seen = response.json()
            if served.fault(number) is None:
                assert (seen["state"], seen["verdict"]["can_be_deployed"]) == ("no_quote_source", True), (what, number, seen["notice"])
                taken += 1
            else:
                assert (seen["state"], seen["reason_code"]) == ("plan_refused", _OUT_OF_RANGE), (what, number, seen["notice"])
                refused += 1

    assert taken + refused == 14 * len(_A_SPREAD_OF_NUMBERS) and taken >= 100 and refused >= 100


_TOO_LARGE_TO_WRITE_OUT = ["1e999999999", "-1e999999999", "1e1000000", "-1e1000000"]
_TOO_SMALL_TO_WRITE_OUT = ["1e-999999999", "1e-1000000"]


async def test_a_number_written_as_text_with_a_huge_exponent_is_refused_or_answered_in_words_in_every_box(
    deploy_app,
) -> None:
    """Numbers a page can send as text and no JSON number can be: too large, or too small, for anything to
    round or write out. Typed into each of the fifteen boxes. None is an error: a box with no top once failed
    on these.

    One too large is a malformed request or is refused in words as out of range, and is never taken. One too
    small is nothing to a box that holds a float, which reads it as zero: taken where zero is in the box's
    range, refused in words where it is not.
    """
    app, _registry = deploy_app
    boxes = await _boxes(app, with_the_date=True)
    answered = {"in words": 0, "malformed": 0, "read as nothing": 0}

    for what, _bounds, typed in boxes:
        for number in _TOO_LARGE_TO_WRITE_OUT + _TOO_SMALL_TO_WRITE_OUT:
            response = await _post(app, typed(number))
            assert response.status_code in (200, 422), (what, number, response.status_code, response.text[:300])
            if response.status_code == 422:
                answered["malformed"] += 1
                continue
            seen = response.json()
            if seen["state"] == "no_quote_source":
                # A structure the plan takes, on a Clerk with no quote source to resolve it against.
                assert number in _TOO_SMALL_TO_WRITE_OUT and seen["verdict"]["can_be_deployed"], (what, number)
                answered["read as nothing"] += 1
                continue
            assert (seen["state"], seen["reason_code"]) == ("plan_refused", _OUT_OF_RANGE), (what, number, seen["notice"])
            assert seen["notice"] and seen["verdict"]["red"] and len(seen["notice"]) < 400, (what, number)
            answered["in words"] += 1

    # Seven of the strike rules', two of the expiry rules', a leg's quantity, the structures, four terms.
    assert len(boxes) == 15
    assert sum(answered.values()) == 15 * 6
    # Each answer happens: a box with a top or a bottom says so, and one with neither has nothing to say.
    assert answered["in words"] >= 40 and answered["malformed"] >= 6 and answered["read as nothing"] >= 1


async def test_a_number_as_text_in_a_box_with_no_top_is_malformed_above_it_and_refused_in_words_below_its_bottom(
    deploy_app,
) -> None:
    """Steps from the money have no top and no bottom, and days out have a bottom of one and no top. These are
    the two boxes where nothing stops a huge number before its step is read."""
    app, _registry = deploy_app

    async def asked(strike: dict[str, Any], expiry: dict[str, Any]) -> httpx.Response:
        return await _post(app, {"preview_key": "page-0001", "underlying": "SPY", "legs": [_leg_with(strike, expiry)]})

    weekly, at_the_money = {"selector": "nearest_weekly"}, {"selector": "atm"}
    for huge in ("1e999999999", "-1e999999999", "1e1000000"):
        assert (await asked({"selector": "atm_offset", "offset": huge}, weekly)).status_code == 422, huge
    for huge in ("1e999999999", "1e1000000"):
        assert (await asked(at_the_money, {"selector": "min_dte", "days": huge})).status_code == 422, huge
    below = await asked(at_the_money, {"selector": "min_dte", "days": "-1e999999999"})
    assert below.status_code == 200, below.text
    assert (below.json()["reason_code"], below.json()["leg_ids"]) == (_OUT_OF_RANGE, ["this_leg"])
    assert "-1e999999999" in below.json()["notice"] and "1 or more" in below.json()["notice"]


async def test_a_fixed_date_is_taken_from_the_first_to_the_last_a_contract_symbol_can_carry(deploy_app) -> None:
    app, _registry = deploy_app
    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()
    (fixed,) = [rule for rule in catalogue["expiry_rules"] if rule["value"] is not None and rule["value"]["unit"] == "date"]
    bounds = fixed["value"]["bounds"]

    async def asked(day: date) -> dict[str, Any]:
        expiry = {"selector": "absolute", "expiration_ms": option_expiry_anchor_ms(day)}
        body = {"preview_key": "page-0001", "underlying": "SPY", "legs": [_leg_with({"selector": "atm"}, expiry)]}
        response = await _post(app, body)
        assert response.status_code == 200, response.text
        return response.json()

    # What the catalogue serves is the two anchors, as moments, and they are the ends of what is taken.
    assert (bounds["min_ms"], bounds["max_ms"]) == (
        option_expiry_anchor_ms(date(2000, 1, 1)), option_expiry_anchor_ms(date(2099, 12, 31)),
    )
    assert bounds["min_included"] and bounds["max_included"] and bounds["step"] is None
    assert [(await asked(day))["state"] for day in (date(2000, 1, 1), date(2099, 12, 31))] == ["no_quote_source"] * 2
    for day in (date(1999, 12, 31), date(2100, 1, 1)):
        seen = await asked(day)
        assert (seen["state"], seen["reason_code"], seen["leg_ids"]) == ("plan_refused", _OUT_OF_RANGE, ["this_leg"])
        # The range reads forwards, with its years in full: "'00 to '99" reads as backwards.
        assert "Leg 1" in seen["notice"] and seen["notice"].index("2000") < seen["notice"].index("2099")
        assert "'00" not in seen["notice"] and "'99" not in seen["notice"]


def _every_range(served: Any, found: list[tuple[str, dict[str, Any]]] | None = None, under: str = "") -> list[tuple[str, dict[str, Any]]]:
    """Every range in the served catalogue, with the name of what holds it."""
    found = [] if found is None else found
    if isinstance(served, dict):
        if "bounds" in served:
            found.append((f"{under}:{served.get('unit', '')}", served["bounds"]))
        for name, part in served.items():
            if name.endswith("_bounds"):
                found.append((name, part))
            else:
                _every_range(part, found, name)
    elif isinstance(served, list):
        for part in served:
            _every_range(part, found, under)
    return found


def _every_text(served: Any) -> list[str]:
    """Every value in the served catalogue that is text, wherever it sits."""
    if isinstance(served, dict):
        return [text for part in served.values() for text in _every_text(part)]
    if isinstance(served, list):
        return [text for part in served for text in _every_text(part)]
    return [served] if isinstance(served, str) else []


async def test_a_time_in_the_served_catalogue_is_a_number_of_milliseconds_and_never_text(deploy_app) -> None:
    """The fixed date's range is two moments. They are served as JSON numbers, ``int64 ms UTC``, in fields of
    their own. A number's range is served as exact decimals, in text, and carries no moment. No range is served
    both ways, and none carries a moment in a field that takes text."""
    app, _registry = deploy_app
    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()

    ranges = _every_range(catalogue)

    of_dates = [bounds for name, bounds in ranges if name.endswith(":date")]
    of_numbers = [bounds for name, bounds in ranges if not name.endswith(":date")]
    # Seven strike boxes, days out, the fixed date, a leg's quantity, the structures and four terms.
    assert (len(of_dates), len(of_numbers)) == (1, 14)
    (dates,) = of_dates
    assert (type(dates["min_ms"]), type(dates["max_ms"])) == (int, int)
    assert 0 < dates["min_ms"] < dates["max_ms"] <= 253_402_300_799_999
    assert (dates["min"], dates["max"], dates["step"]) == (None, None, None)
    assert "2000" in dates["sentence"] and "2099" in dates["sentence"]
    for bounds in of_numbers:
        assert (bounds["min_ms"], bounds["max_ms"]) == (None, None), bounds
        assert all(end is None or isinstance(end, str) for end in (bounds["min"], bounds["max"], bounds["step"])), bounds
    # No text anywhere in the catalogue carries either moment.
    assert not any(str(dates[end]) in text for end in ("min_ms", "max_ms") for text in _every_text(catalogue))


async def test_the_range_of_each_order_term_is_the_one_the_terms_own_module_states(deploy_app) -> None:
    app, _registry = deploy_app

    terms = (await _get(app, f"{_BOTS}/option-structures")).json()["order_terms"]

    stated: dict[str, Bounds] = {
        "gap_limit_percent": order_terms.GAP_LIMIT_PERCENT,
        "entry_wait_seconds": order_terms.ENTRY_WAIT_SECONDS,
        "cost_limit_percent": order_terms.COST_LIMIT_PERCENT,
        "expiry_close_lead_minutes": order_terms.EXPIRY_CLOSE_LEAD_MINUTES,
    }
    for name, bounds in stated.items():
        served = terms[name]["bounds"]
        assert (Decimal(served["min"]), served["min_included"]) == (bounds.lowest, bounds.lowest_included), name
        assert (Decimal(served["max"]), served["max_included"]) == (bounds.highest, bounds.highest_included), name
        assert Decimal(served["step"]) == bounds.step and served["sentence"], name
        # Where a term starts is inside what it may be.
        assert bounds.fault(Decimal(str(terms[name]["default"]))) is None, name


async def test_the_entry_wait_is_offered_from_one_to_thirty_seconds_with_the_reason_it_is_no_longer(deploy_app) -> None:
    """Narrower than the Clerk's own five minutes. The catalogue says why beside the range, and a wait refused
    for being outside it is told the same reason."""
    app, _registry = deploy_app

    terms = (await _get(app, f"{_BOTS}/option-structures")).json()["order_terms"]

    wait = terms["entry_wait_seconds"]
    assert (wait["bounds"]["min"], wait["bounds"]["max"], wait["bounds"]["step"], wait["default"]) == ("1", "30", "1", 20)
    assert wait["range_reason"] == order_terms.ENTRY_WAIT_RANGE_REASON and "50 seconds" in wait["range_reason"]
    # The other three ranges need no reason beside them.
    assert [terms[name]["range_reason"] for name in terms if name != "entry_wait_seconds"] == [None, None, None]
    taken = await _post(app, {**_SPREAD, "entry_wait_seconds": 30})
    refused = await _post(app, {**_SPREAD, "entry_wait_seconds": 31})
    assert (taken.json()["state"], taken.json()["order_terms"]["entry_wait_seconds"]) == ("no_quote_source", 30)
    assert refused.json()["reason_code"] == _OUT_OF_RANGE and wait["range_reason"] in refused.json()["notice"]


async def test_a_rule_with_no_bound_on_a_side_says_so_and_takes_a_number_of_any_size_there(
    deploy_app, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Steps from the money and days out have no top the plan holds them to, and steps no bottom. A number far
    past any chain is a structure the plan takes, and what it comes to is the resolver's to say: never an error."""
    app, _registry = deploy_app
    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()
    steps = next(rule for rule in catalogue["strike_rules"] if rule["selector"] == "atm_offset")["value"]["bounds"]
    days = next(rule for rule in catalogue["expiry_rules"] if rule["selector"] == "min_dte")["value"]["bounds"]
    weekly = {"selector": "nearest_weekly"}

    assert (steps["min"], steps["max"], steps["step"]) == (None, None, "1")
    assert (days["min"], days["max"], days["step"]) == ("1", None, "1")
    for strike, expiry in (
        ({"selector": "atm_offset", "offset": 10**12}, weekly),
        ({"selector": "atm_offset", "offset": -(10**12)}, weekly),
        ({"selector": "atm"}, {"selector": "min_dte", "days": 10**12}),
    ):
        desk = Desk(monkeypatch)
        _on(desk, monkeypatch)
        body = {"preview_key": "page-0001", "underlying": "SPY", "legs": [_leg_with(strike, expiry)]}
        # Asked until the chain is listed, SPY is quoted and every line the window opened has a quote.
        await _post(app, body)
        await desk.settle()
        desk.spy()
        await desk.settle()
        desk.quote_every_line()
        desk.ibkr.now += 400
        response = await _post(app, body)

        assert response.status_code == 200, (strike, expiry, response.text)
        seen = response.json()
        assert seen["verdict"]["can_be_deployed"] is True and seen["state"] != "resolved", (strike, expiry)
        assert seen["notice"]


async def test_the_catalogue_names_each_structure_two_ways_and_says_what_how_tells_every_option_bot(deploy_app) -> None:
    app, _registry = deploy_app

    catalogue = (await _get(app, f"{_BOTS}/option-structures")).json()

    starts = [start for group in catalogue["groups"] for start in group["structures"]]
    assert all(start["in_a_sentence"].endswith(" on") and start["label"][0].isupper() for start in starts)
    assert {start["in_a_sentence"].split()[0] for start in starts} == {"a", "an"}
    assert len({start["in_a_sentence"] for start in starts} | {catalogue["custom_in_a_sentence"]}) == len(starts) + 1
    notes = catalogue["order_term_notes"]
    assert set(notes) == {"entries_sentence", "exits_sentence", "hours_sentence", "alpaca_sentence"}
    assert all(notes.values()) and len(set(notes.values())) == 4
    # An exit has no price limit, and the owner is told so in as many words.
    assert "no price limit" in notes["exits_sentence"] and "limit" in notes["entries_sentence"]
    # What Alpaca may do near expiry is its documentation's word, in its own terms, and is said to be unseen
    # on this account. It is not said as a thing this system has watched happen.
    assert "documentation" in notes["alpaca_sentence"] and "before expiry" in notes["alpaca_sentence"]
    assert "not yet been seen" in notes["alpaca_sentence"]


def test_a_pages_search_goes_to_this_processs_preview_lane_and_never_to_the_lane_a_bots_entry_uses() -> None:
    searches = get_option_searches()

    assert option_structure_panel._preview_searches() is searches.preview
    assert searches.preview.lane is not searches.entry


async def test_the_option_reads_answer_only_for_this_brokers_own_account(deploy_app) -> None:
    app, _registry = deploy_app

    another_account = await _post(app, _SPREAD, "/api/brokers/alpaca/accounts/someone-else/bots/option-structure-preview")
    another_broker = await _post(app, _SPREAD, f"/api/brokers/ibkr/accounts/{ACCT}/bots/option-structure-preview")
    catalogue_elsewhere = await _get(app, "/api/brokers/alpaca/accounts/someone-else/bots/option-structures")

    assert (another_account.status_code, another_broker.status_code, catalogue_elsewhere.status_code) == (404, 404, 404)
