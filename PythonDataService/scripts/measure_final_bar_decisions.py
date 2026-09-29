"""Measure final-bar decisions in backtests of the live strategies (#2467).

A read-only research instrument. It answers three questions from data
already held on this machine, with no vendor fetch and no running service:

1. **Share.** For each strategy that has run on a clerk lane, how many
   backtest trades are decided on the session's final bar? The backtest fills
   such a decision at that bar's close; live decides it after the close.
2. **Models.** What does each candidate backtest model do to those trades:
   fill at the close (today), skip the ENTER and price the EXIT as the
   after-hours limit live now sends (#2440), fill at the next open, or skip
   both?
3. **Closing prints.** For the sessions whose live-observed IBKR minutes are
   held, how does the lake's final minute compare with what live saw, and do
   the lake's buckets reproduce the trace digests live recorded?

The session close always comes from the canonical calendar
(``app.lean_sidecar.trading_calendar``), so an early-close half-day's final
bar is its 12:59 minute. After-hours prices come from the lake's own
extended-hours minutes; the after-hours end comes from
``session_authority.order_session_state_at_ms`` (17:00 on an early-close day).
An after-hours fill is credited only to minutes that start after the close:
live decides the final bar just after it, so the minute that starts at the
close opens on prints that predate the order (``after_the_decision``).

Symbol, bucket width and warmup lookback come from the strategies measured
and their registered signal-program contracts, not from literals.

Lake reads skip the Postgres catalog-receipt check (the shared Postgres is
off-limits to research); every adjusted zip is still verified against its
sidecar ``file_sha256`` by ``AdjustmentVersionGuard``, the same guard the
engine reader applies. SQLite inputs must be COPIES; they are opened with
``mode=ro&immutable=1``.

Usage (from ``PythonDataService/``)::

    POLYGON_API_KEY="" DATA_PLANE_CONTROL_SECRET="" .venv/bin/python -m \\
        scripts.measure_final_bar_decisions \\
        --lake-root ../data-lake-volume/lake/polygon_split_adjusted \\
        --raw-lake-root ../data-lake-volume/lake/raw \\
        --start 2024-05-20 --end 2026-09-25 \\
        --ibkr-ledger <copy>/source_bars.sqlite3 ... \\
        --ibkr-jsonl 'artifacts/live_bars/*/1m/*.jsonl' \\
        --receipts-db <copy>/clerk.db \\
        --grouping-ledger <copy>/source_bars.sqlite3 ... \\
        --out final_bar_decisions.json
"""

from __future__ import annotations

import argparse
import asyncio
import glob
import json
import logging
import sqlite3
import statistics
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from app.broker.alpaca.broker import ALPACA_EXTENDED_HOURS_WINDOW
from app.broker.alpaca.clerk.sqlite.qualification_shadow_trace import (
    ShadowTraceDivergence,
    ShadowTraceDivergenceError,
    _live_adapter_traces,
    compare_canonical_traces,
)
from app.broker.alpaca.marketable_limit import marketable_limit_price
from app.broker.contract.models import OrderSide
from app.engine.data.lean_format import LeanMinuteDataReader
from app.engine.data.trade_bar import TradeBar
from app.engine.engine import BacktestEngine, BacktestResult
from app.engine.execution.execution_config import ExecutionConfig
from app.engine.execution.fill_model import FillModel
from app.engine.execution.order import FillMode
from app.engine.strategy.base import Strategy
from app.engine.strategy.registry import _STRATEGY_REGISTRY
from app.engine.strategy.signal_intent import SignalIntentKind
from app.engine.strategy.signal_program import Settlement, trace_root
from app.lean_sidecar.trading_calendar import (
    expected_sessions,
    is_early_close,
    is_trading_day,
    next_trading_day,
    session_close_ms_utc,
    session_open_ms_utc,
)
from app.marketdata.feed import warmup_window_start_ms
from app.services.run_replay_proof import engine_parity_over_bars
from app.services.session_authority import order_session_state_at_ms
from app.services.spec_strategy_runner import InMemoryDataReader
from app.utils.timestamps import ny_datetime

logger = logging.getLogger(__name__)

MINUTE_MS = 60_000
BPS = Decimal(10_000)
#: Exit allowances swept for the after-hours model. There is no built-in
#: default (#2440): the operator sets ``xh_exit_bps`` per account revision.
EXIT_ALLOWANCES_BPS: tuple[Decimal, ...] = (Decimal(0), Decimal(5), Decimal(10), Decimal(25), Decimal(50))

Model = Literal["close", "live", "next_open", "skip_all"]


@dataclass(frozen=True)
class LiveStrategy:
    """One strategy configuration observed on a clerk lane."""

    label: str
    strategy_key: str
    params: dict[str, Any]
    evidence: str


#: Every configuration found on a clerk lane (see the note for the evidence).
LIVE_STRATEGIES: tuple[LiveStrategy, ...] = (
    LiveStrategy(
        label="ema_sealed_default",
        strategy_key="ema_crossover_signal",
        params={"symbol": "SPY", "gap": 0.2, "gap_bps": 0.0, "rsi_min": 50.0, "rsi_max": 70.0},
        evidence="paper pt-ema-spy-0916/0918, paper-ema-spy-0923/0924; live live-ema-spy-0917/0923/0924",
    ),
    LiveStrategy(
        label="ema_q_variant",
        strategy_key="ema_crossover_signal",
        params={"symbol": "SPY", "gap": 0.0, "gap_bps": 0.0, "rsi_min": 30.0, "rsi_max": 70.0},
        evidence="paper pt-ema-q-0916",
    ),
    LiveStrategy(
        label="deployment_validation",
        strategy_key="deployment_validation",
        params={"symbol": "SPY", "trade_symbol": "SPY"},
        evidence="paper pt-dv-spy-0916, paper-dv-spy-0928; live Dry Run live-dry-dv-spy-0928",
    ),
)


