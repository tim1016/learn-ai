"""The LEAN-compatibility profile pays the cash dividends LEAN pays (#2903).

LEAN, on raw prices, credits ``distribution × held quantity`` when a
position is held over an ex-date. The engine settles the factor-file
schedule on the first bar past the cum-dividend session's close — the
ex-date's first bar — and every later order is sized from the credited
cash. These are the regression guards: without the settle, a held position
ends the run with cash LEAN does not agree with.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from app.engine.data.dividends import dividends_from_factor_file, dividends_from_factor_tree
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.order import FillMode
from app.engine.strategy.base import Strategy
from app.lean_sidecar.workspace import SymbolValidationError
from app.utils.timestamps import datetime_at_ms, to_ms_utc

NY = ZoneInfo("America/New_York")
_SERVICE_ROOT = Path(__file__).resolve().parents[2]
_SPY_CAPTURE_FACTOR_FILE = (
    _SERVICE_ROOT / "tests/fixtures/golden/cross-engine-studies/_lean_data_capture/SPY/equity/usa/factor_files/spy.csv"
)


def _minute(trading_date: date, hour: int, minute: int) -> TradeBar:
    start_ms = to_ms_utc(datetime(trading_date.year, trading_date.month, trading_date.day, hour, minute, tzinfo=NY))
    return TradeBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("100"),
        high=Decimal("100"),
        low=Decimal("100"),
        close=Decimal("100"),
        volume=10_000,
    )


def _two_session_bars() -> list[TradeBar]:
    """Three bars on the cum-dividend Thursday and three on the ex-date Friday."""
    return [_minute(date(2026, 3, 19), 9, 30 + offset) for offset in range(3)] + [
        _minute(date(2026, 3, 20), 9, 30 + offset) for offset in range(3)
    ]


class _SyntheticStream:
    """data_source.iter_bars contract: returns a fresh iterator each call."""

    def __init__(self, bars: list[TradeBar]) -> None:
        self._bars = bars

    def iter_bars(self, symbol: str, start_date: date, end_date: date) -> Iterator[TradeBar]:
        return iter(self._bars)


class _BuyFirstBarHold(Strategy):
    """Goes all-in on the first consolidated bar and holds overnight."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted = False

    def initialize(self) -> None:
        self.set_start_date(2026, 3, 19)
        self.set_end_date(2026, 3, 20)
        self.set_cash(10_000)
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), self._on_minute)

    def _on_minute(self, bar: TradeBar) -> None:
        if not self.submitted:
            self.submitted = True
            self.ctx.set_holdings("SPY", Decimal("1.0"))


