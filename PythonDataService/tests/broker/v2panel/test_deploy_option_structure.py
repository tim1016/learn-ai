"""Deploy takes an option structure as what a bot trades, with the four order terms sealed with it (#2913).

A Deploy says what its bot trades once. Said as the signal's own shares or
as another stock, the Deploy is the one it always was, to the byte. Said as
an option structure, its plan is the structure preview's, its terms ride to
the bot's start, and it is refused by name for a structure the account
Clerk does not take, for an account whose mode is not open, and for what
Alpaca and this Clerk's own quote lines say of it.

The registry here records what a start was handed; the Clerk and Alpaca are
stand-ins. Nothing is sent to a broker.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx
import pytest
from httpx import ASGITransport
from pydantic import ValidationError

from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.options import BrokerOptionAccountSnapshot
from app.broker.contract.registry import get_broker_registry
from app.config import settings
from app.options.contract import OptionContract
from app.schemas.action_plan import OptionEntryLeg, TradedInstrumentView
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.schemas.deployment_budget import DeploymentBudgetPreview
from app.schemas.option_deploy import TradesOptions
from app.schemas.option_order_terms import SealedOptionOrderTerms
from app.services import bot_option_run
from app.services.bot_binding_repository import alpaca_v1_action_plan
from app.services.bot_option_run import OptionMarket
from app.services.bot_start_admission import make_start_request, new_run_binding
from app.services.broker_v2_panel import budget_deploy, option_deploy, panel_deploy
from app.services.option_preview_views import entry_sentence
from app.services.option_structure_preview import plan_of
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.conftest import documented_option_listing
from tests.broker.v2panel.conftest import _BODY, _SETTINGS
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_deploy_traded_symbol import (
    _UNTOUCHED,
    _UNTOUCHED_FINGERPRINT,
    _WORLDS,
    _fingerprint,
    _serve,
)
from tests.broker.v2panel.test_option_structure_preview_route import _on
from tests.broker.v2panel.test_panel_deploy_live import _live_view
from tests.broker.v2panel.test_panel_deploy_shadow import _shadow_view
from tests.services.test_option_structure_preview import _BULL_CALL_SPREAD, _KNOWN, Desk

_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"
_SPREAD_LEGS = _BULL_CALL_SPREAD.model_dump(mode="json")["legs"]
_LONG_CALL = _SPREAD_LEGS[0]
_TERMS = {"gap_limit_percent": "8", "entry_wait_seconds": 20, "cost_limit_percent": "10", "expiry_close_lead_minutes": 45}
_ONE_CALL = {"kind": "options", "underlying": "SPY", "structure": "long_call", "legs": [_LONG_CALL], "order_terms": _TERMS}
_SPREAD = {**_ONE_CALL, "structure": "bull_call_spread", "legs": _SPREAD_LEGS}
_ACCEPTANCE = {
    "acknowledgement": "I_ACCEPT_UNTESTED_TRADED_ASSET_RISK",
    "reason": "A call on SPY is what I mean to try on this signal.",
}
CALL_585 = OptionContract.from_occ_symbol("SPY261009C00585000")


# ── identity ────────────────────────────────────────────────────────────


def test_saying_the_signals_own_shares_is_the_deploy_it_always_was() -> None:
    assert _fingerprint(trades={"kind": "signal"}) == _UNTOUCHED_FINGERPRINT
    assert _fingerprint(trades={"kind": "stock", "symbol": "spy"}) == _UNTOUCHED_FINGERPRINT


def test_another_stock_said_either_way_is_one_deploy() -> None:
    """The old field and the one choice are two spellings of one request: same dump, same fingerprint."""
    said_once = AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "trades": {"kind": "stock", "symbol": "qqq"}})
    the_old_way = AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "traded_symbol": "QQQ"})

    assert said_once.model_dump(mode="json") == the_old_way.model_dump(mode="json")
    assert (said_once.traded_symbol, said_once.trades, said_once.order_symbol) == ("QQQ", None, "QQQ")
    assert _fingerprint(trades={"kind": "stock", "symbol": "qqq"}) == _fingerprint(traded_symbol="QQQ")


def test_a_stock_and_an_option_structure_together_cannot_be_said() -> None:
    with pytest.raises(ValidationError, match="once"):
        AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "traded_symbol": "QQQ", "trades": _ONE_CALL})
    with pytest.raises(ValidationError, match="once"):
        AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "traded_symbol": "QQQ", "trades": {"kind": "signal"}})


def test_a_review_of_an_option_structure_binds_its_legs_and_each_of_its_four_terms() -> None:
    reviewed = _fingerprint(trades=_ONE_CALL)

    assert reviewed != _UNTOUCHED_FINGERPRINT
    for term, other in (
        ("gap_limit_percent", "9"), ("entry_wait_seconds", 21), ("cost_limit_percent", "11"), ("expiry_close_lead_minutes", 46)
    ):
        assert _fingerprint(trades={**_ONE_CALL, "order_terms": {**_TERMS, term: other}}) != reviewed, term
    assert _fingerprint(trades={**_ONE_CALL, "legs": [{**_LONG_CALL, "right": "put"}], "structure": "long_put"}) != reviewed
    assert _fingerprint(trades={**_ONE_CALL, "underlying": "QQQ"}) != reviewed
    # One choice of a percent has one fingerprint however it was typed.
    assert _fingerprint(trades={**_ONE_CALL, "order_terms": {**_TERMS, "gap_limit_percent": "8.000"}}) == reviewed


@pytest.mark.parametrize("term", sorted(_TERMS))
def test_an_option_choice_says_every_one_of_its_four_terms(term: str) -> None:
    """Null is no number, and a term left out is not filled in: what is sealed is what the owner set."""
    for unsaid in ({key: value for key, value in _TERMS.items() if key != term}, {**_TERMS, term: None}):
        with pytest.raises(ValidationError):
            AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "trades": {**_ONE_CALL, "order_terms": unsaid}})


# ── the floor these Deploys stand on ────────────────────────────────────


@dataclass
class _Alpaca:
    """What Alpaca says of the account's option fields and of a contract, and what it was asked."""

    level: int | None = 2
    buying_power: Decimal | None = Decimal("100000")
    listed: dict[str, Any] = field(default_factory=lambda: {CALL_585.occ_symbol: documented_option_listing(CALL_585)})
    account_error: Exception | None = None
    asked: list[str] = field(default_factory=list)

    async def get_option_account(self) -> BrokerOptionAccountSnapshot:
        self.asked.append("account")
        if self.account_error is not None:
            raise self.account_error
        return BrokerOptionAccountSnapshot(
            broker="alpaca", account_id=ACCT, account_mode="paper", options_buying_power=self.buying_power,
            options_approved_level=self.level, options_trading_level=self.level, observed_at_ms=1,
        )

    async def get_option_contract(self, contract: OptionContract) -> Any:
        self.asked.append(contract.occ_symbol)
        return self.listed.get(contract.occ_symbol)


