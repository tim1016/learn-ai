"""Every bot's row says what it listens to and, when that is something else, what it trades (#2912).

The Clerk registers a bot under the asset its orders are in. A row's
``symbol`` is the Signal stream all the same, read from the bot's own
immutable configuration, and ``traded`` names the other asset. A reader that
asks what a bot holds or sends orders in uses the traded one.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport

from app.broker.alpaca.clerk.active_authority import ActiveClerkRuntime, set_active_clerk_runtime
from app.broker.alpaca.clerk.sqlite.commands import (
    submit_retire_strategy_instance,
    submit_start_run,
    submit_stop_run,
)
from app.broker.alpaca.clerk.sqlite.hashchain import canonicalize
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.registry import get_broker_registry, reset_broker_registry_for_testing
from app.engine.live.desired_state import DesiredState
from app.routers.broker_v2_panel import router
from app.schemas.action_plan import TradedInstrumentView, order_symbol
from app.schemas.bot_history import AccountBotHistory, BotHistoryBot, BotHistoryOrders, FleetBotHistoryRow
from app.schemas.broker_v2_panel import BotPanelView
from app.schemas.traded_asset_acknowledgement import (
    UntestedTradedAssetAcknowledgement,
    UntestedTradedAssetWarning,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_carryover import immutable_configuration_payload
from app.services.bot_registry_projection import project_bot_status
from app.services.bot_runner import set_bot_task_registry
from app.services.broker_v2_panel import (
    cohort_flatten,
    panel_data_source,
    sqlite_panel_source,
    sqlite_roster_status,
)
from app.services.broker_v2_panel.action_execution_service import reset_idempotency_store_for_testing
from app.services.broker_v2_panel.bot_page_projection import _manual_order_entry
from app.services.broker_v2_panel.catalog_projection_service import SqliteCatalogProjectionUnavailable
from app.services.broker_v2_panel.panel_projection_service import _recent_decision_views, traded_feed_lane
from app.services.broker_v2_panel.sqlite_panel_adapter import _working_order_view
from app.services.broker_v2_panel.sqlite_panel_source import _uncleanly_ended, home_roster
from app.services.fleet_bot_history import BotHistoryFilters, merge_bot_history
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort
from tests.broker.v2panel.fixtures import ACCT
from tests.broker.v2panel.test_bot_history import _receipt
from tests.broker.v2panel.test_panel_projection import _clerk_status, _status
from tests.broker.v2panel.test_panel_projection import _panel as _panel_view
from tests.broker.v2panel.test_panel_router import _clock_seq, _FakeBrokerPort, _FakeRegistry

_QQQ = TradedInstrumentView(kind="stock", symbol="QQQ")


def _binding(*, trades: str = "QQQ", **more: object) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id="bot", strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="trade", quantity=1, action_plan=alpaca_v1_action_plan(trades), run_id="run-1", created_at_ms=0,
        **more,
    )


def _config(*, listens_to: str | None = "SPY", trades: str | None = "QQQ", **more: object) -> str:
    """A bot's immutable configuration, exactly as the Clerk's registration writes it from the binding.

    ``listens_to=None`` and ``trades=None`` drop those facts, as a
    configuration written before a registration carried them has none.
    """
    config = immutable_configuration_payload(_binding(trades=trades or "SPY"))
    if listens_to is None:
        del config["symbol"]
    if trades is None:
        del config["action_plan"]
    return canonicalize({**config, **more})


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(sqlite_roster_status, "live_artifacts_root", lambda: tmp_path)
    repository = ClerkSqliteRepository.initialize(account_id="PA-ROSTER", artifacts_root=tmp_path)
    yield repository
    repository.close()


def _roster_row(repo: ClerkSqliteRepository, *, registered_under: str, config_json: str):
    repo.register_strategy_instance(
        strategy_instance_id="bot", symbol=registered_under, config_hash="h" * 64,
        strategy_key="ema_crossover_signal", display_name="EMA Crossover Signal", config_json=config_json,
    )
    return sqlite_roster_status.build_roster_status("alpaca", repo.strategy_instance("bot"), repo)


# ── the row ─────────────────────────────────────────────────────────────


def test_a_row_names_what_the_bot_listens_to_and_what_it_trades(repo: ClerkSqliteRepository) -> None:
    """The Clerk holds it under QQQ, the asset its orders are in. It listens to SPY."""
    row = _roster_row(repo, registered_under="QQQ", config_json=_config())

    assert (row.symbol, row.traded) == ("SPY", _QQQ)
    assert order_symbol(row.symbol, row.traded) == "QQQ"


def test_a_bot_that_trades_what_it_listens_to_names_nothing_else(repo: ClerkSqliteRepository) -> None:
    row = _roster_row(repo, registered_under="SPY", config_json=_config(trades="SPY"))

    assert (row.symbol, row.traded) == ("SPY", None)
    assert order_symbol(row.symbol, row.traded) == "SPY"


def test_a_configuration_that_names_no_symbol_is_read_under_the_clerks_registration(
    repo: ClerkSqliteRepository,
) -> None:
    """Every bot before #2912 trades what it listens to, so the registration is both."""
    row = _roster_row(repo, registered_under="SPY", config_json=_config(listens_to=None, trades=None))

    assert (row.symbol, row.traded) == ("SPY", None)