def _write_factor_file(path: Path) -> None:
    """One $1.00 distribution going ex on 2026-03-20, plus a 2025 row outside
    any window and an in-window pair whose factor does not change (a row pair
    with no dividend between them: LEAN's own ``PriceFactor != nextRow.PriceFactor``
    test)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "20250102,0.98,1,50.00\n20260310,0.99,1,99.00\n20260311,0.99,1,99.50\n"
        "20260319,0.99,1,100.00\n20260401,1,1,101.00\n",
        encoding="utf-8",
    )


def _write_same_date_factor_file(path: Path) -> None:
    """Two distributions sharing an ex-date are one dividend (#3000 review).

    LEAN's CorporateFactorProvider.GetSplitsAndDividends walks its rows
    grouped by date and uses each date's first row only, reconstructing from
    that row's factor to the next date's — not one dividend per row.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    # The 2026-03-19 pair carries two distributions (pf 0.99 then 0.9702);
    # LEAN's walk uses only the first row, against 2026-04-01's factor of 1:
    # round_half_even(100 − 100·0.99, 2) = 1.00.
    path.write_text(
        "20250102,0.98,1,50.00\n20260319,0.99,1,100.00\n20260319,0.9702,1,100.00\n20260401,1,1,101.00\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    "write_factor_file",
    [
        pytest.param(_write_factor_file, id="skips_equal_factors_and_out_of_window_rows"),
        pytest.param(_write_same_date_factor_file, id="collapses_same_date_rows_like_lean_3000"),
    ],
)
def test_factor_file_reader_yields_the_one_dividend_leans_walk_finds(write_factor_file, tmp_path: Path) -> None:
    factor_file = tmp_path / "equity/usa/factor_files/syn.csv"
    write_factor_file(factor_file)
    dividends = dividends_from_factor_file(factor_file, "syn", date(2026, 3, 1), date(2026, 3, 31))
    # 2026-03-19 16:00 ET = 20:00 UTC; round_half_even(100 - 100·0.99, 2) = 1.00
    assert [(d.held_through_ms, str(d.per_share)) for d in dividends] == [(1773950400000, "1.00")]


def test_factor_tree_searches_roots_in_order(tmp_path: Path) -> None:
    empty_root = tmp_path / "raw-empty"
    stocked_root = tmp_path / "raw"
    _write_factor_file(stocked_root / "equity/usa/factor_files/syn.csv")
    assert dividends_from_factor_tree(empty_root, "syn", date(2026, 3, 1), date(2026, 3, 31)) == []
    dividends = dividends_from_factor_tree([empty_root, stocked_root], "syn", date(2026, 3, 1), date(2026, 3, 31))
    assert [str(d.per_share) for d in dividends] == ["1.00"]


def test_factor_tree_refuses_a_symbol_that_could_traverse(tmp_path: Path) -> None:
    """The CodeQL finding behind the fix: a request symbol reaches a path join here."""
    for hostile in ("../../etc/passwd", "..", "SPY/../../secrets"):
        with pytest.raises(SymbolValidationError, match="symbol"):
            dividends_from_factor_tree(tmp_path, hostile, date(2026, 3, 1), date(2026, 3, 31))


def test_factor_file_reader_pins_spy_march_2026_distribution() -> None:
    """The committed capture's own numbers: the dividend LEAN paid on the SPY cell (#2903)."""
    dividends = dividends_from_factor_file(_SPY_CAPTURE_FACTOR_FILE, "SPY", date(2026, 3, 1), date(2026, 4, 30))
    assert [(d.held_through_ms, str(d.per_share)) for d in dividends] == [(1773950400000, "1.80")]


def _deferred_fill_bars() -> list[TradeBar]:
    """Three bars on the cum-dividend Thursday, two on the ex-date Friday."""
    return [_minute(date(2026, 3, 19), 9, 30 + offset) for offset in range(3)] + [
        _minute(date(2026, 3, 20), 9, 30 + offset) for offset in range(2)
    ]


class _HookBuyThenHookSell(Strategy):
    """Buys from the per-minute hook off the first bar; sells from the hook off
    the cum-dividend session's last bar, so the sell pends overnight.

    Hook orders are placed before the consolidator phase, so under
    NEXT_BAR_OPEN the sell sits pending into the ex-date's first bar — the
    fill that must not erase entitlement earned through the cum-dividend
    close. (A consolidator-fired signal submits one bar later, after that
    bar's settlement, and cannot pin the ordering.)
    """

    def __init__(self) -> None:
        super().__init__()
        self._bars = 0

    def initialize(self) -> None:
        self.set_start_date(2026, 3, 19)
        self.set_end_date(2026, 3, 20)
        self.set_cash(10_000)
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), lambda bar: None)

    def on_minute_bar(self, bar: TradeBar) -> None:
        self._bars += 1
        if self._bars == 2:  # the first bar whose drain has a fired consolidated bar
            self.ctx.set_holdings("SPY", Decimal("1.0"))
        elif self._bars == 3:  # the cum-dividend session's last bar
            self.ctx.set_holdings("SPY", Decimal("0"))


class _HookBuyThirdBar(Strategy):
    """Buys from the per-minute hook off the cum-dividend session's last bar;
    NEXT_BAR_OPEN fills it on the ex-date's first bar."""

    def __init__(self) -> None:
        super().__init__()
        self._bars = 0

    def initialize(self) -> None:
        self.set_start_date(2026, 3, 19)
        self.set_end_date(2026, 3, 20)
        self.set_cash(10_000)
        symbol = self.ctx.add_equity("SPY")
        self.ctx.register_consolidator(symbol, timedelta(minutes=1), lambda bar: None)

    def on_minute_bar(self, bar: TradeBar) -> None:
        self._bars += 1
        if self._bars == 3:  # the cum-dividend session's last bar
            self.ctx.set_holdings("SPY", Decimal("1.0"))


