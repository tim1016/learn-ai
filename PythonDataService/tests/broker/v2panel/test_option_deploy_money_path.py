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

from app.broker.alpaca.clerk.budgets import BudgetUnavailable
from app.broker.alpaca.clerk.funding import EntryNeed, deploy_funding_answer
from app.broker.alpaca.clerk.live_envelope import AccountObservation, LiveEnvelopeGate
from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.account_risk import AccountRiskPolicy, append_risk_policy
from app.broker.alpaca.clerk.sqlite.budget_authority import commit_budget_authority_cutover
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.day_pnl import day_pnl_window_start_ms
from app.broker.alpaca.clerk.sqlite.fee_evidence import record_fee_evidence
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.options import BrokerOptionAccountSnapshot
from app.config import settings
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import DeploymentBudgetPreview
from app.schemas.exit_terms import ExitTerms
from app.schemas.option_deploy import TradesOptions
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services import market_liveness
from app.services import option_structure_preview as preview_service
from app.services.broker_v2_panel import bot_custody, budget_deploy, option_deploy
from app.services.option_structure_preview import FIGURES_GOOD_FOR_MS, plan_of
from tests.broker.alpaca.clerk.sqlite.conftest import NOON, _TestClock
from tests.broker.v2panel.conftest import _BODY, _SETTINGS
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_budget_deploy import _Sync
from tests.broker.v2panel.test_deploy_option_structure import (
    _ACCEPTANCE,
    _ONE_CALL,
    _SPREAD,
    _TERMS,
    CALL_585,
    _Floor,
    floor,  # noqa: F401 -- pytest fixture, used by name
)
from tests.broker.v2panel.test_deploy_traded_symbol import _UNTOUCHED, _serve
from tests.broker.v2panel.test_panel_deploy_live import _live_view
from tests.services.test_option_structure_preview import _AT_THE_MONEY, Desk, _asking, _from, _leg

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
    # Alpaca's answers are read on the books' clock, so a reading is as old here as it is there.
    monkeypatch.setattr(option_deploy, "now_ms_utc", lambda: NOON)
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
        ("a spread", "OPTION_SEVERAL_LEGS_SETTING_OFF"),
        ("live with the setting off", option_deploy.LIVE_SWITCHED_OFF),
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
        body.update(trades=_SPREAD, execution_mode="paper")
    elif what == "live with the setting off":
        _serve(monkeypatch, _live_view)
        books.account_authority_kind = "real_live"
        body["execution_mode"] = "live"
    else:
        floor.line_budget = 1

    review = await _review(app, **body)

    said = review["option_review"]
    assert gate in _gates(review)
    assert said["preview_state"] != "no_quote_source" and said["preview_reason_code"] != _NO_QUOTE_SOURCE
    assert "no IBKR quote source" not in json.dumps(review)
    assert review["state"] == "unavailable" and review["review_token"] is None
    assert floor.alpaca.asked == (["account"] if what == "a spread" else [])


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
        "OPTION_SEVERAL_LEGS_SETTING_OFF", "OPTION_WINDOW_OVER_LINE_BUDGET", option_deploy.APPROVAL_TOO_LOW,
    ]
    assert "spreads level (3)" in review["option_review"]["blockers"][2]["headline"]
    assert review["detail"] == review["option_review"]["blockers"][0]["headline"], "the budget says the first"
    assert asked_by_the_review[0] == "account"
    assert refusal["reason_code"] == "OPTION_SEVERAL_LEGS_SETTING_OFF" and registry.deploy_calls == []
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


# ── the real price, through the real budget review, the token and the click ──