@dataclass
class _Floor:
    desk: Desk
    alpaca: _Alpaca
    line_budget: int = 60
    shared_status_source: bool = False

    def market(self) -> OptionMarket:
        return OptionMarket(
            quotes=lambda: self.desk.status,
            quotes_are_read_elsewhere=lambda: self.shared_status_source,
            watch=self.desk.watch,
            entry_searches=lambda: self.desk.searches,
            model_inputs=lambda *_: _KNOWN,
            line_budget=self.line_budget,
            strikes_each_side=self.desk.reach,
        )

    async def quotes_arrive(self, ask: Callable[[], Any]) -> None:
        """Ask once so that the lines are opened, quote them, and let a moment pass: the next ask resolves."""
        await ask()
        await self.desk.settle()
        self.desk.spy()
        await self.desk.settle()
        self.desk.quote_every_line()
        self.desk.quote("call", 585, "4.08", "4.12")
        self.desk.ibkr.now += 400


@pytest.fixture
def floor(deploy_app, monkeypatch: pytest.MonkeyPatch) -> _Floor:
    """A Clerk with option quotes on a stand-in Gateway, and an Alpaca account approved to buy options."""
    desk = Desk(monkeypatch)
    _on(desk, monkeypatch)
    floor = _Floor(desk, _Alpaca())
    monkeypatch.setattr(option_deploy, "this_process_option_market", floor.market)
    # A click waits for nothing here: a test that wants a price lets the quotes arrive first.
    monkeypatch.setattr(option_deploy, "PRICE_PATIENCE_MS", 0)
    port = get_broker_registry().resolve("alpaca")
    monkeypatch.setattr(port, "get_option_account", floor.alpaca.get_option_account, raising=False)
    monkeypatch.setattr(port, "get_option_contract", floor.alpaca.get_option_contract, raising=False)
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", ACCT)}),
    )
    return floor


