"""The JSON form of the parts of a run's result that are stored as JSON (#3030).

Formula: none. Every ``Decimal`` is written as its text and read back from it; a rate is its numerator and denominator;
  an enum is its value; a contract is its OCC symbol; a "by reason" list is a list of ``[value, count]`` pairs.
Reference: ``app/options_backtest/statistics.py``, ``run.py``, ``results.py`` and ``session_result.py``, whose
  dataclasses these are the other way round.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_run_json.py, and the exact round trip of a whole run through the database in
  tests/options_backtest/test_saved_runs.py.

No float and no ISO string is anywhere in it. A date the result types carry beside a session's open is not stored: it
is the New York date of the open, and it is made again on the way back (``et_date_at_ms``). Reading raises ``KeyError``
or ``ValueError`` for JSON that is not what this module writes.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.options.contract import OptionContract
from app.options_backtest.market import PickRefusal
from app.options_backtest.results import Event, EventKind, ExitReason, SessionStatus, SkipReason
from app.options_backtest.run import SlippagePoint, SlippageTolerance
from app.options_backtest.session_result import PinnedDay, SessionPins
from app.options_backtest.statistics import DayRow, Drawdown, MonthRow, Ratio, RunSummary
from app.utils.session_anchors import et_date_at_ms

Json = dict[str, Any]


def _decimal(text: str) -> Decimal:
    return Decimal(text)


def _optional_decimal(text: str | None) -> Decimal | None:
    return None if text is None else Decimal(text)


def _optional_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _date_of(session_open_ms: int | None) -> date | None:
    return None if session_open_ms is None else et_date_at_ms(session_open_ms)


# ── Rates ────────────────────────────────────────────────────────────────


def _ratio_to_json(ratio: Ratio | None) -> Json | None:
    return None if ratio is None else {"numerator": str(ratio.numerator), "denominator": str(ratio.denominator)}


def _ratio_from_json(data: Json | None) -> Ratio | None:
    return None if data is None else Ratio(Decimal(data["numerator"]), Decimal(data["denominator"]))


# ── Counts by reason ─────────────────────────────────────────────────────


def _counts_to_json(counts: Sequence[tuple[StrEnum, int]]) -> list[list[Any]]:
    return [[reason.value, count] for reason, count in counts]


def _counts_from_json[E: StrEnum](
    members: Callable[[str], E], data: Sequence[Sequence[Any]]
) -> tuple[tuple[E, int], ...]:
    return tuple((members(reason), int(count)) for reason, count in data)


# ── The summary ──────────────────────────────────────────────────────────


def _drawdown_to_json(drawdown: Drawdown | None) -> Json | None:
    if drawdown is None:
        return None
    return {
        "peak_equity_usd": str(drawdown.peak_equity_usd),
        "trough_equity_usd": str(drawdown.trough_equity_usd),
        "fall_usd": str(drawdown.fall_usd),
        "fall_ratio": _ratio_to_json(drawdown.fall_ratio),
        "peak_session_open_ms": drawdown.peak_session_open_ms,
        "trough_session_open_ms": drawdown.trough_session_open_ms,
        "measured_at": drawdown.measured_at,
    }


def _drawdown_from_json(data: Json | None) -> Drawdown | None:
    if data is None:
        return None
    ratio = _ratio_from_json(data["fall_ratio"])
    assert ratio is not None  # a drawdown always has its ratio
    peak_ms, trough_ms = data["peak_session_open_ms"], data["trough_session_open_ms"]
    trough_date = _date_of(trough_ms)
    assert trough_date is not None  # a drawdown always has its trough
    return Drawdown(
        peak_equity_usd=Decimal(data["peak_equity_usd"]),
        trough_equity_usd=Decimal(data["trough_equity_usd"]),
        fall_usd=Decimal(data["fall_usd"]),
        fall_ratio=ratio,
        peak_date=_date_of(peak_ms),
        peak_session_open_ms=peak_ms,
        trough_date=trough_date,
        trough_session_open_ms=trough_ms,
        measured_at=data["measured_at"],
    )


def _day_to_json(day: DayRow) -> Json:
    return {
        "session_open_ms": day.session_open_ms,
        "status": day.status.value,
        "skip_reason": None if day.skip_reason is None else day.skip_reason.value,
        "trades": day.trades,
        "net_profit_usd": str(day.net_profit_usd),
        "fees_usd": str(day.fees_usd),
        "trades_won": day.trades_won,
        "trades_lost": day.trades_lost,
        "trades_break_even": day.trades_break_even,
    }


def _day_from_json(data: Json) -> DayRow:
    opened = data["session_open_ms"]
    trading_date = _date_of(opened)
    assert trading_date is not None
    return DayRow(
        trading_date=trading_date,
        session_open_ms=opened,
        status=SessionStatus(data["status"]),
        skip_reason=None if data["skip_reason"] is None else SkipReason(data["skip_reason"]),
        trades=data["trades"],
        net_profit_usd=Decimal(data["net_profit_usd"]),
        fees_usd=Decimal(data["fees_usd"]),
        trades_won=data["trades_won"],
        trades_lost=data["trades_lost"],
        trades_break_even=data["trades_break_even"],
    )


def _month_to_json(month: MonthRow) -> Json:
    return {
        "year": month.year,
        "month": month.month,
        "sessions": month.sessions,
        "sessions_traded": month.sessions_traded,
        "trades": month.trades,
        "net_profit_usd": str(month.net_profit_usd),
        "fees_usd": str(month.fees_usd),
        "trades_won": month.trades_won,
        "trades_lost": month.trades_lost,
        "trades_break_even": month.trades_break_even,
        "sessions_by_status": _counts_to_json(month.sessions_by_status),
    }


def _month_from_json(data: Json) -> MonthRow:
    return MonthRow(
        year=data["year"],
        month=data["month"],
        sessions=data["sessions"],
        sessions_traded=data["sessions_traded"],
        trades=data["trades"],
        net_profit_usd=Decimal(data["net_profit_usd"]),
        fees_usd=Decimal(data["fees_usd"]),
        trades_won=data["trades_won"],
        trades_lost=data["trades_lost"],
        trades_break_even=data["trades_break_even"],
        sessions_by_status=_counts_from_json(SessionStatus, data["sessions_by_status"]),
    )


def summary_to_json(summary: RunSummary) -> Json:
    return {
        "sessions_tested": summary.sessions_tested,
        "sessions_traded": summary.sessions_traded,
        "sessions_no_trade": summary.sessions_no_trade,
        "sessions_skipped": summary.sessions_skipped,
        "skipped_by_reason": _counts_to_json(summary.skipped_by_reason),
        "sessions_incomplete": summary.sessions_incomplete,
        "sessions_profitable": summary.sessions_profitable,
        "sessions_losing": summary.sessions_losing,
        "sessions_break_even": summary.sessions_break_even,
        "daily_win_rate": _ratio_to_json(summary.daily_win_rate),
        "trades": summary.trades,
        "trades_won": summary.trades_won,
        "trades_lost": summary.trades_lost,
        "trades_break_even": summary.trades_break_even,
        "trade_win_rate": _ratio_to_json(summary.trade_win_rate),
        "exits_by_reason": _counts_to_json(summary.exits_by_reason),
        "starting_cash_usd": str(summary.starting_cash_usd),
        "net_profit_usd": str(summary.net_profit_usd),
        "gross_profit_usd": str(summary.gross_profit_usd),
        "gross_loss_usd": str(summary.gross_loss_usd),
        "profit_factor": _ratio_to_json(summary.profit_factor),
        "total_fees_usd": str(summary.total_fees_usd),
        "slippage_paid_usd": str(summary.slippage_paid_usd),
        "ending_cash_usd": str(summary.ending_cash_usd),
        "drawdown_by_fraction": _drawdown_to_json(summary.drawdown_by_fraction),
        "drawdown_by_dollars": _drawdown_to_json(summary.drawdown_by_dollars),
        "sessions_without_momentum": summary.sessions_without_momentum,
        "entries_refused_for_cash": summary.entries_refused_for_cash,
        "re_entries_blocked_by_cutoff": summary.re_entries_blocked_by_cutoff,
        "entries_dropped": summary.entries_dropped,
        "picks_refused_by_reason": _counts_to_json(summary.picks_refused_by_reason),
        "legs_without_a_starting_price": summary.legs_without_a_starting_price,
        "re_entries_lost": summary.re_entries_lost,
        "by_day": [_day_to_json(day) for day in summary.by_day],
        "by_month": [_month_to_json(month) for month in summary.by_month],
    }


def summary_from_json(data: Json) -> RunSummary:
    return RunSummary(
        sessions_tested=data["sessions_tested"],
        sessions_traded=data["sessions_traded"],
        sessions_no_trade=data["sessions_no_trade"],
        sessions_skipped=data["sessions_skipped"],
        skipped_by_reason=_counts_from_json(SkipReason, data["skipped_by_reason"]),
        sessions_incomplete=data["sessions_incomplete"],
        sessions_profitable=data["sessions_profitable"],
        sessions_losing=data["sessions_losing"],
        sessions_break_even=data["sessions_break_even"],
        daily_win_rate=_ratio_from_json(data["daily_win_rate"]),
        trades=data["trades"],
        trades_won=data["trades_won"],
        trades_lost=data["trades_lost"],
        trades_break_even=data["trades_break_even"],
        trade_win_rate=_ratio_from_json(data["trade_win_rate"]),
        exits_by_reason=_counts_from_json(ExitReason, data["exits_by_reason"]),
        starting_cash_usd=Decimal(data["starting_cash_usd"]),
        net_profit_usd=Decimal(data["net_profit_usd"]),
        gross_profit_usd=Decimal(data["gross_profit_usd"]),
        gross_loss_usd=Decimal(data["gross_loss_usd"]),
        profit_factor=_ratio_from_json(data["profit_factor"]),
        total_fees_usd=Decimal(data["total_fees_usd"]),
        slippage_paid_usd=Decimal(data["slippage_paid_usd"]),
        ending_cash_usd=Decimal(data["ending_cash_usd"]),
        drawdown_by_fraction=_drawdown_from_json(data["drawdown_by_fraction"]),
        drawdown_by_dollars=_drawdown_from_json(data["drawdown_by_dollars"]),
        sessions_without_momentum=data["sessions_without_momentum"],
        entries_refused_for_cash=data["entries_refused_for_cash"],
        re_entries_blocked_by_cutoff=data["re_entries_blocked_by_cutoff"],
        entries_dropped=data["entries_dropped"],
        picks_refused_by_reason=_counts_from_json(PickRefusal, data["picks_refused_by_reason"]),
        legs_without_a_starting_price=data["legs_without_a_starting_price"],
        re_entries_lost=data["re_entries_lost"],
        by_day=tuple(_day_from_json(day) for day in data["by_day"]),
        by_month=tuple(_month_from_json(month) for month in data["by_month"]),
    )


# ── The slippage ladder and what it says ─────────────────────────────────


def slippage_to_json(points: Sequence[SlippagePoint]) -> list[Json]:
    return [
        {
            "slippage_percent": str(point.slippage_percent),
            "is_setups_own": point.is_setups_own,
            "net_profit_usd": str(point.net_profit_usd),
            "trades": point.trades,
            "traded_sessions": point.traded_sessions,
            "winning_sessions": point.winning_sessions,
        }
        for point in points
    ]


def slippage_from_json(data: Sequence[Json]) -> tuple[SlippagePoint, ...]:
    return tuple(
        SlippagePoint(
            slippage_percent=Decimal(point["slippage_percent"]),
            is_setups_own=point["is_setups_own"],
            net_profit_usd=Decimal(point["net_profit_usd"]),
            trades=point["trades"],
            traded_sessions=point["traded_sessions"],
            winning_sessions=point["winning_sessions"],
        )
        for point in data
    )


def tolerance_to_json(tolerance: SlippageTolerance) -> Json:
    return {
        "highest_profitable_percent": _optional_text(tolerance.highest_profitable_percent),
        "first_unprofitable_percent": _optional_text(tolerance.first_unprofitable_percent),
        "nothing_traded": tolerance.nothing_traded,
    }


def tolerance_from_json(data: Json) -> SlippageTolerance:
    return SlippageTolerance(
        highest_profitable_percent=_optional_decimal(data["highest_profitable_percent"]),
        first_unprofitable_percent=_optional_decimal(data["first_unprofitable_percent"]),
        nothing_traded=data["nothing_traded"],
    )


# ── A session's events and pins ──────────────────────────────────────────


def events_to_json(events: Sequence[Event]) -> list[Json]:
    return [
        {
            "kind": event.kind.value,
            "at_ms": event.at_ms,
            "leg_id": event.leg_id,
            "entry_number": event.entry_number,
            "contract": None if event.contract is None else event.contract.occ_symbol,
            "price": _optional_text(event.price),
            "reason": event.reason,
            "detail": event.detail,
        }
        for event in events
    ]


def events_from_json(data: Sequence[Json]) -> tuple[Event, ...]:
    return tuple(
        Event(
            kind=EventKind(event["kind"]),
            at_ms=event["at_ms"],
            leg_id=event["leg_id"],
            entry_number=event["entry_number"],
            contract=None if event["contract"] is None else OptionContract.from_occ_symbol(event["contract"]),
            price=_optional_decimal(event["price"]),
            reason=event["reason"],
            detail=event["detail"],
        )
        for event in data
    )


def option_days_to_json(pins: SessionPins) -> list[Json]:
    return [{"contract": day.contract.occ_symbol, "sha256": day.sha256} for day in pins.option_days]


def pins_from_json(
    underlying_sha256: str | None, listing_sha256: str | None, option_days: Sequence[Mapping[str, str]]
) -> SessionPins:
    return SessionPins(
        underlying_sha256=underlying_sha256,
        listing_sha256=listing_sha256,
        option_days=tuple(
            PinnedDay(contract=OptionContract.from_occ_symbol(day["contract"]), sha256=day["sha256"])
            for day in option_days
        ),
    )
