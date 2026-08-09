"""Parse LEAN's output artifacts into typed Pydantic DTOs.

LEAN writes a handful of files into ``workspace/output/`` after every
backtest. This module turns them into a stable, typed surface so the
Phase 4 frontend renders consistent tables across LEAN versions and
Phase 5 reconciliation has a deterministic structure to diff against.

Authority: ``docs/architecture/lean-sidecar-lab.md`` §"Normalized output
parser". Three rules survive every LEAN version bump:

1. **All timestamps cross this boundary as int64 ms UTC.** LEAN writes
   unix-seconds (often as float); the parser converts immediately.
2. **Statistics are kept as strings**, not parsed into floats. LEAN's
   stats are version- and definition-sensitive (Sharpe annualization
   constant, sample vs population stdev, benchmark selection). String
   pass-through preserves fidelity; downstream consumers parse when
   they decide on a convention. See ADR §"Statistics parity scope".
3. **Unknown fields are tolerated** (Pydantic ``extra="allow"``) so a
   minor LEAN version that adds fields doesn't crash the parser; the
   pinned ``normalized_parser_version`` records exactly what schema
   the run was parsed under.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from app.lean_sidecar.manifest import sha256_file
from app.lean_sidecar.workspace import Workspace

# Bumped any time the parser's output schema changes in a non-additive
# way. The manifest records this; a different value invalidates
# reconciliation fixtures built against the prior parser version.
NORMALIZED_PARSER_VERSION = "lean-native-r1"


class NormalizedParserError(RuntimeError):
    """Raised when LEAN's output cannot be parsed.

    Either a required file is missing (run died before producing it)
    or the file is present but the structure does not match anything
    this parser knows how to handle. The launcher's
    ``result_classifier`` already surfaces the "LEAN crashed" case
    via ``analysis_failed`` / ``runtime_error``; this exception fires
    only when a *successful* run produced an unparseable artifact —
    that's a parser bug or a LEAN-schema drift worth surfacing.
    """


# ---------------------------------------------------------------------------
# Equity curve
# ---------------------------------------------------------------------------


class NormalizedEquityPoint(BaseModel):
    """One point on the strategy-equity curve.

    LEAN's series values are ``[unix_seconds, open, high, low, close]``
    for OHLC series; the equity curve has all four price components
    set to the same dollar value at each sample. We keep ``close`` as
    the canonical value and expose ``open/high/low`` so a future Phase 4
    chart can render candlestick equity curves without re-parsing.
    """

    model_config = ConfigDict(extra="forbid")

    ms_utc: int = Field(..., description="Timestamp as int64 ms UTC.")
    value: float = Field(..., description="Equity value (close).")
    open: float
    high: float
    low: float


# ---------------------------------------------------------------------------
# Order events
# ---------------------------------------------------------------------------


class NormalizedOrderEvent(BaseModel):
    """One LEAN order event (submission, fill, cancel, etc.).

    LEAN emits multiple events per logical order — typically one
    ``submitted`` and one ``filled`` per market order. Fees and fill
    price live on the ``filled`` event.

    ``populate_by_name=True`` so the model round-trips: it accepts
    LEAN's camelCase keys (``orderEventId``) AND its own snake_case
    names (``order_event_id``). Without that, re-reading the
    serialized ``result.json`` (which uses snake_case) would fail
    validation on the aliased fields.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    order_event_id: int = Field(..., alias="orderEventId")
    order_id: int = Field(..., alias="orderId")
    algorithm_id: str = Field(..., alias="algorithmId")
    symbol: str = Field(..., description="LEAN symbol token (e.g., 'SPY 2T').")
    symbol_value: str = Field(..., alias="symbolValue")
    ms_utc: int = Field(
        ...,
        description="Event time as int64 ms UTC (LEAN writes unix seconds).",
    )
    status: str
    direction: str
    quantity: float
    fill_price: float = Field(..., alias="fillPrice")
    fill_price_currency: str = Field(..., alias="fillPriceCurrency")
    fill_quantity: float = Field(..., alias="fillQuantity")
    is_assignment: bool = Field(..., alias="isAssignment")
    order_fee_amount: float | None = Field(default=None, alias="orderFeeAmount")
    order_fee_currency: str | None = Field(default=None, alias="orderFeeCurrency")
    message: str | None = None


