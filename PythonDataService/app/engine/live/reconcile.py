"""Three-way reconciliation: Python live engine vs QC Cloud vs IBKR fills.

Implements the daily reconciliation report defined in
``docs/superpowers/specs/2026-05-08-ibkr-paper-shadow-deployment-design.md``
sections 6.1–6.5.

For each consolidated 15-min bar in a trading day the reconciler joins:
  * Python decision-time state (EMA5, EMA10, RSI, signal, intended price)
  * QC Cloud decision-time state for the same bar (EMA5, EMA10, RSI, signal)
  * The IBKR execution that resulted from any Python signal on that bar
and classifies the row into:
  * ``cross_engine_class`` ∈ {none, data, engine}  (§ 6.2)
  * ``fill_class`` ∈ {none, within_tol, breach}    (§ 6.3 fill tolerances)

Outputs four artifacts per day:
  * ``docs/references/reconciliations/<run_label>/day-N.md`` — committed
  * ``live_runs/<run_id>/reconcile/day-N.json`` — uncommitted
  * ``live_runs/<run_id>/reconcile/day-N.parquet`` — uncommitted
  * ``live_runs/<run_id>/reconcile/day-N.hashes.json`` — uncommitted sidecar
The committed Markdown embeds a SHA-256 manifest of every uncommitted
artifact it summarizes (§ 6.5), giving an audit-grade receipt without
checking in large machine artifacts.

Engine-class divergences and fill breaches feed the next-session halt
gate (§ 6.4) — see ``run.py`` for the morning pre-flight that consumes
``halt.flag`` written next to ``day-(N-1).md`` when a halt condition
trips.

Schemas (consumed by the loaders below; produced by the live runtime
and by QC Cloud's daily export):

PYTHON SIDE — under ``live_runs/<run_id>/``::

  decisions.parquet
    bar_close_ms     int64  canonical timestamp (ms UTC, end of bar)
    ema5             float64
    ema10            float64
    rsi              float64
    signal           str    ENTER | EXIT | HOLD
    intended_price   float64  consolidated-bar close at signal time

  executions.parquet
    ts_ms            int64
    exec_id          str
    perm_id          int64
    client_order_id  str
    account_id       str
    symbol           str
    fill_quantity    int64  signed (+ buy / − sell)
    fill_price       float64
    fee              float64

  trades.parquet
    entry_time_ms    int64
    exit_time_ms     int64
    entry_price      float64
    exit_price       float64
    pnl_points       float64

QC SIDE — under ``artifacts/qc/<YYYY-MM-DD>/`` (manual export from QC Cloud)::

  indicators.csv
    bar_close_ms     int64
    ema5             float
    ema10            float
    rsi              float
    signal           str    ENTER | EXIT | HOLD

  trades.csv
    entry_time_ms    int64
    exit_time_ms     int64
    entry_price      float
    exit_price       float
    pnl_points       float
"""

from __future__ import annotations

import argparse
import enum
import hashlib
import json
import logging
import sys
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


# ──────────────────────────── Tolerances ─────────────────────────────


@dataclass(frozen=True)
class CrossEngineTolerances:
    """§ 6.3 cross-feed shadowing tolerances (Test 2 + paper week).

    Defaults match the spec's accepted §13 #1b decision: EMA atol $0.10,
    RSI atol 2.0 RSI units. Strict same-bar parity (Test 1) uses much
    tighter atol=1e-9 — that's a separate code path; here we use the
    operational envelope appropriate for cross-feed shadowing where
    QC's data feed and IBKR's diverge legitimately.
    """

    ema_atol: float = 0.10
    rsi_atol: float = 2.0


@dataclass(frozen=True)
class FillTolerances:
    """§ 6.3 fill divergence tolerances (Python's IBKR fill vs intended)."""

    price_atol: float = 0.05
    time_atol_seconds: int = 5
    quantity_atol: int = 0


# ──────────────────────────── Enums ──────────────────────────────────


class CrossEngineClass(enum.StrEnum):
    """§ 6.2 cross-engine divergence taxonomy.

    DATA classifies any indicator delta beyond tolerance — both engines
    looked at materially different bars; signals may agree by chance,
    that doesn't make them comparable. ENGINE classifies the case where
    indicators agree but signals don't — that's the only class that
    points at a real port bug, and it's the one that trips the halt.
    """

    NONE = "none"
    DATA = "data"
    ENGINE = "engine"


