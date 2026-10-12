"""Two saved runs side by side: every setting that differs, and how they did on the sessions they share (#3030).

Formula: shared sessions = sessions whose open is in both runs; for each run, over the shared sessions only,
    net profit = Σ stored session profit, trades = Σ stored trade counts, winning sessions = #{profit > 0}
Reference: the stored per-session figures of a complete run (``saved_runs.SessionLine``); the setup as the run saved it.
Canonical implementation: this file.
Validated against: tests/options_backtest/test_comparison.py.

Settings are compared as the validated objects they are, never as text: ``0.3`` and ``0.3000`` are one slippage, and a
setting is a difference only when the two objects are not equal. The words for each side are made from the objects and
change nothing about what counts as a difference. Legs are matched by their id, not their place: the same legs in
another order are the same setup; a leg only one run has is one difference. The two runs are of the same underlying (the
caller refuses any other pair), so the underlying is not compared.

Totals are sums of the stored per-session figures, in exact decimals (``fills.EXACT``): a sum is never rounded and never
runs through a float. A session that one run has and the other lacks is counted, and is in neither run's totals.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from enum import StrEnum
from typing import Any

from app.options_backtest import fills
from app.options_backtest.describe import fee_policy_sentence
from app.options_backtest.saved_runs import RunHeader, RunSetupCannotBeRead, SessionLine
from app.options_backtest.setup import (
    MS_PER_MINUTE,
    ExpiryRule,
    LegSetup,
    Momentum,
    OptionsBacktestSetup,
    ReentryOnStop,
    SessionTime,
    StopLoss,
    StrikeRule,
)

NOT_SET = "not set"
NOT_IN_THIS_RUN = "not in this run"


class Setting(StrEnum):
    ENTRY_TIME = "entry_time"
    EXIT_TIME = "exit_time"
    NO_REENTRY_AFTER = "no_reentry_after"
    SQUARE_OFF = "square_off"
    # A leg only one of the runs has.
    LEG = "leg"
    LEG_RIGHT = "leg_right"
    LEG_CONTRACTS = "leg_contracts"
    LEG_EXPIRY = "leg_expiry"
    LEG_STRIKE = "leg_strike"
    LEG_STOP_LOSS = "leg_stop_loss"
    LEG_MOMENTUM = "leg_momentum"
    LEG_REENTRY = "leg_reentry"
    SLIPPAGE = "slippage"
    PRICE_STEP = "price_step"
    FEES = "fees"
    STARTING_CASH = "starting_cash"


@dataclass(frozen=True)
class SettingDifference:
    """One setting that differs, in words an owner can read, with each run's value in words."""

    setting: Setting
    # The leg it is about, for a setting of a leg.
    leg_id: str | None
    label: str
    a: str
    b: str


# ── Words ────────────────────────────────────────────────────────────────


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}{'' if number == 1 else 's'}"


def _plain(value: Decimal) -> str:
    """``value`` without a trailing zero or an exponent: 25.0000 reads 25."""
    return format(value.normalize(), "f")


def _points(value: Decimal) -> str:
    return f"{_plain(value)} {'point' if value == 1 else 'points'}"


def _dollars(value: Decimal) -> str:
    """Dollars and cents, with thousands grouped: $10,000.00. Every dollar figure of a setup has at most two decimals."""
    return f"${format(value.quantize(Decimal('0.01')), ',f')}"


def _time_words(time: SessionTime | None) -> str:
    if time is None:
        return NOT_SET
    minutes = time.offset_ms // MS_PER_MINUTE
    if minutes == 0:
        return f"at the {time.anchor}"
    return f"{_count(minutes, 'minute')} {'after' if time.anchor == 'open' else 'before'} the {time.anchor}"


def _expiry_words(rule: ExpiryRule) -> str:
    if rule.kind == "same_day":
        return "same day"
    if rule.kind == "this_week":
        return "the last trading day of the week"
    if rule.days == 0:
        return "the nearest listed, today included"
    return f"at least {_count(rule.days, 'calendar day')} out"


def _strike_words(rule: StrikeRule) -> str:
    if rule.kind == "atm":
        return "at the money"
    return f"{_count(abs(rule.steps), 'strike')} {'above' if rule.steps > 0 else 'below'} the money"


def _stop_words(rule: StopLoss | None) -> str:
    if rule is None:
        return NOT_SET
    amount = f"{_plain(rule.value)}%" if rule.kind == "percent" else _points(rule.value)
    return f"{amount} below the price paid"