def test_a_plan_that_cannot_be_read_is_refused_rather_than_guessed(repo: ClerkSqliteRepository) -> None:
    """Guessing "trades what it listens to" would put its position under the wrong symbol on every screen."""
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="unreadable Action Plan"):
        _roster_row(repo, registered_under="QQQ", config_json=_config(trades=None, action_plan={"on_enter": []}))


def test_a_runner_backed_row_reads_the_same_facts_from_its_binding() -> None:
    row = project_bot_status(
        _binding(traded_asset_acknowledgement=_ACCEPTANCE), None, DesiredState.RUNNING, running=True
    )

    assert (row.symbol, row.traded, row.traded_asset_acknowledgement) == ("SPY", _QQQ, _ACCEPTANCE)


def test_a_retired_row_still_says_what_the_bot_listened_to_and_traded(repo: ClerkSqliteRepository) -> None:
    _roster_row(repo, registered_under="QQQ", config_json=_config())
    submit_retire_strategy_instance(repo, account_id=repo.account_id, strategy_instance_id="bot", retired_at_ms=5)

    row = sqlite_roster_status.build_terminal_roster_status("alpaca", repo.strategy_instance("bot"), repo)

    assert (row.phase, row.symbol, row.traded) == ("RETIRED", "SPY", _QQQ)


# ── what the owner accepted ─────────────────────────────────────────────

_ACCEPTANCE = UntestedTradedAssetAcknowledgement(
    acknowledgement="I_ACCEPT_UNTESTED_TRADED_ASSET_RISK", reason="QQQ has followed SPY closely enough to try this.",
)


def test_a_row_carries_the_acceptance_its_bot_was_deployed_with(repo: ClerkSqliteRepository) -> None:
    """Read from the bot's own immutable configuration, where its configuration hash covers it."""
    row = _roster_row(
        repo, registered_under="QQQ",
        config_json=_config(traded_asset_acknowledgement=_ACCEPTANCE.model_dump(mode="json")),
    )

    assert row.traded_asset_acknowledgement == _ACCEPTANCE


def test_an_acceptance_that_cannot_be_read_is_refused_rather_than_dropped(repo: ClerkSqliteRepository) -> None:
    """Showing the bot with no acceptance would say the owner never gave one."""
    with pytest.raises(SqliteCatalogProjectionUnavailable, match="unreadable untested-asset acceptance"):
        _roster_row(
            repo, registered_under="QQQ",
            config_json=_config(traded_asset_acknowledgement={"acknowledgement": "something else"}),
        )