class FillClass(enum.StrEnum):
    NONE = "none"
    WITHIN_TOL = "within_tol"
    BREACH = "breach"


SIGNAL_VALUES = ("ENTER", "EXIT", "HOLD")


# ──────────────────────────── Loaders ────────────────────────────────


_PY_DECISIONS_COLS = {"bar_close_ms", "ema5", "ema10", "rsi", "signal", "intended_price"}
_PY_EXECUTIONS_COLS = {
    "ts_ms",
    "exec_id",
    "perm_id",
    "client_order_id",
    "account_id",
    "symbol",
    "fill_quantity",
    "fill_price",
    "fee",
}
_QC_INDICATORS_COLS = {"bar_close_ms", "ema5", "ema10", "rsi", "signal"}


class ReconcileSchemaError(ValueError):
    """Raised when a loaded artifact is missing required columns or has invalid signal values."""


def _validate_columns(df: pd.DataFrame, required: set[str], source: str) -> None:
    missing = required - set(df.columns)
    if missing:
        raise ReconcileSchemaError(
            f"{source} is missing required columns: {sorted(missing)} (have {sorted(df.columns)})"
        )


def _validate_signals(df: pd.DataFrame, source: str) -> None:
    bad = set(df["signal"].unique()) - set(SIGNAL_VALUES)
    if bad:
        raise ReconcileSchemaError(
            f"{source} has unrecognized signal values {sorted(bad)} — expected one of {SIGNAL_VALUES}"
        )