def measured_symbol(strategies: Sequence[LiveStrategy]) -> str:
    """The one symbol every measured configuration trades; refuses a mixed set."""
    symbols = {live.params["symbol"] for live in strategies}
    if len(symbols) != 1:
        raise ValueError(f"the lake measurement covers one symbol; the live strategies trade {sorted(symbols)}")
    return symbols.pop()


def decision_timeframe_ms(strategy_key: str) -> int:
    """The strategy's decision bucket width, from its registered signal-program contract."""
    return _STRATEGY_REGISTRY[strategy_key].signal_program_contract.decision_timeframe_ms


def warmup_lookback_days(strategy_key: str) -> int:
    """The sealed warmup lookback a live run of this strategy starts with."""
    return _STRATEGY_REGISTRY[strategy_key].signal_program_contract.warmup_lookback_days


# ---------------------------------------------------------------------------
# Lake access
# ---------------------------------------------------------------------------
class LakeFileReader(LeanMinuteDataReader):
    """The engine's minute reader over lake files, without the catalog round trip.

    ``read_day`` differs from the parent only in reading the bytes directly
    instead of through ``read_committed_bytes`` (which needs the shared
    Postgres catalog). The sidecar digest check still runs on the exact bytes
    parsed.
    """

    def read_day(self, symbol: str, trading_date: date) -> list[TradeBar]:
        zip_path = self._zip_path(symbol, trading_date)
        if not zip_path.exists():
            return []
        payload = zip_path.read_bytes()
        self.adjustment_guard.verify(zip_path, payload, symbol)
        return self.parse_day_zip(payload, symbol, trading_date)


def session_date_of(timestamp_ms: int) -> date:
    return ny_datetime(timestamp_ms).date()


def is_final_bar_close(bar_close_ms: int) -> bool:
    """True when ``bar_close_ms`` is its session's regular close (canonical calendar)."""
    day = session_date_of(bar_close_ms)
    return is_trading_day(day) and bar_close_ms == session_close_ms_utc(day)


def after_the_decision(bar: TradeBar, close_ms: int) -> bool:
    """True when every print in ``bar`` postdates a final-bar decision taken at ``close_ms``.

    Live decides the final bar just after its close (``paper-ema-spy-0924``
    recorded 16:00:00.595), so the after-hours limit it sends exists only
    partway into the minute that starts at the close. That minute's open --
    the first print at or after the close -- is a price the order could not
    reach. The first minute a fill can be credited to is the first one that
    starts after the close.
    """
    return bar.start_ms > close_ms


def after_hours_end_ms(close_ms: int) -> int:
    """When an after-hours DAY limit sent at ``close_ms`` ends (20:00, or 17:00 on an early close)."""
    state = order_session_state_at_ms(now_ms=close_ms, extended_window=ALPACA_EXTENDED_HOURS_WINDOW)
    if state.phase != "POST" or state.next_transition_ms is None:
        raise ValueError(f"{close_ms} is not an after-hours instant: {state.phase}")
    return state.next_transition_ms


# ---------------------------------------------------------------------------
# Backtest runs under each final-bar model
# ---------------------------------------------------------------------------
@dataclass
class DiscardedDecision:
    bar_close_ms: int
    kind: str


#: The engine's private per-bar settlement hook this script overrides. Checked
#: at import so a rename in ``app/engine/engine.py`` fails loudly here instead
#: of silently measuring the unmodified engine.
_ENGINE_SETTLEMENT_HOOK = "_commit_staged_signal_program"
if not callable(getattr(BacktestEngine, _ENGINE_SETTLEMENT_HOOK, None)):
    raise ImportError(
        f"BacktestEngine.{_ENGINE_SETTLEMENT_HOOK} no longer exists; FinalBarPolicyEngine cannot apply its models"
    )


