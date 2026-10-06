"""Deploy takes the asset a bot trades when that is not the symbol it listens to (#2912).

A Deploy that names no traded asset must be exactly the Deploy it was before
the field existed: same fingerprint, same plan, no question asked of Alpaca.
One that names another asset trades it, is priced from it, and until the
owner's written acceptance can be recorded with the Deploy, runs as a Dry Run
only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

import httpx
import pytest
from httpx import ASGITransport

from app.broker.alpaca.clerk.sealed_ledger import canonical_sha256
from app.broker.alpaca.clerk.sqlite.budget_commands import submit_budgeted_deploy
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.contract.errors import BrokerUnavailable
from app.broker.contract.models import BrokerAsset
from app.broker.contract.registry import get_broker_registry
from app.schemas.broker_bots import AlpacaPaperDeployRequest
from app.services import market_liveness
from app.services.bot_binding_repository import alpaca_v1_action_plan
from app.services.broker_v2_panel import budget_deploy, sqlite_roster_status
from tests.broker.v2panel.conftest import _BODY, _SETTINGS
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_budget_deploy import (
    NOON,
    _committed_view,
    _deployed_bot,
    _publish_book,
    _request,
    authority,  # noqa: F401 -- pytest fixture, used by name
)

_BOTS = f"/api/brokers/alpaca/accounts/{ACCT}/bots"

# The fingerprint of this request on the commit before the field existed.
_UNTOUCHED = {
    "strategy_key": "ema_crossover_signal",
    "exit_terms": {"exit_allowance_bps": 20, "band_multiple": 2, "spread_cap_bps": 50},
    "budget": {"amount_usd": "600.00", "risk_revision": 1},
    "symbol": "spy",
    "sizing": {"preset": "custom", "quantity": 3},
    "execution_mode": "paper",
    "parameters": {"hold_bars": 5},
}
_UNTOUCHED_FINGERPRINT = "6cf996be549569b0f2c4facac5f810f89a39c70e51a1f8fb8d4304f5ef3f5b61"


def _fingerprint(**changes: object) -> str:
    request = AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, **changes})
    return request.fingerprint(account="PA-TEST", world="real_paper")


# ── identity ────────────────────────────────────────────────────────────


def test_a_deploy_that_names_no_traded_asset_has_the_fingerprint_it_always_had() -> None:
    assert _fingerprint() == _UNTOUCHED_FINGERPRINT


def test_naming_the_symbol_the_bot_listens_to_is_naming_no_other_asset() -> None:
    """One bot never has two identities: the same choice is the same Deploy."""
    request = AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "traded_symbol": "Spy"})

    assert request.traded_symbol is None
    assert request.order_symbol == "SPY"
    assert _fingerprint(traded_symbol="SPY") == _UNTOUCHED_FINGERPRINT


def test_another_traded_asset_is_another_deploy() -> None:
    """Consent binds what the bot trades: a review for SPY shares is no review for QQQ's."""
    request = AlpacaPaperDeployRequest.model_validate({**_UNTOUCHED, "traded_symbol": "qqq"})

    assert (request.symbol, request.traded_symbol, request.order_symbol) == ("SPY", "QQQ", "QQQ")
    assert _fingerprint(traded_symbol="QQQ") != _UNTOUCHED_FINGERPRINT


# ── the Deploy route ────────────────────────────────────────────────────


def _asset(symbol: str, **changes: object) -> BrokerAsset:
    return BrokerAsset(
        broker="alpaca", asset_id=f"id-{symbol}", symbol=symbol, name=symbol, asset_class="us_equity",
        exchange="NASDAQ", status="active", tradable=True, fractionable=True, shortable=True, marginable=True,
    ).model_copy(update=changes)


@dataclass
class _Alpaca:
    """What Alpaca says of each asset, and every symbol it was asked about."""

    answers: dict[str, BrokerAsset | None] = field(default_factory=lambda: {"QQQ": _asset("QQQ")})
    asked: list[str] = field(default_factory=list)

    async def get_asset(self, symbol: str) -> BrokerAsset | None:
        self.asked.append(symbol)
        return self.answers.get(symbol)


