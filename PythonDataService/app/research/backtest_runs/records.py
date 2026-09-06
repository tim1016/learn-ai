"""The one row shape every backtest-run writer produces (PRD #1929, ADR 0058).

Three producers persist runs — the engine backtest path, the LEAN sidecar's
completed-run persistence, and the spec-strategy runner — and each used to
POST its own payload to a .NET endpoint that mapped it onto the study table.
They now hand the same snake_case *persist payload* (the shape
``lean_sidecar_persistence.build_persist_payload`` established) to
:func:`record_from_payload`, which validates it and returns the
:class:`BacktestRunRecord` the repository writes. Adding a column means one
converter and one INSERT, not three payload builders.

The validation rules are the ones the retired ``StudiesApi`` and
``BacktestRunPersistenceService`` enforced, kept exactly: a source must be
``engine`` or ``lean-sidecar``; a LEAN run carries its ``lean_run_id`` and an
engine run does not; ``requested_engine`` must agree with the source; trade
timestamps are positive ``int64 ms UTC``; a missing data policy is
synthesised the way the .NET layer synthesised it for pre-policy callers; a
missing brokerage policy is ``algorithm_default`` for the engine (which
models no brokerage) and stays unknown for LEAN.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from app.research.documentation.analytical_metric_catalog import metric_documentation_context_for_source

RunSource = Literal["engine", "lean-sidecar"]
RequestedEngine = Literal["python", "lean", "both"]

REQUESTED_ENGINES_BY_SOURCE: Mapping[str, tuple[str, ...]] = {
    "engine": ("python", "both"),
    "lean-sidecar": ("lean", "both"),
}
DEFAULT_FILL_MODE_BY_SOURCE: Mapping[str, str] = {"engine": "signal_bar_close", "lean-sidecar": "lean-sidecar"}


class RunPayloadError(ValueError):
    """The payload cannot become a run row. The run that produced it is unaffected."""


@dataclass(frozen=True, slots=True)
class TradeRecord:
    trade_number: int
    entry_ms: int
    exit_ms: int
    entry_price: float
    exit_price: float
    quantity: float
    pnl: float
    signal_reason: str
    is_synthetic_exit: bool


@dataclass(frozen=True, slots=True)
class BacktestRunRecord:
    """One run row plus its trades, ready for the repository."""

    source: RunSource
    requested_engine: RequestedEngine | None
    lean_run_id: str | None
    parity_group_id: str | None
    strategy_name: str
    symbol: str
    parameters: dict[str, Any]
    start_date: date
    end_date: date
    timespan: str
    fill_mode: str
    duration_ms: int
    total_trades: int
    winning_trades: int
    losing_trades: int
    win_rate: float
    total_pnl: float
    initial_cash: float
    final_equity: float
    total_fees: float
    max_drawdown: float
    sharpe_ratio: float | None
    sortino_ratio: float | None
    profit_factor: float | None
    commission_per_order: float
    brokerage_policy: str | None
    data_policy_json: str
    lean_statistics_json: str | None
    lean_analysis_json: str | None
    run_verdict_json: str | None
    verdict_version: int | None
    verdict_grade: str | None
    verdict_signal: str | None
    equity_curve_json: str | None
    validation_analytics_json: str | None
    insight_summary_json: str | None
    metric_documentation_json: str
    trades: tuple[TradeRecord, ...]


def record_from_payload(payload: Mapping[str, Any]) -> BacktestRunRecord:
    """Validate a persist payload and shape it into the row the repository writes.

    Raises :class:`RunPayloadError` for anything the retired .NET layer
    answered with a 400: the caller treats that as a persistence failure
    (run id ``None``), never as a failed backtest.
    """
    source = payload.get("source")
    if source not in REQUESTED_ENGINES_BY_SOURCE:
        raise RunPayloadError(f"Expected source in {{'lean-sidecar','engine'}}, got {source!r}")
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise RunPayloadError("symbol is required")
    trades = payload.get("trades")
    if trades is None:
        raise RunPayloadError("trades is required (use empty list, not null)")
    lean_run_id = payload.get("lean_run_id") or None
    if source == "lean-sidecar" and not lean_run_id:
        raise RunPayloadError("lean_run_id is required when source='lean-sidecar'")
    if source == "engine" and lean_run_id:
        raise RunPayloadError("lean_run_id must be null when source='engine'")
    requested_engine = payload.get("requested_engine")
    if requested_engine is not None and requested_engine not in REQUESTED_ENGINES_BY_SOURCE[source]:
        raise RunPayloadError("requested_engine must match source (engine: python|both; lean-sidecar: lean|both)")
    start_date, end_date = _iso_date(payload, "start_date"), _iso_date(payload, "end_date")
    if start_date > end_date:
        raise RunPayloadError(f"start_date ({start_date}) must be <= end_date ({end_date})")

    parameters = dict(payload.get("parameters") or {})
    # The symbol on the row is authoritative; the parameters echo it so a
    # consumer reading the configuration alone still sees the instrument.
    parameters["symbol"] = symbol
    lean_statistics = payload.get("lean_statistics")
    headline = _headline_metrics(payload, source=source, lean_statistics=lean_statistics)
    return BacktestRunRecord(
        source=source,
        requested_engine=requested_engine,
        lean_run_id=lean_run_id,
        parity_group_id=payload.get("parity_group_id") or None,
        strategy_name=str(payload.get("strategy_name") or ""),
        symbol=symbol,
        parameters=parameters,
        start_date=start_date,
        end_date=end_date,
        timespan=str(payload.get("timespan") or "minute"),
        fill_mode=str(payload.get("fill_mode") or DEFAULT_FILL_MODE_BY_SOURCE[source]),
        duration_ms=int(payload.get("duration_ms") or 0),
        total_trades=int(payload["total_trades"]),
        winning_trades=int(payload["winning_trades"]),
        losing_trades=int(payload["losing_trades"]),
        win_rate=float(payload["win_rate"]),
        total_pnl=float(payload["total_pnl"]),
        initial_cash=float(payload["starting_cash"]),
        final_equity=float(payload["final_equity"]),
        total_fees=float(payload["total_fees"]),
        max_drawdown=headline["max_drawdown"],
        sharpe_ratio=headline["sharpe_ratio"],
        sortino_ratio=headline["sortino_ratio"],
        profit_factor=headline["profit_factor"],
        commission_per_order=float(payload.get("commission_per_order") or 0.0),
        brokerage_policy=payload.get("brokerage_policy") or ("algorithm_default" if source == "engine" else None),
        data_policy_json=payload.get("data_policy_json") or synthesize_legacy_data_policy(symbol),
        lean_statistics_json=None if lean_statistics is None else json.dumps(lean_statistics, sort_keys=True),
        lean_analysis_json=_json_text(payload.get("lean_analysis_json")),
        run_verdict_json=_json_text(payload.get("run_verdict_json")),
        verdict_version=payload.get("verdict_version"),
        verdict_grade=payload.get("verdict_grade"),
        verdict_signal=payload.get("verdict_signal"),
        equity_curve_json=_json_text(payload.get("equity_curve_json")),
        validation_analytics_json=_json_text(payload.get("validation_analytics_json")),
        insight_summary_json=_json_text(payload.get("insight_summary_json")),
        metric_documentation_json=_metric_documentation(payload.get("metric_documentation_json"), source),
        trades=tuple(_trade(index, trade) for index, trade in enumerate(trades)),
    )


def synthesize_legacy_data_policy(symbol: str) -> str:
    """The block the .NET layer recorded for callers that sent no policy: what the engines actually did."""
    return json.dumps(
        {
            "source": "polygon",
            "symbol": symbol.upper(),
            "adjusted": True,
            "session": "regular",
            "input_bars": {"timespan": "minute", "multiplier": 1},
            "strategy_bars": {"timespan": "minute", "multiplier": 15},
            "timestamp_policy": "bar_close_ms_utc",
            "timezone": "America/New_York",
            "provider_kind": "live",
            "fixture_id": None,
            "fixture_sha256": None,
        }
    )


def _iso_date(payload: Mapping[str, Any], key: str) -> date:
    value = payload.get(key)
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise RunPayloadError(f"{key} must be a YYYY-MM-DD date, got {value!r}") from exc


def _json_text(value: Any) -> str | None:
    """JSON columns take text; a builder that hands over an object gets it serialised once, here."""
    if value is None or isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _metric_documentation(value: Any, source: str) -> str:
    """The recorded documentation context, or the producer catalogue's context for this source.

    The .NET reader inferred the context from the source whenever a row
    carried none; the writer records that same inference once so the read
    is a plain pass-through.
    """
    recorded = json.loads(value) if isinstance(value, str) else value
    if isinstance(recorded, list) and recorded:
        return json.dumps(recorded, sort_keys=True)
    return json.dumps(list(metric_documentation_context_for_source(source)), sort_keys=True)


def _headline_metrics(
    payload: Mapping[str, Any], *, source: str, lean_statistics: Mapping[str, Any] | None
) -> dict[str, float | None]:
    """Max drawdown, Sharpe, Sortino and profit factor for the row.

    The engine payload names them from its canonical statistics. A LEAN
    payload carries none, so — as the .NET detail read did — they come from
    the native ``lean_statistics`` (``portfolio.drawdown`` and friends), and
    an absent native value is an honest null rather than a fabricated zero.
    """
    portfolio = _mapping(lean_statistics, "portfolio")
    trade = _mapping(lean_statistics, "trade")
    native = source == "lean-sidecar"

    def pick(key: str, *, native_source: Mapping[str, Any], native_key: str) -> float | None:
        if key in payload:
            value = payload[key]
        elif native:
            value = native_source.get(native_key)
        else:
            value = None
        return None if value is None else float(value)

    return {
        "max_drawdown": pick("max_drawdown", native_source=portfolio, native_key="drawdown") or 0.0,
        "sharpe_ratio": pick("sharpe_ratio", native_source=portfolio, native_key="sharpe_ratio"),
        "sortino_ratio": pick("sortino_ratio", native_source=portfolio, native_key="sortino_ratio"),
        "profit_factor": pick("profit_factor", native_source=trade, native_key="profit_factor"),
    }


def _mapping(container: Mapping[str, Any] | None, key: str) -> Mapping[str, Any]:
    value = container.get(key) if isinstance(container, Mapping) else None
    return value if isinstance(value, Mapping) else {}


def _trade(index: int, trade: Mapping[str, Any]) -> TradeRecord:
    entry_ms, exit_ms = trade.get("entry_ms_utc"), trade.get("exit_ms_utc")
    if not isinstance(entry_ms, int) or entry_ms <= 0:
        raise RunPayloadError(
            f"trades[{index}].entry_ms_utc is required and must be a positive int64 ms UTC timestamp."
        )
    if not isinstance(exit_ms, int) or exit_ms <= 0:
        raise RunPayloadError(f"trades[{index}].exit_ms_utc is required and must be a positive int64 ms UTC timestamp.")
    quantity = trade.get("quantity")
    return TradeRecord(
        trade_number=int(trade.get("trade_number") or index + 1),
        entry_ms=entry_ms,
        exit_ms=exit_ms,
        entry_price=float(trade["entry_price"]),
        exit_price=float(trade["exit_price"]),
        quantity=1.0 if quantity is None else float(quantity),
        pnl=float(trade["pnl"]),
        signal_reason=str(trade.get("signal_reason") or ""),
        is_synthetic_exit=bool(trade.get("is_synthetic_exit", False)),
    )


def trades_as_compare_payload(trades: Sequence[TradeRecord]) -> list[dict[str, Any]]:
    """The trade dicts ``lean_sidecar_compare_service.reconcile_trade_lists`` reads, entry-ordered and renumbered."""
    ordered = sorted(trades, key=lambda trade: (trade.entry_ms, trade.trade_number))
    return [
        {
            "trade_number": index + 1,
            "entry_ms_utc": trade.entry_ms,
            "exit_ms_utc": trade.exit_ms,
            "entry_price": trade.entry_price,
            "exit_price": trade.exit_price,
            "quantity": trade.quantity,
            "pnl": trade.pnl,
            "signal_reason": trade.signal_reason,
            "is_synthetic_exit": trade.is_synthetic_exit,
        }
        for index, trade in enumerate(ordered)
    ]
