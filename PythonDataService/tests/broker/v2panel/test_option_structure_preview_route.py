"""The Deploy form's two option reads on a Clerk: the named-structure catalogue and the preview (#2913).

The routes are asked through the app, as the builder will ask them. What a
preview works out is proven in ``tests/services/test_option_structure_preview.py``;
here it is the request and the answer on the wire.
"""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest
from httpx import ASGITransport

from app.options.named_structures import NamedStructure
from app.schemas.option_preview import OptionStructurePreview
from app.services.broker_v2_panel import option_structure_panel
from tests.broker.v2panel.fixtures import ACCT

# A Clerk's option quotes on a stand-in Gateway, and a bull call spread to ask about.
from tests.services.test_option_structure_preview import _BULL_CALL_SPREAD, _KNOWN, Desk

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


async def test_the_option_reads_answer_only_for_this_brokers_own_account(deploy_app) -> None:
    app, _registry = deploy_app

    another_account = await _post(app, _SPREAD, "/api/brokers/alpaca/accounts/someone-else/bots/option-structure-preview")
    another_broker = await _post(app, _SPREAD, f"/api/brokers/ibkr/accounts/{ACCT}/bots/option-structure-preview")
    catalogue_elsewhere = await _get(app, "/api/brokers/alpaca/accounts/someone-else/bots/option-structures")

    assert (another_account.status_code, another_broker.status_code, catalogue_elsewhere.status_code) == (404, 404, 404)