def test_the_bot_page_shows_deploys_warning_and_what_the_owner_accepted() -> None:
    """One wording on Deploy and on the bot page; nothing left to accept, so no sentence to tick."""
    status = _status(mode="trade").model_copy(update={"traded": _QQQ, "traded_asset_acknowledgement": _ACCEPTANCE})

    page = _panel_view(status, _clerk_status(), [])
    own_symbol = _panel_view(_status(mode="trade"), _clerk_status(), [])

    assert page.traded_asset_warning == UntestedTradedAssetWarning.for_symbols(
        "SPY", "QQQ", acceptance_required=False
    )
    assert page.traded_asset_warning is not None and page.traded_asset_warning.acceptance_label is None
    assert page.traded_asset_acknowledgement == _ACCEPTANCE
    assert (own_symbol.traded_asset_warning, own_symbol.traded_asset_acknowledgement) == (None, None)


# ── readers that ask what a bot holds ───────────────────────────────────


def test_home_keeps_a_bot_under_the_asset_it_trades(repo: ClerkSqliteRepository) -> None:
    """Home's roster answers "which bots trade this asset" and "whose position is unverified"."""
    _roster_row(repo, registered_under="QQQ", config_json=_config())

    [bot] = home_roster(repo, world="real_paper")

    assert bot.symbol == "QQQ"


def test_an_unclean_end_is_reported_under_the_asset_the_bot_may_still_hold(
    repo: ClerkSqliteRepository, tmp_path: Path
) -> None:
    """The stranded-position notices are about a position, and the position is in QQQ."""
    _roster_row(repo, registered_under="QQQ", config_json=_config())
    for command in (submit_start_run, submit_stop_run):
        command(repo, account_id=repo.account_id, strategy_instance_id="bot", lifecycle_run_id="run-1")
    _receipt(tmp_path, "bot", "run-1", "CRASHED", "RUN_CRASHED")

    ended, unreadable = _uncleanly_ended(SimpleNamespace(repository=repo, account_id="PA-ROSTER"))  # type: ignore[arg-type]

    assert [(bot.sid, bot.symbol) for bot in ended] == [("bot", "QQQ")]
    assert unreadable == []


def _bot_page() -> BotPanelView:
    """The page of a running Paper bot that listens to SPY and trades QQQ."""
    return _panel_view(_status(mode="trade").model_copy(update={"traded": _QQQ}), _clerk_status(), [])


def test_a_working_order_with_no_recorded_symbol_is_in_the_asset_the_bot_trades() -> None:
    order = ProjectedOrder(
        order_ref="ref-1", client_order_id="ref-1", broker_order_id=None, role="ENTER", broker_state="new",
        submitted_at_ms=1, updated_at_ms=2,
    )

    assert _working_order_view(_bot_page(), order, strict=False).symbol == "QQQ"


def test_a_manual_order_from_the_bots_page_is_offered_in_the_asset_it_trades() -> None:
    """The ticket is there to fix the bot's position, which is in QQQ."""
    entry = _manual_order_entry(_bot_page())

    assert "QQQ" in entry.reason and "SPY" not in entry.reason


async def test_a_flatten_cohort_is_the_bots_that_listen_to_the_same_symbol(monkeypatch: pytest.MonkeyPatch) -> None:
    """ADR 0051: a stop wave strands the bots that move in lockstep on one signal, whatever each trades."""
    def row(sid: str, *, symbol: str, traded: TradedInstrumentView | None = None) -> SimpleNamespace:
        return SimpleNamespace(
            strategy_instance_id=sid, strategy_key="ema_crossover_signal", strategy_label="EMA Crossover Signal",
            symbol=symbol, traded=traded, phase="OFF_DUTY",
        )

    async def accept(_broker: str, account_id: str) -> str:
        return account_id

    async def catalog(_broker: str, _account_id: str) -> list[SimpleNamespace]:
        return [row("spy-qqq", symbol="SPY", traded=_QQQ), row("qqq", symbol="QQQ"), row("spy", symbol="SPY")]

    async def panel(_broker: str, _account_id: str, sid: str) -> SimpleNamespace:
        return SimpleNamespace(strategy_instance_id=sid, actions=[], exposure={})

    monkeypatch.setattr(cohort_flatten, "validate_account", accept)
    monkeypatch.setattr(cohort_flatten.panel_data_source, "get_catalog", catalog)
    monkeypatch.setattr(cohort_flatten.panel_data_source, "get_panel", panel)

    view = await cohort_flatten.get_cohort_flatten_view("alpaca", "PA-1")

    assert [(cohort.symbol, [leg.strategy_instance_id for leg in cohort.legs]) for cohort in view.cohorts] == [
        ("SPY", ["spy", "spy-qqq"]),
    ]