@pytest.fixture
def alpaca(deploy_app, monkeypatch: pytest.MonkeyPatch) -> _Alpaca:
    """Alpaca trades QQQ."""
    alpaca = _Alpaca()
    monkeypatch.setattr(get_broker_registry().resolve("alpaca"), "get_asset", alpaca.get_asset, raising=False)
    monkeypatch.setattr(
        "app.services.canary_admission.CANARY_ADMITTED_PROGRAM_ACCOUNT_PAIRS",
        frozenset({("ema_crossover_signal", ACCT)}),
    )
    return alpaca


async def _deploy(app, **changes: object) -> httpx.Response:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post(_BOTS, json={**_BODY, **changes})


async def test_a_dry_run_that_names_another_asset_trades_it(deploy_app, alpaca: _Alpaca) -> None:
    app, registry = deploy_app

    response = await _deploy(app, execution_mode="dry_run", traded_symbol="qqq")

    assert response.status_code == 201, response.text
    [call] = registry.deploy_calls
    assert (call["symbol"], call["traded_symbol"]) == ("SPY", "QQQ")
    assert response.json()["action_plan"]["on_enter"][0]["instrument"]["underlying"] == "QQQ"
    assert alpaca.asked == ["QQQ"]


async def test_a_bot_that_contacts_a_broker_cannot_trade_another_asset_yet(deploy_app, alpaca: _Alpaca) -> None:
    """The strategy is untested trading it, and Deploy cannot yet record that being accepted."""
    app, registry = deploy_app

    response = await _deploy(app, execution_mode="paper", traded_symbol="QQQ")

    assert response.status_code == 409
    assert "listens to SPY and trades QQQ" in response.text and "Dry Run" in response.text
    assert registry.deploy_calls == []


@pytest.mark.parametrize(
    "alpacas_answer",
    [
        pytest.param(lambda: None, id="unknown-to-alpaca"),
        pytest.param(lambda: _asset("XYZ", tradable=False), id="not-tradable"),
        pytest.param(lambda: _asset("XYZ", status="inactive"), id="delisted"),
        pytest.param(lambda: _asset("XYZ", asset_class="crypto"), id="not-a-stock"),
    ],
)
async def test_an_asset_alpaca_cannot_trade_is_refused_before_the_bot_exists(
    deploy_app, alpaca: _Alpaca, alpacas_answer: Callable[[], BrokerAsset | None]
) -> None:
    app, registry = deploy_app
    alpaca.answers["XYZ"] = alpacas_answer()

    response = await _deploy(app, execution_mode="dry_run", traded_symbol="XYZ")

    assert response.status_code == 409
    assert "Alpaca cannot trade XYZ" in response.text
    assert registry.deploy_calls == []


