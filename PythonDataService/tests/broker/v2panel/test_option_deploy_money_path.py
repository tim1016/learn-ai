"""An option Deploy through the real routes, the real structure preview and the real money review (#2913).

``test_deploy_option_structure`` replaces the money review with a stand-in
to look at what Deploy asks of it. Here nothing between the click and the
registry is replaced: the route, the structure preview on a stand-in
Gateway, the review of a budget on real books, the token and the consent.
The registry records what a start was handed, and Alpaca is a stand-in.
Nothing is sent to a broker.

What is held here, in an owner's words:

- an option bot is deployed only while one entry of its structure can be
  priced, with a budget on the request or without one;
- the review never says the quote source is missing when it is not, and it
  lists everything that stops the Deploy;
- a bot in shares is reviewed in the bytes it always was.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.config import settings
from app.schemas.deployment_budget import DeploymentBudgetPreview
from app.services import market_liveness
from app.services.broker_v2_panel import bot_custody, budget_deploy, option_deploy
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.v2panel.conftest import _BODY, _SETTINGS
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_budget_deploy import _Sync
from tests.broker.v2panel.test_deploy_option_structure import (
    _ACCEPTANCE,
    _ONE_CALL,
    _SPREAD,
    _Floor,
    floor,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.v2panel.test_deploy_traded_symbol import _serve
from tests.broker.v2panel.test_panel_deploy_live import _live_view
from tests.broker.v2panel.test_panel_deploy_shadow import _shadow_view

_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"
_NO_QUOTE_SOURCE = "OPTION_PREVIEW_NO_QUOTE_SOURCE"
# A strike nobody lists: the rules come to no contract, and that never passes by itself.
_NOBODY_LISTS_IT = {**_ONE_CALL, "legs": [{**_ONE_CALL["legs"][0], "strike": {"selector": "absolute", "strike": "9999"}}]}


@pytest.fixture
def books(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SimpleNamespace]:
    """A real budget authority for the route's own account: $5,000 of cash, a risk policy at revision 1."""
    repo = ClerkSqliteRepository.initialize(account_id=ACCT, artifacts_root=tmp_path / "books", clock=_TestClock(NOON))
    policy = AccountRiskPolicy(1, 0.1, 100, "profile", 1, "owner", NOON)
    commit_budget_authority_cutover(repo, actor="owner", reviewed_token="empty-account", stop_receipt="no-runs")
    append_risk_policy(repo, policy=policy, expected_revision=0)
    record_fee_evidence(repo, [], checked_at_ms=NOON, history_complete=True)
    observation = AccountObservation(
        observed_at_ms=NOON, broker_cash_usd=5000, cash_available_usd=5000, last_equity_usd=5000, unrealized_pl_usd=0,
        position_count=0, risk_revision=1, equity_usd=5000, risk_cash_flow_evidence_complete=True,
        risk_cash_flow_window_start_ms=day_pnl_window_start_ms(NOON),
        risk_equity_window_start_ms=day_pnl_window_start_ms(NOON),
    )
    gate = LiveEnvelopeGate(values=None, custody_is_simulated=False)
    gate.publish(observation)
    runtime = SimpleNamespace(
        selected_account_id=repo.account_id, account_authority_kind="real_paper", sqlite_repository=repo,
        envelope_sync=_Sync(gate, observation, SimpleNamespace(observation=observation, policy=policy, hold=None)),
        reconnecting=False, startup_failure=None,
    )
    monkeypatch.setattr(budget_deploy, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(bot_custody, "get_active_clerk_runtime", lambda: runtime)
    monkeypatch.setattr(bot_custody, "get_bot_task_registry", lambda: None)
    monkeypatch.setattr(
        budget_deploy, "get_broker_configuration_service",
        lambda: SimpleNamespace(owner=lambda: SimpleNamespace(owner_id="server-owner")),
    )
    monkeypatch.setattr(budget_deploy, "same_symbol_note", lambda account_id, request: None)
    market_liveness.reset_market_liveness_store_for_testing()
    yield runtime
    market_liveness.reset_market_liveness_store_for_testing()
    repo.close()


async def _post(app, path: str, **changes: object) -> httpx.Response:
    body = {**(_BODY if path == "" else _SETTINGS), **changes}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"{_BOTS}{path}", json=body)


async def _review(app, **changes: object) -> dict[str, Any]:
    response = await _post(app, "/budget-preview", **changes)
    assert response.status_code == 200, response.text
    return response.json()


async def _quotes_up(app, floor: _Floor, **body: object) -> None:  # noqa: F811
    """The review is asked once, as a page asks it, which opens its quote lines. Then they are quoted."""
    await _review(app, **body)
    await floor.lines_are_quoted(body["trades"]["legs"][0]["right"])