# ── History ─────────────────────────────────────────────────────────────


def _history_row(sid: str, *, symbol: str, traded: TradedInstrumentView | None = None) -> FleetBotHistoryRow:
    return FleetBotHistoryRow(
        strategy_instance_id=sid, strategy_key="ema_crossover_signal", strategy_label="EMA Crossover Signal",
        symbol=symbol, traded=traded, account_id="PA-1", world="paper", world_label="PAPER · practice money",
        status="finished", status_label="Finished", started_at_ms=1, stopped_at_ms=2, outcome=None,
        transaction_count=0, orders=BotHistoryOrders(sent=0, filled=0, cancelled=0, rejected=0),
        budget_usd=None, result_usd=None, fees_usd=None, money_unavailable_reason=None, money_scope_note=None,
        runs=(), page_unavailable_reason=None, broker="alpaca", clerk_id="clerk-1",
    )


def _one_lanes_answer(*rows: FleetBotHistoryRow) -> dict:
    """The coordinator's read of one lane, as that lane's route serialises its bots."""
    lane = AccountBotHistory(
        account_id="PA-1", observed_at_ms=1,
        bots=tuple(BotHistoryBot(**row.model_dump(exclude={"broker", "clerk_id"})) for row in rows), gaps=(),
    )
    return {
        "observed_at_ms": 1,
        "lanes": [{"ok": True, "broker": "alpaca", "clerk_id": "clerk-1", "value": lane.model_dump(mode="json")}],
    }


def _history_page(answer: dict, *, symbol: str | None = None):
    return merge_bot_history(
        answer, accounts={"clerk-1": "PA-1"}, extra_gaps=(), filters=BotHistoryFilters(symbol=symbol),
        page=1, page_size=25,
    )


def test_history_finds_a_bot_under_either_symbol() -> None:
    answer = _one_lanes_answer(_history_row("spy-qqq", symbol="SPY", traded=_QQQ), _history_row("iwm", symbol="IWM"))

    assert _history_page(answer).symbols == ("IWM", "QQQ", "SPY")
    for symbol in ("SPY", "QQQ"):
        assert [row.strategy_instance_id for row in _history_page(answer, symbol=symbol).rows] == ["spy-qqq"]
    assert [row.traded for row in _history_page(answer, symbol="QQQ").rows] == [_QQQ]


def test_an_ordinary_bots_history_row_is_sent_as_it_was_before_the_field_existed() -> None:
    """The coordinator reads a lane's answer strictly, so a ``null`` here would blank History on one still
    running the build before this field: an absent traded instrument is left off the wire."""
    answer = _one_lanes_answer(_history_row("spy", symbol="SPY"))

    [sent] = answer["lanes"][0]["value"]["bots"]
    assert "traded" not in sent
    assert [(row.symbol, row.traded) for row in _history_page(answer).rows] == [("SPY", None)]


# ── a refused decision says why ─────────────────────────────────────────

_NO_TRADE = "QQQ had no trade in the minute SPY decided to enter, so the order has no price. It is not retried."