async def test_a_dry_run_is_reviewed_at_the_previews_price_and_the_click_commits_exactly_that(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """Two calls offered at $4.12: one entry sets aside $824.09. A budget a cent under is refused in an entry's
    words, a budget of exactly that is given a token, and the click hands the start a consent for it."""
    app, registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL}

    arriving = await _review(app, **body)
    await floor.lines_are_quoted()
    priced = await _review(app, **body)
    under = await _review(app, **body, budget={"amount_usd": "824.08", "risk_revision": 0})
    exact = await _review(app, **body, budget={"amount_usd": "824.09", "risk_revision": 0})
    admitted = await _post(
        app, "/admission", **body, budget={"amount_usd": "824.09", "risk_revision": 0, "review_token": exact["review_token"]}
    )
    # The stand-in registry commits no budget, so the click's own answer is not what is looked at here: what
    # it handed the start is.
    await _post(app, "", **body, budget={"amount_usd": "824.09", "risk_revision": 0, "review_token": exact["review_token"]})

    assert (arriving["state"], arriving["option_review"]["ask_again_in_ms"]) == ("awaiting_price", 1_000)
    assert (priced["state"], priced["minimum_budget_usd"], priced["estimated_price_usd"]) == ("ready", "824.09", None)
    assert under["state"] == "unavailable" and under["review_token"] is None
    assert under["detail"] == (
        "This budget is only $824.08. The entry needs $824.09 including its fee provision. Shortfall: $0.01."
    )
    assert under["funding"]["binding"] == "bot_budget" and under["funding"]["shortfall_usd"] == "0.01"
    assert exact["state"] == "ready" and exact["review_token"] and exact["budget_usd"] == "824.09"
    assert admitted.status_code == 200, admitted.text
    [call] = registry.deploy_calls
    assert call["budget_consent"].committed_cents == 82_409
    assert call["option_order_terms"] == SealedOptionOrderTerms.model_validate(_TERMS)
    assert call["action_plan"] == plan_of(TradesOptions.model_validate(_ONE_CALL))


async def test_a_token_is_no_consent_once_the_price_has_moved_past_the_budget_or_the_size_has_changed(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    app, registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)
    reviewed = await _review(app, **body, budget={"amount_usd": "824.09", "risk_revision": 0})
    consent = {"amount_usd": "824.09", "risk_revision": 0, "review_token": reviewed["review_token"]}

    three = _refused(await _post(
        app, "", **{**body, "sizing": {"preset": "custom", "quantity": 3}}, budget={**consent, "amount_usd": "5000.00"}
    ))
    floor.desk.quote("call", 585, "4.60", "4.70")
    floor.desk.spy()
    floor.desk.ibkr.now += 400
    moved = _refused(await _post(app, "", **body, budget=consent, submission_key="submission-0002"))

    assert "Review this exact budget and configuration" in json.dumps(three)
    assert "The entry needs $940.09 including its fee provision" in json.dumps(moved)
    assert registry.deploy_calls == []


