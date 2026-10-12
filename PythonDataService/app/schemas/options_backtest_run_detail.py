"""Wire shapes for reading a saved Options Backtest run closely: its leg trades, one of its sessions, and two runs side
by side (#3030).

The rules are the run service's (``options_backtest_runs.py``): snake_case; every instant is ``int64 ms UTC`` under
``MAX_TIMESTAMP_MS`` and a session is identified by its open, never by a date; money and prices are strings of exact
decimals and a name ending ``_usd`` is dollars; every model's name begins ``OptionsBacktest``; a closed set is an
inline ``Literal`` of its words and a test holds each to the enum it comes from.
"""

from __future__ import annotations

from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.options.contract import OptionContract, OptionRight
from app.options_backtest.results import Fill, Mark
from app.options_backtest.run_detail import TradePage
from app.options_backtest.saved_runs import StoredTrade
from app.schemas.options_backtest_runs import ExitReasonWord, Ms, exact

TRADES_PAGE_MAX = 5000
TRADES_PAGE_DEFAULT = 1000


class OptionsBacktestContractResponse(BaseModel):
    """An option contract: its OCC symbol, and the symbol read out for display."""

    symbol: str
    underlying: str
    right: OptionRight
    strike_usd: str
    # The expiry's 16:00 New York anchor. It names a date: show it as a date in New York time, never in the viewer's.
    expiry_ms: Ms

    @classmethod
    def from_contract(cls, contract: OptionContract) -> Self:
        return cls(
            symbol=contract.occ_symbol,
            underlying=contract.root,
            right=contract.right,
            strike_usd=exact(contract.strike),
            expiry_ms=contract.expiry_ms,
        )


# ── Leg trades ───────────────────────────────────────────────────────────


class OptionsBacktestTradeFillResponse(BaseModel):
    """An order that filled: the bar it filled in, the open of that bar (the price seen) and the price it filled at."""

    bar_close_ms: Ms
    seen_usd: str
    filled_usd: str


class OptionsBacktestTradeMarkResponse(BaseModel):
    """A leg valued and not sold: the last bar its contract traded in and the close of that bar."""

    bar_close_ms: Ms
    price_usd: str


class OptionsBacktestTradeResponse(BaseModel):
    """One leg round trip. It left by a ``sale`` or, when its sale never filled, by a ``mark``; exactly one is set."""

    session_open_ms: Ms
    # Its place in the session's order, from 0, as the run recorded it.
    trade_number: int = Field(ge=0)
    leg_id: str
    # 0 for the leg's first entry, 1 for its first re-entry, and so on.
    entry_number: int = Field(ge=0)
    contract: OptionsBacktestContractResponse
    contracts: int
    multiplier: int
    # Set when the leg waited on momentum.
    starting_price_usd: str | None = None
    momentum_level_usd: str | None = None
    # Set when the leg carried a stop loss.
    stop_level_usd: str | None = None
    entry_decided_at_ms: Ms
    entry: OptionsBacktestTradeFillResponse
    exit_reason: ExitReasonWord
    exit_group: int
    exit_decided_at_ms: Ms
    exit_kind: Literal["sale", "mark"]
    sale: OptionsBacktestTradeFillResponse | None = None
    mark: OptionsBacktestTradeMarkResponse | None = None
    entry_fee_usd: str
    # Zero for a leg that was marked and not sold.
    exit_fee_usd: str
    pnl_usd: str

    @model_validator(mode="after")
    def _the_exit_is_the_one_it_says(self) -> Self:
        if (self.exit_kind == "sale") != (self.sale is not None) or (self.exit_kind == "mark") != (
            self.mark is not None
        ):
            raise ValueError("a trade left by a sale or by a mark, and carries exactly the one its exit_kind names")
        return self

    @classmethod
    def from_stored(cls, stored: StoredTrade) -> Self:
        trade, leaving = stored.trade, stored.trade.exit
        entry = trade.entry
        sold = isinstance(leaving, Fill)
        return cls(
            session_open_ms=stored.session_open_ms,
            trade_number=stored.trade_number,
            leg_id=trade.leg_id,
            entry_number=trade.entry_number,
            contract=OptionsBacktestContractResponse.from_contract(trade.contract),
            contracts=trade.contracts,
            multiplier=trade.multiplier,
            starting_price_usd=None if trade.starting_price is None else exact(trade.starting_price),
            momentum_level_usd=None if trade.momentum_level is None else exact(trade.momentum_level),
            stop_level_usd=None if trade.stop_level is None else exact(trade.stop_level),
            entry_decided_at_ms=trade.entry_decided_at_ms,
            entry=OptionsBacktestTradeFillResponse(
                bar_close_ms=entry.bar_close_ms, seen_usd=exact(entry.seen), filled_usd=exact(entry.filled)
            ),
            exit_reason=trade.exit_reason.value,  # type: ignore[arg-type]
            exit_group=trade.exit_group,
            exit_decided_at_ms=trade.exit_decided_at_ms,
            exit_kind="sale" if sold else "mark",
            sale=OptionsBacktestTradeFillResponse(
                bar_close_ms=leaving.bar_close_ms, seen_usd=exact(leaving.seen), filled_usd=exact(leaving.filled)
            )
            if isinstance(leaving, Fill)
            else None,
            mark=OptionsBacktestTradeMarkResponse(bar_close_ms=leaving.bar_close_ms, price_usd=exact(leaving.price))
            if isinstance(leaving, Mark)
            else None,
            entry_fee_usd=exact(trade.entry_fee_usd),
            exit_fee_usd=exact(trade.exit_fee_usd),
            pnl_usd=exact(trade.pnl_usd),
        )


class OptionsBacktestTradesResponse(BaseModel):
    """A page of a run's leg trades. ``total`` counts every trade that matches, on every page; ``next_offset`` is the
    ``offset`` to ask for the rest, and is ``None`` on the last page. The CSV export is never paged."""

    run_id: int
    total: int = Field(ge=0)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=TRADES_PAGE_MAX)
    next_offset: int | None = None
    trades: list[OptionsBacktestTradeResponse]

    @classmethod
    def from_page(cls, page: TradePage) -> Self:
        return cls(
            run_id=page.run_id,
            total=page.total,
            offset=page.offset,
            limit=page.limit,
            next_offset=page.next_offset,
            trades=[OptionsBacktestTradeResponse.from_stored(stored) for stored in page.trades],
        )