def _stored_decision(seq: int, outcome: str, **facts: object) -> DecisionReceiptResource:
    return DecisionReceiptResource(
        strategy_instance_id="bot", seq=seq, outcome=outcome, symbol="SPY", intent_id=f"eval-{seq}",
        order_ref=None, observed_at_ms=1_700_000_000_000 + seq,
        facts_json=json.dumps({"bar_ref": f"SPY@{seq}", **facts}),
    )


def test_a_refused_entrys_recorded_sentence_reaches_its_row_on_the_bot_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner and the Clerk record why an entry sent no order; the page shows that sentence, not only the code."""
    stored = [
        _stored_decision(1, "no_action", reason_code="NO_SIGNAL"),
        _stored_decision(2, "blocked", reason_code="PRICING_BAR_NO_TRADE", refusal_reason=_NO_TRADE),
    ]
    monkeypatch.setattr(sqlite_panel_source, "active_sqlite_facade", lambda _broker: SimpleNamespace(
        account_id="PA-1", account_mode="paper", repository=SimpleNamespace(decision_receipt_tail=lambda **_: stored),
    ))

    receipts = sqlite_panel_source.read_sqlite_decision_receipts("alpaca", "bot")
    assert receipts is not None
    newest_first = _recent_decision_views(receipts)

    assert [(row.reason_code, row.refusal_reason) for row in newest_first] == [
        ("PRICING_BAR_NO_TRADE", _NO_TRADE),
        ("NO_SIGNAL", None),
    ]


def test_the_traded_assets_lane_holds_the_entries_its_price_feed_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """#2915: a quiet minute, a late price and a broken feed are three different things on the lane."""
    stored = [
        _stored_decision(1, "blocked", reason_code="PRICING_BAR_NO_TRADE", refusal_reason=_NO_TRADE),
        _stored_decision(2, "no_action", reason_code="NO_SIGNAL"),
        _stored_decision(3, "blocked", reason_code="PRICING_BAR_LATE"),
        _stored_decision(4, "blocked", reason_code="PRICING_FEED_UNHEALTHY"),
        _stored_decision(5, "blocked", reason_code="MARKET_LIVENESS_HALTED"),
    ]
    monkeypatch.setattr(sqlite_panel_source, "active_sqlite_facade", lambda _broker: SimpleNamespace(
        account_id="PA-1", account_mode="paper", repository=SimpleNamespace(decision_receipt_tail=lambda **_: stored),
    ))
    receipts = sqlite_panel_source.read_sqlite_decision_receipts("alpaca", "bot")
    assert receipts is not None
    decisions = _recent_decision_views(receipts)

    lane = traded_feed_lane(TradedInstrumentView(kind="stock", symbol="QQQ"), decisions)

    assert lane is not None and lane.symbol == "QQQ"
    assert sorted((event.state, event.sentence) for event in lane.events) == [
        ("late", None),
        ("no_trade", _NO_TRADE),
        ("unhealthy", None),
    ]
    by_reason = {row.reason_code: row for row in decisions}
    assert {event.at_ms for event in lane.events} == {
        by_reason[code].decision_bar_close_ms or by_reason[code].recorded_at_ms
        for code in ("PRICING_BAR_NO_TRADE", "PRICING_BAR_LATE", "PRICING_FEED_UNHEALTHY")
    }
    assert traded_feed_lane(None, decisions) is None


# ── the bot page, read whole ────────────────────────────────────────────

_PAGE_RTH_MS = 1_700_060_400_000  # 2023-11-15 10:00 ET, a Wednesday


