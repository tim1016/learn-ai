"""Books that hold option lots, and Alpaca's documented option activities to read against them (#2913).

The books are the Clerk's real ones. A lot gets onto them through the
test-only order acceptance of ``option_custody_fixtures`` and the real fill
fold, so a bot can hold a sold option here though no bot may sell one yet.
The activities are the rows Alpaca's pages document, read by the real port.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.broker.alpaca.clerk.fifo_pnl import PnLResult, compute_fifo_pnl
from app.broker.alpaca.clerk.sqlite.contract_terms import ContractTerms
from app.broker.alpaca.clerk.sqlite.economic_projection import effective_fill_records
from app.broker.alpaca.clerk.sqlite.off_loop import run_inline
from app.broker.alpaca.clerk.sqlite.option_event_acknowledgement import (
    HoldAcknowledgementReceipt,
    HoldAcknowledgementRequest,
)
from app.broker.alpaca.clerk.sqlite.option_events import (
    OptionEventPoll,
    acknowledge_option_event_hold,
    poll_option_events,
)
from app.broker.alpaca.clerk.sqlite.repository import ClerkSqliteRepository
from app.broker.alpaca.clerk.sqlite.uncertainty_causes import OPTION_EVENT_HOLD_REASON_CODES
from app.broker.contract.models import BrokerPosition
from app.options.contract import OptionContract
from app.services.session_authority import et_minute_of_day_ms
from tests.broker.alpaca.clerk.sqlite.conftest import _clock_at, _FakeReadPort, _TestClock
from tests.broker.alpaca.clerk.sqlite.option_custody_fixtures import (
    ACCOUNT_ID,
    accept_option_order,
    fill,
    open_option_books,
)


def noon(day: date) -> int:
    return et_minute_of_day_ms(day, 12 * 60)


def books_at(tmp_path: Path, day: date, bots: dict[str, str]) -> ClerkSqliteRepository:
    """Fresh books at noon New York time on ``day``, each bot registered under its underlying and running."""
    return open_option_books(tmp_path, clock=_clock_at(noon(day)), bots=bots)


def holds_option(
    repo: ClerkSqliteRepository, bot: str, symbol: str, *, contracts: int, price: float = 1.94, multiplier: int = 100
) -> None:
    """``bot`` gets a lot of ``symbol`` now: bought when ``contracts`` is above zero, sold when below."""
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    side = "buy" if contracts > 0 else "sell"
    order = accept_option_order(
        repo, terms=ContractTerms(symbol=symbol, multiplier=multiplier), side=side, contracts=abs(contracts),
        decision_id=f"{bot}-{symbol}-{clock()}", strategy_instance_id=bot,
    )
    fill(
        repo, order, execution_id=f"x-{bot}-{symbol}-{clock()}", symbol=symbol, side=side.upper(),
        quantity=abs(contracts), price=price, occurred_at_ms=clock(),
    )
    clock.advance(1_000)


def later(repo: ClerkSqliteRepository, ms: int) -> None:
    """Time passes, and the Clerk's hold on its account is taken up again as a running Clerk keeps it."""
    clock = repo.clock
    assert isinstance(clock, _TestClock)
    clock.advance(ms)
    repo.revive_execution_lease()


def broker_holds(**quantities: float) -> list[BrokerPosition]:
    """The account's positions as the broker would list them, by symbol."""
    return [
        BrokerPosition(
            broker="alpaca", symbol=symbol, asset_id=None,
            asset_class="us_option" if len(symbol) > 10 else "us_equity", quantity=abs(quantity),
            side="long" if quantity > 0 else "short", average_entry_price=1.0, market_value=0.0, cost_basis=0.0,
            current_price=None, unrealized_pl=0.0, unrealized_plpc=None, observed_at_ms=0,
        )
        for symbol, quantity in quantities.items()
        if quantity
    ]


async def poll(repo: ClerkSqliteRepository, account: _FakeReadPort, **how: Any) -> OptionEventPoll:
    """One poll of the account's option events, as the keeper's round and the pass make it."""
    return await poll_option_events(repo, read=account, off_loop=run_inline, **how)


def fifo(repo: ClerkSqliteRepository, bot: str) -> PnLResult:
    with repo.write_fence() as conn:
        return compute_fifo_pnl(effective_fill_records(conn, account_id=ACCOUNT_ID, strategy_instance_ids=[bot]))


def held(repo: ClerkSqliteRepository, bot: str) -> dict[str, float]:
    return {symbol: qty for symbol, qty in repo.attributed_positions_for_strategy(bot).items() if qty}


def share_cash(repo: ClerkSqliteRepository, bot: str, underlying: str) -> Decimal:
    """What the bot's fills in ``underlying`` paid (negative) or received (positive), exact."""
    with repo.write_fence() as conn:
        records = effective_fill_records(conn, account_id=ACCOUNT_ID, strategy_instance_ids=[bot])
    return sum(
        (record.exact_notional * (-1 if record.side.value == "buy" else 1) for record in records if record.symbol == underlying),
        Decimal(0),
    )


def option_holds(repo: ClerkSqliteRepository) -> dict[tuple[str | None, str], dict[str, Any]]:
    """Every option-event hold now open, by (bot or None, reason code)."""
    return {
        (row["strategy_instance_id"], row["reason_code"]): row
        for row in repo.active_uncertainties()
        if row["reason_code"] in OPTION_EVENT_HOLD_REASON_CODES
    }


@dataclass(frozen=True)
class Compared:
    """What a reconciliation pass found when it compared the account with the broker, as a test says it did."""

    through_sequence: int | None
    verdict: str = "clean"
    drifted_symbols: tuple[str, ...] = ()
    indeterminate_symbols: tuple[str, ...] = ()


def as_read(repo: ClerkSqliteRepository, bot: str | None, reason_code: str, **changes: Any) -> HoldAcknowledgementRequest:
    """An acknowledgement of one open notice exactly as its owner read it just now, with ``changes`` laid over."""
    hold = option_holds(repo)[(bot, reason_code)]
    return HoldAcknowledgementRequest(**{
        "strategy_instance_id": bot, "reason_code": reason_code, "uncertainty_id": hold["uncertainty_id"],
        "observed_at_ms": hold["observed_at_ms"],
        "item_refs": tuple(item["ref"] for item in json.loads(hold["facts_json"])["cause_facts"]["items"]),
        **changes,
    })


def acknowledge(
    repo: ClerkSqliteRepository, bot: str | None, reason_code: str, **differs: Any
) -> HoldAcknowledgementReceipt:
    """The owner acknowledges an open notice, on a pass that has just compared the account with the broker.

    The pass found them agreeing, but for what ``differs`` says of it.
    """
    return acknowledge_option_event_hold(
        repo, request=as_read(repo, bot, reason_code), operator="owner",
        compared=Compared(**{"through_sequence": repo.last_custody_sequence(), **differs}),
    )


def bookings(repo: ClerkSqliteRepository) -> list[dict[str, Any]]:
    """Every option position the books recorded as ended, oldest first."""
    with repo.write_fence() as conn:
        rows = conn.execute(
            "SELECT strategy_instance_id, summary_code, facts_json FROM custody_transitions "
            "WHERE transition_kind = 'OPTION_POSITION_ENDED' ORDER BY sequence"
        ).fetchall()
    return [dict(row) for row in rows]


def contract(symbol: str) -> OptionContract:
    return OptionContract.from_occ_symbol(symbol)