class NormalizedArtifactReceipt(BaseModel):
    """Immutable identity and size metadata for one LEAN-authored artifact."""

    model_config = ConfigDict(extra="forbid")

    relative_path: str
    sha256: str
    size_bytes: int
    row_count: int | None = None


class NormalizedProfitLossPoint(BaseModel):
    """One entry from LEAN's timestamp-keyed ``profitLoss`` object."""

    model_config = ConfigDict(extra="forbid")

    ms_utc: int
    value: str


class NormalizedLeanAnalysis(BaseModel):
    """One complete LEAN-authored analysis finding.

    The analysis catalog is intentionally open-ended. LEAN can add new sample
    shapes without a learn-ai release; ``JsonValue`` preserves those shapes
    while the ingestion boundary converts timestamp fields to int64 ms UTC.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    issue: str
    sample: JsonValue = None
    solutions: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Top-level result
# ---------------------------------------------------------------------------


class NormalizedResult(BaseModel):
    """The parsed LEAN backtest result.

    The full ``<algorithm-id>.json`` is the native evidence source. LEAN's
    reduced ``-summary.json`` is retained separately and can never replace the
    full artifact silently. Numeric lexemes on the lossless native surfaces
    remain strings; typed display projections such as ``equity_curve`` are
    explicitly derived conveniences.
    """

    model_config = ConfigDict(extra="forbid")

    parser_version: str
    algorithm_id: str
    statistics: dict[str, str] = Field(
        ...,
        description=(
            "LEAN's statistics block as strings (preserves the "
            "version-specific formatting). Parse downstream if needed."
        ),
    )
    runtime_statistics: dict[str, str] = Field(default_factory=dict)
    equity_curve: list[NormalizedEquityPoint] = Field(default_factory=list)
    summary_statistics: dict[str, str] = Field(default_factory=dict)
    summary_runtime_statistics: dict[str, str] = Field(default_factory=dict)
    summary_equity_curve: list[NormalizedEquityPoint] = Field(default_factory=list)
    order_events: list[NormalizedOrderEvent] = Field(default_factory=list)
    algorithm_configuration: dict[str, JsonValue] = Field(default_factory=dict)
    algorithm_start_ms_utc: int | None = None
    algorithm_end_ms_utc: int | None = None
    state: dict[str, JsonValue] = Field(default_factory=dict)
    total_performance: dict[str, JsonValue] = Field(default_factory=dict)
    orders: dict[str, JsonValue] = Field(default_factory=dict)
    profit_loss: list[NormalizedProfitLossPoint] = Field(default_factory=list)
    rolling_window: dict[str, JsonValue] = Field(default_factory=dict)
    analysis: list[NormalizedLeanAnalysis] = Field(default_factory=list)
    full_charts: dict[str, JsonValue] = Field(default_factory=dict)
    summary_charts: dict[str, JsonValue] = Field(default_factory=dict)
    data_monitor: dict[str, JsonValue] = Field(default_factory=dict)
    artifacts: dict[str, NormalizedArtifactReceipt] = Field(default_factory=dict)
    # Derived: simple counts the frontend usually wants without re-walking
    # the equity curve or order events.
    total_order_events: int
    total_equity_points: int
    total_summary_equity_points: int = 0
    total_orders: int = 0
    total_closed_trades: int = 0
    total_rolling_windows: int = 0
    total_analyses: int = 0
    first_equity_ms_utc: int | None = Field(default=None)
    last_equity_ms_utc: int | None = Field(default=None)
    first_summary_equity_ms_utc: int | None = Field(default=None)
    last_summary_equity_ms_utc: int | None = Field(default=None)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _LeanArtifactPaths:
    """The output files we know how to parse, located by suffix.

    LEAN names files ``<algorithm-id>-<suffix>``; the ``algorithm-id``
    can differ from ``MyAlgorithm`` if the user overrides it, so we
    find files by suffix instead of by full name.
    """

    full_result: Path
    summary: Path
    order_events: Path | None
    data_monitor: Path | None
    observations: Path | None
    state_export: Path | None
    other_artifacts: tuple[Path, ...]
    algorithm_id: str


def _one_optional(paths: list[Path], *, label: str) -> Path | None:
    if len(paths) > 1:
        joined = ", ".join(path.name for path in paths)
        raise NormalizedParserError(f"multiple {label} artifacts found: {joined}")
    return paths[0] if paths else None


def _locate_lean_artifacts(output_dir: Path) -> _LeanArtifactPaths:
    """Bind every retained artifact to the summary's algorithm ID.

    The two filenames look like ``<algo>-summary.json`` and
    ``<algo>.json``. The full result is mandatory and is the native source of
    truth. Every algorithm-prefixed side artifact MUST match the same ID;
    otherwise stale output could contaminate a run.
    """
    if not output_dir.is_dir():
        raise NormalizedParserError(f"output directory missing: {output_dir}")

    summary_candidates = sorted(output_dir.glob("*-summary.json"))
    if not summary_candidates:
        raise NormalizedParserError(f"no *-summary.json under {output_dir}; LEAN did not write a backtest summary")
    if len(summary_candidates) > 1:
        joined = ", ".join(path.name for path in summary_candidates)
        raise NormalizedParserError(f"multiple summary artifacts found: {joined}")
    summary = summary_candidates[0]

    # Derive algorithm-id from the summary filename (the part before
    # ``-summary.json``).
    algorithm_id = summary.name[: -len("-summary.json")]

    full_result = output_dir / f"{algorithm_id}.json"
    if not full_result.exists():
        raise NormalizedParserError(
            f"matching full result missing for algorithm {algorithm_id!r}: {full_result}"
        )

    # Bind the order-events file to the same algorithm-id. A
    # ``<other-algo>-order-events.json`` left over from a prior run
    # in the same workspace must NOT be picked up — it would
    # masquerade as the current run's order stream.
    expected_events = output_dir / f"{algorithm_id}-order-events.json"
    order_events = expected_events if expected_events.exists() else None

    data_monitor = _one_optional(
        sorted(output_dir.glob("data-monitor-report-*.json")),
        label="data-monitor report",
    )
    storage_dir = output_dir / "storage"
    observations = storage_dir / "observations.csv"
    state_export = storage_dir / "state.csv"

    known = {
        full_result,
        summary,
        *(path for path in (order_events, data_monitor) if path is not None),
        *(path for path in (observations, state_export) if path.exists()),
    }
    other_artifacts = tuple(
        path
        for path in sorted(output_dir.rglob("*"))
        if path.is_file() and path not in known
    )

    return _LeanArtifactPaths(
        full_result=full_result,
        summary=summary,
        order_events=order_events,
        data_monitor=data_monitor,
        observations=observations if observations.exists() else None,
        state_export=state_export if state_export.exists() else None,
        other_artifacts=other_artifacts,
        algorithm_id=algorithm_id,
    )


def _unix_seconds_to_ms_utc(seconds: float | int) -> int:
    """LEAN writes timestamps as unix seconds (float); we want int64 ms.

    Truncation, not rounding — matches LEAN's own millisecond
    arithmetic which floors when converting. Equity-curve sub-second
    resolution is not meaningful (LEAN samples on bar boundaries).
    """
    return int(float(seconds) * 1000)


_TEMPORAL_FIELD_NAMES = frozenset(
    {
        "time",
        "createdTime",
        "lastFillTime",
        "lastUpdateTime",
        "canceledTime",
        "entryTime",
        "exitTime",
        "startDateTime",
        "endDateTime",
        "startDate",
        "endDate",
        "StartTime",
        "EndTime",
        "start",
        "end",
    }
)


def _reject_nonfinite_json(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _read_json(path: Path, *, label: str, lossless_numbers: bool) -> JsonValue:
    """Read one LEAN artifact while preserving floating-point lexemes.

    LEAN's full-precision objects frequently serialize decimals as JSON strings,
    but charts, orders and analysis samples contain JSON numbers. Parsing those
    through binary float would make the normalized artifact a lossy Oracle, so
    the lossless path keeps every non-integer numeric lexeme as text.
    """
    try:
        body = path.read_text(encoding="utf-8")
        if lossless_numbers:
            return json.loads(
                body,
                parse_float=str,
                parse_constant=_reject_nonfinite_json,
            )
        return json.loads(body, parse_constant=_reject_nonfinite_json)
    except (OSError, ValueError) as exc:
        raise NormalizedParserError(f"could not read {label} {path}: {exc}") from exc


def _object_field(container: dict[str, Any], key: str, *, label: str) -> dict[str, Any]:
    value = container.get(key)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise NormalizedParserError(f"{label} '{key}' must be a JSON object or null, got {type(value).__name__}")
    return value


def _parse_lean_instant(value: str, *, allow_naive_utc: bool) -> int:
    """Convert one LEAN result timestamp to canonical int64 ms UTC.

    Some LEAN analysis records serialize the first interval with ``Z`` and
    subsequent intervals without a suffix even though all values originate from
    the same UTC series. ``allow_naive_utc`` is restricted to that analysis
    surface and versioned by ``NORMALIZED_PARSER_VERSION``; every other native
    timestamp must carry an explicit offset.
    """
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise NormalizedParserError(f"invalid LEAN timestamp {value!r}") from exc
    if parsed.tzinfo is None:
        if not allow_naive_utc:
            raise NormalizedParserError(f"ambiguous LEAN timestamp has no UTC offset: {value!r}")
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.astimezone(UTC).timestamp() * 1000)


def _normalize_native_value(value: Any, *, allow_naive_utc: bool = False) -> JsonValue:
    if isinstance(value, dict):
        normalized: dict[str, JsonValue] = {}
        for key, child in value.items():
            if not isinstance(key, str):
                raise NormalizedParserError(f"LEAN JSON object key must be text, got {type(key).__name__}")
            if key in _TEMPORAL_FIELD_NAMES and isinstance(child, str) and "T" in child:
                normalized[key] = _parse_lean_instant(child, allow_naive_utc=allow_naive_utc)
            else:
                normalized[key] = _normalize_native_value(child, allow_naive_utc=allow_naive_utc)
        return normalized
    if isinstance(value, list):
        return [_normalize_native_value(child, allow_naive_utc=allow_naive_utc) for child in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise NormalizedParserError(f"unsupported LEAN JSON value type: {type(value).__name__}")


def _normalize_charts(charts: dict[str, Any]) -> dict[str, JsonValue]:
    """Normalize every chart timestamp to ms while retaining all series."""
    normalized = _normalize_native_value(charts)
    assert isinstance(normalized, dict)
    for chart_name, chart_value in normalized.items():
        if not isinstance(chart_value, dict):
            raise NormalizedParserError(f"chart {chart_name!r} must be a JSON object")
        series = chart_value.get("series")
        if series is None:
            continue
        if not isinstance(series, dict):
            raise NormalizedParserError(f"chart {chart_name!r} series must be a JSON object")
        for series_name, series_value in series.items():
            if not isinstance(series_value, dict):
                raise NormalizedParserError(
                    f"chart {chart_name!r} series {series_name!r} must be a JSON object"
                )
            values = series_value.get("values")
            if values is None:
                continue
            if not isinstance(values, list):
                raise NormalizedParserError(
                    f"chart {chart_name!r} series {series_name!r} values must be an array"
                )
            for row in values:
                if not isinstance(row, list) or not row:
                    continue
                try:
                    row[0] = _unix_seconds_to_ms_utc(row[0])
                except (TypeError, ValueError) as exc:
                    raise NormalizedParserError(
                        f"chart {chart_name!r} series {series_name!r} has invalid timestamp {row[0]!r}"
                    ) from exc
    return normalized


def _parse_profit_loss(raw: Any) -> list[NormalizedProfitLossPoint]:
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise NormalizedParserError(f"full result 'profitLoss' must be a JSON object or null, got {type(raw).__name__}")
    points: list[NormalizedProfitLossPoint] = []
    for instant, value in raw.items():
        if not isinstance(instant, str):
            raise NormalizedParserError("LEAN profitLoss timestamp key must be text")
        points.append(
            NormalizedProfitLossPoint(
                ms_utc=_parse_lean_instant(instant, allow_naive_utc=False),
                value=str(value),
            )
        )
    points.sort(key=lambda point: point.ms_utc)
    return points


def _parse_analysis(raw: Any) -> list[NormalizedLeanAnalysis]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise NormalizedParserError(f"full result 'analysis' must be an array or null, got {type(raw).__name__}")
    findings: list[NormalizedLeanAnalysis] = []
    for index, finding in enumerate(raw):
        if not isinstance(finding, dict):
            raise NormalizedParserError(f"analysis[{index}] must be a JSON object")
        normalized = _normalize_native_value(finding, allow_naive_utc=True)
        assert isinstance(normalized, dict)
        try:
            findings.append(NormalizedLeanAnalysis.model_validate(normalized))
        except Exception as exc:
            raise NormalizedParserError(f"analysis[{index}] failed to parse: {exc}") from exc
    return findings


def _line_count(path: Path) -> int:
    with path.open("rb") as handle:
        return sum(1 for _ in handle)


def _artifact_receipt(path: Path, output_dir: Path, *, count_rows: bool = False) -> NormalizedArtifactReceipt:
    return NormalizedArtifactReceipt(
        relative_path=path.relative_to(output_dir).as_posix(),
        sha256=sha256_file(path),
        size_bytes=path.stat().st_size,
        row_count=_line_count(path) if count_rows else None,
    )


def _artifact_receipts(paths: _LeanArtifactPaths, output_dir: Path) -> dict[str, NormalizedArtifactReceipt]:
    receipts = {
        "full_result": _artifact_receipt(paths.full_result, output_dir),
        "summary_result": _artifact_receipt(paths.summary, output_dir),
    }
    optional = (
        ("order_events", paths.order_events, False),
        ("data_monitor", paths.data_monitor, False),
        ("observations", paths.observations, True),
        ("state_export", paths.state_export, True),
    )
    for key, path, count_rows in optional:
        if path is not None:
            receipts[key] = _artifact_receipt(path, output_dir, count_rows=count_rows)
    for path in paths.other_artifacts:
        receipts[f"other:{path.relative_to(output_dir).as_posix()}"] = _artifact_receipt(path, output_dir)
    return receipts


def _parse_equity_curve(summary: dict) -> list[NormalizedEquityPoint]:
    """Extract the Strategy Equity series, or return [] if missing."""
    charts = summary.get("charts", {})
    strategy_equity = charts.get("Strategy Equity", {})
    series = strategy_equity.get("series", {})
    equity_series = series.get("Equity", {})
    values = equity_series.get("values", [])
    points: list[NormalizedEquityPoint] = []
    for row in values:
        # LEAN OHLC series rows are [ts, open, high, low, close].
        if not isinstance(row, list) or len(row) < 5:
            continue
        ts, o, h, low, close = row[0], row[1], row[2], row[3], row[4]
        points.append(
            NormalizedEquityPoint(
                ms_utc=_unix_seconds_to_ms_utc(ts),
                value=float(close),
                open=float(o),
                high=float(h),
                low=float(low),
            )
        )
    return points


def _parse_order_events(events_raw: list[dict]) -> list[NormalizedOrderEvent]:
    """Build typed order events from LEAN's order-events list.

    LEAN's ``time`` field is unix seconds (float); we substitute the
    int64-ms-UTC equivalent before validation so the model field is
    typed correctly. The original ``time`` is discarded — ``ms_utc``
    is the canonical timestamp.
    """
    out: list[NormalizedOrderEvent] = []
    for raw in events_raw:
        if not isinstance(raw, dict):
            continue
        event = dict(raw)
        if "time" in event:
            event["ms_utc"] = _unix_seconds_to_ms_utc(event.pop("time"))
        try:
            out.append(NormalizedOrderEvent.model_validate(event))
        except Exception as e:
            raise NormalizedParserError(f"order event failed to parse: {e}; raw={raw!r}") from e
    return out


def parse_workspace(workspace: Workspace) -> NormalizedResult:
    """Parse the LEAN output artifacts under ``workspace`` into a
    :class:`NormalizedResult`.

    This is the only public function in this module. The launcher
    service calls it after a successful run and writes the result to
    ``workspace.normalized_dir / "result.json"``.
    """
    paths = _locate_lean_artifacts(workspace.output_dir)

    summary_raw = _read_json(paths.summary, label="summary", lossless_numbers=True)
    if not isinstance(summary_raw, dict):
        raise NormalizedParserError(f"summary file {paths.summary} is not a JSON object")
    full_raw = _read_json(paths.full_result, label="full result", lossless_numbers=True)
    if not isinstance(full_raw, dict):
        raise NormalizedParserError(f"full result file {paths.full_result} is not a JSON object")

    raw_stats = _object_field(full_raw, "statistics", label="full result")
    raw_runtime = _object_field(full_raw, "runtimeStatistics", label="full result")
    raw_summary_stats = _object_field(summary_raw, "statistics", label="summary")
    raw_summary_runtime = _object_field(summary_raw, "runtimeStatistics", label="summary")
    statistics = {str(key): str(value) for key, value in raw_stats.items()}
    runtime_statistics = {str(key): str(value) for key, value in raw_runtime.items()}
    summary_statistics = {str(key): str(value) for key, value in raw_summary_stats.items()}
    summary_runtime_statistics = {str(key): str(value) for key, value in raw_summary_runtime.items()}

    raw_full_charts = _object_field(full_raw, "charts", label="full result")
    raw_summary_charts = _object_field(summary_raw, "charts", label="summary")
    equity_curve = _parse_equity_curve(full_raw)
    summary_equity_curve = _parse_equity_curve(summary_raw)
    full_charts = _normalize_charts(raw_full_charts)
    summary_charts = _normalize_charts(raw_summary_charts)

    raw_algorithm_configuration = _object_field(
        full_raw,
        "algorithmConfiguration",
        label="full result",
    )
    algorithm_configuration = _normalize_native_value(raw_algorithm_configuration)
    assert isinstance(algorithm_configuration, dict)
    algorithm_start_ms_utc = algorithm_configuration.get("startDate")
    algorithm_end_ms_utc = algorithm_configuration.get("endDate")
    if algorithm_start_ms_utc is not None and not isinstance(algorithm_start_ms_utc, int):
        raise NormalizedParserError("algorithmConfiguration.startDate did not normalize to int64 ms UTC")
    if algorithm_end_ms_utc is not None and not isinstance(algorithm_end_ms_utc, int):
        raise NormalizedParserError("algorithmConfiguration.endDate did not normalize to int64 ms UTC")

    raw_state = _object_field(full_raw, "state", label="full result")
    state = _normalize_native_value(raw_state)
    assert isinstance(state, dict)
    raw_total_performance = _object_field(full_raw, "totalPerformance", label="full result")
    total_performance = _normalize_native_value(raw_total_performance)
    assert isinstance(total_performance, dict)
    raw_orders = _object_field(full_raw, "orders", label="full result")
    orders = _normalize_native_value(raw_orders)
    assert isinstance(orders, dict)
    raw_rolling_window = _object_field(full_raw, "rollingWindow", label="full result")
    rolling_window = _normalize_native_value(raw_rolling_window)
    assert isinstance(rolling_window, dict)
    profit_loss = _parse_profit_loss(full_raw.get("profitLoss"))
    analysis = _parse_analysis(full_raw.get("analysis"))

    order_events: list[NormalizedOrderEvent] = []
    if paths.order_events is not None:
        events_raw = _read_json(paths.order_events, label="order events", lossless_numbers=False)
        if not isinstance(events_raw, list):
            raise NormalizedParserError(f"order-events file {paths.order_events} is not a JSON array")
        order_events = _parse_order_events(events_raw)

    data_monitor: dict[str, JsonValue] = {}
    if paths.data_monitor is not None:
        data_monitor_raw = _read_json(paths.data_monitor, label="data-monitor report", lossless_numbers=True)
        if not isinstance(data_monitor_raw, dict):
            raise NormalizedParserError(f"data-monitor report {paths.data_monitor} is not a JSON object")
        normalized_monitor = _normalize_native_value(data_monitor_raw)
        assert isinstance(normalized_monitor, dict)
        data_monitor = normalized_monitor

    closed_trades = total_performance.get("closedTrades")
    if closed_trades is None:
        total_closed_trades = 0
    elif isinstance(closed_trades, list):
        total_closed_trades = len(closed_trades)
    else:
        raise NormalizedParserError("totalPerformance.closedTrades must be an array or null")

    return NormalizedResult(
        parser_version=NORMALIZED_PARSER_VERSION,
        algorithm_id=paths.algorithm_id,
        statistics=statistics,
        runtime_statistics=runtime_statistics,
        equity_curve=equity_curve,
        summary_statistics=summary_statistics,
        summary_runtime_statistics=summary_runtime_statistics,
        summary_equity_curve=summary_equity_curve,
        order_events=order_events,
        algorithm_configuration=algorithm_configuration,
        algorithm_start_ms_utc=algorithm_start_ms_utc,
        algorithm_end_ms_utc=algorithm_end_ms_utc,
        state=state,
        total_performance=total_performance,
        orders=orders,
        profit_loss=profit_loss,
        rolling_window=rolling_window,
        analysis=analysis,
        full_charts=full_charts,
        summary_charts=summary_charts,
        data_monitor=data_monitor,
        artifacts=_artifact_receipts(paths, workspace.output_dir),
        total_order_events=len(order_events),
        total_equity_points=len(equity_curve),
        total_summary_equity_points=len(summary_equity_curve),
        total_orders=len(orders),
        total_closed_trades=total_closed_trades,
        total_rolling_windows=len(rolling_window),
        total_analyses=len(analysis),
        first_equity_ms_utc=equity_curve[0].ms_utc if equity_curve else None,
        last_equity_ms_utc=equity_curve[-1].ms_utc if equity_curve else None,
        first_summary_equity_ms_utc=(summary_equity_curve[0].ms_utc if summary_equity_curve else None),
        last_summary_equity_ms_utc=(summary_equity_curve[-1].ms_utc if summary_equity_curve else None),
    )


def write_normalized_result(workspace: Workspace, result: NormalizedResult) -> Path:
    """Persist the parsed result to ``workspace/normalized/result.json``.

    The launcher service calls this so an operator can ``GET /runs/{id}/normalized``
    later without re-parsing. Pretty-printed + sorted keys so the
    file hash is stable for the manifest.
    """
    workspace.normalized_dir.mkdir(parents=True, exist_ok=True)
    dest = workspace.normalized_dir / "result.json"
    payload = result.model_dump(mode="json", by_alias=False)
    # Atomic write: temp + rename so a partial write never appears as
    # a valid result.json (Phase 1c manifest pattern).
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    tmp.replace(dest)
    return dest