@pytest.fixture
async def bot_pages(tmp_path: Path):
    """One Paper account with two bots: ``spy-qqq`` listens to SPY and trades QQQ; ``spy`` trades what it listens to."""
    reset_broker_registry_for_testing()
    reset_idempotency_store_for_testing()
    set_active_clerk_runtime(None)
    set_bot_task_registry(_FakeRegistry(tmp_path, sids=("spy-qqq", "spy")))  # type: ignore[arg-type]
    get_broker_registry().register(_FakeBrokerPort())  # type: ignore[arg-type]
    repo = ClerkSqliteRepository.initialize(
        account_id=ACCT, artifacts_root=tmp_path, clock=_clock_seq(start=_PAGE_RTH_MS)
    )
    for sid, registered_under, config_json in (
        ("spy-qqq", "QQQ", _config()),
        ("spy", "SPY", _config(trades="SPY")),
    ):
        repo.register_strategy_instance(
            exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=sid, symbol=registered_under, config_hash="config-1",
            strategy_key="deployment_validation", display_name="Deployment Validation", config_json=config_json,
        )
        submit_start_run(repo, account_id=ACCT, strategy_instance_id=sid, lifecycle_run_id=f"run-{sid}")
    facade = SqliteAlpacaClerkFacade(
        account_mode="paper", repo=repo, read=_FakeReadPort(), trade=_FakeTradePort(),  # type: ignore[arg-type]
    )
    set_active_clerk_runtime(ActiveClerkRuntime(authority_kind="sqlite", clerk=facade))
    app = FastAPI()
    app.include_router(router)
    try:
        yield app
    finally:
        set_active_clerk_runtime(None)
        set_bot_task_registry(None)
        repo.close()
        reset_broker_registry_for_testing()
        reset_idempotency_store_for_testing()


async def _page(app: FastAPI, sid: str) -> dict:
    async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/{sid}/panel")
    assert response.status_code == 200, response.text
    return response.json()


@pytest.fixture
def clerk_card_symbols(monkeypatch: pytest.MonkeyPatch) -> list[str | None]:
    """The symbol each bot page asks the Clerk's channel health for."""
    asked: list[str | None] = []
    real = panel_data_source.clerk_status

    async def recording(*, symbol: str | None = None, **kwargs: object):
        asked.append(symbol)
        return await real(symbol=symbol, **kwargs)

    monkeypatch.setattr(panel_data_source, "clerk_status", recording)
    return asked


async def test_the_bot_page_of_a_bot_that_trades_another_asset(
    bot_pages: FastAPI, clerk_card_symbols: list[str | None]
) -> None:
    """It listens to SPY and trades QQQ: both are named, and the traded asset has a market read of its own."""
    page = await _page(bot_pages, "spy-qqq")

    assert (page["symbol"], page["traded"]) == ("SPY", {"kind": "stock", "symbol": "QQQ"})
    assert "SPY" in page["traded_asset_warning"]["headline"] and "QQQ" in page["traded_asset_warning"]["headline"]
    assert page["traded_market_pulse"] is not None
    assert page["market_pulse"] is not None
    assert page["traded_feed_lane"]["symbol"] == "QQQ"
    assert clerk_card_symbols == ["QQQ"], "the Clerk's submission gate reads the feed of the asset its orders are in"


async def test_homes_rows_name_what_each_bot_listens_to_and_trades(bot_pages: FastAPI) -> None:
    async with httpx.AsyncClient(transport=ASGITransport(app=bot_pages), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/{ACCT}/bots/catalog")

    assert response.status_code == 200, response.text
    rows = {row["strategy_instance_id"]: row for row in response.json()}
    assert (rows["spy-qqq"]["symbol"], rows["spy-qqq"]["traded"]) == ("SPY", {"kind": "stock", "symbol": "QQQ"})
    assert (rows["spy"]["symbol"], rows["spy"]["traded"]) == ("SPY", None)


async def test_the_bot_page_of_a_bot_that_trades_what_it_listens_to_is_unchanged(
    bot_pages: FastAPI, clerk_card_symbols: list[str | None]
) -> None:
    page = await _page(bot_pages, "spy")

    assert page["symbol"] == "SPY"
    assert (page["traded"], page["traded_asset_warning"], page["traded_market_pulse"]) == (None, None, None)
    assert page["traded_feed_lane"] is None
    assert clerk_card_symbols == ["SPY"]
