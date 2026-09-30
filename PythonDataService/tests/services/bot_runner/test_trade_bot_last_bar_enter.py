"""#2607 through the runner: a regular-hours ENTER decided on the session's last bar is refused.

The last bar closes *at* the regular close, so its decision lands a fraction of
a second after it: it is the closing bar, and the runner never sends a
closing-bar decision (``bot_trade_strategy._refused_on_the_closing_bar``).
The ENTER is discarded and receipted ``CLOSING_BAR`` before the strategy's
liveness gate or the Clerk is reached, reading only the canonical calendar.

The Clerk's market-closed gate (#2596) stays as the backstop behind that
screen -- a market ENTER that could not reach the broker inside the regular
session is still refused there -- and its own suite
(``test_runtime_program_leg.py``) pins it on the facade.

The real ``run_trade_bot`` drives the sealed ``ema_crossover_signal`` program
into a real ``SqliteAlpacaClerkFacade`` over a fake broker, on the retained
LEAN input that makes QQQ's first ENTER on 2026-02-03's last 15-minute bucket
-- the ENTER LEAN itself submitted at the close and filled at the next open.
An ordinary ENTER still goes out: ``test_trade_bot_last_bar_exit.py`` enters
through the same clock and Clerk sixteen minutes before the close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import app.broker.alpaca.clerk.sqlite.runtime as clerk_runtime
import app.services.bot_trade_strategy as bot_trade_strategy
import app.services.feed_continuity_policy as feed_continuity_policy
from app.broker.alpaca.clerk import set_alpaca_clerk
from app.broker.alpaca.clerk.sqlite.decision_receipts import SqliteDecisionReceipts
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.runtime import SqliteAlpacaClerkFacade
from app.broker.contract.models import BrokerOrder, BrokerOrderLeg
from app.lean_sidecar.closing_bar import CLOSING_BAR_REASON_CODE
from app.lean_sidecar.trading_calendar import session_close_ms_utc
from app.marketdata.feed import MarketDataBar
from app.services.bot_binding_repository import BrokerBotBinding, alpaca_v1_action_plan
from app.services.source_bar_ledger import SourceBarLedger
from app.utils.session_anchors import et_date_at_ms
from tests._helpers.bot_runner.custody import _SID, _T0
from tests._helpers.bot_runner.doubles import _FakeFeed, _SqliteRuntimeBroker
from tests._helpers.bot_runner.ema_parity import EMA_LAST_BAR_ENTER_DAY, ema_bars_through_a_last_bar_enter
from tests._helpers.bot_runner.market import clock_read_before_the_close, patch_wall_clock_to_the_fed_bar
from tests._helpers.exit_terms import DEPLOY_EXIT_TERMS

_ACCOUNT_ID = "PA-TEST"
# A live decision lands this long after its bar closes (the issue's estimate).
_DECISION_DELAY_MS = 600
# The replayed clock jumps a bar at a time, overnight included, with no lease
# heartbeat in between; the Clerk's execution lease must outlive the span.
_REPLAY_LEASE_TTL_MS = 2 * 86_400_000


class _RecordingBroker(_SqliteRuntimeBroker):
    """Keeps every leg the Clerk put on the wire."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted_legs: list[BrokerOrderLeg] = []

    async def submit(self, leg: BrokerOrderLeg, *, client_order_id: str) -> BrokerOrder:
        self.submitted_legs.append(leg)
        return await super().submit(leg, client_order_id=client_order_id)


def _regular_hours_binding(symbol: str) -> BrokerBotBinding:
    return BrokerBotBinding(
        exit_terms=DEPLOY_EXIT_TERMS, strategy_instance_id=_SID,
        strategy_key="ema_crossover_signal",
        broker="alpaca",
        symbol=symbol,
        use_rth=True,
        mode="trade",
        quantity=1,
        carryover_policy="FORBID",
        sealed_account_id=_ACCOUNT_ID,
        action_plan=alpaca_v1_action_plan(symbol),
        run_id="run-current",
        created_at_ms=_T0,
    )


async def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bars: list[MarketDataBar],
    *,
    decision_delay_ms: int = _DECISION_DELAY_MS,
) -> tuple[_RecordingBroker, list[dict[str, object]]]:
    """Feed ``bars`` to a regular-hours EMA run; return the wire and the receipts."""
    # Seed before acquiring the lease: no future-dated lease can mask expiry.
    patch_wall_clock_to_the_fed_bar(
        monkeypatch, start_ms=bars[0].start_ms, decision_delay_ms=decision_delay_ms
    )
    liveness = clock_read_before_the_close(session_close_ms_utc(et_date_at_ms(bars[-1].end_ms)))
    monkeypatch.setattr(bot_trade_strategy, "market_liveness_fact", liveness)
    monkeypatch.setattr(clerk_runtime, "market_liveness_fact", liveness)
    repo = ClerkSqliteRepository.initialize(
        account_id=_ACCOUNT_ID,
        artifacts_root=tmp_path / "clerk",
        clock=lambda: feed_continuity_policy.now_ms_utc(),
        lease_ttl_ms=_REPLAY_LEASE_TTL_MS,
    )
    broker = _RecordingBroker()
    facade = SqliteAlpacaClerkFacade(repo=repo, read=broker, trade=broker, account_mode="paper")
    binding = _regular_hours_binding(bars[0].symbol)
    await facade.register_strategy_run(binding)
    feed = _FakeFeed(bars, mode="finite")
    ledger = SourceBarLedger(artifacts_root=tmp_path / "ledger", account_id=_ACCOUNT_ID)
    set_alpaca_clerk(facade)
    try:
        await bot_trade_strategy.run_trade_bot(binding, feed, source_bars=ledger)
        await facade.drain_effects()
        receipts = [
            {"outcome": receipt.outcome, **json.loads(receipt.facts_json)}
            for receipt in SqliteDecisionReceipts(repo, strategy_instance_id=_SID).tail(20)
        ]
    finally:
        set_alpaca_clerk(None)
        ledger.close()
        repo.close()
    return broker, receipts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "decision_delay_ms",
    [_DECISION_DELAY_MS, -_DECISION_DELAY_MS],
    ids=["clock-read-open-before-the-close", "clock-running-behind"],
)
async def test_a_regular_hours_enter_decided_on_the_last_bar_is_refused_and_nothing_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, decision_delay_ms: int
) -> None:
    """The runner refuses the closing-bar ENTER before any clock or Clerk is read.

    Both clocks that let a last-bar ENTER slip through before #2596 -- the
    broker's last answer read OPEN just before the close, and a host clock
    that reads 15:59:59.4 when the 16:00 decision lands -- now meet the
    closing-bar screen first, which reads only the calendar.
    """
    close_ms = session_close_ms_utc(EMA_LAST_BAR_ENTER_DAY)

    broker, receipts = await _run(
        tmp_path, monkeypatch, ema_bars_through_a_last_bar_enter(), decision_delay_ms=decision_delay_ms
    )

    assert broker.submitted_legs == []
    last = receipts[-1]
    assert (last["outcome"], last["reason_code"], last["decision_bar_close_ms"]) == (
        "blocked",
        CLOSING_BAR_REASON_CODE,
        close_ms,
    )
    assert "market_liveness" not in last