async def _post(app, path: str, **changes: object) -> httpx.Response:
    body = {**(_BODY if path == "" else _SETTINGS), **changes}
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(f"{_BOTS}{path}", json=body)


async def _deploy(app, **changes: object) -> httpx.Response:
    return await _post(app, "", **changes)


def _refusal(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 409, response.text
    return response.json()["detail"]


# ── a Dry Run and a Paper account ───────────────────────────────────────


async def test_a_dry_run_of_one_bought_call_starts_with_the_previews_plan_and_the_owners_four_terms(
    deploy_app, floor: _Floor
) -> None:
    app, registry = deploy_app
    terms = {"gap_limit_percent": "12.5", "entry_wait_seconds": 7, "cost_limit_percent": "4", "expiry_close_lead_minutes": 90}

    response = await _deploy(
        app, execution_mode="dry_run", trades={**_ONE_CALL, "order_terms": terms}, sizing={"preset": "custom", "quantity": 3}
    )

    assert response.status_code == 201, response.text
    [call] = registry.deploy_calls
    assert (call["symbol"], call["traded_symbol"], call["mode"], call["quantity"], call["use_rth"]) == (
        "SPY", None, "dry_run", 3, True
    )
    [leg] = call["action_plan"].on_enter
    assert isinstance(leg, OptionEntryLeg)
    assert (leg.instrument.underlying, leg.position, leg.right, leg.qty_ratio) == ("SPY", "long", "call", 1)
    assert [closed.entry_leg_id for closed in call["action_plan"].on_exit] == [leg.leg_id]
    assert call["action_plan"].structure == "long_call"
    assert call["option_order_terms"] == SealedOptionOrderTerms.model_validate(terms)
    assert call["traded_asset_acknowledgement"] is None
    assert floor.alpaca.asked == [], "a Dry Run asks nothing of a broker"


async def test_what_a_deploy_hands_the_start_is_what_the_bots_runner_reads_its_terms_from(deploy_app, floor: _Floor) -> None:
    """Request to plan to binding to runner: the binding a start makes from this Deploy carries the plan and the
    terms, and the runner's one read of a bot's order terms gives the owner's four numbers back."""
    app, registry = deploy_app
    terms = {"gap_limit_percent": "12.5", "entry_wait_seconds": 7, "cost_limit_percent": "4", "expiry_close_lead_minutes": 90}
    await _deploy(app, execution_mode="dry_run", trades={**_ONE_CALL, "order_terms": terms})
    [call] = registry.deploy_calls

    binding = new_run_binding(
        make_start_request(
            broker=call["broker"], strategy_instance_id=call["strategy_instance_id"], strategy_key=call["strategy_key"],
            symbol=call["symbol"], use_rth=call["use_rth"], mode=call["mode"], quantity=call["quantity"],
            carryover_policy=call["carryover_policy"], evidence_override=call["evidence_override"],
            action_plan=call["action_plan"], exit_terms=DEPLOY_EXIT_TERMS, option_order_terms=call["option_order_terms"],
        ),
        now_ms=1,
    )
    read = bot_option_run.option_order_terms(binding)

    assert binding.action_plan == call["action_plan"]
    assert (read.leg_gap_limit_percent, read.cost_limit_percent) == (Decimal("12.5"), Decimal(4))
    assert (read.close.entry_wait_ms, read.close.expiry_close_lead_ms) == (7_000, 90 * 60_000)


async def test_on_paper_an_accepted_option_bot_starts_once_alpaca_approves_the_account(deploy_app, floor: _Floor) -> None:
    app, registry = deploy_app

    response = await _deploy(app, execution_mode="paper", trades=_ONE_CALL, traded_asset_acknowledgement=_ACCEPTANCE)

    assert response.status_code == 201, response.text
    [call] = registry.deploy_calls
    assert (call["mode"], call["option_order_terms"]) == ("trade", SealedOptionOrderTerms.model_validate(_TERMS))
    assert call["traded_asset_acknowledgement"].reason == _ACCEPTANCE["reason"]
    assert floor.alpaca.asked == ["account"], "no contract was picked yet, so no listing was asked about"


# ── the acknowledgement, by mode ────────────────────────────────────────


@_WORLDS
async def test_options_on_the_bots_own_symbol_still_need_the_owners_acceptance_where_money_is_at_stake(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch, execution_mode: str, view_of: Callable | None
) -> None:
    """Options on SPY are something else than SPY shares. Paper, Live Shadow and Live alike."""
    app, registry = deploy_app
    _serve(monkeypatch, view_of)

    refusal = _refusal(await _deploy(app, execution_mode=execution_mode, trades=_ONE_CALL))

    assert refusal["reason_code"] == "TRADED_ASSET_ACKNOWLEDGEMENT_REQUIRED"
    assert "options on SPY" in refusal["message"]
    assert refusal["submission_settled"] is True
    assert registry.deploy_calls == []


async def test_a_dry_run_of_options_is_asked_for_no_acceptance_and_may_carry_none(deploy_app, floor: _Floor) -> None:
    app, registry = deploy_app

    refusal = _refusal(
        await _deploy(app, execution_mode="dry_run", trades=_ONE_CALL, traded_asset_acknowledgement=_ACCEPTANCE)
    )

    assert refusal["reason_code"] == "TRADED_ASSET_ACKNOWLEDGEMENT_NOT_APPLICABLE"
    assert registry.deploy_calls == []


@pytest.mark.parametrize(
    ("execution_mode", "view_of", "asks_the_owner_to_accept"),
    [("paper", None, True), ("shadow", _shadow_view, True), ("live", _live_view, True), ("dry_run", None, False)],
    ids=["paper", "live-shadow", "live", "dry-run"],
)
async def test_the_review_warns_that_options_have_no_backtest_and_asks_only_a_broker_bot_to_accept(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch,
    execution_mode: str, view_of: Callable | None, asks_the_owner_to_accept: bool,
) -> None:
    app, _registry = deploy_app
    _serve(monkeypatch, view_of)
    money = DeploymentBudgetPreview(state="ready", detail="Ready", world="real_paper", custody_account_id=ACCT)
    monkeypatch.setattr(budget_deploy, "preview_budget", lambda *_args, **_kwargs: money)
    monkeypatch.setattr(budget_deploy, "same_symbol_note", lambda account_id, request: None)

    response = await _post(app, "/budget-preview", execution_mode=execution_mode, trades=_ONE_CALL)

    assert response.status_code == 200, response.text
    warning = response.json()["traded_asset_warning"]
    assert "no backtest" in warning["headline"]
    assert "SPY shares" in warning["detail"] and "a long call on SPY" in warning["detail"]
    label = warning["acceptance_label"]
    assert (label is not None) is asks_the_owner_to_accept
    assert label is None or "options on SPY" in label
    assert response.json()["trades_summary"] == "listens to SPY · trades a long call on SPY · 2 structures"


# ── what stops it ───────────────────────────────────────────────────────


async def _stopped(app, registry, **changes: object) -> dict[str, Any]:
    refusal = _refusal(await _deploy(app, **{"execution_mode": "dry_run", "trades": _ONE_CALL, **changes}))
    assert refusal["submission_settled"] is True, "nothing started, so the form may send again"
    assert registry.deploy_calls == []
    return refusal


async def test_a_structure_of_several_legs_is_refused_by_the_clerks_own_rule_until_it_is_built(
    deploy_app, floor: _Floor
) -> None:
    app, registry = deploy_app

    refusal = await _stopped(app, registry, trades=_SPREAD)

    assert refusal["reason_code"] == "OPTION_STRUCTURE_NOT_SUPPORTED"
    assert "2 option legs" in refusal["message"]


async def test_a_sold_leg_is_refused_by_its_place_and_never_by_an_id_in_quotes(deploy_app, floor: _Floor) -> None:
    app, registry = deploy_app
    sold = {**_ONE_CALL, "structure": None, "legs": [{**_LONG_CALL, "position": "short"}]}

    refusal = await _stopped(app, registry, trades=sold)

    # The plan's own rule comes first: a sold call that nothing covers is no structure a plan can hold.
    assert refusal["reason_code"] == "OPTION_PREVIEW_PLAN_REFUSED"
    assert "long_call" not in str(refusal) and "'" not in refusal["message"]
    assert "Leg 1" in refusal["message"] or "sells 1 call" in refusal["message"]


async def test_a_structure_no_plan_can_hold_is_refused_in_the_previews_own_words(deploy_app, floor: _Floor) -> None:
    app, registry = deploy_app

    no_legs = await _stopped(app, registry, trades={**_ONE_CALL, "structure": None, "legs": []})
    share_class = await _stopped(app, registry, trades={**_ONE_CALL, "underlying": "BRK.B"})

    assert no_legs["reason_code"] == "OPTION_PREVIEW_NO_LEGS" and "Add a leg" in no_legs["message"]
    assert share_class["reason_code"] == "OPTION_PREVIEW_SHARE_CLASS_UNDERLYING"


async def test_a_live_shadow_takes_no_option_bot_and_says_it_is_not_built(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, registry = deploy_app
    _serve(monkeypatch, _shadow_view)
    monkeypatch.setattr(settings, "ALPACA_LIVE_OPTION_BOTS_ENABLED", True)

    refusal = await _stopped(app, registry, execution_mode="shadow", traded_asset_acknowledgement=_ACCEPTANCE)

    assert refusal["reason_code"] == option_deploy.LIVE_SHADOW_NOT_BUILT
    assert "not built" in refusal["message"]
    assert floor.alpaca.asked == []


async def test_a_live_option_deploy_is_refused_by_name_while_the_setting_is_off(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, registry = deploy_app
    _serve(monkeypatch, _live_view)
    assert settings.ALPACA_LIVE_OPTION_BOTS_ENABLED is False, "off unless a deployment turns it on"

    refusal = await _stopped(app, registry, execution_mode="live", traded_asset_acknowledgement=_ACCEPTANCE)

    assert refusal["reason_code"] == option_deploy.LIVE_SWITCHED_OFF
    assert "ALPACA_LIVE_OPTION_BOTS_ENABLED" in refusal["next_action"] and "Paper" in refusal["next_action"]
    assert floor.alpaca.asked == []


async def test_turning_the_setting_on_is_all_a_live_option_deploy_needs_to_pass_deploys_fence(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, registry = deploy_app
    _serve(monkeypatch, _live_view)
    monkeypatch.setattr(settings, "ALPACA_LIVE_OPTION_BOTS_ENABLED", True)

    response = await _deploy(app, execution_mode="live", trades=_ONE_CALL, traded_asset_acknowledgement=_ACCEPTANCE)

    assert response.status_code == 201, response.text
    [call] = registry.deploy_calls
    assert (call["mode"], call["option_order_terms"]) == ("trade", SealedOptionOrderTerms.model_validate(_TERMS))


async def test_a_clerk_that_counts_no_quote_line_free_refuses_and_says_the_count_is_its_own(
    deploy_app, floor: _Floor
) -> None:
    app, registry = deploy_app
    floor.line_budget = 0

    refusal = await _stopped(app, registry)

    assert refusal["reason_code"] == "OPTION_WINDOW_OVER_LINE_BUDGET"
    assert "counts no IBKR quote lines free" in refusal["message"]
    assert "best estimate" in refusal["message"], "the count cannot see other IBKR sessions, and says so"


async def test_the_lines_other_option_bots_can_need_count_against_the_clerks_budget(deploy_app, floor: _Floor) -> None:
    """One call's window can need five lines at this reach. A budget of nine has four left beside a running
    bot's five, so the second bot is refused; a budget of ten takes both."""
    app, registry = deploy_app
    running = await _deploy(app, execution_mode="dry_run", trades=_ONE_CALL)
    floor.desk.watch.watch(
        "bot:already-running",
        bot_option_run.WatchRequest(
            underlying="SPY", line_class=bot_option_run.LineClass.CANDIDATE, plan=registry.deploy_calls[0]["action_plan"]
        ),
    )
    floor.line_budget = 9

    refusal = _refusal(await _deploy(app, execution_mode="dry_run", trades=_ONE_CALL, submission_key="submission-0002"))
    floor.line_budget = 10
    fits = await _deploy(app, execution_mode="dry_run", trades=_ONE_CALL, submission_key="submission-0003")

    assert running.status_code == 201
    assert refusal["reason_code"] == "OPTION_WINDOW_OVER_LINE_BUDGET"
    assert "counts 4 IBKR quote lines free" in refusal["message"] and "can need 5" in refusal["message"]
    assert fits.status_code == 201, fits.text


async def test_a_clerk_with_no_option_quotes_of_its_own_refuses_before_anything_is_asked(deploy_app, floor: _Floor) -> None:
    app, registry = deploy_app
    floor.shared_status_source = True

    refusal = await _stopped(app, registry)

    assert refusal["reason_code"] == "OPTION_CLERK_HAS_NO_OPTION_QUOTES"


@pytest.mark.parametrize(
    ("level", "reason"),
    [(1, option_deploy.APPROVAL_TOO_LOW), (0, option_deploy.APPROVAL_TOO_LOW), (None, option_deploy.APPROVAL_UNKNOWN)],
)
async def test_an_account_not_approved_to_buy_options_is_refused_and_unknown_is_never_approval(
    deploy_app, floor: _Floor, level: int | None, reason: str
) -> None:
    app, registry = deploy_app
    floor.alpaca.level = level

    refusal = await _stopped(app, registry, execution_mode="paper", traded_asset_acknowledgement=_ACCEPTANCE)

    assert refusal["reason_code"] == reason
    assert "level to buy options (2)" in refusal["message"]


async def test_an_alpaca_that_cannot_be_asked_about_the_account_stops_the_deploy(deploy_app, floor: _Floor) -> None:
    app, registry = deploy_app
    floor.alpaca.account_error = BrokerUnavailable("Alpaca is unreachable.", broker="alpaca", detail="connection refused")

    refusal = await _stopped(app, registry, execution_mode="paper", traded_asset_acknowledgement=_ACCEPTANCE)

    assert refusal["reason_code"] == option_deploy.ACCOUNT_NOT_READ


# ── the money review ────────────────────────────────────────────────────


async def _review(app, floor: _Floor, monkeypatch: pytest.MonkeyPatch, **changes: object) -> tuple[dict[str, Any], Any]:
    """The review of an option Deploy once its quotes have arrived, and the price its budget was judged by."""
    priced: list[Any] = []

    def reviewed(account_id: str, request: AlpacaPaperDeployRequest, *, resolved_parameters: dict, option_entry: Any):
        priced.append(option_entry)
        return DeploymentBudgetPreview(state="ready", detail="Ready", world="real_paper", custody_account_id=ACCT)

    monkeypatch.setattr(budget_deploy, "preview_budget", reviewed)
    monkeypatch.setattr(budget_deploy, "same_symbol_note", lambda account_id, request: None)
    body = {"execution_mode": "dry_run", "trades": _ONE_CALL, **changes}

    async def ask() -> httpx.Response:
        return await _post(app, "/budget-preview", **body)

    await floor.quotes_arrive(ask)
    response = await ask()
    assert response.status_code == 200, response.text
    return response.json(), priced[-1]


async def test_the_rows_of_one_bought_call_add_up_to_what_is_set_aside_and_price_the_budget(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two SPY 585 calls offered at $4.12: $824.00 of premium, no requirement, and the fee estimate."""
    app, _registry = deploy_app

    seen, price = await _review(app, floor, monkeypatch, budget={"amount_usd": "1000.00", "risk_revision": 0})

    review = seen["option_review"]
    rows = {row["key"]: row for row in review["money_rows"]}
    parts = [row["amount_cents"] for row in review["money_rows"] if row["part_of_set_aside"]]
    assert (rows["premium"]["amount_cents"], rows["premium"]["note"], rows["broker_requirement"]["amount_cents"]) == (
        82_400, "paid", 0
    )
    assert sum(parts) == rows["set_aside"]["amount_cents"] == price.commitment_cents
    assert rows["set_aside"]["label"] == "Set aside for 2 structures"
    assert rows["set_aside"]["amount_usd"] == f"${rows['set_aside']['amount_cents'] / 100:,.2f}"
    assert rows["loss_at_expiry"]["amount_cents"] == 82_400 and "estimate" in rows["loss_at_expiry"]["label"]
    assert rows["budget_free"]["amount_cents"] == 100_000 - rows["set_aside"]["amount_cents"]
    assert (price.waiting, price.observed_at_ms) == (False, review["quotes_read_ms"])
    assert review["preview_state"] == "resolved" and review["blockers"] == []
    assert [pick["contract"]["occ_symbol"] for pick in review["picks"]] == [CALL_585.occ_symbol]
    assert review["orders_summary"] == "Limit in, market out · gap up to 8% a leg · wait 20 s"
    assert (review["structure_label"], review["structure_in_a_sentence"]) == ("Long call", "a long call on")
    assert review["end_sentence"] is None, "nothing is sold, so the bot's end is the owner's"


async def test_a_review_whose_quotes_are_still_arriving_waits_and_has_no_price(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app
    priced: list[Any] = []
    money = DeploymentBudgetPreview(state="awaiting_price", detail="waiting", world="synthetic")
    monkeypatch.setattr(budget_deploy, "preview_budget", lambda *_a, option_entry, **_k: priced.append(option_entry) or money)
    monkeypatch.setattr(budget_deploy, "same_symbol_note", lambda account_id, request: None)

    response = await _post(app, "/budget-preview", execution_mode="dry_run", trades=_ONE_CALL)

    review = response.json()["option_review"]
    assert (priced[0].commitment_cents, priced[0].waiting) == (None, True)
    assert review["money_rows"] == [] and review["ask_again_in_ms"] is not None
    assert review["preview_state"] != "resolved" and review["preview_sentence"]


async def test_on_paper_the_review_lists_what_stops_the_deploy_and_prices_nothing(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An account approved for covered calls only: listed in "Can't deploy yet", with its fix, beside the notes
    every real account's option bot is shown."""
    app, _registry = deploy_app
    floor.alpaca.level = 1

    seen, price = await _review(app, floor, monkeypatch, execution_mode="paper")

    review = seen["option_review"]
    [blocker] = review["blockers"]
    assert (blocker["gate_id"], blocker["label"]) == (option_deploy.APPROVAL_TOO_LOW, "Options approval")
    assert "Ask Alpaca" in blocker["fix"]
    assert price.commitment_cents is None and price.waiting is False
    assert {note["code"] for note in review["notes"]} >= {
        option_deploy.FEES_ARE_AN_ESTIMATE, option_deploy.STREAM_NOT_SEEN, option_deploy.UNTESTED_AT_ALPACA
    }
    assert floor.alpaca.asked[-2:] == ["account", CALL_585.occ_symbol], "the picked contract's listing was asked"


async def test_a_contract_alpaca_does_not_list_stops_a_paper_deploy(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app
    floor.alpaca.listed = {}

    seen, _price = await _review(app, floor, monkeypatch, execution_mode="paper")

    [blocker] = seen["option_review"]["blockers"]
    assert blocker["label"] == "Alpaca's listing" and CALL_585.occ_symbol in blocker["headline"]


async def test_option_buying_power_short_of_one_entry_stops_a_paper_deploy_with_both_figures(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app
    floor.alpaca.buying_power = Decimal("500")

    seen, _price = await _review(app, floor, monkeypatch, execution_mode="paper")

    [blocker] = seen["option_review"]["blockers"]
    assert blocker["gate_id"] == option_deploy.BUYING_POWER_SHORT
    assert "$500.00" in blocker["headline"] and "$824." in blocker["headline"]


async def test_buying_power_alpaca_did_not_report_is_a_note_and_stops_nothing(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app
    floor.alpaca.buying_power = None

    seen, price = await _review(app, floor, monkeypatch, execution_mode="paper")

    review = seen["option_review"]
    assert review["blockers"] == [] and price.commitment_cents is not None
    assert option_deploy.BUYING_POWER_UNKNOWN in {note["code"] for note in review["notes"]}


async def test_a_dry_run_is_shown_no_note_about_a_real_account(
    deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, _registry = deploy_app

    seen, _price = await _review(app, floor, monkeypatch)

    assert seen["option_review"]["notes"] == []
    assert floor.alpaca.asked == []


async def test_a_bot_in_shares_has_no_option_review(deploy_app, floor: _Floor, monkeypatch: pytest.MonkeyPatch) -> None:
    app, _registry = deploy_app
    money = DeploymentBudgetPreview(state="ready", detail="Ready", world="synthetic")
    monkeypatch.setattr(budget_deploy, "preview_budget", lambda account_id, request, *, resolved_parameters: money)
    monkeypatch.setattr(budget_deploy, "same_symbol_note", lambda account_id, request: None)

    response = await _post(app, "/budget-preview", execution_mode="dry_run")

    assert response.status_code == 200, response.text
    assert response.json()["option_review"] is None


# ── what the screens are told of an option bot ──────────────────────────


def test_a_bots_row_names_the_structure_it_trades_for_a_list_and_for_a_sentence() -> None:
    """So a bot that trades options on the symbol it listens to never reads "SPY → SPY"."""
    spread = plan_of(TradesOptions.model_validate(_SPREAD))
    custom = plan_of(TradesOptions.model_validate({**_ONE_CALL, "structure": None}))

    named = TradedInstrumentView.of(spread, signal_symbol="SPY")
    unnamed = TradedInstrumentView.of(custom, signal_symbol="SPY")

    assert named is not None and unnamed is not None
    assert named.model_dump(mode="json") == {
        "kind": "option", "symbol": "SPY", "label": "Bull call spread", "in_a_sentence": "a bull call spread on",
    }
    assert (unnamed.label, unnamed.in_a_sentence) == ("Custom", "a custom option structure on")


def test_a_stock_bots_row_is_written_as_it_was_before_a_structure_had_a_label() -> None:
    other = TradedInstrumentView.of(alpaca_v1_action_plan("QQQ"), signal_symbol="SPY")

    assert other is not None and other.model_dump(mode="json") == {"kind": "stock", "symbol": "QQQ"}
    assert TradedInstrumentView.of(alpaca_v1_action_plan("SPY"), signal_symbol="SPY") is None


async def test_picking_options_is_warned_of_at_once_even_on_the_symbol_the_bot_listens_to(deploy_app) -> None:
    app, _registry = deploy_app
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        options = await client.get(f"{_BOTS}/traded-asset", params={"symbol": "SPY", "traded_symbol": "SPY", "options": "true"})
        shares = await client.get(f"{_BOTS}/traded-asset", params={"symbol": "SPY", "traded_symbol": "SPY"})

    assert options.status_code == 200, options.text
    warning = options.json()["warning"]
    assert "no backtest" in warning["headline"] and "options on SPY" in warning["detail"]
    assert "time" in warning["detail"] and "volatility" in warning["detail"]
    assert warning["acceptance_label"] is None, "the owner accepts at Confirm, not while still choosing"
    assert shares.json() == {"refusal": None, "warning": None}


def test_deploy_again_of_an_option_bot_carries_its_structure_and_its_sealed_terms() -> None:
    terms = SealedOptionOrderTerms.model_validate({**_TERMS, "entry_wait_seconds": 7})
    binding = new_run_binding(
        make_start_request(
            broker="alpaca", strategy_instance_id="spy-ema-1", strategy_key="ema_crossover_signal", symbol="SPY",
            use_rth=True, mode="dry_run", quantity=2, carryover_policy="FORBID", evidence_override=None,
            action_plan=plan_of(TradesOptions.model_validate(_SPREAD)), exit_terms=DEPLOY_EXIT_TERMS,
            option_order_terms=terms,
        ),
        now_ms=1,
    )

    choice = panel_deploy._option_choice_of(binding)

    assert choice is not None and choice.order_terms == terms
    assert (choice.underlying, choice.structure) == ("SPY", "bull_call_spread")
    assert plan_of(choice) == binding.action_plan, "the same legs make the same plan again"
    assert panel_deploy._option_choice_of(binding.model_copy(update={
        "action_plan": alpaca_v1_action_plan("QQQ"), "option_order_terms": None,
    })) is None


def test_money_that_cannot_be_set_aside_is_not_said_to_be_skipped_right_now() -> None:
    """A red verdict does not pass by itself, so the entry's sentence does not say it would."""
    red = entry_sentence([], not_as_built=True)

    assert "right now" not in red and "cannot be made" in red
    assert "right now" in entry_sentence([])