def _run_deferred(strategy: type[Strategy], *, with_dividends: bool, tmp_path: Path):
    factor_file = tmp_path / "equity/usa/factor_files/spy.csv"
    _write_factor_file(factor_file)
    schedule = dividends_from_factor_file(factor_file, "SPY", date(2026, 3, 1), date(2026, 3, 31))
    return BacktestEngine(
        data_source=_SyntheticStream(_deferred_fill_bars()),
        dividends=schedule if with_dividends else None,
        execution_config=ExecutionConfig(fill_mode=FillMode.NEXT_BAR_OPEN),
    ).run(strategy())


def test_deferred_sell_filled_on_ex_date_still_earns_the_dividend(tmp_path: Path) -> None:
    """Settlement reads the position held through the cum-dividend close, not the
    one left after the ex-date bar's deferred fills (#3000 review)."""
    with_div = _run_deferred(_HookBuyThenHookSell, with_dividends=True, tmp_path=tmp_path)
    without = _run_deferred(_HookBuyThenHookSell, with_dividends=False, tmp_path=tmp_path)
    buy = with_div.order_events[0]
    held = Decimal(buy.fill_quantity)
    assert held > 0, "the strategy must be holding over the ex-date for the credit to be due"
    assert with_div.equity_curve[-1].cash - without.equity_curve[-1].cash == held * Decimal("1.00")


def test_deferred_buy_filled_on_ex_date_earns_no_dividend(tmp_path: Path) -> None:
    """A position first held on the ex-date itself never saw the cum-dividend close."""
    with_div = _run_deferred(_HookBuyThirdBar, with_dividends=True, tmp_path=tmp_path)
    without = _run_deferred(_HookBuyThirdBar, with_dividends=False, tmp_path=tmp_path)
    assert with_div.equity_curve[-1].cash == without.equity_curve[-1].cash


def test_position_held_over_ex_date_ends_with_leans_cash(tmp_path: Path) -> None:
    factor_file = tmp_path / "equity/usa/factor_files/spy.csv"
    _write_factor_file(factor_file)
    dividends = dividends_from_factor_file(factor_file, "SPY", date(2026, 3, 1), date(2026, 3, 31))
    result = BacktestEngine(data_source=_SyntheticStream(_two_session_bars()), dividends=dividends).run(
        _BuyFirstBarHold()
    )

    (fill,) = result.order_events
    held = Decimal(fill.fill_quantity)
    assert held > 0, "the strategy must be holding over the ex-date for the credit to be due"
    buy_cost = Decimal(fill.fill_quantity) * fill.fill_price + fill.fee
    expected_cash = Decimal(10_000) - buy_cost + held * Decimal("1.00")
    assert result.equity_curve[-1].cash == expected_cash

    # The credit lands on the ex-date's first bar, not during the cum-dividend
    # session: nothing trades between the two, so the overnight cash jump is
    # exactly the distribution.
    cash_by_day: dict[date, list[Decimal]] = {}
    for snapshot in result.equity_curve:
        cash_by_day.setdefault(datetime_at_ms(snapshot.timestamp_ms, tz=NY).date(), []).append(snapshot.cash)
    first_ex_date_cash = cash_by_day[date(2026, 3, 20)][0]
    last_cum_div_cash = cash_by_day[date(2026, 3, 19)][-1]
    assert first_ex_date_cash - last_cum_div_cash == held * Decimal("1.00")


def test_engine_without_dividends_pays_none() -> None:
    result = BacktestEngine(data_source=_SyntheticStream(_two_session_bars())).run(_BuyFirstBarHold())

    (fill,) = result.order_events
    assert result.equity_curve[-1].cash == Decimal(10_000) - (Decimal(fill.fill_quantity) * fill.fill_price + fill.fee)