class FinalBarPolicyEngine(BacktestEngine):
    """The production engine, settling a final-bar decision the way a model says.

    ``discard`` names the intent kinds whose final-bar stage is settled
    DISCARD instead of COMMIT -- the same settlement the live runner applies
    when its liveness gate refuses an ENTER (``bot_trade_strategy``). A
    discarded EXIT keeps its countdown, so the strategy decides EXIT again on
    its next bar, exactly as a live rejected EXIT would be retried.
    ``discard_before_ms`` discards every decision before that instant, which
    is how a live run's warmup treats historical candidates.

    It overrides a private engine hook, so it is research scaffolding, not a
    model: delete it when #2467's backtest follow-up (#2607) makes the
    engine settle final-bar decisions itself. ``settlement_calls`` lets every
    caller prove the override actually ran (``require_settlement_hook``).
    """

    def __init__(
        self,
        *args: Any,
        discard: frozenset[SignalIntentKind] = frozenset(),
        discard_before_ms: int | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._discard = discard
        self._discard_before_ms = discard_before_ms
        self.discarded: list[DiscardedDecision] = []
        self.settlement_calls = 0

    def require_settlement_hook(self) -> None:
        """Refuse a run in which the engine never called the overridden hook."""
        if self.settlement_calls == 0:
            raise RuntimeError(
                f"BacktestEngine never called {_ENGINE_SETTLEMENT_HOOK}; the final-bar model was not applied"
            )

    def _commit_staged_signal_program(self, strategy: Strategy) -> None:  # type: ignore[override]
        self.settlement_calls += 1
        program = strategy.signal_program
        if program is None:
            return
        stage = program.session.active_stage
        if stage is not None and stage.intents:
            kind = stage.intents[0].kind
            close_ms = stage.bar.end_ms
            warmup = self._discard_before_ms is not None and close_ms < self._discard_before_ms
            if warmup or (kind in self._discard and is_final_bar_close(close_ms)):
                program.session.settle(Settlement.DISCARD)
                if not warmup:
                    self.discarded.append(DiscardedDecision(bar_close_ms=close_ms, kind=kind.value))
                return
        program.session.commit_if_staged()


def build_strategy(live: LiveStrategy, start: date, end: date) -> Strategy:
    registration = _STRATEGY_REGISTRY[live.strategy_key]
    strategy = registration.build(registration.param_schema.model_validate(live.params))
    original_initialize = strategy.initialize

    def initialize() -> None:
        original_initialize()
        strategy.set_start_date(start.year, start.month, start.day)
        strategy.set_end_date(end.year, end.month, end.day)

    strategy.initialize = initialize  # type: ignore[method-assign]
    return strategy


#: Which final-bar decisions each model settles DISCARD instead of filling.
_MODEL_DISCARDS: dict[Model, frozenset[SignalIntentKind]] = {
    "close": frozenset(),
    "next_open": frozenset(),
    "live": frozenset({SignalIntentKind.ENTER}),
    "skip_all": frozenset({SignalIntentKind.ENTER, SignalIntentKind.EXIT}),
}


def run_model(
    live: LiveStrategy,
    model: Model,
    *,
    reader: LeanMinuteDataReader,
    start: date,
    end: date,
) -> tuple[BacktestResult, Strategy, list[DiscardedDecision]]:
    strategy = build_strategy(live, start, end)
    # ``next_open`` is the LEAN-compatibility path the engine already has: a
    # signal bar emitted only after a session gap fills at the current
    # minute's open. Every other model keeps today's signal-bar-close fill.
    fill_model = FillModel(mode=FillMode.SIGNAL_BAR_CLOSE, fill_stale_signal_at_current_open=model == "next_open")
    engine = FinalBarPolicyEngine(
        data_source=reader, execution_config=ExecutionConfig(), fill_model=fill_model, discard=_MODEL_DISCARDS[model]
    )
    result = engine.run(strategy, retain_bars=False)
    engine.require_settlement_hook()
    return result, strategy, engine.discarded


@dataclass
class TradeRow:
    entry_ms: int
    exit_ms: int
    entry_price: str
    exit_price: str
    pnl_points: str
    final_bar_entry: bool
    final_bar_exit: bool
    synthetic_exit: bool


def trade_rows(strategy: Strategy) -> list[TradeRow]:
    rows: list[TradeRow] = []
    for trade in strategy.trade_log:
        rows.append(
            TradeRow(
                entry_ms=trade.entry_time_ms,
                exit_ms=trade.exit_time_ms,
                entry_price=str(trade.entry_price),
                exit_price=str(trade.exit_price),
                pnl_points=str(trade.exit_price - trade.entry_price),
                final_bar_entry=is_final_bar_close(trade.entry_time_ms),
                final_bar_exit=is_final_bar_close(trade.exit_time_ms),
                synthetic_exit=bool(trade.is_synthetic_exit),
            )
        )
    return rows


# ---------------------------------------------------------------------------
# After-hours and next-open prices for a final-bar decision
# ---------------------------------------------------------------------------
@dataclass
class CloseContext:
    """What the lake shows around one session close.

    ``close_minute_*`` is the minute that starts at the close (16:00, or
    13:00 on a half-day). Its open is the first print at or after the close,
    which predates a final-bar order, so it is recorded for comparison only.
    ``after_decision_*`` is the first minute that starts after the close:
    the earliest price a final-bar order can be credited with.
    """

    session_date: str
    early_close: bool
    close_ms: int
    close: str
    final_minute_volume: int
    close_minute_open: str | None
    close_minute_close: str | None
    close_minute_volume: int | None
    after_decision_start_ms: int | None
    after_decision_open: str | None
    after_decision_volume: int | None
    next_open_ms: int | None
    next_open: str | None
    after_hours_bars: int
    after_hours_high: str | None


class LakeSessions:
    """Per-session close context, read once per session from the extended lake file."""

    def __init__(self, reader: LeanMinuteDataReader, symbol: str) -> None:
        self._reader = reader
        self._symbol = symbol
        self._cache: dict[date, list[TradeBar]] = {}

    def bars(self, day: date) -> list[TradeBar]:
        if day not in self._cache:
            self._cache[day] = self._reader.read_day(self._symbol, day)
        return self._cache[day]

    def after_decision_bars(self, day: date) -> list[TradeBar]:
        """The after-hours minutes a final-bar order can fill in: after the decision, before the after-hours end."""
        close_ms = session_close_ms_utc(day)
        ah_end = after_hours_end_ms(close_ms)
        return [b for b in self.bars(day) if after_the_decision(b, close_ms) and b.end_ms <= ah_end]

    def context(self, day: date) -> CloseContext | None:
        close_ms = session_close_ms_utc(day)
        open_ms = session_open_ms_utc(day)
        day_bars = self.bars(day)
        rth = [b for b in day_bars if open_ms <= b.start_ms < close_ms]
        if not rth or rth[-1].end_ms != close_ms:
            return None
        close_minute = next((b for b in day_bars if b.start_ms == close_ms), None)
        after = self.after_decision_bars(day)
        following = next_trading_day(day)
        next_bars = [
            b
            for b in self.bars(following)
            if session_open_ms_utc(following) <= b.start_ms < session_close_ms_utc(following)
        ]
        return CloseContext(
            session_date=day.isoformat(),
            early_close=is_early_close(day),
            close_ms=close_ms,
            close=str(rth[-1].close),
            final_minute_volume=rth[-1].volume,
            close_minute_open=str(close_minute.open) if close_minute else None,
            close_minute_close=str(close_minute.close) if close_minute else None,
            close_minute_volume=close_minute.volume if close_minute else None,
            after_decision_start_ms=after[0].start_ms if after else None,
            after_decision_open=str(after[0].open) if after else None,
            after_decision_volume=after[0].volume if after else None,
            next_open_ms=next_bars[0].start_ms if next_bars else None,
            next_open=str(next_bars[0].open) if next_bars else None,
            after_hours_bars=len(after),
            after_hours_high=str(max(b.high for b in after)) if after else None,
        )

    def after_hours_sell_fill(
        self, day: date, *, anchor: Decimal, allowance_bps: Decimal
    ) -> tuple[Decimal | None, Decimal | None]:
        """(touch fill, marketable fill) for a sell limit sent just after the close.

        The limit is ``marketable_limit_price`` -- the live anchor formula.
        Only minutes that start after the decision are eligible
        (``after_the_decision``). *Touch* is ``fill_models.limit_touch_fill``'s
        rule: the first eligible minute whose high reaches the limit fills at
        the limit. *Marketable* credits the price improvement a limit below
        the market gets: the first eligible minute's open when it is at or
        above the limit, else the touch price. ``None`` when nothing reached
        the limit before the after-hours end.
        """
        limit = marketable_limit_price(side=OrderSide.SELL, anchor=anchor, allowance_bps=allowance_bps)
        after = self.after_decision_bars(day)
        if not any(bar.high >= limit for bar in after):
            return None, None
        marketable = after[0].open if after[0].open >= limit else limit
        return limit, marketable


def bps_of(delta: Decimal, base: Decimal) -> float:
    return float(delta / base * BPS)


def describe(values: Sequence[float]) -> dict[str, float | int]:
    if not values:
        return {"n": 0}
    ordered = sorted(values)
    absolute = sorted(abs(v) for v in values)

    def pct(data: list[float], q: float) -> float:
        index = min(len(data) - 1, max(0, round(q * (len(data) - 1))))
        return data[index]

    return {
        "n": len(values),
        "mean": statistics.fmean(values),
        "median": statistics.median(values),
        "stdev": statistics.pstdev(values),
        "p05": pct(ordered, 0.05),
        "p95": pct(ordered, 0.95),
        "mean_abs": statistics.fmean(absolute),
        "median_abs": statistics.median(absolute),
        "p95_abs": pct(absolute, 0.95),
        "max_abs": absolute[-1],
    }


# ---------------------------------------------------------------------------
# Live-observed IBKR minutes held on this machine
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HeldMinute:
    symbol: str
    start_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    live: bool  # observed within 60 s of its close (realtime), not history


def _is_live(fetched_at_ms: int, end_ms: int) -> bool:
    return 0 <= fetched_at_ms - end_ms < MINUTE_MS


def load_held_minutes(ledgers: Iterable[Path], jsonl_globs: Iterable[str]) -> dict[tuple[str, int], list[HeldMinute]]:
    """Every held one-minute IBKR bar, keyed by (symbol, start), one entry per copy."""
    held: dict[tuple[str, int], list[HeldMinute]] = {}

    def add(minute: HeldMinute) -> None:
        held.setdefault((minute.symbol, minute.start_ms), []).append(minute)

    for path in ledgers:
        conn = sqlite3.connect(f"file:{path}?mode=ro&immutable=1", uri=True)
        try:
            rows = conn.execute(
                "SELECT symbol, start_ms, end_ms, open, high, low, close, volume, fetched_at_ms "
                "FROM source_bars WHERE provider = 'ibkr' AND end_ms - start_ms = ?",
                (MINUTE_MS,),
            ).fetchall()
        finally:
            conn.close()
        for symbol, start_ms, end_ms, o, h, lo, c, v, fetched in rows:
            add(HeldMinute(symbol, start_ms, Decimal(o), Decimal(h), Decimal(lo), Decimal(c), int(v), _is_live(fetched, end_ms)))
    for pattern in jsonl_globs:
        for name in sorted(glob.glob(pattern)):
            with open(name, encoding="utf-8") as handle:
                for line in handle:
                    record = json.loads(line)
                    if record.get("action") != "append":
                        continue
                    bar = record["bar"]
                    if bar["end_ms"] - bar["start_ms"] != MINUTE_MS:
                        continue
                    add(
                        HeldMinute(
                            bar["symbol"],
                            bar["start_ms"],
                            Decimal(bar["open"]),
                            Decimal(bar["high"]),
                            Decimal(bar["low"]),
                            Decimal(bar["close"]),
                            int(bar["volume"]),
                            _is_live(bar["fetched_at_ms"], bar["end_ms"]),
                        )
                    )
    return held


def canonical_minute(copies: list[HeldMinute]) -> tuple[HeldMinute, bool]:
    """The live-observed copy when one exists, and whether all copies agree on OHLC."""
    chosen = next((m for m in copies if m.live), copies[0])
    agree = len({(m.open, m.high, m.low, m.close) for m in copies}) == 1
    return chosen, agree


@dataclass
class ClosingPrintRow:
    symbol: str
    session_date: str
    ibkr_live: bool
    copies_agree: bool
    lake_close: str
    ibkr_close: str
    close_diff: str
    bucket_ohlc_equal: bool
    close_minute_lake_open: str | None
    close_minute_ibkr_open: str | None


def compare_closing_prints(
    held: dict[tuple[str, int], list[HeldMinute]], lake_root: Path, *, bucket_ms: int
) -> tuple[list[ClosingPrintRow], dict[str, Any]]:
    """Lake vs held IBKR minutes, per symbol: the final minute, the final bucket, every RTH minute.

    ``bucket_ms`` is the decision bucket width of the strategy whose buckets
    are compared (the sealed EMA's, from its signal-program contract).
    """
    reader = LakeFileReader([lake_root], session="extended")
    rows: list[ClosingPrintRow] = []
    per_symbol: dict[str, dict[str, Any]] = {}
    days = sorted({(symbol, session_date_of(start)) for symbol, start in held})
    for symbol, day in days:
        if not is_trading_day(day):
            continue
        lake = {b.start_ms: b for b in reader.read_day(symbol, day)}
        if not lake:
            continue
        stats = per_symbol.setdefault(
            symbol,
            {"rth_close_bps": [], "bucket_closes": 0, "bucket_close_equal": 0, "final_close_bps": []},
        )
        open_ms, close_ms = session_open_ms_utc(day), session_close_ms_utc(day)
        for start in range(open_ms, close_ms, MINUTE_MS):
            copies = held.get((symbol, start))
            if copies is None or start not in lake:
                continue
            minute, _ = canonical_minute(copies)
            stats["rth_close_bps"].append(bps_of(minute.close - lake[start].close, lake[start].close))
            if (start + MINUTE_MS - open_ms) % bucket_ms == 0:
                stats["bucket_closes"] += 1
                stats["bucket_close_equal"] += minute.close == lake[start].close
        last = close_ms - MINUTE_MS
        copies = held.get((symbol, last))
        if copies is None or last not in lake:
            continue
        minute, agree = canonical_minute(copies)
        stats["final_close_bps"].append(bps_of(minute.close - lake[last].close, lake[last].close))
        bucket = range(close_ms - bucket_ms, close_ms, MINUTE_MS)
        ibkr_bucket = [held.get((symbol, s)) for s in bucket]
        bucket_equal = all(c is not None for c in ibkr_bucket) and all(s in lake for s in bucket)
        if bucket_equal:
            ibkr_bars = [canonical_minute(c)[0] for c in ibkr_bucket if c is not None]
            lake_bars = [lake[s] for s in bucket]
            bucket_equal = (
                ibkr_bars[0].open == lake_bars[0].open
                and max(b.high for b in ibkr_bars) == max(b.high for b in lake_bars)
                and min(b.low for b in ibkr_bars) == min(b.low for b in lake_bars)
                and ibkr_bars[-1].close == lake_bars[-1].close
            )
        close_minute_copies = held.get((symbol, close_ms))
        rows.append(
            ClosingPrintRow(
                symbol=symbol,
                session_date=day.isoformat(),
                ibkr_live=minute.live,
                copies_agree=agree,
                lake_close=str(lake[last].close),
                ibkr_close=str(minute.close),
                close_diff=str(minute.close - lake[last].close),
                bucket_ohlc_equal=bucket_equal,
                close_minute_lake_open=str(lake[close_ms].open) if close_ms in lake else None,
                close_minute_ibkr_open=str(canonical_minute(close_minute_copies)[0].open) if close_minute_copies else None,
            )
        )
    summary = {
        symbol: {
            "rth_minutes_compared": len(stats["rth_close_bps"]),
            "rth_minute_close_equal": sum(1 for v in stats["rth_close_bps"] if v == 0),
            "rth_minute_close_diff_bps": describe(stats["rth_close_bps"]),
            "bucket_closes_compared": stats["bucket_closes"],
            "bucket_close_equal": stats["bucket_close_equal"],
            "final_minutes_compared": len(stats["final_close_bps"]),
            "final_minute_close_equal": sum(1 for v in stats["final_close_bps"] if v == 0),
            "final_minute_close_diff_bps": describe(stats["final_close_bps"]),
        }
        for symbol, stats in per_symbol.items()
    }
    return rows, summary


# ---------------------------------------------------------------------------
# Bar grouping: the live runner's seam against the backtest's, on bars live saw
# ---------------------------------------------------------------------------
@dataclass
class GroupingParity:
    ledger: str
    strategy_key: str
    symbol: str
    rth_minutes: int
    first_session: str
    last_session: str
    production_check_compared: int
    production_check_divergence: str | None
    windowed_compared: int
    windowed_final_bar_traces: int
    windowed_divergence: str | None


def _held_rth_bars(ledger: Path, symbol: str) -> list[TradeBar]:
    conn = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
    try:
        rows = conn.execute(
            "SELECT start_ms, end_ms, open, high, low, close, volume FROM source_bars "
            "WHERE provider = 'ibkr' AND symbol = ? AND end_ms - start_ms = ? ORDER BY start_ms",
            (symbol, MINUTE_MS),
        ).fetchall()
    finally:
        conn.close()
    bars: list[TradeBar] = []
    for start, end, o, h, lo, c, v in rows:
        day = session_date_of(start)
        if is_trading_day(day) and session_open_ms_utc(day) <= start < session_close_ms_utc(day):
            bars.append(
                TradeBar(
                    symbol=symbol, start_ms=start, end_ms=end, open=Decimal(o), high=Decimal(h),
                    low=Decimal(lo), close=Decimal(c), volume=int(v),
                )
            )
    return bars


def _divergence_text(divergence: ShadowTraceDivergence | None) -> str | None:
    if divergence is None:
        return None
    return f"index {divergence.index} {divergence.field}: expected {divergence.expected}, observed {divergence.observed}"


def _holds_a_complete_session(bars: Sequence[TradeBar]) -> bool:
    """True when ``bars`` hold every regular-session minute of at least one session (canonical calendar)."""
    held_per_day: dict[date, int] = {}
    for bar in bars:
        day = session_date_of(bar.start_ms)
        held_per_day[day] = held_per_day.get(day, 0) + 1
    return any(
        count == (session_close_ms_utc(day) - session_open_ms_utc(day)) // MINUTE_MS
        for day, count in held_per_day.items()
    )


def _symbols_with_a_full_session(ledger: Path) -> list[str]:
    """Symbols whose ledger holds every regular-session IBKR minute of at least one session."""
    conn = sqlite3.connect(f"file:{ledger}?mode=ro&immutable=1", uri=True)
    try:
        symbols = [row[0] for row in conn.execute("SELECT DISTINCT symbol FROM source_bars WHERE provider = 'ibkr'")]
    finally:
        conn.close()
    return [symbol for symbol in sorted(symbols) if _holds_a_complete_session(_held_rth_bars(ledger, symbol))]


def grouping_parity(ledger: Path, live: LiveStrategy) -> GroupingParity:
    """Run the two decision seams over the RTH minutes one live ledger holds.

    *Production check*: ``run_replay_proof.engine_parity_over_bars``, exactly as
    a run's replay receipt calls it. *Windowed*: the same comparison with the
    backtest reference reading the bars' own dates instead of the strategy's
    built-in default window -- the live seam is
    ``qualification_shadow_trace._live_adapter_traces`` (``strategy_evaluations``
    over the bars, ``_drain_bar`` included), the reference is the production
    ``BacktestEngine``, and ``compare_canonical_traces`` judges them.
    """
    symbol = live.params["symbol"]
    bars = _held_rth_bars(ledger, symbol)
    first, last = session_date_of(bars[0].start_ms), session_date_of(bars[-1].start_ms)
    params = {k: v for k, v in live.params.items() if k != "symbol"}
    production = engine_parity_over_bars(live.strategy_key, symbol, params, bars)
    reference = build_strategy(live, first, last)
    BacktestEngine(InMemoryDataReader(list(bars))).run(reference, retain_bars=False)
    assert reference.signal_program is not None
    expected = tuple(reference.signal_program.session.traces)
    observed = asyncio.run(_live_adapter_traces(live.strategy_key, symbol, params, bars))
    windowed_divergence: ShadowTraceDivergence | None = None
    try:
        compare_canonical_traces(expected, observed)
    except ShadowTraceDivergenceError as error:
        windowed_divergence = error.divergence
    return GroupingParity(
        ledger=ledger.parent.name,
        strategy_key=live.strategy_key,
        symbol=symbol,
        rth_minutes=len(bars),
        first_session=first.isoformat(),
        last_session=last.isoformat(),
        production_check_compared=production.compared_count,
        production_check_divergence=_divergence_text(production.divergence),
        windowed_compared=len(observed),
        windowed_final_bar_traces=sum(is_final_bar_close(t.bar_close_ms) for t in observed),
        windowed_divergence=_divergence_text(windowed_divergence),
    )


# ---------------------------------------------------------------------------
# Live decision receipts reproduced from lake buckets
# ---------------------------------------------------------------------------
@dataclass
class ReceiptReplay:
    strategy_instance_id: str
    run_started_ms: int
    receipts: int
    reproduced: int
    same_decision: int
    first_mismatch_bar_close_ms: int | None
    final_bar_receipts: int
    final_bar_reproduced: int


#: A receipt outcome's staged candidate (``EvaluationTrace.staged_candidate``).
_RECEIPT_CANDIDATE: dict[str, str | None] = {"no_action": None, "enter_intent": "ENTER", "exit_intent": "EXIT"}
# Crash-candidate and quarantine receipts have no decision kind to compare; they never count as a match.
_UNMAPPED = "unmapped"


def replay_receipts(receipts_db: Path, lake_root: Path) -> list[ReceiptReplay]:
    """Recompute each EMA run's trace digests from lake buckets and compare with its receipts.

    The live warmup window is the strategy's sealed ``warmup_lookback_days``
    before the run's start (``marketdata.feed.warmup_window_start_ms``);
    historical candidates in it are discarded, as live warmup discards them.
    Equal digests mean the lake's bucket closes reproduce, digit for digit,
    every indicator value live computed from IBKR bars.
    """
    conn = sqlite3.connect(f"file:{receipts_db}?mode=ro&immutable=1", uri=True)
    try:
        configs = {
            sid: (key, json.loads(cfg))
            for sid, key, cfg in conn.execute("SELECT strategy_instance_id, strategy_key, config_json FROM bot_config")
        }
        runs = conn.execute(
            "SELECT strategy_instance_id, lifecycle_run_id, started_at_ms FROM runs ORDER BY started_at_ms"
        ).fetchall()
        receipts = conn.execute(
            "SELECT outcome, facts_json FROM decision_receipts ORDER BY strategy_instance_id, seq"
        ).fetchall()
    finally:
        conn.close()
    by_run: dict[str, list[tuple[int, str, str | None]]] = {}
    for outcome, facts_json in receipts:
        facts = json.loads(facts_json)
        digest = facts.get("trace_digest")
        close_ms = facts.get("decision_bar_close_ms")
        if digest is None or close_ms is None:
            continue
        by_run.setdefault(facts["run_id"], []).append((int(close_ms), digest, _RECEIPT_CANDIDATE.get(outcome, _UNMAPPED)))
    reader = LakeFileReader([lake_root], session="regular")
    replays: list[ReceiptReplay] = []
    for sid, run_id, started_ms in runs:
        key, config = configs[sid]
        run_receipts = by_run.get(run_id)
        if key != "ema_crossover_signal" or not run_receipts:
            continue
        warmup_start_ms = warmup_window_start_ms(warmup_lookback_days(key), now_ms=started_ms)
        last_ms = max(close for close, _, _ in run_receipts)
        live = LiveStrategy(label=sid, strategy_key=key, params={"symbol": config["symbol"], **config["strategy_params"]}, evidence="")
        strategy = build_strategy(live, session_date_of(warmup_start_ms), session_date_of(last_ms))
        engine = FinalBarPolicyEngine(data_source=_WindowReader(reader, warmup_start_ms), discard_before_ms=started_ms)
        engine.run(strategy, retain_bars=False)
        engine.require_settlement_hook()
        assert strategy.signal_program is not None
        traces = {t.bar_close_ms: t for t in strategy.signal_program.session.traces}
        reproduced = 0
        same_decision = 0
        final_total = 0
        final_reproduced = 0
        first_mismatch: int | None = None
        for close_ms, digest, candidate in sorted(run_receipts):
            trace = traces.get(close_ms)
            same = trace is not None and trace_root([trace]) == digest
            reproduced += same
            same_decision += candidate != _UNMAPPED and trace is not None and trace.staged_candidate == candidate
            if is_final_bar_close(close_ms):
                final_total += 1
                final_reproduced += same
            if not same and first_mismatch is None:
                first_mismatch = close_ms
        replays.append(
            ReceiptReplay(
                strategy_instance_id=sid,
                run_started_ms=started_ms,
                receipts=len(run_receipts),
                reproduced=reproduced,
                same_decision=same_decision,
                first_mismatch_bar_close_ms=first_mismatch,
                final_bar_receipts=final_total,
                final_bar_reproduced=final_reproduced,
            )
        )
    return replays


class _WindowReader:
    """A reader yielding only minutes that start at or after ``start_ms`` (the live warmup cut)."""

    def __init__(self, inner: LeanMinuteDataReader, start_ms: int) -> None:
        self._inner = inner
        self._start_ms = start_ms

    def iter_bars(self, symbol: str, start: date, end: date) -> Iterator[TradeBar]:
        for bar in self._inner.iter_bars(symbol, start, end):
            if bar.start_ms >= self._start_ms:
                yield bar


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
@dataclass
class StrategyMeasurement:
    label: str
    strategy_key: str
    params: dict[str, Any]
    evidence: str
    sessions: int
    early_close_sessions: int
    fills: int
    trades: int
    final_bar_enter: int
    final_bar_exit: int
    final_bar_trades: int
    final_bar_trade_share: float
    models: dict[str, dict[str, Any]] = field(default_factory=dict)
    final_bar_decisions: list[dict[str, Any]] = field(default_factory=list)


def _points(rows: Sequence[TradeRow]) -> Decimal:
    return sum((Decimal(r.pnl_points) for r in rows if not r.synthetic_exit), Decimal(0))


def measure_strategy(
    live: LiveStrategy,
    *,
    reader: LeanMinuteDataReader,
    sessions: LakeSessions,
    start: date,
    end: date,
) -> StrategyMeasurement:
    close_result, close_strategy, _ = run_model(live, "close", reader=reader, start=start, end=end)
    rows = [r for r in trade_rows(close_strategy) if not r.synthetic_exit]
    all_days = [d for d in reader.iter_dates(live.params["symbol"], start, end) if is_trading_day(d)]
    final_enter = sum(r.final_bar_entry for r in rows)
    final_exit = sum(r.final_bar_exit for r in rows)
    final_trades = sum(r.final_bar_entry or r.final_bar_exit for r in rows)
    measurement = StrategyMeasurement(
        label=live.label,
        strategy_key=live.strategy_key,
        params=live.params,
        evidence=live.evidence,
        sessions=len(all_days),
        early_close_sessions=sum(is_early_close(d) for d in all_days),
        fills=len(close_result.order_events),
        trades=len(rows),
        final_bar_enter=final_enter,
        final_bar_exit=final_exit,
        final_bar_trades=final_trades,
        final_bar_trade_share=final_trades / len(rows) if rows else 0.0,
    )
    logger.info(
        "measured final-bar share",
        extra={"action": "final_bar_share", "label": live.label, "trades": len(rows), "final": final_trades},
    )
    measurement.models["close"] = {"trades": len(rows), "points": str(_points(rows))}
    if final_trades == 0:
        return measurement

    for model in ("live", "next_open", "skip_all"):
        _, strategy, discarded = run_model(live, model, reader=reader, start=start, end=end)
        model_rows = [r for r in trade_rows(strategy) if not r.synthetic_exit]
        entry = {
            "trades": len(model_rows),
            "points": str(_points(model_rows)),
            "discarded": [asdict(d) for d in discarded],
        }
        if model == "live":
            entry["after_hours_exit"] = _after_hours_exit_models(model_rows, sessions)
        measurement.models[model] = entry

    for row in rows:
        if not (row.final_bar_entry or row.final_bar_exit):
            continue
        close_ms = row.exit_ms if row.final_bar_exit else row.entry_ms
        context = sessions.context(session_date_of(close_ms))
        measurement.final_bar_decisions.append(
            {
                "kind": "EXIT" if row.final_bar_exit else "ENTER",
                "trade": asdict(row),
                "close_context": asdict(context) if context is not None else None,
            }
        )
    return measurement


def _after_hours_exit_models(rows: Sequence[TradeRow], sessions: LakeSessions) -> dict[str, Any]:
    """Re-price each final-bar EXIT of the live-model run as the after-hours sell limit live sends."""
    out: dict[str, Any] = {}
    final_exits = [r for r in rows if r.final_bar_exit]
    for allowance in EXIT_ALLOWANCES_BPS:
        touch_points = Decimal(0)
        marketable_points = Decimal(0)
        unfilled = 0
        touch_bps: list[float] = []
        marketable_bps: list[float] = []
        for row in final_exits:
            close = Decimal(row.exit_price)
            touch, marketable = sessions.after_hours_sell_fill(
                session_date_of(row.exit_ms), anchor=close, allowance_bps=allowance
            )
            if touch is None or marketable is None:
                unfilled += 1
                continue
            touch_points += touch - close
            marketable_points += marketable - close
            touch_bps.append(bps_of(touch - close, close))
            marketable_bps.append(bps_of(marketable - close, close))
        out[str(allowance)] = {
            "final_bar_exits": len(final_exits),
            "unfilled": unfilled,
            "touch_points_vs_close": str(touch_points),
            "marketable_points_vs_close": str(marketable_points),
            "touch_bps_vs_close": describe(touch_bps),
            "marketable_bps_vs_close": describe(marketable_bps),
        }
    return out


def session_wide_distributions(sessions: LakeSessions, days: Sequence[date]) -> dict[str, Any]:
    """Every session in the window: prices after the close against the final minute's close.

    ``after_decision_open`` is the proxy a final-bar exit can be credited
    with. ``close_minute_open`` is the first print at or after the close; it
    predates a final-bar order and is reported only to show what counting it
    would overstate. Volumes are the medians of the final minute, the minute
    that starts at the close, and the first minute after the decision.
    """
    after_decision_open: list[float] = []
    close_minute_open: list[float] = []
    close_minute_close: list[float] = []
    next_open: list[float] = []
    final_volume: list[int] = []
    close_minute_volume: list[int] = []
    after_decision_volume: list[int] = []
    no_minute_after_decision = 0
    fill_rates: dict[str, dict[str, Any]] = {}
    contexts: list[tuple[date, CloseContext]] = []
    for day in days:
        context = sessions.context(day)
        if context is None:
            continue
        contexts.append((day, context))
        close = Decimal(context.close)
        final_volume.append(context.final_minute_volume)
        if context.after_decision_open is None or context.after_decision_volume is None:
            no_minute_after_decision += 1
        else:
            after_decision_open.append(bps_of(Decimal(context.after_decision_open) - close, close))
            after_decision_volume.append(context.after_decision_volume)
        if context.close_minute_open is not None and context.close_minute_close is not None:
            close_minute_open.append(bps_of(Decimal(context.close_minute_open) - close, close))
            close_minute_close.append(bps_of(Decimal(context.close_minute_close) - close, close))
        if context.close_minute_volume is not None:
            close_minute_volume.append(context.close_minute_volume)
        if context.next_open is not None:
            next_open.append(bps_of(Decimal(context.next_open) - close, close))
    for allowance in EXIT_ALLOWANCES_BPS:
        filled = 0
        marketable_bps: list[float] = []
        for day, context in contexts:
            close = Decimal(context.close)
            touch, marketable = sessions.after_hours_sell_fill(day, anchor=close, allowance_bps=allowance)
            if touch is not None and marketable is not None:
                filled += 1
                marketable_bps.append(bps_of(marketable - close, close))
        fill_rates[str(allowance)] = {
            "sessions": len(contexts),
            "sell_limit_filled": filled,
            "marketable_bps_vs_close": describe(marketable_bps),
        }
    return {
        "sessions": len(contexts),
        "sessions_without_a_minute_after_the_decision": no_minute_after_decision,
        "after_decision_open_bps_vs_close": describe(after_decision_open),
        "close_minute_close_bps_vs_close": describe(close_minute_close),
        "close_minute_open_bps_vs_close_predates_the_order": describe(close_minute_open),
        "next_open_bps_vs_close": describe(next_open),
        "median_volume": {
            "final_minute": statistics.median(final_volume) if final_volume else None,
            "close_minute": statistics.median(close_minute_volume) if close_minute_volume else None,
            "first_minute_after_the_decision": (
                statistics.median(after_decision_volume) if after_decision_volume else None
            ),
        },
        "after_hours_sell_limit": fill_rates,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--lake-root", type=Path, required=True, help="lake root holding equity/usa/minute (adjusted)")
    parser.add_argument(
        "--raw-lake-root", type=Path, default=None,
        help="raw (unadjusted) lake root: what IBKR minutes are compared against, since live bars are unadjusted",
    )
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--ibkr-ledger", type=Path, nargs="*", default=[], help="COPIES of source_bars.sqlite3")
    parser.add_argument("--ibkr-jsonl", nargs="*", default=[], help="globs of live_bars 1m jsonl files")
    parser.add_argument("--receipts-db", type=Path, default=None, help="a COPY of a clerk.db with decision receipts")
    parser.add_argument(
        "--grouping-ledger", type=Path, nargs="*", default=[],
        help="COPIES of source_bars.sqlite3 whose RTH minutes both decision seams replay",
    )
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    regular = LakeFileReader([args.lake_root], session="regular")
    extended = LakeFileReader([args.lake_root], session="extended")
    symbol = measured_symbol(LIVE_STRATEGIES)
    sessions = LakeSessions(extended, symbol)
    days = [d for d in regular.iter_dates(symbol, args.start, args.end) if is_trading_day(d)]
    expected = expected_sessions(args.start, args.end)
    missing = sorted(set(expected) - set(days))

    report: dict[str, Any] = {
        "generated_for": "issue #2467",
        "window": {"start": args.start.isoformat(), "end": args.end.isoformat(), "symbol": symbol},
        "lake_sessions": len(days),
        "calendar_sessions": len(expected),
        "missing_sessions": [d.isoformat() for d in missing],
        "early_close_sessions": [d.isoformat() for d in days if is_early_close(d)],
        "strategies": [
            asdict(measure_strategy(live, reader=regular, sessions=sessions, start=args.start, end=args.end))
            for live in LIVE_STRATEGIES
        ],
        # The last session has no next open inside the window; the context drops it.
        "session_wide": session_wide_distributions(sessions, days[:-1]),
    }
    if args.ibkr_ledger or args.ibkr_jsonl:
        held = load_held_minutes(args.ibkr_ledger, args.ibkr_jsonl)
        rows, summary = compare_closing_prints(
            held, args.raw_lake_root or args.lake_root, bucket_ms=decision_timeframe_ms(LIVE_STRATEGIES[0].strategy_key)
        )
        report["closing_prints"] = {"summary": summary, "rows": [asdict(r) for r in rows]}
    if args.receipts_db is not None:
        report["receipt_replay"] = [asdict(r) for r in replay_receipts(args.receipts_db, args.lake_root)]
    grouping: list[dict[str, Any]] = []
    for ledger in args.grouping_ledger:
        for held_symbol in _symbols_with_a_full_session(ledger):
            for live in (LIVE_STRATEGIES[0], LIVE_STRATEGIES[2]):
                bound = LiveStrategy(
                    label=live.label, strategy_key=live.strategy_key, evidence=live.evidence,
                    params={**live.params, "symbol": held_symbol}
                    | ({"trade_symbol": held_symbol} if "trade_symbol" in live.params else {}),
                )
                grouping.append(asdict(grouping_parity(ledger, bound)))
    if grouping:
        report["grouping_parity"] = grouping
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    logger.info("wrote final-bar measurement", extra={"action": "final_bar_report", "path": str(args.out)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "LIVE_STRATEGIES",
    "FinalBarPolicyEngine",
    "LakeFileReader",
    "is_final_bar_close",
    "main",
]