def _momentum_words(rule: Momentum | None) -> str:
    if rule is None:
        return NOT_SET
    way = "up" if rule.kind.endswith("_up") else "down"
    amount = f"{_plain(rule.value)}%" if rule.kind.startswith("percent") else _points(rule.value)
    return f"{amount} {way} from where it stood at the entry time"


def _reentry_words(rule: ReentryOnStop | None) -> str:
    if rule is None:
        return NOT_SET
    when = "at once" if rule.kind == "at_once" else "on momentum"
    return f"{when}, up to {_count(rule.count, 'time')}"


def _leg_words(leg: LegSetup) -> str:
    parts = [
        f"{leg.right}, {_count(leg.contracts, 'contract')}, expiry {_expiry_words(leg.expiry)}, "
        f"strike {_strike_words(leg.strike)}"
    ]
    if leg.stop_loss is not None:
        parts.append(f"stop loss {_stop_words(leg.stop_loss)}")
    if leg.momentum is not None:
        parts.append(f"momentum {_momentum_words(leg.momentum)}")
    if leg.reentry_on_stop is not None:
        parts.append(f"re-entry on stop {_reentry_words(leg.reentry_on_stop)}")
    return "; ".join(parts)


# ── Settings ─────────────────────────────────────────────────────────────

# Each setting of a leg that is compared: its code, what it is called, how to read it off a leg and how to say it.
_LEG_SETTINGS: tuple[tuple[Setting, str, Callable[[LegSetup], Any], Callable[[Any], str]], ...] = (
    (Setting.LEG_RIGHT, "call or put", lambda leg: leg.right, str),
    (Setting.LEG_CONTRACTS, "contracts", lambda leg: leg.contracts, str),
    (Setting.LEG_EXPIRY, "expiry", lambda leg: leg.expiry, _expiry_words),
    (Setting.LEG_STRIKE, "strike", lambda leg: leg.strike, _strike_words),
    (Setting.LEG_STOP_LOSS, "stop loss", lambda leg: leg.stop_loss, _stop_words),
    (Setting.LEG_MOMENTUM, "momentum", lambda leg: leg.momentum, _momentum_words),
    (Setting.LEG_REENTRY, "re-entry on stop", lambda leg: leg.reentry_on_stop, _reentry_words),
)


def _leg_differences(a: LegSetup, b: LegSetup) -> list[SettingDifference]:
    found = []
    for setting, name, read, say in _LEG_SETTINGS:
        first, second = read(a), read(b)
        if first != second:
            found.append(SettingDifference(setting, a.leg_id, f"leg {a.leg_id}: {name}", say(first), say(second)))
    return found


def setting_differences(a: OptionsBacktestSetup, b: OptionsBacktestSetup) -> tuple[SettingDifference, ...]:
    """Every setting on which the two setups differ, in a fixed order.

    The window first (entry, exit, "no re-entry after", square-off), then the legs of ``a`` in its order, then the legs
    only ``b`` has, then the costs (slippage, price step, fees, starting cash).
    """
    found: list[SettingDifference] = []

    def compare(setting: Setting, label: str, first: object, second: object, say_a: str, say_b: str) -> None:
        if first != second:
            found.append(SettingDifference(setting, None, label, say_a, say_b))

    compare(Setting.ENTRY_TIME, "entry time", a.entry, b.entry, _time_words(a.entry), _time_words(b.entry))
    compare(Setting.EXIT_TIME, "exit time", a.exit, b.exit, _time_words(a.exit), _time_words(b.exit))
    compare(
        Setting.NO_REENTRY_AFTER,
        "no re-entry after",
        a.no_reentry_after,
        b.no_reentry_after,
        _time_words(a.no_reentry_after),
        _time_words(b.no_reentry_after),
    )
    compare(Setting.SQUARE_OFF, "square off", a.square_off, b.square_off, a.square_off, b.square_off)

    b_legs = {leg.leg_id: leg for leg in b.legs}
    a_ids = {leg.leg_id for leg in a.legs}
    for leg in a.legs:
        other = b_legs.get(leg.leg_id)
        if other is None:
            found.append(
                SettingDifference(Setting.LEG, leg.leg_id, f"leg {leg.leg_id}", _leg_words(leg), NOT_IN_THIS_RUN)
            )
        else:
            found.extend(_leg_differences(leg, other))
    for leg in b.legs:
        if leg.leg_id not in a_ids:
            found.append(
                SettingDifference(Setting.LEG, leg.leg_id, f"leg {leg.leg_id}", NOT_IN_THIS_RUN, _leg_words(leg))
            )

    compare(
        Setting.SLIPPAGE,
        "slippage",
        a.slippage_percent,
        b.slippage_percent,
        f"{_plain(a.slippage_percent)}%",
        f"{_plain(b.slippage_percent)}%",
    )
    compare(
        Setting.PRICE_STEP,
        "price step",
        a.price_step_usd,
        b.price_step_usd,
        f"${_plain(a.price_step_usd)}",
        f"${_plain(b.price_step_usd)}",
    )
    compare(Setting.FEES, "fees", a.fees, b.fees, fee_policy_sentence(a.fees), fee_policy_sentence(b.fees))
    compare(
        Setting.STARTING_CASH,
        "starting cash",
        a.starting_cash_usd,
        b.starting_cash_usd,
        _dollars(a.starting_cash_usd),
        _dollars(b.starting_cash_usd),
    )
    return tuple(found)


