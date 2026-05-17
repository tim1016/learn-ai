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
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.lean_sidecar.workspace import Workspace

# Bumped any time the parser's output schema changes in a non-additive
# way. The manifest records this; a different value invalidates
# reconciliation fixtures built against the prior parser version.
NORMALIZED_PARSER_VERSION = "phase-3a-r1"


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


# ---------------------------------------------------------------------------
# Top-level result
# ---------------------------------------------------------------------------


class NormalizedResult(BaseModel):
    """The parsed LEAN backtest result.

    What's omitted by design (Phase 3a):
      * Per-order detail beyond order events (the Orders dict is in
        LEAN's full ``<algorithm-id>.json`` — Phase 3b will fold it in
        once order-aggregation semantics are pinned).
      * Trade-pair P&L (LEAN's ``TotalPerformance.ClosedTrades``) —
        Phase 5 reconciliation work.
      * Charts other than the strategy equity curve.
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
    order_events: list[NormalizedOrderEvent] = Field(default_factory=list)
    # Derived: simple counts the frontend usually wants without re-walking
    # the equity curve or order events.
    total_order_events: int
    total_equity_points: int
    first_equity_ms_utc: int | None = Field(default=None)
    last_equity_ms_utc: int | None = Field(default=None)


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

    summary: Path
    order_events: Path | None
    algorithm_id: str


def _locate_lean_artifacts(output_dir: Path) -> _LeanArtifactPaths:
    """Find LEAN's summary + order-events files in ``output_dir``.

    The two filenames look like ``<algo>-summary.json`` and
    ``<algo>-order-events.json``. We pick the first match for each
    suffix — Phase 3a assumes a single algorithm per run (which is
    LEAN's default; multi-algo lab runs are out of scope).
    """
    if not output_dir.is_dir():
        raise NormalizedParserError(f"output directory missing: {output_dir}")

    summary_candidates = sorted(output_dir.glob("*-summary.json"))
    order_candidates = sorted(output_dir.glob("*-order-events.json"))
    if not summary_candidates:
        raise NormalizedParserError(f"no *-summary.json under {output_dir}; LEAN did not write a backtest summary")
    summary = summary_candidates[0]

    # Derive algorithm-id from the summary filename (the part before
    # ``-summary.json``).
    algorithm_id = summary.name[: -len("-summary.json")]
    return _LeanArtifactPaths(
        summary=summary,
        # Zero-order runs don't write the events file; that's a valid
        # zero-events outcome, not an error.
        order_events=order_candidates[0] if order_candidates else None,
        algorithm_id=algorithm_id,
    )


def _unix_seconds_to_ms_utc(seconds: float | int) -> int:
    """LEAN writes timestamps as unix seconds (float); we want int64 ms.

    Truncation, not rounding — matches LEAN's own millisecond
    arithmetic which floors when converting. Equity-curve sub-second
    resolution is not meaningful (LEAN samples on bar boundaries).
    """
    return int(float(seconds) * 1000)


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

    try:
        summary_raw = json.loads(paths.summary.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise NormalizedParserError(f"could not read {paths.summary}: {e}") from e
    if not isinstance(summary_raw, dict):
        raise NormalizedParserError(f"summary file {paths.summary} is not a JSON object")

    statistics = {k: str(v) for k, v in summary_raw.get("statistics", {}).items()}
    runtime_statistics = {k: str(v) for k, v in summary_raw.get("runtimeStatistics", {}).items()}
    equity_curve = _parse_equity_curve(summary_raw)

    order_events: list[NormalizedOrderEvent] = []
    if paths.order_events is not None:
        try:
            events_raw = json.loads(paths.order_events.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise NormalizedParserError(f"could not read {paths.order_events}: {e}") from e
        if not isinstance(events_raw, list):
            raise NormalizedParserError(f"order-events file {paths.order_events} is not a JSON array")
        order_events = _parse_order_events(events_raw)

    return NormalizedResult(
        parser_version=NORMALIZED_PARSER_VERSION,
        algorithm_id=paths.algorithm_id,
        statistics=statistics,
        runtime_statistics=runtime_statistics,
        equity_curve=equity_curve,
        order_events=order_events,
        total_order_events=len(order_events),
        total_equity_points=len(equity_curve),
        first_equity_ms_utc=equity_curve[0].ms_utc if equity_curve else None,
        last_equity_ms_utc=equity_curve[-1].ms_utc if equity_curve else None,
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
