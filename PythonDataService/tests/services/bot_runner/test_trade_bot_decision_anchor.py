"""The real trade path hands the Clerk its exact retained decision bar.

``shape_program_leg`` anchors an extended-session leg to that bar (ADR 0059
D5.3), so a ``run_trade_bot`` that resolved no bar refused **every**
``use_rth=False`` decision with ``EXTENDED_ANCHOR_UNAVAILABLE`` — including
the RTH-inside-extended ones that only ever wanted a market leg. The
facade-level suites supplied the bar directly and could not see it; both
tests here drive the runner itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import app.services.bot_trade_strategy as bot_trade_strategy
from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.program_leg import ProgramLegPolicy
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.alpaca.marketable_limit import ExtendedHoursAllowances
from app.broker.contract.models import OrderType, TimeInForce
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import SourceBarLedger
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeClerk, _FakeFeed
from tests.broker.alpaca.clerk.sqlite.conftest import _FakeReadPort, _FakeTradePort

from ._support import _SESSION_OPEN_MS, _green_bar

# 09:59 and 10:00 ET on 2024-01-02 — inside the regular session, and so also
# inside the broker's declared 04:00–20:00 ET extended window. The second bar
# closes the green streak `deployment_validation` enters on.
_FIRST_GREEN_END_MS = _SESSION_OPEN_MS + 29 * 60_000
_DECISION_END_MS = _SESSION_OPEN_MS + 30 * 60_000

_ALLOWANCES = ExtendedHoursAllowances(entry_bps=10, exit_bps=20)


def _extended_binding(account_id: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        strategy_instance_id=_SID,
        strategy_key="deployment_validation",
        broker="alpaca",
        symbol="SPY",
        use_rth=False,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id=account_id,
        action_plan=alpaca_v1_action_plan("SPY"),
        run_id="run-current",
        created_at_ms=_T0,
    )


@pytest.mark.asyncio
async def test_the_trade_path_passes_the_ledgers_decision_bar_to_the_clerk(
    tmp_path: Path,
) -> None:
    """The bar the Clerk receives is the ledger's exact row for that close."""
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST", artifacts_root=tmp_path / "clerk"
    )
    repo.register_strategy_instance(
        strategy_instance_id=_SID, symbol="SPY", config_hash="config-1"
    )
    clerk = _FakeClerk(repository=repo)
    clerk.authority_kind = "sqlite"
    clerk.account_id = "PA-TEST"
    clerk.program_leg_policy = ProgramLegPolicy(
        window=ALPACA_EXTENDED_HOURS_WINDOW, allowances=_ALLOWANCES
    )
    feed = _FakeFeed(
        [_green_bar(_FIRST_GREEN_END_MS), _green_bar(_DECISION_END_MS)], mode="finite"
    )
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-TEST")
    set_alpaca_clerk(clerk)
    try:
        await bot_trade_strategy.run_trade_bot(
            _extended_binding("PA-TEST"), feed, source_bars=ledger
        )

        (call,) = clerk.calls
        assert call["purpose"] == "ENTER"
        assert call["use_rth"] is False
        retained = call["retained_source_bar"]
        assert retained is not None, "the Clerk cannot anchor an extended leg without it"
        expected = ledger.find_by_closed_end(
            provider="fake", symbol="SPY", end_ms=_DECISION_END_MS
        )
        assert expected is not None
        assert retained.bar_identity == expected.bar_identity
        assert retained.end_ms == _DECISION_END_MS
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()


@pytest.mark.asyncio
async def test_an_rth_decision_on_an_extended_run_submits_a_market_leg(
    tmp_path: Path,
) -> None:
    """The case the missing anchor refused: an extended run deciding at 10:00 ET.

    Its decision instant resolves to RTH, so the leg is the ordinary market
    DAY order — but only once the runner hands the Clerk the bar that lets
    ``shape_program_leg`` resolve the phase at all.
    """
    repo = ClerkSqliteRepository.initialize(
        account_id="PA-TEST", artifacts_root=tmp_path / "clerk"
    )
    trade = _FakeTradePort()
    facade = SqliteAlpacaClerkFacade(
        repo=repo,
        read=_FakeReadPort(),
        trade=trade,
        account_mode="paper",
        program_leg_policy=ProgramLegPolicy(
            window=ALPACA_EXTENDED_HOURS_WINDOW, allowances=_ALLOWANCES
        ),
    )
    binding = _extended_binding("PA-TEST")
    await facade.register_strategy_run(binding)
    feed = _FakeFeed(
        [_green_bar(_FIRST_GREEN_END_MS), _green_bar(_DECISION_END_MS)], mode="finite"
    )
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id="PA-TEST")
    set_alpaca_clerk(facade)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger)
        await facade.drain_effects()

        (leg,) = trade.submitted_legs
        assert (leg.order_type, leg.time_in_force, leg.limit_price, leg.extended_hours) == (
            OrderType.MARKET,
            TimeInForce.DAY,
            None,
            False,
        )
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()
