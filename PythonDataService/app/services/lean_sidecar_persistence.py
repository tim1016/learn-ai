"""Persistence layer: normalize LEAN sidecar output into StrategyExecution rows.

Consumed by lean_sidecar_service.run_trusted_sample() at the tail of a successful
run. Reads the normalized result.json, pairs filled order events into round-trip
trades, synthesizes a mark-to-market exit for any half-open position, computes
aggregate KPIs, and writes one StrategyExecution row + N BacktestTrade rows.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from app.engine.results.equity_downsample import from_lean_curve
from app.models.responses import (
    LeanPortfolioStatsResponse,
    LeanRuntimeStatsResponse,
    LeanStatisticsResponse,
    LeanTradeStatsResponse,
)
from app.schemas.run_verdict import RunVerdictCleanliness
from app.services.engine_validation_analytics import (
    ValidationEquityPoint,
    ValidationTrade,
    build_validation_analytics_envelope,
    compute_engine_validation_analytics,
)
from app.services.run_verdict_service import compute_run_verdict, failed_run_verdict
from app.utils.timestamps import now_ms_utc

if TYPE_CHECKING:
    from app.lean_sidecar.manifest import RunManifest

logger = logging.getLogger(__name__)


@dataclass
class OpenLot:
    """A buy fill that has not yet been matched with a sell."""

    entry_ms_utc: int
    entry_price: float
    quantity: float
    fees: list[float] = field(default_factory=list)


@dataclass
class PairedTrade:
    """Round-trip trade reconstructed from a buy/sell event pair."""

    trade_number: int
    entry_ms_utc: int
    exit_ms_utc: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    signal_reason: str
    is_synthetic_exit: bool


def pair_order_events(
    events: Sequence[dict[str, Any]],
    signal_reason: str = "EMA crossover exit (5-bar time stop)",
) -> tuple[list[PairedTrade], OpenLot | None]:
    """Pair buy/sell filled events into round-trip trades.

    Returns (trades, leftover_open_lot). If the events end on an unmatched buy
    the caller is responsible for synthesizing an MTM exit.

    Raises NotImplementedError if a second buy arrives without an intervening
    sell (pyramiding). EMA crossover and buy-and-hold both have pyramiding=1
    so this branch is defensive.
    """
    fills = [e for e in events if e.get("status") == "filled"]
    open_lot: OpenLot | None = None
    trade_number = 0
    trades: list[PairedTrade] = []

    for fill in fills:
        direction = fill["direction"]
        ms_utc = int(fill["ms_utc"])
        price = float(fill["fill_price"])
        qty = float(fill["fill_quantity"])
        fee = float(fill.get("order_fee_amount") or 0.0)

        if direction == "buy":
            if open_lot is None:
                open_lot = OpenLot(
                    entry_ms_utc=ms_utc,
                    entry_price=price,
                    quantity=qty,
                    fees=[fee],
                )
            else:
                raise NotImplementedError("Pyramiding not supported in Phase 1; expected at most one open lot")
        elif direction == "sell":
            if open_lot is None:
                raise ValueError(
                    f"Sell fill at ms_utc={ms_utc} has no matching open lot — "
                    "templates today only support long-only round-trip trades"
                )
            trade_number += 1
            entry_fees = sum(open_lot.fees)
            pnl = (price - open_lot.entry_price) * open_lot.quantity - entry_fees - fee
            trades.append(
                PairedTrade(
                    trade_number=trade_number,
                    entry_ms_utc=open_lot.entry_ms_utc,
                    exit_ms_utc=ms_utc,
                    entry_price=open_lot.entry_price,
                    exit_price=price,
                    quantity=open_lot.quantity,
                    pnl=pnl,
                    signal_reason=signal_reason,
                    is_synthetic_exit=False,
                )
            )
            open_lot = None

    return trades, open_lot


def finalize_open_lot_as_synthetic(
    open_lot: OpenLot,
    equity_curve: Sequence[dict[str, Any]],
    starting_cash: float,
    trade_number: int,
) -> PairedTrade:
    """Synthesize an MTM exit at the last equity-curve point.

    Reconstructs exit price by reversing the portfolio-value identity:
        equity_value = cash_remaining + qty * exit_price
        cash_remaining = starting_cash - qty * entry_price - sum(fees)
    Solving:
        exit_price = (equity_value - starting_cash + qty * entry_price + sum(fees)) / qty
    """
    if not equity_curve:
        raise ValueError("equity_curve is empty — cannot synthesize MTM exit")

    last_point = equity_curve[-1]
    last_ms = int(last_point["ms_utc"])
    last_value = float(last_point["value"])
    entry_fees = sum(open_lot.fees)

    exit_price = (
        last_value - starting_cash + open_lot.entry_price * open_lot.quantity + entry_fees
    ) / open_lot.quantity

    pnl = (exit_price - open_lot.entry_price) * open_lot.quantity - entry_fees

    return PairedTrade(
        trade_number=trade_number,
        entry_ms_utc=open_lot.entry_ms_utc,
        exit_ms_utc=last_ms,
        entry_price=open_lot.entry_price,
        exit_price=exit_price,
        quantity=open_lot.quantity,
        pnl=pnl,
        signal_reason="EndOfAlgorithm:MTM (synthetic exit)",
        is_synthetic_exit=True,
    )


@dataclass
class AggregateKpis:
    total_trades: int
    winning_trades: int
    losing_trades: int
    total_pnl: float
    final_equity: float
    win_rate: float


def compute_aggregates(
    trades: Sequence[PairedTrade],
    starting_cash: float,
    total_fees: float,
) -> AggregateKpis:
    """Compute aggregate KPIs from a list of round-trip trades."""
    total_pnl = sum(t.pnl for t in trades)
    winning = sum(1 for t in trades if t.pnl > 0)
    losing = sum(1 for t in trades if t.pnl < 0)
    win_rate = winning / len(trades) if trades else 0.0
    # NOTE: t.pnl already nets out entry and exit fees (see pair_order_events:
    # pnl = (price - entry_price) * qty - entry_fees - exit_fee).
    # Do NOT subtract total_fees again here — that would double-count them.
    return AggregateKpis(
        total_trades=len(trades),
        winning_trades=winning,
        losing_trades=losing,
        total_pnl=total_pnl,
        final_equity=starting_cash + total_pnl,
        win_rate=win_rate,
    )


@dataclass
class NormalizedResult:
    """Schema-versioned view of normalized/result.json."""

    parser_version: str
    order_events: list[dict[str, Any]]
    equity_curve: list[dict[str, Any]]
    statistics: dict[str, Any]
    runtime_statistics: dict[str, Any]

    @classmethod
    def from_path(cls, path: Path) -> NormalizedResult:
        data = json.loads(path.read_text())
        return cls(
            parser_version=data.get("parser_version", "unknown"),
            order_events=data.get("order_events") or [],
            equity_curve=data.get("equity_curve") or [],
            statistics=data.get("statistics") or {},
            runtime_statistics=data.get("runtime_statistics") or {},
        )


def _algorithm_name_for_run(template: str | None, algorithm_source: str | None) -> str:
    """Pick the persisted algorithm_name based on which run type was requested.

    When the caller provides their own ``algorithm_source``, the run is labeled
    "user_provided" regardless of what the router filled in for ``template``
    (which defaults to "trusted_default" for custom submissions). When only a
    template is in play, its name is used verbatim.
    """
    if algorithm_source:
        return "user_provided"
    return template or "user_provided"


def _data_policy_json_from_manifest(
    manifest: RunManifest | Mapping[str, Any] | None,
) -> str | None:
    """Serialize a manifest's ``data_policy`` to canonical JSON.

    Accepts either a ``RunManifest`` dataclass (the in-process call-site
    has it as a typed object) or a manifest dict (the backfill CLI loads
    ``manifest.json`` from disk via ``json.loads``). Returns ``None`` if
    the manifest isn't available, in which case the .NET persistence
    layer preserves the row's ``DataPolicyJson`` as NULL.
    """
    if manifest is None:
        return None
    if hasattr(manifest, "data_policy") and not isinstance(manifest, Mapping):
        # RunManifest dataclass path
        return json.dumps(asdict(manifest.data_policy), sort_keys=True)
    if isinstance(manifest, Mapping):
        dp = manifest.get("data_policy")
        if dp is None:
            return None
        return json.dumps(dp, sort_keys=True)
    return None


def _brokerage_policy_from_manifest(
    manifest: RunManifest | Mapping[str, Any] | None,
) -> str | None:
    """Read the LEAN manifest's ``brokerage_policy`` enum.

    Returns ``None`` when the manifest is unavailable (legacy path / failed
    runs that never built one) — the .NET persistence layer preserves NULL
    rather than fabricating ``algorithm_default``, because LEAN runs may
    actually use Interactive Brokers (reconciliation template) and silently
    labeling them ``algorithm_default`` would corrupt compare-view gating.
    """
    if manifest is None:
        return None
    if hasattr(manifest, "brokerage_policy") and not isinstance(manifest, Mapping):
        return manifest.brokerage_policy
    if isinstance(manifest, Mapping):
        bp = manifest.get("brokerage_policy")
        return str(bp) if bp is not None else None
    return None


def _parse_pct(raw: Any) -> float:
    """Parse a LEAN STATISTICS percent string like ``"12.345%"`` → ``0.12345``.

    Returns 0.0 on missing / unparseable input. LEAN emits these as
    locale-free strings with a trailing ``%``; bare numbers (no ``%``)
    are interpreted as already-fractional (e.g. ``"0.05"`` → ``0.05``).
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return 0.0
    try:
        if s.endswith("%"):
            return float(s[:-1].replace(",", "").strip()) / 100.0
        return float(s.replace(",", "").strip())
    except ValueError:
        return 0.0