# ── Sessions ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SharedSession:
    """A session both runs tested, with what each run's stored rows say of it."""

    session_open_ms: int
    a: SessionLine
    b: SessionLine


@dataclass(frozen=True)
class RunTotals:
    """A run's figures over the shared sessions only."""

    net_profit_usd: Decimal
    trades: int
    # Shared sessions whose profit is above zero.
    winning_sessions: int


@dataclass(frozen=True)
class SessionOverlap:
    shared: tuple[SharedSession, ...]
    only_in_a: int
    only_in_b: int
    a: RunTotals
    b: RunTotals


def _totals(lines: Sequence[SessionLine]) -> RunTotals:
    with localcontext(fills.EXACT):
        net_profit = sum((line.profit_usd for line in lines), start=Decimal(0))
    return RunTotals(
        net_profit_usd=net_profit,
        trades=sum(line.trades for line in lines),
        winning_sessions=sum(1 for line in lines if line.profit_usd > 0),
    )


def overlap(a: Sequence[SessionLine], b: Sequence[SessionLine]) -> SessionOverlap:
    """The sessions both runs tested (by their opens, in ``a``'s order), how many each has that the other lacks, and each
    run's totals over the shared ones."""
    b_by_open = {line.session_open_ms: line for line in b}
    shared = tuple(
        SharedSession(line.session_open_ms, line, b_by_open[line.session_open_ms])
        for line in a
        if line.session_open_ms in b_by_open
    )
    return SessionOverlap(
        shared=shared,
        only_in_a=len(a) - len(shared),
        only_in_b=len(b) - len(shared),
        a=_totals([each.a for each in shared]),
        b=_totals([each.b for each in shared]),
    )


# ── The whole comparison ─────────────────────────────────────────────────


@dataclass(frozen=True)
class RangeOfRun:
    first_session_open_ms: int
    last_session_open_ms: int
    sessions_requested: int


@dataclass(frozen=True)
class Comparison:
    underlying: str
    settings: tuple[SettingDifference, ...]
    range_a: RangeOfRun
    range_b: RangeOfRun
    sessions: SessionOverlap

    @property
    def ranges_differ(self) -> bool:
        return self.range_a != self.range_b


def _range_of(header: RunHeader) -> RangeOfRun:
    record = header.record
    return RangeOfRun(record.first_session_open_ms, record.last_session_open_ms, record.sessions_requested)


def _setup_of(header: RunHeader) -> OptionsBacktestSetup:
    if header.setup is None:
        raise RunSetupCannotBeRead(header.record.id, header.setup_cannot_open_reason or "")
    return header.setup


def compare(a: RunHeader, a_lines: Sequence[SessionLine], b: RunHeader, b_lines: Sequence[SessionLine]) -> Comparison:
    """Two complete runs of the same underlying compared: settings, range and the sessions they share.

    ``RunSetupCannotBeRead`` when either run's stored setup no longer validates: its settings cannot be compared.
    """
    return Comparison(
        underlying=a.record.underlying,
        settings=setting_differences(_setup_of(a), _setup_of(b)),
        range_a=_range_of(a),
        range_b=_range_of(b),
        sessions=overlap(a_lines, b_lines),
    )