async def test_alpaca_being_unreachable_is_not_a_verdict_on_the_asset(
    deploy_app, alpaca: _Alpaca, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Nothing was learned about QQQ, so the Deploy may be sent again: a 503, never a refusal of the asset."""
    app, registry = deploy_app

    async def unreachable(symbol: str) -> BrokerAsset | None:
        raise BrokerUnavailable("Alpaca did not answer.", broker="alpaca")

    monkeypatch.setattr(get_broker_registry().resolve("alpaca"), "get_asset", unreachable, raising=False)

    response = await _deploy(app, execution_mode="dry_run", traded_symbol="QQQ")

    assert response.status_code == 503
    assert "could not be asked whether it trades QQQ" in response.text
    assert response.json()["detail"]["submission_settled"] is True
    assert registry.deploy_calls == []


@pytest.mark.parametrize("review", ["budget-preview", "admission"])
async def test_each_review_refuses_an_asset_alpaca_cannot_trade(deploy_app, alpaca: _Alpaca, review: str) -> None:
    """The owner learns it at the first review, not at the final click."""
    app, _registry = deploy_app

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{_BOTS}/{review}", json={**_SETTINGS, "execution_mode": "dry_run", "traded_symbol": "XYZ"},
        )

    assert response.status_code == 409
    assert "Alpaca cannot trade XYZ" in response.text
    assert alpaca.asked == ["XYZ"]


async def test_the_admission_preview_is_of_the_bot_that_trades_the_named_asset(
    deploy_app, alpaca: _Alpaca, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, registry = deploy_app
    previewed: list[dict] = []
    answer = registry.preview_start_admission

    async def recording(**kwargs: object):
        previewed.append(kwargs)
        return await answer(**kwargs)

    monkeypatch.setattr(registry, "preview_start_admission", recording)

    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            f"{_BOTS}/admission", json={**_SETTINGS, "execution_mode": "dry_run", "traded_symbol": "QQQ"},
        )

    assert response.status_code == 200, response.text
    [call] = previewed
    assert (call["symbol"], call["traded_symbol"]) == ("SPY", "QQQ")


async def test_a_deploy_that_names_no_traded_asset_is_sent_as_it_always_was(deploy_app, alpaca: _Alpaca) -> None:
    app, registry = deploy_app

    response = await _deploy(app)

    assert response.status_code == 201, response.text
    assert registry.deploy_calls[0]["traded_symbol"] is None
    assert response.json()["action_plan"]["on_enter"][0]["instrument"]["underlying"] == "SPY"
    assert alpaca.asked == [], "Alpaca is asked about an asset only when the bot trades another one"


# ── the money: priced from what the bot buys ────────────────────────────


def test_the_budget_review_prices_the_asset_the_bot_will_buy(authority: tuple) -> None:  # noqa: F811
    """SPY asks $500 and QQQ $40: one position is one QQQ."""
    _publish_book(("SPY", 500), ("QQQ", 40))

    preview = budget_deploy.preview_budget(
        "BUDGET-PAPER", _request("SPY", traded_symbol="QQQ", execution_mode="dry_run"), resolved_parameters={}
    )

    assert preview.state == "ready" and preview.estimated_price_usd == "40.00"
    assert Decimal(preview.minimum_budget_usd or "0") < Decimal("41")


def test_the_budget_review_waits_for_the_traded_assets_price_and_says_whose(
    authority: tuple, monkeypatch: pytest.MonkeyPatch  # noqa: F811
) -> None:
    """SPY has a price; that is no price for QQQ. The review asks IBKR for QQQ and says what it waits for."""
    monkeypatch.setattr("app.utils.timestamps.now_ms_utc", lambda: NOON)

    waiting = budget_deploy.preview_budget(
        "BUDGET-PAPER", _request("SPY", traded_symbol="QQQ", execution_mode="dry_run"), resolved_parameters={}
    )

    assert waiting.state == "awaiting_price"
    assert waiting.detail == "Waiting for a QQQ price from IBKR."
    assert "QQQ" in market_liveness.get_market_liveness_store().requested_symbols()


def test_the_other_bots_warning_is_about_the_asset_being_traded(
    authority: tuple, monkeypatch: pytest.MonkeyPatch, tmp_path  # noqa: F811
) -> None:
    """Alpaca's wash-trade refusal is between orders in one asset, whatever each bot listens to."""
    repo, _, snapshot = authority
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    _deployed_bot(repo, snapshot.observation, "qqq-a", "QQQ")

    trades_qqq = _request("SPY", traded_symbol="QQQ")
    assert budget_deploy.same_symbol_note("BUDGET-PAPER", trades_qqq) == "Also traded here by qqq-a."
    assert budget_deploy.same_symbol_note("BUDGET-PAPER", _request("SPY")) is None


def test_a_bots_next_entry_is_judged_at_the_price_of_what_it_buys(authority: tuple) -> None:  # noqa: F811
    """The bot has $200 free. One SPY at $500 would not fit; it buys QQQ, at $40, and is ready."""
    repo, runtime, gate = _committed_view(authority)
    terms = _request().exit_terms.seal()
    config = {
        "symbol": "SPY", "quantity": 1,
        "action_plan": alpaca_v1_action_plan("QQQ").model_dump(mode="json"),
    }
    repo.register_strategy_instance(
        strategy_instance_id="listens-spy", symbol="QQQ", config_hash=canonical_sha256(config),
        config_json=canonicalize(config), exit_terms=terms,
    )
    submit_budgeted_deploy(
        repo, strategy_instance_id="listens-spy", lifecycle_run_id="listens-spy-run", world="real_paper",
        committed_cents=20_000, configuration_hash=canonical_sha256(config),
        exit_terms_hash=canonical_sha256(terms.model_dump(mode="json")), risk_revision=1, actor="owner",
        envelope=gate, minimum_position_cost=Decimal("40.02"),
    )
    _publish_book(("SPY", 500), ("QQQ", 40))

    view = budget_deploy._budget_view(runtime, "listens-spy")

    assert (view.headline, view.entry_eligible) == ("Ready for its next entry", True)