def _parse_dollar(raw: Any) -> float:
    """Parse a LEAN STATISTICS dollar string like ``"$95,343.16"`` → ``95343.16``.

    Returns 0.0 on missing / unparseable input. Handles a leading ``$``
    and embedded commas.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip()
    if not s:
        return 0.0
    s = s.replace("$", "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_ratio(raw: Any) -> float:
    """Parse a LEAN STATISTICS ratio string like ``"-1.072"`` → ``-1.072``.

    Returns 0.0 on missing / unparseable input. Tolerates a trailing
    ``%`` defensively in case LEAN evolves a field's formatting.
    """
    if raw is None:
        return 0.0
    if isinstance(raw, (int, float)):
        return float(raw)
    s = str(raw).strip().replace(",", "")
    if not s:
        return 0.0
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_int(raw: Any) -> int:
    """Parse an integer-shaped LEAN STATISTICS value. Returns 0 on failure."""
    if raw is None:
        return 0
    if isinstance(raw, (int, float)):
        return int(raw)
    s = str(raw).strip().replace(",", "")
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _format_ms_iso(ms_utc: int) -> str:
    """``int64 ms UTC`` → ``"YYYY-MM-DD HH:MM:SS"`` for LEAN trade-stat boundaries.

    This is a display-only string in a stat row consumed by the frontend.
    The canonical wire/storage timestamp on every order/equity payload
    stays ``int64 ms UTC`` per ``.claude/rules/numerical-rigor.md``.
    """
    return datetime.fromtimestamp(ms_utc / 1000.0, tz=UTC).strftime("%Y-%m-%d %H:%M:%S")


def _format_duration_seconds(total_seconds: float) -> str:
    """Format an average trade duration as ``"H:MM:SS"`` (matches LEAN TS.cs)."""
    if total_seconds <= 0:
        return "0:00:00"
    total = round(total_seconds)
    hours = total // 3600
    minutes = (total % 3600) // 60
    seconds = total % 60
    return f"{hours}:{minutes:02d}:{seconds:02d}"


# Mapping from LEAN STATISTICS:: key → (portfolio-stat attr, parser).
# Keep alphabetical-ish by attr name for grep. Keys are exactly the
# strings LEAN emits — case-sensitive.
_PORTFOLIO_STAT_MAPPING: list[tuple[str, str, Any]] = [
    ("Net Profit", "total_net_profit", _parse_pct),
    ("Compounding Annual Return", "compounding_annual_return", _parse_pct),
    ("Sharpe Ratio", "sharpe_ratio", _parse_ratio),
    ("Sortino Ratio", "sortino_ratio", _parse_ratio),
    ("Probabilistic Sharpe Ratio", "probabilistic_sharpe_ratio", _parse_pct),
    ("Drawdown", "drawdown", _parse_pct),
    ("Drawdown Recovery", "drawdown_recovery", _parse_int),
    ("Alpha", "alpha", _parse_ratio),
    ("Beta", "beta", _parse_ratio),
    ("Information Ratio", "information_ratio", _parse_ratio),
    ("Tracking Error", "tracking_error", _parse_pct),
    ("Treynor Ratio", "treynor_ratio", _parse_ratio),
    ("Annual Standard Deviation", "annual_standard_deviation", _parse_pct),
    ("Annual Variance", "annual_variance", _parse_ratio),
    ("Win Rate", "win_rate", _parse_pct),
    ("Loss Rate", "loss_rate", _parse_pct),
    ("Expectancy", "expectancy", _parse_ratio),
    ("Profit-Loss Ratio", "profit_loss_ratio", _parse_ratio),
    ("Portfolio Turnover", "portfolio_turnover", _parse_pct),
]


def _normalized_to_lean_statistics_response(
    normalized_statistics: Mapping[str, Any],
    paired_trades: Sequence[PairedTrade],
    starting_cash: float,
    total_fees: float,
) -> LeanStatisticsResponse:
    """Build the canonical ``LeanStatisticsResponse`` from a LEAN sidecar run.

    The shape (``{portfolio, trade, runtime}``) matches the engine
    path's emission so the frontend's ``LeanStatistics`` interface
    renders both runs identically. LEAN's raw ``STATISTICS::`` block
    is a flat string-keyed dict; this helper applies typed parsing
    and computes trade-level aggregates from the paired-trade list
    (the order-event reconstruction the persistence layer already
    performs).

    Fields LEAN does not surface in ``STATISTICS::`` (VaR 99/95,
    average_win_rate, average_loss_rate) default to 0.0 — they are
    populated only on the engine path which computes them directly
    from daily-perf series.

    NOTE: ``trade.sharpe_ratio`` / ``trade.sortino_ratio`` cannot be
    reproduced from sidecar paired trades because LEAN doesn't pass
    through the per-trade ``pnl_pct`` series. They stay 0 here; the
    engine path computes them directly from ``TradeRecord.pnl_pct``.
    """
    port = LeanPortfolioStatsResponse()
    trade = LeanTradeStatsResponse()
    runtime = LeanRuntimeStatsResponse()

    # ─── Portfolio statistics from LEAN's STATISTICS:: dict ─────────
    for key, attr, parser in _PORTFOLIO_STAT_MAPPING:
        if key in normalized_statistics:
            setattr(port, attr, parser(normalized_statistics[key]))

    port.start_equity = starting_cash
    port.end_equity = starting_cash + sum(t.pnl for t in paired_trades)

    # ─── Trade statistics computed from paired trades ───────────────
    n_trades = len(paired_trades)
    trade.total_number_of_trades = n_trades
    trade.total_fees = total_fees

    if paired_trades:
        pnls = [t.pnl for t in paired_trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        trade.number_of_winning_trades = len(wins)
        trade.number_of_losing_trades = len(losses)
        trade.total_profit_loss = sum(pnls)
        trade.total_profit = sum(wins) if wins else 0.0
        trade.total_loss = sum(losses) if losses else 0.0
        trade.largest_profit = max(wins) if wins else 0.0
        trade.largest_loss = min(losses) if losses else 0.0
        trade.average_profit_loss = trade.total_profit_loss / n_trades
        trade.average_profit = (trade.total_profit / len(wins)) if wins else 0.0
        trade.average_loss = (trade.total_loss / len(losses)) if losses else 0.0

        if trade.total_loss != 0:
            trade.profit_factor = abs(trade.total_profit / trade.total_loss)
        if port.drawdown > 1e-12:
            trade.profit_to_max_drawdown_ratio = port.total_net_profit / port.drawdown

        # Max consecutive wins/losses
        max_w = cur_w = max_l = cur_l = 0
        for p in pnls:
            if p > 0:
                cur_w += 1
                cur_l = 0
                max_w = max(max_w, cur_w)
            elif p < 0:
                cur_l += 1
                cur_w = 0
                max_l = max(max_l, cur_l)
            else:
                cur_w = 0
                cur_l = 0
        trade.max_consecutive_winning_trades = max_w
        trade.max_consecutive_losing_trades = max_l

        first = paired_trades[0]
        last = paired_trades[-1]
        trade.start_date_time = _format_ms_iso(first.entry_ms_utc)
        trade.end_date_time = _format_ms_iso(last.exit_ms_utc)

        all_durations = [(t.exit_ms_utc - t.entry_ms_utc) / 1000.0 for t in paired_trades]
        win_durations = [(t.exit_ms_utc - t.entry_ms_utc) / 1000.0 for t in paired_trades if t.pnl > 0]
        loss_durations = [(t.exit_ms_utc - t.entry_ms_utc) / 1000.0 for t in paired_trades if t.pnl < 0]
        if all_durations:
            trade.average_trade_duration = _format_duration_seconds(sum(all_durations) / len(all_durations))
        if win_durations:
            trade.average_winning_trade_duration = _format_duration_seconds(sum(win_durations) / len(win_durations))
        if loss_durations:
            trade.average_losing_trade_duration = _format_duration_seconds(sum(loss_durations) / len(loss_durations))

    # ─── Runtime statistics ─────────────────────────────────────────
    runtime.equity = port.end_equity
    runtime.fees = total_fees
    runtime.net_profit = port.end_equity - starting_cash
    runtime.total_return = port.total_net_profit
    runtime.total_orders = n_trades

    return LeanStatisticsResponse(portfolio=port, trade=trade, runtime=runtime)


def build_persist_payload(
    workspace_path: Path,
    run_id: str,
    starting_cash: float,
    symbol: str,
    algorithm_name: str,
    start_date_ms: int,
    end_date_ms: int,
    manifest: RunManifest | Mapping[str, Any] | None = None,
    cleanliness: RunVerdictCleanliness | Mapping[str, Any] | None = None,
    parity_group_id: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-serializable payload to POST to the .NET persist endpoint.

    This function is pure: it reads normalized/result.json from disk and computes
    aggregates + paired trades using pair_order_events, finalize_open_lot_as_synthetic,
    and compute_aggregates. It performs no DB writes and no HTTP calls.

    The .NET endpoint at POST /api/backtest-runs/persist-lean is responsible for
    persisting the payload into the StrategyExecution + BacktestTrade tables.

    If the workspace has no normalized/result.json (LEAN crashed before output),
    returns a "failed run" payload with TotalTrades=0 and a frozen Reject
    verdict. The .NET endpoint should still persist this so the failed run
    appears in the unified history.

    All timestamps in the returned payload are int64 ms UTC (canonical).

    PR B P1 fix (2026-05-20) — ``manifest`` (optional) lets the caller forward
    the LEAN ``RunManifest`` so the persist payload carries the true
    ``brokerage_policy`` and ``data_policy_json`` from the run. When omitted,
    both fields are ``None`` on the payload and the .NET service preserves
    NULL on the row (truthful "unknown") rather than fabricating
    ``algorithm_default`` — which would mislabel Interactive Brokers
    reconciliation runs.
    """
    result_path = workspace_path / "normalized" / "result.json"

    if not result_path.exists():
        logger.warning(
            "LEAN run %s has no normalized/result.json at %s; persisting zero-trade row",
            run_id,
            result_path,
        )
        return _failed_run_payload(
            run_id=run_id,
            starting_cash=starting_cash,
            symbol=symbol,
            algorithm_name=algorithm_name,
            start_date_ms=start_date_ms,
            end_date_ms=end_date_ms,
            workspace_path=workspace_path,
            error="No normalized/result.json — LEAN run did not produce output",
            manifest=manifest,
        )

    try:
        normalized = NormalizedResult.from_path(result_path)

        paired_trades, open_lot = pair_order_events(normalized.order_events)
        if open_lot is not None:
            paired_trades.append(
                finalize_open_lot_as_synthetic(
                    open_lot=open_lot,
                    equity_curve=normalized.equity_curve,
                    starting_cash=starting_cash,
                    trade_number=len(paired_trades) + 1,
                )
            )

        total_fees = sum(
            float(ev.get("order_fee_amount") or 0.0) for ev in normalized.order_events if ev.get("status") == "filled"
        )
        agg = compute_aggregates(
            trades=paired_trades,
            starting_cash=starting_cash,
            total_fees=total_fees,
        )
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError, NotImplementedError) as exc:
        logger.warning("Failed to normalize LEAN result for run %s: %s", run_id, exc)
        return _failed_run_payload(
            run_id=run_id,
            starting_cash=starting_cash,
            symbol=symbol,
            algorithm_name=algorithm_name,
            start_date_ms=start_date_ms,
            end_date_ms=end_date_ms,
            workspace_path=workspace_path,
            error=f"normalization_error: {type(exc).__name__}: {exc}",
            manifest=manifest,
        )

    lean_statistics = _normalized_to_lean_statistics_response(
        normalized_statistics=normalized.statistics,
        paired_trades=paired_trades,
        starting_cash=starting_cash,
        total_fees=total_fees,
    ).model_dump(mode="json")

    return {
        "lean_run_id": run_id,
        "source": "lean-sidecar",
        "strategy_name": algorithm_name,
        "symbol": symbol,
        "starting_cash": starting_cash,
        "start_date_ms": start_date_ms,
        "end_date_ms": end_date_ms,
        "total_trades": agg.total_trades,
        "winning_trades": agg.winning_trades,
        "losing_trades": agg.losing_trades,
        "total_pnl": agg.total_pnl,
        "total_fees": total_fees,
        "final_equity": agg.final_equity,
        "win_rate": agg.win_rate,
        "trades": [
            {
                "trade_number": t.trade_number,
                "entry_ms_utc": t.entry_ms_utc,
                "exit_ms_utc": t.exit_ms_utc,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "quantity": t.quantity,
                "pnl": t.pnl,
                "signal_reason": t.signal_reason,
                "is_synthetic_exit": t.is_synthetic_exit,
            }
            for t in paired_trades
        ],
        # The frontend's LeanStatistics interface expects the canonical
        # ``{portfolio, trade, runtime}`` shape emitted by the engine
        # path. Previously the sidecar wrote a flat dict of LEAN's
        # STATISTICS:: strings here, which crashed the LEAN-stats
        # dashboard on history-click (undefined ``.portfolio``). The
        # helper below applies typed parsing + paired-trade aggregation
        # so both engines persist the same shape.
        "lean_statistics": lean_statistics,
        # PR B P1 fix — forward the manifest's brokerage/data_policy so the
        # .NET row is the truthful record. ``commission_per_order`` stays at
        # 0 for LEAN: LEAN charges per-fill (captured in ``total_fees``),
        # not per-order, so there is no single configured-commission value
        # to surface here. The engine path is the one that actually
        # configures a commission per-order.
        "data_policy_json": _data_policy_json_from_manifest(manifest),
        "brokerage_policy": _brokerage_policy_from_manifest(manifest),
        "commission_per_order": 0.0,
        "equity_curve_json": json.dumps(
            from_lean_curve(
                normalized.equity_curve,
                trade_timestamps={t.entry_ms_utc for t in paired_trades} | {t.exit_ms_utc for t in paired_trades},
            )
        ),
        "validation_analytics_json": _validation_analytics_json(paired_trades, normalized.equity_curve),
        # Engine Lab parity — only successful runs carry the group id.
        # Failed runs never trigger verdict computation at persist time;
        # the job worker marks the group run_failed instead.
        "parity_group_id": parity_group_id,
    } | _run_verdict_fields(
        total_trades=agg.total_trades,
        total_pnl=agg.total_pnl,
        total_fees=total_fees,
        win_rate=agg.win_rate,
        lean_statistics=lean_statistics,
        cleanliness=cleanliness,
    )


