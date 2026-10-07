"""The Deploy form's two option reads on a Clerk: the named-structure catalogue and the preview (#2913).

The routes are asked through the app, as the builder will ask them. What a
preview works out is proven in ``tests/services/test_option_structure_preview.py``;
here it is the request and the answer on the wire.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport
from pydantic import TypeAdapter

from app.options.deadlines import CloseTerms
from app.options.entry import DEFAULT_COST_LIMIT_PERCENT, DEFAULT_LEG_GAP_LIMIT_PERCENT
from app.options.expiry import option_expiry_anchor_ms
from app.options.named_structures import NamedStructure
from app.options.structure_catalogue import order_term_starts
from app.schemas.action_plan import ExpirySelector, StrikeSelector
from app.schemas.option_preview import OptionStructurePreview, OptionStructurePreviewRequest
from app.services.broker_v2_panel import option_structure_panel
from app.services.option_search import get_option_searches
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


# What an owner might type into the box beside each rule.
_TYPED = {"target": 0.30, "offset": -2, "dollars": 5.0, "percent": 1.5, "days": 7}


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
    for rule in rules:
        assert rule["label"] and rule["value"]["unit_label"]
        built = {"selector": rule["selector"], rule["value"]["field"]: _TYPED[rule["value"]["field"]]}
        if rule["selector"] == "leg_distance":
            built["leg_id"] = "other"
        left_unsaid = as_a_strike_rule.validate_python(built)
        if rule["tolerance"] is None:
            continue
        assert rule["tolerance"]["unit_label"]
        # Left unsaid, the plan's own default is what the catalogue said the box starts at.
        assert getattr(left_unsaid, rule["tolerance"]["field"]) == rule["tolerance"]["default"]
        said = as_a_strike_rule.validate_python(built | {rule["tolerance"]["field"]: rule["tolerance"]["default"]})
        assert said == left_unsaid


async def test_the_catalogue_offers_the_expiry_rules_the_builder_lists_and_each_makes_a_rule_the_plan_takes(
    deploy_app,
) -> None:
    app, _registry = deploy_app
    as_an_expiry_rule = TypeAdapter(ExpirySelector)
    typed = _TYPED | {"expiration_ms": _BULL_CALL_SPREAD.legs[0].expiry.expiration_ms}

    rules = (await _get(app, f"{_BOTS}/option-structures")).json()["expiry_rules"]

    assert [rule["selector"] for rule in rules] == ["min_dte", "nearest_weekly", "same_day", "absolute"]
    assert [None if rule["value"] is None else rule["value"]["unit"] for rule in rules] == ["days", None, None, "date"]
    for rule in rules:
        assert rule["label"]
        value = {} if rule["value"] is None else {rule["value"]["field"]: typed[rule["value"]["field"]]}
        assert as_an_expiry_rule.validate_python({"selector": rule["selector"]} | value).selector == rule["selector"]
    # A fixed date is sent as its 16:00 New York anchor, never as a date's text.
    assert typed["expiration_ms"] == option_expiry_anchor_ms(FRIDAY)


async def test_the_catalogue_gives_the_order_terms_a_deploy_starts_from_each_where_the_code_holds_it(deploy_app) -> None:
    app, _registry = deploy_app

    terms = (await _get(app, f"{_BOTS}/option-structures")).json()["order_terms"]

    assert set(terms) == {"gap_limit_percent", "entry_wait_seconds", "cost_limit_percent", "expiry_close_lead_minutes"}
    assert all(term["label"] and term["unit_label"] for term in terms.values())
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


@pytest.mark.parametrize(
    "broken",
    [
        pytest.param({"preview_key": None}, id="no key to hold its quotes under"),
        pytest.param({"underlying": "not a ticker"}, id="an underlying that is no ticker"),
        pytest.param({"structures": 0}, id="no structures"),
        pytest.param({"cost_limit_percent": "-1"}, id="a limit below zero"),
        pytest.param({"cost_limit_percent": "3.3333333"}, id="a cost limit finer than it can be compared in"),
        pytest.param({"cost_limit_percent": "1e-7"}, id="a cost limit finer than that, written with an exponent"),
        pytest.param({"cost_limit_percent": "NaN"}, id="a cost limit that is no number"),
        pytest.param({"gap_limit_percent": "7.1234567"}, id="a gap limit as fine"),
        pytest.param({"gap_limit_percent": "0"}, id="a gap limit of nothing"),
        pytest.param({"structure": "short_straddle"}, id="a name that is no named structure"),
        pytest.param(
            {"legs": [_SPREAD["legs"][0] | {"strike": {"selector": "delta", "target": 1.5}}]}, id="a delta above one"
        ),
        pytest.param({"what_else": 1}, id="a field nobody defined"),
    ],
)
async def test_a_request_that_is_not_a_preview_request_is_refused_as_malformed(deploy_app, broken: dict) -> None:
    app, _registry = deploy_app

    response = await _post(app, {**_SPREAD, **broken})

    assert response.status_code == 422, response.text


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
