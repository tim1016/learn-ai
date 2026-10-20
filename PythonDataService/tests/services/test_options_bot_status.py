"""What the bot page says about an Options bot's legs."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.broker.alpaca.clerk.fills import FillRecord
from app.broker.contract.models import OrderSide
from app.engine.live.identity import strategy_instance_artifact_dir
from app.options_backtest.market import MinuteBar
from app.options_bots.journal import OptionsBotJournal
from app.options_bots.observations import ConsumedMinute, ObservedOrder
from app.options_bots.session import OptionsSession
from app.routers import options_bots as router_module
from app.schemas.options_deploy import OptionsBotSessionView
from app.services import options_bot_status as status_service
from app.services.options_bot_status import leg_views
from tests.options_backtest._market import D, TableMarket, at, contract, flat, leg, picked, setup, steady
from tests.options_bots.test_runtime import _binding
from tests.options_bots.test_session import _observed

CALL = contract("call", "500")


def test_waiting_leg_names_its_contract_trigger_and_latest_saved_premium() -> None:
    market = TableMarket(underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2", 5, 60)})
    rules = OptionsSession(setup(leg(momentum=("percent_up", "25"))), market)
    rules.advance(at(5))
    saved = ConsumedMinute(CALL.occ_symbol, at(6), MinuteBar(at(6), D("2.3"), D("2.3"), D("2.3"), D("2.3")))
    view = leg_views(rules.state, minutes=(saved,), fills=())[0]
    assert view.waiting_contract == CALL.occ_symbol
    assert D(view.starting_price) == D(2)
    assert D(view.momentum_level) == D("2.5")
    assert [(watched.symbol, watched.premium, watched.close_ms) for watched in view.watched_contracts] == [(CALL.occ_symbol, "2.3", at(6))]


def test_leg_pnl_is_fifo_over_its_own_orders_with_a_missing_mark_left_unknown() -> None:
    market = TableMarket(underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.6", 5, 60)})
    rules = OptionsSession(setup(leg(contracts=3)), market)
    rules.advance(at(5))
    rules.advance(at(6), (_observed("entry", quantity=3, price="2"),))
    rules.advance(at(7), (_observed("close", minute=7, quantity=1, price="3", kind="close"),))
    fills = [FillRecord(account_id="paper", sid="bot", intent_id="entry", order_ref=ref,
                        event_key=ref, symbol=CALL.occ_symbol, side=side, quantity=quantity,
                        fill_price=price, filled_at_ms=at(minute), fee=None, multiplier=100)
             for ref, side, quantity, price, minute in (("entry", OrderSide.BUY, 3, 2, 6), ("close", OrderSide.SELL, 1, 3, 7), ("another-leg", OrderSide.BUY, 10, 5, 7))]
    owners = {("entry", CALL.occ_symbol): "call", ("close", CALL.occ_symbol): "call", ("another-leg", CALL.occ_symbol): "other"}
    saved = ConsumedMinute(CALL.occ_symbol, at(7), market.option_bar(CALL, at(7)))
    view = leg_views(rules.state, fills=fills, order_legs=owners, minutes=(saved,))[0]
    assert D(view.realized_pnl_usd) == D(100)
    assert D(view.open_pnl_usd) == D(120)
    missing = leg_views(rules.state, fills=fills, order_legs=owners)[0]
    assert missing.open_pnl_usd is None
    assert "unavailable" in missing.pnl_notice
    incomplete = leg_views(rules.state, fills=fills, order_legs={key: value for key, value in owners.items() if key[0] != "close"}, minutes=(saved,))[0]
    assert incomplete.realized_pnl_usd is None and incomplete.open_pnl_usd is None
    assert "not been attributed" in incomplete.pnl_notice


@pytest.mark.parametrize("mode", ["trade", "dry_run"])
async def test_stopped_session_reads_its_own_partial_close_after_exchange_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    binding = _binding(setup(leg(contracts=3))).model_copy(update={"mode": mode, "sealed_account_id": "unregistered-session-account"})
    folder = strategy_instance_artifact_dir(tmp_path, "live_state", binding.strategy_instance_id)
    journal = OptionsBotJournal(folder, strategy_instance_id=binding.strategy_instance_id, setup_sha256=binding.options_bot.setup_sha256)
    market = TableMarket(underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.6", 5, 60)})
    rules = OptionsSession(binding.options_bot.setup, market)
    journal.commit(rules.state, rules.advance(at(5)))
    attempt = journal.decisions()[0].attempt_id
    journal.commit(rules.state, rules.advance(at(6), (_observed("entry", quantity=3, price="2"),)),
                   orders=(ObservedOrder("entry", attempt, "call", 0, CALL.occ_symbol, "entry", at(6)),),
                   minutes=(ConsumedMinute(CALL.occ_symbol, at(6), market.option_bar(CALL, at(6))),), read_at_ms=at(6))
    journal.commit(rules.state, rules.advance(at(390)), read_at_ms=at(390))
    journal.commit(rules.state, rules.advance(at(390), (_observed("partial-close", minute=391, quantity=1, price="3", kind="close"),), observed_at_ms=at(391)),
                   orders=(ObservedOrder("close", attempt, "call", 0, CALL.occ_symbol, "close", at(391)),), read_at_ms=at(391))
    assert max(event.at_ms for event in journal.state.events) < at(391)
    journal.close()
    fills = tuple(FillRecord(account_id="paper", sid=binding.strategy_instance_id, intent_id=ref, order_ref=ref, event_key=ref,
                             symbol=CALL.occ_symbol, side=side, quantity=qty, fill_price=price, filled_at_ms=at(minute), fee=None, multiplier=100)
                  for ref, side, qty, price, minute in (("entry", OrderSide.BUY, 3, 2, 6), ("close", OrderSide.SELL, 1, 3, 391)))
    class Reader:
        def bot_fill_window(self, _sid: str, *, from_ms: int, to_ms: int) -> SimpleNamespace:
            assert from_ms == at(0) and to_ms == at(392) + 1
            return SimpleNamespace(fills=fills)
        def close(self) -> None:
            return
    monkeypatch.setattr(status_service, "now_ms_utc", lambda: at(392))
    monkeypatch.setattr(status_service.SqliteEconomicProjectionReader, "from_repository", lambda _: Reader())
    registry = SimpleNamespace(artifacts_root=tmp_path, binding_for_control=lambda *_: binding,
                               status=lambda *_: SimpleNamespace(running=False, duty_outcome=None),
                               bot_end=lambda *_: SimpleNamespace(status="open"))
    opened = []
    @asynccontextmanager
    async def selected(_registry: object, selected_binding: object) -> AsyncIterator[SimpleNamespace]:
        assert selected_binding is binding
        opened.append("opened")
        try:
            yield SimpleNamespace(sqlite_repository=object())
        finally:
            opened.append("closed")
    async def valid_scope(*_args: object) -> None:
        return
    monkeypatch.setattr(status_service, "binding_clerk_runtime", selected)
    monkeypatch.setattr(router_module, "_runner", lambda: registry)
    monkeypatch.setattr(router_module.panel_scope, "validate_account_scope", valid_scope)
    app = FastAPI()
    app.include_router(router_module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/api/brokers/alpaca/accounts/account/options-bots/{binding.strategy_instance_id}/session")
    assert response.status_code == 200, response.text
    view = OptionsBotSessionView.model_validate(response.json())
    assert opened == ["opened", "closed"]
    assert view.legs[0].realized_pnl_usd is not None
    assert view.legs[0].held_contracts == 2
    assert D(view.legs[0].realized_pnl_usd) == D(100)
    assert D(view.legs[0].open_pnl_usd) == D(120)


def test_legs_read_not_started_until_the_entry_minute_starts_the_session() -> None:
    """A session's legs default to ``done``; before the entry minute that read as if the day were over."""
    market = TableMarket(
        underlying=steady("500"), picks=picked(("call", "500", CALL)), options={CALL: flat("2.00", 5, 60)},
    )
    rules = OptionsSession(setup(leg()), market)

    before = [view.phase for view in leg_views(rules.state)]
    rules.advance(at(4))
    still_before = [view.phase for view in leg_views(rules.state)]
    rules.advance(at(5))
    at_entry = [view.phase for view in leg_views(rules.state)]

    assert before == still_before == ["not_started"]
    assert at_entry == ["ordered"]