def _trade_return(trade: PairedTrade) -> float:
    """Per-trade fractional return on deployed capital, net of fees.

    LEAN's paired trades carry dollar PnL; the validation analytics
    consume fractional returns (the engine path's ``pnl_pct``
    convention). Deployed capital is ``entry_price × quantity``.
    """
    deployed = trade.entry_price * trade.quantity
    if deployed == 0:
        return 0.0
    return trade.pnl / deployed


def _validation_analytics_json(
    paired_trades: Sequence[PairedTrade],
    equity_curve: Sequence[dict[str, Any]],
) -> str | None:
    """Frozen validation-analytics envelope for a LEAN run, or None.

    ``None`` (honest missing) when the analytics computation rejects the
    run's output — e.g. a non-strictly-increasing equity curve — mirroring
    the engine router's behavior for its own runs.
    """
    try:
        analytics = compute_engine_validation_analytics(
            trades=[
                ValidationTrade(
                    trade_number=t.trade_number,
                    entry_ms_utc=t.entry_ms_utc,
                    exit_ms_utc=t.exit_ms_utc,
                    pnl_pct=_trade_return(t),
                )
                for t in paired_trades
            ],
            equity_curve=[
                ValidationEquityPoint(timestamp_ms_utc=int(p["ms_utc"]), equity=float(p["value"]))
                for p in equity_curve
            ],
        )
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning("Validation analytics unavailable for LEAN run: %s", exc)
        return None
    return json.dumps(
        build_validation_analytics_envelope(
            analytics,
            engine="lean",
            computed_at_ms=now_ms_utc(),
        )
    )