def load_python_decisions(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    _validate_columns(df, _PY_DECISIONS_COLS, str(path))
    _validate_signals(df, str(path))
    return df.sort_values("bar_close_ms").reset_index(drop=True)


def load_python_executions(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    _validate_columns(df, _PY_EXECUTIONS_COLS, str(path))
    return df.sort_values("ts_ms").reset_index(drop=True)


def load_qc_indicators(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    _validate_columns(df, _QC_INDICATORS_COLS, str(path))
    _validate_signals(df, str(path))
    return df.sort_values("bar_close_ms").reset_index(drop=True)


# ──────────────────────────── Classifiers ────────────────────────────


def classify_cross_engine(
    py_ema5: float,
    py_ema10: float,
    py_rsi: float,
    py_signal: str,
    qc_ema5: float,
    qc_ema10: float,
    qc_rsi: float,
    qc_signal: str,
    tols: CrossEngineTolerances,
) -> CrossEngineClass:
    """Classify one bar per § 6.2.

    Indicators within tolerance AND signals identical → ``none``.
    Indicators outside tolerance → ``data`` (regardless of signals).
    Indicators within tolerance, signals differ → ``engine`` — the only
    case that points at a port bug.
    """
    indicators_within = (
        abs(py_ema5 - qc_ema5) <= tols.ema_atol
        and abs(py_ema10 - qc_ema10) <= tols.ema_atol
        and abs(py_rsi - qc_rsi) <= tols.rsi_atol
    )
    if not indicators_within:
        return CrossEngineClass.DATA
    if py_signal == qc_signal:
        return CrossEngineClass.NONE
    return CrossEngineClass.ENGINE


def classify_fill(
    intended_price: float | None,
    fill_price: float | None,
    intended_time_ms: int | None,
    fill_time_ms: int | None,
    intended_quantity: int | None,
    fill_quantity: int | None,
    tols: FillTolerances,
) -> FillClass:
    """Classify Python's IBKR fill against the strategy's intended order.

    ``none`` — neither side has a fill (HOLD bars).
    ``within_tol`` — present on both sides and all three dimensions
        (price, time, quantity) are within their respective atol.
    ``breach`` — any of (a) one side missing, (b) price delta exceeds
        atol, (c) time delta exceeds atol_seconds, (d) quantity delta
        exceeds atol. Fed into § 6.4 next-session halt rules.
    """
    if intended_price is None and fill_price is None:
        return FillClass.NONE
    if intended_price is None or fill_price is None:
        return FillClass.BREACH
    if abs(fill_price - intended_price) > tols.price_atol:
        return FillClass.BREACH
    if (
        intended_time_ms is not None
        and fill_time_ms is not None
        and abs(fill_time_ms - intended_time_ms) > tols.time_atol_seconds * 1000
    ):
        return FillClass.BREACH
    if (
        intended_quantity is not None
        and fill_quantity is not None
        and abs(fill_quantity - intended_quantity) > tols.quantity_atol
    ):
        return FillClass.BREACH
    return FillClass.WITHIN_TOL


# ──────────────────────────── Build the table ────────────────────────


def _attach_fills(decisions: pd.DataFrame, executions: pd.DataFrame) -> pd.DataFrame:
    """Attach the matching execution to each ENTER/EXIT decision row.

    A signalled bar (ENTER or EXIT) may produce one execution. We pick
    the chronologically-first execution at or after the bar close (the
    ``NEXT_BAR_OPEN`` fill model fills at the next bar's open; in
    practice the broker timestamp is a few seconds past bar_close_ms).
    Unsignalled (HOLD) bars get NaN fill columns.

    The matching is intentionally simple: signalled bars are rare (≤ 1
    entry + 1 exit per day in the SPY 15-min strategy), so a linear
    walk over executions is fine. If the executions list grows we'd
    want a proper as-of merge, but for now this is the cheapest correct
    implementation.
    """
    out = decisions.copy()
    out["python_fill_price"] = pd.NA
    out["python_fill_time_ms"] = pd.NA
    out["python_fill_quantity"] = pd.NA

    if executions.empty:
        return out

    used_indices: set[int] = set()
    for i, row in out.iterrows():
        if row["signal"] not in {"ENTER", "EXIT"}:
            continue
        bar_close_ms = int(row["bar_close_ms"])
        candidates = executions[(executions["ts_ms"] >= bar_close_ms) & (~executions.index.isin(used_indices))]
        if candidates.empty:
            continue
        match_idx = int(candidates.index[0])
        used_indices.add(match_idx)
        match = executions.loc[match_idx]
        out.at[i, "python_fill_price"] = float(match["fill_price"])
        out.at[i, "python_fill_time_ms"] = int(match["ts_ms"])
        out.at[i, "python_fill_quantity"] = int(match["fill_quantity"])

    return out


def build_reconciliation_table(
    py_decisions: pd.DataFrame,
    qc_indicators: pd.DataFrame,
    py_executions: pd.DataFrame,
    cross_tols: CrossEngineTolerances,
    fill_tols: FillTolerances,
) -> pd.DataFrame:
    """Produce the per-bar decision-time reconciliation table per § 6.1.

    Inner-join Python decisions to QC indicators on ``bar_close_ms``;
    attach Python fills; classify each row twice (cross-engine, fill).
    Bars where one side has no row are excluded from the joined table —
    the row count delta itself is reported in the day summary.
    """
    py_with_fills = _attach_fills(py_decisions, py_executions)
    merged = py_with_fills.merge(
        qc_indicators,
        on="bar_close_ms",
        how="inner",
        suffixes=("_py", "_qc"),
    )

    cross_classes: list[str] = []
    fill_classes: list[str] = []
    for _, row in merged.iterrows():
        cross = classify_cross_engine(
            py_ema5=float(row["ema5_py"]),
            py_ema10=float(row["ema10_py"]),
            py_rsi=float(row["rsi_py"]),
            py_signal=str(row["signal_py"]),
            qc_ema5=float(row["ema5_qc"]),
            qc_ema10=float(row["ema10_qc"]),
            qc_rsi=float(row["rsi_qc"]),
            qc_signal=str(row["signal_qc"]),
            tols=cross_tols,
        )
        cross_classes.append(cross.value)

        # intended_price is meaningful only when the bar carries an
        # actual order intent. HOLD bars have a row-wise intended_price
        # (the bar close) but no order was submitted, so passing it to
        # classify_fill would falsely flag every HOLD as a missing-fill
        # breach. Strip it to None on HOLD so the classifier returns
        # NONE for the un-traded majority of bars.
        py_signal = str(row["signal_py"])
        if py_signal == "HOLD":
            intended_price = None
            intended_time_ms = None
        else:
            intended_price = float(row["intended_price"]) if pd.notna(row["intended_price"]) else None
            intended_time_ms = int(row["bar_close_ms"])

        fill = classify_fill(
            intended_price=intended_price,
            fill_price=(float(row["python_fill_price"]) if pd.notna(row["python_fill_price"]) else None),
            intended_time_ms=intended_time_ms,
            fill_time_ms=(int(row["python_fill_time_ms"]) if pd.notna(row["python_fill_time_ms"]) else None),
            intended_quantity=None,
            fill_quantity=(int(row["python_fill_quantity"]) if pd.notna(row["python_fill_quantity"]) else None),
            tols=fill_tols,
        )
        fill_classes.append(fill.value)

    merged["cross_engine_class"] = cross_classes
    merged["fill_class"] = fill_classes

    return merged.rename(
        columns={
            "ema5_py": "python_ema5",
            "ema10_py": "python_ema10",
            "rsi_py": "python_rsi",
            "signal_py": "python_signal",
            "ema5_qc": "qc_ema5",
            "ema10_qc": "qc_ema10",
            "rsi_qc": "qc_rsi",
            "signal_qc": "qc_signal",
            "intended_price": "python_intended_price",
        }
    )[
        [
            "bar_close_ms",
            "python_signal",
            "python_ema5",
            "python_ema10",
            "python_rsi",
            "qc_signal",
            "qc_ema5",
            "qc_ema10",
            "qc_rsi",
            "cross_engine_class",
            "python_fill_price",
            "python_intended_price",
            "fill_class",
        ]
    ]


# ──────────────────────────── Day summary ────────────────────────────


@dataclass(frozen=True)
class DaySummary:
    day_n: int
    day_date: str
    bars_total: int
    bars_python_only: int
    bars_qc_only: int
    cross_none: int
    cross_data: int
    cross_engine: int
    fill_none: int
    fill_within_tol: int
    fill_breach: int
    halt_triggered: bool
    halt_reasons: tuple[str, ...]


def summarize_day(
    table: pd.DataFrame,
    py_decisions: pd.DataFrame,
    qc_indicators: pd.DataFrame,
    day_n: int,
    day_date: date,
) -> DaySummary:
    py_only = int(set(py_decisions["bar_close_ms"]).difference(qc_indicators["bar_close_ms"]).__len__())
    qc_only = int(set(qc_indicators["bar_close_ms"]).difference(py_decisions["bar_close_ms"]).__len__())

    counts = table["cross_engine_class"].value_counts().to_dict()
    fill_counts = table["fill_class"].value_counts().to_dict()
    cross_engine = int(counts.get(CrossEngineClass.ENGINE.value, 0))
    cross_data = int(counts.get(CrossEngineClass.DATA.value, 0))
    cross_none = int(counts.get(CrossEngineClass.NONE.value, 0))
    fill_breach = int(fill_counts.get(FillClass.BREACH.value, 0))
    fill_within = int(fill_counts.get(FillClass.WITHIN_TOL.value, 0))
    fill_none = int(fill_counts.get(FillClass.NONE.value, 0))

    reasons: list[str] = []
    if cross_engine > 0:
        reasons.append(f"engine-class divergence count={cross_engine}")
    if fill_breach > 0:
        reasons.append(f"fill-class breach count={fill_breach}")
    halt = bool(reasons)

    return DaySummary(
        day_n=day_n,
        day_date=day_date.isoformat(),
        bars_total=len(table),
        bars_python_only=py_only,
        bars_qc_only=qc_only,
        cross_none=cross_none,
        cross_data=cross_data,
        cross_engine=cross_engine,
        fill_none=fill_none,
        fill_within_tol=fill_within,
        fill_breach=fill_breach,
        halt_triggered=halt,
        halt_reasons=tuple(reasons),
    )


# ──────────────────────────── SHA-256 manifest ───────────────────────


def file_sha256(path: Path) -> str:
    """Return a stable lowercase SHA-256 for a file or artifact directory."""
    if path.is_dir():
        return _directory_sha256(path)
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _directory_sha256(path: Path) -> str:
    h = hashlib.sha256()
    for child in sorted(p for p in path.rglob("*") if p.is_file()):
        rel = child.relative_to(path).as_posix().encode("utf-8")
        h.update(rel)
        h.update(b"\0")
        with child.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        h.update(b"\0")
    return h.hexdigest()


def _maybe_sha256(path: Path) -> str | None:
    return file_sha256(path) if path.exists() else None


def build_hash_manifest(
    *,
    json_path: Path,
    parquet_path: Path,
    py_executions_path: Path,
    py_trades_path: Path,
    qc_trades_path: Path,
    qc_indicators_path: Path,
    run_ledger_path: Path,
    hydration_receipt_path: Path | None = None,
) -> dict[str, str | None]:
    manifest: dict[str, str | None] = {
        "reconcile_json": _maybe_sha256(json_path),
        "reconcile_parquet": _maybe_sha256(parquet_path),
        "python_executions_parquet": _maybe_sha256(py_executions_path),
        "python_trades_parquet": _maybe_sha256(py_trades_path),
        "qc_export_trades": _maybe_sha256(qc_trades_path),
        "qc_export_indicators": _maybe_sha256(qc_indicators_path),
        "run_ledger": _maybe_sha256(run_ledger_path),
    }
    if hydration_receipt_path is not None and hydration_receipt_path.exists():
        manifest["indicator_state_hydration.json"] = file_sha256(hydration_receipt_path)
    return manifest


# ──────────────────────────── Markdown rendering ─────────────────────


def render_day_md(
    summary: DaySummary,
    table: pd.DataFrame,
    hash_manifest: dict[str, str | None],
    *,
    run_label: str,
    cross_tols: CrossEngineTolerances,
    fill_tols: FillTolerances,
) -> str:
    """Render the committed day-N.md report.

    Embeds the SHA-256 manifest in a fenced YAML block at the top so
    downstream tooling can verify the on-disk artifacts match what this
    Markdown receipt summarizes (§ 6.5).
    """
    halt_block = (
        "**Halt triggered for next session:** " + ", ".join(summary.halt_reasons)
        if summary.halt_triggered
        else "**Halt triggered for next session:** no"
    )

    parts: list[str] = []
    parts.append(f"# Day {summary.day_n} reconciliation — {summary.day_date}")
    parts.append("")
    parts.append(f"**Run:** `{run_label}`  ")
    parts.append(f"**Generated:** {datetime.now(UTC).isoformat()}")
    parts.append("")
    parts.append(halt_block)
    parts.append("")

    parts.append("## Artifact hashes (SHA-256)")
    parts.append("")
    parts.append("```yaml")
    parts.append("artifact_hashes:")
    for key, value in hash_manifest.items():
        parts.append(f"  {key}: {value or '~'}")
    parts.append("```")
    parts.append("")

    parts.append("## Tolerances applied")
    parts.append("")
    parts.append("| Dimension | Value |")
    parts.append("|---|---|")
    parts.append(f"| EMA atol | `{cross_tols.ema_atol}` |")
    parts.append(f"| RSI atol | `{cross_tols.rsi_atol}` |")
    parts.append(f"| Fill price atol | `{fill_tols.price_atol}` |")
    parts.append(f"| Fill time atol (s) | `{fill_tols.time_atol_seconds}` |")
    parts.append(f"| Fill quantity atol | `{fill_tols.quantity_atol}` |")
    parts.append("")

    parts.append("## Counts")
    parts.append("")
    parts.append("| Metric | Value |")
    parts.append("|---|---:|")
    parts.append(f"| Bars matched (Python ∩ QC) | {summary.bars_total} |")
    parts.append(f"| Bars Python-only | {summary.bars_python_only} |")
    parts.append(f"| Bars QC-only | {summary.bars_qc_only} |")
    parts.append(f"| Cross-engine `none` | {summary.cross_none} |")
    parts.append(f"| Cross-engine `data` | {summary.cross_data} |")
    parts.append(f"| Cross-engine `engine` | {summary.cross_engine} |")
    parts.append(f"| Fill `none` | {summary.fill_none} |")
    parts.append(f"| Fill `within_tol` | {summary.fill_within_tol} |")
    parts.append(f"| Fill `breach` | {summary.fill_breach} |")
    parts.append("")

    interesting = table[
        (table["cross_engine_class"] != CrossEngineClass.NONE.value)
        | (table["fill_class"] == FillClass.BREACH.value)
        | (table["python_signal"] != "HOLD")
        | (table["qc_signal"] != "HOLD")
    ]
    parts.append("## Notable rows")
    parts.append("")
    if interesting.empty:
        parts.append("_All bars classified `none` / no signals — full table at `day-N.parquet`._")
    else:
        parts.append("(All non-`none` cross-engine rows, all fill breaches, all signal bars.)")
        parts.append("")
        parts.append(_dataframe_to_markdown(interesting))
    parts.append("")

    return "\n".join(parts) + "\n"


def _dataframe_to_markdown(df: pd.DataFrame) -> str:
    """Minimal Markdown table renderer.

    Avoids the optional ``tabulate`` dependency that ``pd.to_markdown``
    requires. Floats render to 4 decimal places to match the spec's
    decision-table presentation; everything else uses ``str``.
    """
    cols = list(df.columns)
    header = "| " + " | ".join(cols) + " |"
    sep = "|" + "|".join("---" for _ in cols) + "|"
    rows: list[str] = [header, sep]
    for _, row in df.iterrows():
        cells: list[str] = []
        for col in cols:
            value = row[col]
            if pd.isna(value):
                cells.append("")
            elif isinstance(value, float):
                cells.append(f"{value:.4f}")
            else:
                cells.append(str(value))
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


# ──────────────────────────── Day write orchestration ────────────────


def _json_default(obj: object) -> object:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    raise TypeError(f"unhandled type {type(obj).__name__} for JSON serialization")


@dataclass(frozen=True)
class DayPaths:
    parquet: Path
    json: Path
    hashes: Path
    md: Path


def write_day_report(
    *,
    run_dir: Path,
    qc_dir: Path,
    docs_dir: Path,
    run_label: str,
    day_n: int,
    day_date: date,
    cross_tols: CrossEngineTolerances | None = None,
    fill_tols: FillTolerances | None = None,
) -> DayPaths:
    """Run the full day pipeline: load → classify → write four artifacts.

    Returns the paths written. The Markdown is the only artifact under
    ``docs/`` (and therefore in git); the other three live under
    ``run_dir/reconcile/`` and are .gitignored.
    """
    cross_tols = cross_tols or CrossEngineTolerances()
    fill_tols = fill_tols or FillTolerances()

    py_decisions = load_python_decisions(run_dir / "decisions.parquet")
    py_executions = load_python_executions(run_dir / "executions.parquet")
    qc_indicators = load_qc_indicators(qc_dir / "indicators.csv")

    table = build_reconciliation_table(
        py_decisions=py_decisions,
        qc_indicators=qc_indicators,
        py_executions=py_executions,
        cross_tols=cross_tols,
        fill_tols=fill_tols,
    )
    summary = summarize_day(table, py_decisions, qc_indicators, day_n=day_n, day_date=day_date)

    reconcile_dir = run_dir / "reconcile"
    reconcile_dir.mkdir(parents=True, exist_ok=True)
    docs_dir.mkdir(parents=True, exist_ok=True)

    parquet_path = reconcile_dir / f"day-{day_n}.parquet"
    json_path = reconcile_dir / f"day-{day_n}.json"
    hashes_path = reconcile_dir / f"day-{day_n}.hashes.json"
    md_path = docs_dir / f"day-{day_n}.md"

    table.to_parquet(parquet_path, index=False)

    json_payload = {
        "summary": asdict(summary),
        "rows": table.to_dict(orient="records"),
    }
    json_path.write_text(
        json.dumps(json_payload, indent=2, default=_json_default, sort_keys=True),
        encoding="utf-8",
    )

    hash_manifest = build_hash_manifest(
        json_path=json_path,
        parquet_path=parquet_path,
        py_executions_path=run_dir / "executions.parquet",
        py_trades_path=run_dir / "trades.parquet",
        qc_trades_path=qc_dir / "trades.csv",
        qc_indicators_path=qc_dir / "indicators.csv",
        run_ledger_path=run_dir / "run_ledger.json",
        hydration_receipt_path=run_dir / "indicator_state_hydration.json",
    )
    hashes_path.write_text(json.dumps(hash_manifest, indent=2, sort_keys=True), encoding="utf-8")

    md_text = render_day_md(
        summary=summary,
        table=table,
        hash_manifest=hash_manifest,
        run_label=run_label,
        cross_tols=cross_tols,
        fill_tols=fill_tols,
    )
    md_path.write_text(md_text, encoding="utf-8")

    if summary.halt_triggered:
        (run_dir / "halt.flag").write_text(
            json.dumps(
                {
                    "day_n": day_n,
                    "day_date": day_date.isoformat(),
                    "reasons": list(summary.halt_reasons),
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return DayPaths(parquet=parquet_path, json=json_path, hashes=hashes_path, md=md_path)


# ──────────────────────────── Week rollup ────────────────────────────


def write_week_rollup(
    *,
    run_dir: Path,
    docs_dir: Path,
    run_label: str,
    days: list[DaySummary],
) -> Path:
    """Aggregate day summaries into a week.md, including a hash manifest of every per-day md and json."""
    md_path = docs_dir / "week.md"
    parts: list[str] = []
    parts.append(f"# Week rollup — {run_label}")
    parts.append("")
    parts.append(f"**Generated:** {datetime.now(UTC).isoformat()}")
    parts.append(f"**Days:** {len(days)}")
    parts.append("")

    parts.append("## Day-by-day hash manifest")
    parts.append("")
    parts.append("```yaml")
    parts.append("days:")
    for day in days:
        md_file = docs_dir / f"day-{day.day_n}.md"
        json_file = run_dir / "reconcile" / f"day-{day.day_n}.json"
        parts.append(f"  - day_n: {day.day_n}")
        parts.append(f"    day_date: {day.day_date}")
        parts.append(f"    md_sha256: {_maybe_sha256(md_file) or '~'}")
        parts.append(f"    json_sha256: {_maybe_sha256(json_file) or '~'}")
    parts.append("```")
    parts.append("")

    parts.append("## Aggregate counts")
    parts.append("")
    parts.append("| Day | Date | Bars | data | engine | fill_breach | halt? |")
    parts.append("|---:|---|---:|---:|---:|---:|---|")
    for day in days:
        halt_marker = "**HALT**" if day.halt_triggered else "ok"
        parts.append(
            f"| {day.day_n} | {day.day_date} | {day.bars_total} | "
            f"{day.cross_data} | {day.cross_engine} | {day.fill_breach} | {halt_marker} |"
        )
    parts.append("")

    md_path.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return md_path


# ──────────────────────────── CLI ────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m app.engine.live.reconcile",
        description="Daily three-way reconciliation report for IBKR paper / QC Cloud / Python live engine.",
    )
    p.add_argument("--run-dir", type=Path, required=True, help="live_runs/<run_id>/ directory")
    p.add_argument("--qc-dir", type=Path, required=True, help="artifacts/qc/<YYYY-MM-DD>/ directory")
    p.add_argument(
        "--docs-dir",
        type=Path,
        required=True,
        help="docs/references/reconciliations/<run_label>/ directory",
    )
    p.add_argument("--run-label", required=True, help="e.g. spy-ema-crossover-paper-2026-05")
    p.add_argument("--day-n", type=int, required=True)
    p.add_argument("--day-date", type=date.fromisoformat, required=True, help="YYYY-MM-DD")
    p.add_argument("--ema-atol", type=float, default=0.10)
    p.add_argument("--rsi-atol", type=float, default=2.0)
    p.add_argument("--fill-price-atol", type=float, default=0.05)
    p.add_argument("--fill-time-atol-seconds", type=int, default=5)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cross_tols = CrossEngineTolerances(ema_atol=args.ema_atol, rsi_atol=args.rsi_atol)
    fill_tols = FillTolerances(
        price_atol=args.fill_price_atol,
        time_atol_seconds=args.fill_time_atol_seconds,
    )
    paths = write_day_report(
        run_dir=args.run_dir,
        qc_dir=args.qc_dir,
        docs_dir=args.docs_dir,
        run_label=args.run_label,
        day_n=args.day_n,
        day_date=args.day_date,
        cross_tols=cross_tols,
        fill_tols=fill_tols,
    )
    logger.info(
        "Wrote day-%s report: md=%s parquet=%s json=%s hashes=%s",
        args.day_n,
        paths.md,
        paths.parquet,
        paths.json,
        paths.hashes,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
