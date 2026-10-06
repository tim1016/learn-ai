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

import pytest

from app.broker.alpaca.clerk.sqlite.commands import submit_start_run, submit_stop_run
from app.broker.alpaca.clerk.sqlite.models import DecisionReceiptResource
from app.broker.alpaca.clerk.sqlite.projection_models import ProjectedOrder
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.engine.live.desired_state import DesiredState
from app.schemas.action_plan import TradedInstrumentView, order_symbol
from app.schemas.bot_history import BotHistoryOrders, FleetBotHistoryRow
from app.schemas.traded_asset_acknowledgement import (
    UntestedTradedAssetAcknowledgement,
    UntestedTradedAssetWarning,
)
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.bot_registry_projection import project_bot_status
from app.services.broker_v2_panel import cohort_flatten, sqlite_panel_source, sqlite_roster_status
from app.services.broker_v2_panel.bot_page_projection import _manual_order_entry
from app.services.broker_v2_panel.catalog_projection_service import SqliteCatalogProjectionUnavailable
from app.services.broker_v2_panel.panel_projection_service import _recent_decision_views
from app.services.broker_v2_panel.sqlite_panel_adapter import _working_order_view
from app.services.broker_v2_panel.sqlite_panel_source import _uncleanly_ended, home_roster
from app.services.fleet_bot_history import BotHistoryFilters, _symbols_of
from tests.broker.v2panel.test_bot_history import _receipt
from tests.broker.v2panel.test_panel_projection import _clerk_status, _status
from tests.broker.v2panel.test_panel_projection import _panel as _panel_view

_QQQ = TradedInstrumentView(kind="stock", symbol="QQQ")


def _config(*, listens_to: str | None = "SPY", trades: str | None = "QQQ", **more: object) -> str:
    """A bot's immutable configuration as the runner seals it: the Signal stream's symbol and the plan."""
    config: dict[str, object] = {"mode": "trade", "quantity": 1, "carryover_policy": "FORBID", **more}
    if listens_to is not None:
        config["symbol"] = listens_to
    if trades is not None:
        config["action_plan"] = alpaca_v1_action_plan(trades).model_dump(mode="json")
    return json.dumps(config)


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


def test_a_runner_backed_row_reads_the_same_two_facts_from_its_binding() -> None:
    binding = BrokerBotBinding(
        strategy_instance_id="bot", strategy_key="ema_crossover_signal", broker="alpaca", symbol="SPY",
        mode="dry_run", quantity=1, action_plan=alpaca_v1_action_plan("QQQ"), run_id="run-1", created_at_ms=0,
    )

    row = project_bot_status(binding, None, DesiredState.RUNNING, running=True)

    assert (row.symbol, row.traded) == ("SPY", _QQQ)


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


def _panel(**overrides: object) -> SimpleNamespace:
    """The two facts of a bot page these readers take from it."""
    return SimpleNamespace(symbol="SPY", traded=_QQQ, mode="trade", status="running", **overrides)


def test_a_working_order_with_no_recorded_symbol_is_in_the_asset_the_bot_trades() -> None:
    order = ProjectedOrder(
        order_ref="ref-1", client_order_id="ref-1", broker_order_id=None, role="ENTER", broker_state="new",
        submitted_at_ms=1, updated_at_ms=2,
    )

    assert _working_order_view(_panel(), order, strict=False).symbol == "QQQ"  # type: ignore[arg-type]


def test_a_manual_order_from_the_bots_page_is_offered_in_the_asset_it_trades() -> None:
    """The ticket is there to fix the bot's position, which is in QQQ."""
    entry = _manual_order_entry(_panel())  # type: ignore[arg-type]

    assert entry.reason == "Place an order for QQQ on this account."


async def test_a_flatten_cohort_is_the_bots_that_hold_the_same_asset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two bots hold QQQ, one listening to SPY and one to QQQ itself. A third listens to SPY and holds SPY."""
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
        ("QQQ", ["qqq", "spy-qqq"]),
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


def test_history_finds_a_bot_under_either_symbol() -> None:
    listens_spy_trades_qqq = _history_row("a", symbol="SPY", traded=_QQQ)
    trades_iwm = _history_row("b", symbol="IWM")

    assert _symbols_of(listens_spy_trades_qqq) == ("SPY", "QQQ")
    assert BotHistoryFilters(symbol="QQQ").admits(listens_spy_trades_qqq)
    assert BotHistoryFilters(symbol="SPY").admits(listens_spy_trades_qqq)
    assert not BotHistoryFilters(symbol="QQQ").admits(trades_iwm)


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