def _failed_run_payload(
    *,
    run_id: str,
    starting_cash: float,
    symbol: str,
    algorithm_name: str,
    start_date_ms: int,
    end_date_ms: int,
    workspace_path: Path,
    error: str,
    manifest: RunManifest | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a zero-trade payload for a LEAN run that failed or produced no result."""
    failed_verdict = failed_run_verdict(error)
    return {
        "lean_run_id": run_id,
        "source": "lean-sidecar",
        "strategy_name": algorithm_name,
        "symbol": symbol,
        "starting_cash": starting_cash,
        "start_date_ms": start_date_ms,
        "end_date_ms": end_date_ms,
        "total_trades": 0,
        "winning_trades": 0,
        "losing_trades": 0,
        "total_pnl": 0.0,
        "total_fees": 0.0,
        "final_equity": starting_cash,
        "win_rate": 0.0,
        "trades": [],
        # Canonical shape with all-zero defaults — ``total_trades=0``
        # elsewhere in the payload already conveys the failure. The
        # frontend's defensive guard accepts this shape and renders an
        # empty dashboard rather than crashing on a flat error dict.
        # The diagnostic ``error`` string previously stashed here is
        # now only surfaced via ``logger.warning`` at the call site so
        # the .NET row's ``lean_statistics`` column always has the
        # canonical shape.
        "lean_statistics": LeanStatisticsResponse().model_dump(mode="json"),
        # Even on failed runs we forward the manifest fields if available;
        # the .NET service preserves NULL when the manifest is unavailable
        # rather than fabricating ``algorithm_default``.
        "data_policy_json": _data_policy_json_from_manifest(manifest),
        "brokerage_policy": _brokerage_policy_from_manifest(manifest),
        "commission_per_order": 0.0,
        "validation_analytics_json": None,
        "run_verdict_json": failed_verdict.model_dump_json(),
        "verdict_version": failed_verdict.verdict_version,
        "verdict_grade": failed_verdict.grade,
        "verdict_signal": failed_verdict.signal,
    }


def _run_verdict_fields(
    *,
    total_trades: int,
    total_pnl: float,
    total_fees: float,
    win_rate: float,
    lean_statistics: dict[str, Any],
    cleanliness: RunVerdictCleanliness | Mapping[str, Any] | None,
) -> dict[str, Any]:
    verdict = compute_run_verdict(
        {
            "statistics": {},
            "win_rate": win_rate,
            "total_trades": total_trades,
            "net_profit": total_pnl,
            "total_fees": total_fees,
            "lean_statistics": lean_statistics,
        },
        engine="lean",
        cleanliness=cleanliness,
    )
    return {
        "run_verdict_json": verdict.model_dump_json(),
        "verdict_version": verdict.verdict_version,
        "verdict_grade": verdict.grade,
        "verdict_signal": verdict.signal,
    }


async def persist_via_dotnet(
    payload: dict[str, Any],
    base_url: str,
    *,
    timeout_seconds: float = 30.0,
) -> int | None:
    """POST a LEAN run payload to the .NET backend for persistence.

    Returns the assigned StrategyExecution.Id on success, or None on any
    HTTP/network failure. Persistence failure must not abort the LEAN run —
    the artifacts on disk are the authoritative record and the backfill CLI
    (Task 5.1) can be used to retry later.
    """
    url = f"{base_url.rstrip('/')}/api/backtest-runs/persist-lean"
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(url, json=payload)
            response.raise_for_status()
            data = response.json()
            return int(data["strategy_execution_id"])
    except httpx.HTTPStatusError as exc:
        logger.warning(
            "persist-lean returned HTTP %s for run %s: %s",
            exc.response.status_code,
            payload.get("lean_run_id"),
            exc.response.text[:500],
        )
        return None
    except httpx.HTTPError as exc:
        logger.warning(
            "persist-lean transport error for run %s: %s",
            payload.get("lean_run_id"),
            exc,
        )
        return None
    except (KeyError, ValueError) as exc:
        logger.warning(
            "persist-lean response malformed for run %s: %s",
            payload.get("lean_run_id"),
            exc,
        )
        return None
