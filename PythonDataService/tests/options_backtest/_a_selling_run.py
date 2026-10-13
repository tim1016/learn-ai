"""A real run of a setup that sells: a credit put spread over two sessions, one closed and one left owing (#3106).

``a_selling_run`` replays a put 495 bought and a put 500 sold over Wednesday and Thursday, one contract each, by the
clock: in at minute 5, out at minute 60, no slippage and no fee. Every figure is worked by hand:

* Wednesday: the 495 is bought at 1.00 and the 500 sold at 2.20 at the open of minute 6, and at the open of minute 61
  the 500 is bought back at 1.50 and then the 495 sold at 0.60. Profit (0.60 - 1.00) x 100 + (2.20 - 1.50) x 100 = +30.00.
* Thursday: the 500 put's last bar is minute 60, closing at 2.40, so it cannot be bought back, and the 495, which may not
  leave it bare, is marked at its last close, 1.00. Cash that moved +120.00; marked value 100.00 - 240.00 = -140.00; profit
  (1.00 - 1.00) x 100 + (2.20 - 2.40) x 100 = -20.00.
"""

from __future__ import annotations

from app.options_backtest.results import ExitReason, Fill
from app.options_backtest.run import RunResult, run_sessions
from app.options_backtest.setup import OptionsBacktestSetup
from tests.options_backtest._market import leg, setup
from tests.options_backtest._run_data import THU, WED, Day, fake_data, flat, steady


def the_selling_setup() -> OptionsBacktestSetup:
    """A put 495 bought and a put 500 sold, one contract each, for the day itself: a credit put spread 5 wide."""
    return setup(
        {**leg("long", "put"), "strike": {"kind": "atm_offset", "steps": -1}},
        leg("short", "put", side="sell"),
    )


def a_spread_that_closes() -> Day:
    return Day(
        underlying=steady("500.00"),
        options={
            ("put", 495): {**flat("1.00", 5, 60), 61: ("0.60", "0.60")},
            ("put", 500): {**flat("2.20", 5, 60), 61: ("1.50", "1.50")},
        },
    )


def a_spread_whose_sold_leg_stops_trading() -> Day:
    return Day(
        underlying=steady("500.00"),
        options={
            ("put", 495): flat("1.00", 5, 61),
            ("put", 500): {**flat("2.20", 5, 59), 60: ("2.20", "2.40")},
        },
    )


def a_selling_run() -> RunResult:
    data = fake_data({WED: a_spread_that_closes(), THU: a_spread_whose_sold_leg_stops_trading()})
    return run_sessions(the_selling_setup(), [WED, THU], data)


def what_the_selling_run_reaches(result: RunResult) -> dict[str, bool]:
    trades = [trade for session in result.sessions for trade in session.outcome.trades]
    return {
        "a sale to open": any(trade.entry.side == "sell" for trade in trades),
        "a buy-back": any(isinstance(trade.exit, Fill) and trade.exit.side == "buy" for trade in trades),
        "a bought leg sold": any(isinstance(trade.exit, Fill) and trade.exit.side == "sell" for trade in trades),
        "a sold leg owed": any(trade.exit_reason is ExitReason.MARKED_NOT_BOUGHT_BACK for trade in trades),
        "a bought leg marked": any(trade.exit_reason is ExitReason.MARKED_NOT_SOLD for trade in trades),
    }