def _refused(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    return response.json()["detail"]


def _gates(review: dict[str, Any]) -> list[str]:
    return [blocker["gate_id"] for blocker in review["option_review"]["blockers"]]


# ── no price at the click, no Deploy ────────────────────────────────────


async def test_on_paper_an_option_deploy_with_no_budget_and_no_price_is_refused_by_name_and_then_held_to_alpaca(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """Alpaca lists no contract and reports no buying power. While the quotes have not arrived the click is
    refused for having no price. Once they have, the same click is refused for what Alpaca says: the answer
    never turns on whether a price happened to be there."""
    app, registry = deploy_app
    floor.alpaca.listed = {}
    floor.alpaca.buying_power = Decimal("0")
    click = {"execution_mode": "paper", "trades": _ONE_CALL, "traded_asset_acknowledgement": _ACCEPTANCE}

    not_priced = _refused(await _post(app, "", **click))
    await floor.lines_are_quoted()
    priced = _refused(await _post(app, "", **click, submission_key="submission-0002"))

    assert not_priced["reason_code"] == option_deploy.NOT_PRICED
    assert not_priced["message"].startswith("Waiting to price this structure.")
    assert not_priced["submission_settled"] is True
    assert priced["reason_code"] == option_deploy.BUYING_POWER_SHORT
    assert registry.deploy_calls == []


async def test_a_dry_run_whose_rules_come_to_no_contract_is_not_deployed_with_no_budget(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """The review says this structure cannot be priced. The click, with no budget to hold it to a price, says
    the same and deploys nothing."""
    app, registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _NOBODY_LISTS_IT}
    await _quotes_up(app, floor, execution_mode="dry_run", trades=_ONE_CALL)
    await _review(app, **body)
    await floor.desk.settle()
    floor.desk.quote_every_line()
    floor.desk.ibkr.now += 400

    review = await _review(app, **body)
    refusal = _refused(await _post(app, "", **body))

    assert review["state"] == "unavailable" and review["detail"].startswith("This structure cannot be priced right now.")
    assert review["option_review"]["blockers"] == [], "nothing stops it but the price it does not have"
    assert refusal["reason_code"] == option_deploy.NOT_PRICED
    assert refusal["message"] == review["detail"], "the click is refused in the price's own sentence"
    assert refusal["submission_settled"] is True and registry.deploy_calls == []


async def test_a_budgeted_click_with_no_price_is_refused_under_the_same_name(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    app, registry = deploy_app

    refusal = _refused(await _post(
        app, "", execution_mode="dry_run", trades=_ONE_CALL,
        budget={"amount_usd": "1000.00", "risk_revision": 0, "review_token": "x" * 64},
    ))

    assert refusal["reason_code"] == option_deploy.NOT_PRICED and registry.deploy_calls == []


# ── the review says nothing of quotes it did not read, and lists everything ──


@pytest.mark.parametrize(
    ("what", "gate"),
    [
        ("a spread", "OPTION_STRUCTURE_NOT_SUPPORTED"),
        ("live with the setting off", option_deploy.LIVE_SWITCHED_OFF),
        ("a live shadow", option_deploy.LIVE_SHADOW_NOT_BUILT),
        ("a short line budget", "OPTION_WINDOW_OVER_LINE_BUDGET"),
    ],
)
async def test_with_quotes_up_a_review_that_something_stops_never_says_the_quote_source_is_missing(
    deploy_app, floor: _Floor, books, monkeypatch: pytest.MonkeyPatch, what: str, gate: str  # noqa: F811
) -> None:
    app, _registry = deploy_app
    await _quotes_up(app, floor, execution_mode="dry_run", trades=_ONE_CALL)
    body: dict[str, Any] = {"execution_mode": "dry_run", "trades": _ONE_CALL}
    if what == "a spread":
        body["trades"] = _SPREAD
    elif what == "live with the setting off":
        _serve(monkeypatch, _live_view)
        books.account_authority_kind = "real_live"
        body["execution_mode"] = "live"
    elif what == "a live shadow":
        _serve(monkeypatch, _shadow_view)
        books.account_authority_kind = "shadow"
        body["execution_mode"] = "shadow"
    else:
        floor.line_budget = 1

    review = await _review(app, **body)

    said = review["option_review"]
    assert gate in _gates(review)
    assert said["preview_state"] != "no_quote_source" and said["preview_reason_code"] != _NO_QUOTE_SOURCE
    assert "no IBKR quote source" not in json.dumps(review)
    assert review["state"] == "unavailable" and review["review_token"] is None
    assert floor.alpaca.asked == [], "a Dry Run, a Live Shadow and a switched-off Live review ask Alpaca nothing"


@pytest.mark.parametrize(
    ("trades", "gate"),
    [
        ({**_ONE_CALL, "structure": None, "legs": []}, "OPTION_PREVIEW_NO_LEGS"),
        ({**_ONE_CALL, "underlying": "BRK.B"}, "OPTION_PREVIEW_SHARE_CLASS_UNDERLYING"),
    ],
    ids=["no legs", "a share class"],
)
async def test_a_structure_no_plan_can_hold_is_reviewed_without_a_word_about_the_preview(
    deploy_app, floor: _Floor, books, trades: dict[str, Any], gate: str  # noqa: F811
) -> None:
    """The preview was not asked, and the review says exactly that: no state of the preview's, no reason code
    and no sentence. The blocker carries the words."""
    app, _registry = deploy_app

    review = await _review(app, execution_mode="dry_run", trades=trades)

    said = review["option_review"]
    assert _gates(review) == [gate]
    assert (said["preview_state"], said["preview_reason_code"], said["preview_sentence"]) == ("not_asked", None, None)
    assert (said["ask_again_in_ms"], said["quotes_read_ms"], said["money_rows"], said["picks"]) == (None, None, [], [])
    assert review["detail"] == said["blockers"][0]["headline"]


async def test_a_clerk_with_no_option_quotes_of_its_own_reviews_without_asking_the_preview(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    app, _registry = deploy_app
    floor.shared_status_source = True

    review = await _review(app, execution_mode="dry_run", trades=_ONE_CALL)

    assert _gates(review) == ["OPTION_CLERK_HAS_NO_OPTION_QUOTES"]
    assert review["option_review"]["preview_state"] == "not_asked"


async def test_the_review_lists_everything_that_stops_a_deploy_and_the_click_is_refused_for_the_first(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """A spread on a Paper account approved for covered calls only, with $1 of buying power, on a Clerk that
    counts one quote line free. The review lists all of it. The click names the first and asks Alpaca nothing."""
    app, registry = deploy_app
    await _quotes_up(app, floor, execution_mode="paper", trades=_ONE_CALL)
    floor.alpaca.level, floor.alpaca.buying_power, floor.line_budget = 1, Decimal("1"), 1
    asked_before = len(floor.alpaca.asked)

    review = await _review(app, execution_mode="paper", trades=_SPREAD)
    asked_by_the_review = floor.alpaca.asked[asked_before:]
    refusal = _refused(await _post(app, "", execution_mode="paper", trades=_SPREAD, traded_asset_acknowledgement=_ACCEPTANCE))

    assert _gates(review)[:3] == [
        "OPTION_STRUCTURE_NOT_SUPPORTED", "OPTION_WINDOW_OVER_LINE_BUDGET", option_deploy.APPROVAL_TOO_LOW,
    ]
    assert "spreads level (3)" in review["option_review"]["blockers"][2]["headline"]
    assert review["detail"] == review["option_review"]["blockers"][0]["headline"], "the budget says the first"
    assert asked_by_the_review[0] == "account"
    assert refusal["reason_code"] == "OPTION_STRUCTURE_NOT_SUPPORTED" and registry.deploy_calls == []
    assert floor.alpaca.asked[asked_before + len(asked_by_the_review):] == [], "a click that is stopped asks nobody"


# ── a symbol the preview's own request would refuse ─────────────────────


@pytest.mark.parametrize("path", ["/budget-preview", "/admission"])
async def test_a_listened_to_symbol_longer_than_the_preview_takes_is_answered_and_never_a_500(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch, path: str  # noqa: F811
) -> None:
    app, _registry = deploy_app
    money = DeploymentBudgetPreview(state="unavailable", detail="-", world="synthetic")
    monkeypatch.setattr(budget_deploy, "preview_budget", lambda *_args, **_kwargs: money)
    monkeypatch.setattr(budget_deploy, "same_symbol_note", lambda account_id, request: None)

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    ) as client:
        response = await client.post(
            f"{_BOTS}{path}", json={**_SETTINGS, "symbol": "ABCDEF", "execution_mode": "dry_run", "trades": _ONE_CALL}
        )

    assert response.status_code == 200, response.text


# ── a bot in shares ─────────────────────────────────────────────────────


async def test_the_money_review_of_a_bot_in_shares_carries_no_option_review_at_all(deploy_app, books) -> None:
    """Not a null one: the key is left out, so the review is the bytes it was before the field existed."""
    app, _registry = deploy_app

    in_shares = await _review(app, execution_mode="dry_run")

    assert "option_review" not in in_shares
    assert "option_review" not in DeploymentBudgetPreview(state="ready", detail="Ready", world="synthetic").model_dump()
    assert settings.ALPACA_LIVE_OPTION_BOTS_ENABLED is False