async def test_on_paper_what_alpaca_says_at_the_click_stops_a_deploy_that_was_reviewed_clean(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """Between the review and the click: buying power falls, the listing goes, the listing cannot be read,
    the level is no longer reported. Each stops the Deploy by its own name. As reviewed, it is taken."""
    app, registry = deploy_app
    body = {"execution_mode": "paper", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)
    reviewed = await _review(app, **body, budget={"amount_usd": "1000.00", "risk_revision": 1})
    assert reviewed["state"] == "ready", reviewed["detail"]
    click = {
        **body, "traded_asset_acknowledgement": _ACCEPTANCE,
        "budget": {"amount_usd": "1000.00", "risk_revision": 1, "review_token": reviewed["review_token"]},
    }

    async def stopped_by(key: str, **alpaca: Any) -> str:
        was = {name: getattr(floor.alpaca, name) for name in alpaca}
        for name, value in alpaca.items():
            setattr(floor.alpaca, name, value)
        refusal = _refused(await _post(app, "", **click, submission_key=key))
        for name, value in was.items():
            setattr(floor.alpaca, name, value)
        assert refusal["submission_settled"] is True
        return refusal["reason_code"]

    unreachable = BrokerUnavailable("Alpaca is unreachable.", broker="alpaca", detail="connection refused")
    assert await stopped_by("submission-0002", buying_power=Decimal("100")) == option_deploy.BUYING_POWER_SHORT
    assert await stopped_by("submission-0003", listed={}) == "OPTION_CONTRACT_NOT_LISTED"
    assert await stopped_by("submission-0004", listing_error=unreachable) == option_deploy.CONTRACT_NOT_READ
    assert await stopped_by("submission-0005", level=None) == option_deploy.APPROVAL_UNKNOWN
    assert await stopped_by("submission-0006", level=1, approved_level=3) == option_deploy.APPROVAL_TOO_LOW
    assert registry.deploy_calls == [] and CALL_585.occ_symbol in floor.alpaca.listed

    await _post(app, "", **click, submission_key="submission-0007")

    [call] = registry.deploy_calls
    assert call["budget_consent"].committed_cents == 100_000 and call["mode"] == "trade"


async def test_option_buying_power_is_held_to_everything_one_entry_sets_aside_to_the_cent(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """$824.00 of premium and 9 cents of fees. $824.08 of buying power is short; $824.09 is enough."""
    app, _registry = deploy_app
    body = {"execution_mode": "paper", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)

    floor.alpaca.buying_power = Decimal("824.08")
    short = await _review(app, **body)
    floor.alpaca.buying_power = Decimal("824.09")
    enough = await _review(app, **body)

    [blocker] = short["option_review"]["blockers"]
    assert blocker["gate_id"] == option_deploy.BUYING_POWER_SHORT
    assert blocker["headline"] == (
        "Alpaca's option buying power is $824.08, but the entry needs $824.09 including its fee provision. "
        "Shortfall: $0.01."
    )
    assert enough["option_review"]["blockers"] == [] and enough["state"] == "ready"


async def test_a_buying_power_reading_that_is_stale_shows_with_its_time_and_is_never_a_pass(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """Alpaca's answer was read 46 seconds ago and the figure would not cover one entry. It is stale, shown at
    the time it was read, a note and no blocker, and it is not counted as buying power that covers the entry."""
    app, _registry = deploy_app
    body = {"execution_mode": "paper", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)
    floor.alpaca.buying_power, floor.alpaca.read_ms_ago = Decimal("1"), 46_000

    stale = await _review(app, **body, budget={"amount_usd": "1000.00", "risk_revision": 1})
    floor.alpaca.buying_power, floor.alpaca.read_ms_ago = None, 0
    unknown = await _review(app, **body, budget={"amount_usd": "1000.00", "risk_revision": 1})

    reading = stale["funding"]["broker_buying_power"]
    assert (reading["state"], reading["as_of_ms"], reading["amount_usd"]) == ("stale", NOON - 46_000, "1.00")
    assert stale["option_review"]["blockers"] == []
    assert option_deploy.BUYING_POWER_STALE in {note["code"] for note in stale["option_review"]["notes"]}
    assert stale["funding"]["binding"] == "bot_budget" and "option buying power" not in stale["funding"]["sentence"]
    unsaid = unknown["funding"]["broker_buying_power"]
    assert (unsaid["state"], unsaid["amount_usd"], unsaid["as_of_ms"]) == ("unknown", None, NOON)
    assert option_deploy.BUYING_POWER_UNKNOWN in {note["code"] for note in unknown["option_review"]["notes"]}


async def test_the_budget_review_the_option_review_and_the_clerks_recheck_read_one_rule(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """One moment: $5,000 of cash, one entry that sets aside $824.09 and Alpaca's $100,000 of buying power.

    A budget of $6,000 is over what the account has. The review refuses it with the figures, the option review
    shows the same figures beside its rows, and the Clerk's re-check at the commit refuses it over the same two
    amounts. A budget of exactly $5,000 passes all three."""
    app, _registry = deploy_app
    body = {"execution_mode": "paper", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)

    over = await _review(app, **body, budget={"amount_usd": "6000.00", "risk_revision": 1})
    exact = await _review(app, **body, budget={"amount_usd": "5000.00", "risk_revision": 1})

    funding = over["funding"]
    assert (funding["free_budget"]["amount_usd"], funding["account_headroom"]["amount_usd"]) == ("6000.00", "5000.00")
    assert funding["need"]["commitment_usd"] == "824.09"
    assert (funding["admits"], funding["binding"], funding["shortfall_usd"]) == (False, "account_headroom", "1000.00")
    assert over["state"] == "unavailable" and over["detail"] == funding["sentence"]
    rows = {row["key"]: row for row in over["option_review"]["money_rows"]}
    assert rows["set_aside"]["amount_usd"] == "$824.09"
    assert (rows["budget_free"]["amount_usd"], rows["headroom_left"]["amount_usd"]) == ("$5,175.91", "$4,175.91")
    assert exact["state"] == "ready" and exact["funding"]["admits"] is True and exact["funding"]["shortfall_usd"] is None

    repo = books.sqlite_repository
    terms = ExitTerms(band_multiple=2.0, spread_cap_bps=100.0, exit_allowance_bps=5.0, provenance="deployed")
    repo.register_strategy_instance(strategy_instance_id="clerk-a", symbol="SPY", config_hash="seal", exit_terms=terms)

    def recheck(cents: int):
        return submit_budgeted_deploy(
            repo, strategy_instance_id="clerk-a", lifecycle_run_id="run-a", world="real_paper", committed_cents=cents,
            configuration_hash="seal", exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")),
            risk_revision=1, actor="owner", envelope=books.envelope_sync.envelope, minimum_position_cost=Decimal(0),
        )

    with pytest.raises(BudgetUnavailable) as refused:
        recheck(600_000)
    assert "$6,000.00" in str(refused.value) and "$5,000.00" in str(refused.value)
    assert recheck(500_000).created is True


async def test_the_option_review_says_how_many_entries_fit_and_that_every_bot_competes_for_the_headroom(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """The budget binds: $1,648.18 is exactly two $824.09 entries, and one cent under it is one."""
    app, _registry = deploy_app
    body = {"execution_mode": "paper", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)

    two = await _review(app, **body, budget={"amount_usd": "1648.18", "risk_revision": 1})
    one = await _review(app, **body, budget={"amount_usd": "1648.17", "risk_revision": 1})

    for review, fits in ((two, 2), (one, 1)):
        assert (review["funding"]["fits"], review["option_review"]["fits"]) == (fits, fits)
        said = review["option_review"]["fits_sentence"]
        assert review["funding"]["fits_sentence"] == said
        assert "Every bot competes for the same account headroom" in said and "never added up across bots" in said
    assert two["option_review"]["fits_sentence"].startswith("2 whole entries fit right now under the bot's budget")


async def test_the_option_review_shows_the_prices_it_assumed_beside_the_time_the_quotes_were_read(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    app, _registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)

    review = (await _review(app, **body))["option_review"]

    assert [(pick["side_label"], pick["price_usd"]) for pick in review["picks"]] == [("Buy", "$4.12")]
    assert review["quotes_read_ms"] is not None
    assert "priced at its ask and each sold one at its bid" in review["prices_sentence"]


async def test_a_red_verdict_is_listed_and_stops_the_click_and_offers_no_fix_that_would_not_help(
    deploy_app, floor: _Floor, books, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """No fee rate is pinned for the day, so what an entry sets aside is not known. Changing the structure's
    rules would not help, and the review does not say it would."""
    app, registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)
    monkeypatch.setattr(preview_service, "_fee_provision", lambda *_args, **_kwargs: None)

    review = await _review(app, **body, budget={"amount_usd": "2000.00", "risk_revision": 0})
    refusal = _refused(await _post(app, "", **body))

    [blocker] = review["option_review"]["blockers"]
    assert blocker["gate_id"] == refusal["reason_code"] == "OPTION_FEE_RATE_NOT_PINNED"
    assert blocker["fix"] is None and review["option_review"]["money_rows"] == []
    assert review["state"] == "unavailable" and review["review_token"] is None
    assert registry.deploy_calls == []


async def test_each_money_row_writes_out_in_dollars_exactly_the_cents_beside_it(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    app, _registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)

    review = (await _review(app, **body, budget={"amount_usd": "1000.00", "risk_revision": 0}))["option_review"]

    assert [(row["key"], row["amount_cents"], row["amount_usd"]) for row in review["money_rows"]] == [
        ("set_aside", 82_409, "$824.09"),
        ("premium", 82_400, "$824.00"),
        ("broker_requirement", 0, "$0.00"),
        ("fees", 9, "$0.09"),
        ("loss_at_expiry", 82_400, "$824.00"),
        ("budget_free", 17_591, "$175.91"),
        ("incremental_requirement", 0, "$0.00"),
        ("headroom_left", 17_591, "$175.91"),
    ]
    assert review["figures_good_until_ms"] == review["quotes_read_ms"] + FIGURES_GOOD_FOR_MS


async def test_an_entry_that_would_be_skipped_right_now_is_said_beside_a_review_that_can_still_be_consented_to(
    deploy_app, floor: _Floor, books  # noqa: F811
) -> None:
    """A call quoted $3.00 by $5.00 under an 8% gap limit. The bot picks again at every entry, so nothing stops
    the Deploy, and the owner is told an entry made now would be skipped."""
    app, _registry = deploy_app
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL}
    await _quotes_up(app, floor, **body)
    floor.desk.quote("call", 585, "3.00", "5.00")
    floor.desk.ibkr.now += 400

    review = await _review(app, **body, budget={"amount_usd": "2000.00", "risk_revision": 0})

    [note] = [said for said in review["option_review"]["notes"] if said["code"] == option_deploy.ENTRY_WOULD_BE_SKIPPED]
    assert "skipped right now" in note["headline"] and "8% limit" in note["detail"]
    assert review["state"] == "ready" and review["review_token"] and review["option_review"]["blockers"] == []


# ── the wait: none at a review, a deadline at the click ─────────────────


async def test_a_review_waits_for_nothing_and_a_click_waits_until_its_deadline_and_no_longer(
    deploy_app, floor: _Floor, books, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """The click's patience is six seconds on a clock, and each ask of the preview takes its own time out of
    it. Here every ask takes two seconds and says to ask again in one: three asks fit, not seven."""
    app, registry = deploy_app
    monkeypatch.setattr(option_deploy, "PRICE_PATIENCE_MS", 6_000)
    now, slept, asks = [100.0], [], []
    ask = option_deploy.option_structure_panel.option_structure_preview

    async def an_ask_that_takes_two_seconds(broker: str, account_id: str, asked: Any) -> Any:
        asks.append(now[0])
        now[0] += 2.0
        return await ask(broker, account_id, asked)

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(option_deploy.option_structure_panel, "option_structure_preview", an_ask_that_takes_two_seconds)
    monkeypatch.setattr(option_deploy, "asyncio", SimpleNamespace(sleep=sleep))
    monkeypatch.setattr(option_deploy, "time", SimpleNamespace(monotonic=lambda: now[0]))

    review = await _review(app, execution_mode="dry_run", trades=_ONE_CALL)
    assert review["state"] == "awaiting_price" and slept == [] and len(asks) == 1

    refusal = _refused(await _post(app, "", execution_mode="dry_run", trades=_ONE_CALL))

    assert refusal["reason_code"] == option_deploy.NOT_PRICED and registry.deploy_calls == []
    assert slept == [1.0, 1.0] and len(asks) == 4, "one ask for the review, three for the click"
    assert now[0] - asks[1] == 8.0, "the last ask began inside the six seconds and was heard out"


# ── what Alpaca's answer about the account is taken to mean ─────────────


def _account(approved: int | None, in_force: int | None, power: str | None = "1000") -> BrokerOptionAccountSnapshot:
    return BrokerOptionAccountSnapshot(
        broker="alpaca", account_id=ACCT, account_mode="paper",
        options_buying_power=None if power is None else Decimal(power),
        options_approved_level=approved, options_trading_level=in_force, observed_at_ms=1,
    )


@pytest.mark.parametrize(
    ("approved", "in_force", "stopped_as"),
    [
        (3, 1, option_deploy.APPROVAL_TOO_LOW),
        (3, 0, option_deploy.APPROVAL_TOO_LOW),
        (1, None, option_deploy.APPROVAL_TOO_LOW),
        (None, None, option_deploy.APPROVAL_UNKNOWN),
        (3, 2, None),
        (2, None, None),
        (None, 2, None),
    ],
)
def test_the_option_level_in_force_decides_and_the_approved_level_only_when_none_is_in_force(
    approved: int | None, in_force: int | None, stopped_as: str | None
) -> None:
    plan = plan_of(TradesOptions.model_validate(_ONE_CALL))

    blocker = option_deploy.approval_blocker(plan, _account(approved, in_force))

    assert (None if blocker is None else blocker.gate_id) == stopped_as


def test_an_account_the_owner_set_to_a_lower_level_than_alpaca_approved_is_not_told_to_ask_alpaca_for_approval() -> None:
    plan = plan_of(TradesOptions.model_validate(_ONE_CALL))

    set_lower = option_deploy.approval_blocker(plan, _account(3, 1))
    not_approved = option_deploy.approval_blocker(plan, _account(1, 1))

    assert set_lower is not None and not_approved is not None
    assert "set to 1" in set_lower.headline and "approved for" in set_lower.headline
    assert "Ask Alpaca" not in (set_lower.fix or "") and "Raise" in (set_lower.fix or "")
    assert "Ask Alpaca" in (not_approved.fix or "")


def test_a_negative_buying_power_is_written_with_its_sign_before_the_dollar() -> None:
    need = EntryNeed.for_options(commitment_cents=82_409, fee_provision_cents=9)

    short = option_deploy.buying_power_finding(_account(2, 2, "-5"), need, 2, now_ms=1)

    assert short is not None and "option buying power is -$5.00" in short.headline


# ── a structure that pays: its rows by the review's own function ────────


async def test_the_rows_of_a_spread_that_pays_add_up_with_a_premium_received(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three bear call spreads, five dollars wide: $921.00 received, $1,500.00 required, 29 cents of fees. What
    is set aside is the requirement less the premium, with the fees: $579.29."""
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread", structures=3,
    )
    seen = await desk.quoted(received, c585=("5.00", "5.04"), c590=("1.89", "1.93"))
    request = AlpacaPaperDeployRequest.model_validate({
        **_SETTINGS, "execution_mode": "paper", "sizing": {"preset": "custom", "quantity": 3},
        "trades": {
            "kind": "options", "underlying": "SPY", "structure": "bear_call_spread",
            "legs": [leg.model_dump(mode="json") for leg in received.legs], "order_terms": _TERMS,
        },
    })
    assert request.options is not None

    review = option_deploy.review_of(request, plan_of(request.options), seen, blockers=[], notes=[])

    rows = {row.key: row for row in review.money_rows}
    assert [(row.key, row.amount_cents, row.amount_usd) for row in review.money_rows if row.part_of_set_aside] == [
        ("premium", -92_100, "$921.00"), ("broker_requirement", 150_000, "$1,500.00"), ("fees", 29, "$0.29"),
    ]
    assert rows["premium"].note == "received", "the dollars carry no sign: the note says which way"
    assert (rows["set_aside"].amount_cents, rows["set_aside"].amount_usd) == (57_929, "$579.29")
    assert sum(row.amount_cents or 0 for row in review.money_rows if row.part_of_set_aside) == 57_929
    assert [(pick.number, pick.side_label, pick.ratio) for pick in review.picks] == [(1, "Sell", 1), (2, "Buy", 1)]
    assert review.end_sentence is not None and "10 minutes" in review.end_sentence
    assert option_deploy.price_of(seen).commitment_cents == 57_929


async def test_the_requirement_a_structure_adds_equals_its_own_on_a_flat_account_and_is_smaller_beside_an_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three bear call spreads need $1,500.00 alone. On a flat account they add that. Beside the opposite
    three-lot spread they net to nothing, so they add $0.00, and the bot still sets aside the $579.29.

    The account's holdings and the picked contracts are added count by count: the offsetting lot nets to zero
    on each contract, where laying the picks over the holdings would have left $1,500.00 again."""
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread", structures=3,
    )
    seen = await desk.quoted(received, c585=("5.00", "5.04"), c590=("1.89", "1.93"))
    request = AlpacaPaperDeployRequest.model_validate({
        **_SETTINGS, "execution_mode": "paper", "sizing": {"preset": "custom", "quantity": 3},
        "trades": {
            "kind": "options", "underlying": "SPY", "structure": "bear_call_spread",
            "legs": [leg.model_dump(mode="json") for leg in received.legs], "order_terms": _TERMS,
        },
    })
    assert request.options is not None
    entry = option_deploy.price_of(seen)
    short_585, long_590 = (leg.contract.occ_symbol for leg in seen.legs)
    answer = deploy_funding_answer(
        None, amount_cents=100_000, need=EntryNeed.for_options(commitment_cents=57_929, fee_provision_cents=29),
        broker_buying_power=None, observed_at_ms=NOON, now_ms=NOON, simulated=True,
    )
    review = option_deploy.review_of(request, plan_of(request.options), seen, blockers=[], notes=[])

    flat = budget_deploy._requirement_effect(entry, {}, "")
    offset = budget_deploy._requirement_effect(entry, {(short_585, 100): 3, (long_590, 100): -3}, "")
    said = {row.key: row for row in option_deploy.with_account(review, answer, offset).money_rows}

    assert dict(entry.picked) == {(short_585, 100): -3, (long_590, 100): 3} and entry.requirement_cents == 150_000
    assert (flat.change.amount_cents, flat.netting) == (entry.requirement_cents, None)
    assert offset.change.amount_cents == 0 and offset.change.amount_cents < entry.requirement_cents
    assert offset.netting is not None and "does not spend the $1,500.00 that netting saves" in offset.netting
    assert (said["incremental_requirement"].amount_cents, said["incremental_requirement"].note) == (0, offset.netting)
    assert said["set_aside"].amount_cents == 57_929, "the bot's own reservation is the same beside the offset"


async def test_an_account_whose_loss_has_no_limit_has_an_unknown_requirement_that_says_why(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk = Desk(monkeypatch)
    received = _asking(
        _leg("short_call", "short", "call", _AT_THE_MONEY),
        _leg("long_call", "long", "call", _from("short_call", 5.0)),
        structure="bear_call_spread", structures=1,
    )
    seen = await desk.quoted(received, c585=("5.00", "5.04"), c590=("1.89", "1.93"))
    entry = option_deploy.price_of(seen)
    held_short_call = {("SPY261016C00600000", 100): -1}

    effect = budget_deploy._requirement_effect(entry, held_short_call, "")
    unread = budget_deploy._requirement_effect(entry, None, "the books hold SPY261016C00600000 with no recorded terms")

    assert (effect.change.state, effect.change.amount_cents) == ("unknown", None)
    assert "more calls than it buys" in effect.change.source
    assert unread.change.state == "unknown" and "no recorded terms" in unread.change.source


# ── what a request may say ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "trades",
    [
        {**_ONE_CALL, "symbol": "QQQ"},
        {**_ONE_CALL, "traded_symbol": "QQQ"},
        {"kind": "signal", "symbol": "QQQ"},
        {"kind": "signal", "order_terms": _TERMS},
        {**_ONE_CALL, "order_terms": {**_TERMS, "sold_leg_close_lead_minutes": 10}},
    ],
    ids=["a stock's field on options", "the old field inside options", "a symbol on the signal's own shares",
         "terms on the signal's own shares", "a fifth term"],
)
async def test_a_choice_that_says_a_field_it_does_not_have_is_a_422(deploy_app, trades: dict[str, Any]) -> None:
    app, registry = deploy_app

    response = await _post(app, "", execution_mode="dry_run", trades=trades)

    assert response.status_code == 422, response.text
    assert registry.deploy_calls == []


def test_an_option_bots_orders_are_in_its_contracts_underlying_and_not_in_what_it_listens_to() -> None:
    on_another = AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "trades": {**_ONE_CALL, "underlying": "QQQ"}})

    assert (on_another.symbol, on_another.order_symbol, on_another.traded_symbol) == ("SPY", "QQQ", None)
