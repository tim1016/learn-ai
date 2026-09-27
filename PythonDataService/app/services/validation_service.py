"""Validation service: compare pandas-ta generated data against TradingView CSV exports.

A report grades the comparison only when the evidence is sound (#2461):
rows must pair one-to-one on a shared, unique time column, and enough of
the aligned rows must carry comparable values. Anything less is said
apart — an invalid comparison (duplicate time keys), an unverified one
(rows paired by position), or insufficient comparable evidence (coverage
below the minimum this module names) — never a grade over what was
actually compared.
"""

from __future__ import annotations

import io
import logging
from datetime import UTC, datetime
from typing import Any

import pandas as pd

logger = logging.getLogger(__name__)

# Classification thresholds (absolute % diff)
_EXACT = 0.001
_CLOSE = 0.01
_OK = 0.1

# The overall grade is shown only when at least this share of the aligned
# rows × common fields was actually compared; the report names this minimum.
_MIN_COVERAGE_PCT = 80.0

# Carried columns that are never compared as indicator fields.
_SKIP_COLS = {"unix_ts", "iso_time", "open", "high", "low", "close", "volume", "vwap", "transactions"}


def generate_validation_report(
    our_csv_bytes: bytes,
    tv_csv_bytes: bytes,
    ticker: str,
) -> str:
    """
    Compare a pandas-ta generated CSV against a TradingView CSV export.
    Returns a full markdown report.
    """
    our_df = pd.read_csv(io.BytesIO(our_csv_bytes))
    tv_df = pd.read_csv(io.BytesIO(tv_csv_bytes))
    our_total = len(our_df)
    tv_total = len(tv_df)

    # Determine common indicator columns (exclude time/ohlcv)
    our_ind_cols = [c for c in our_df.columns if c not in _SKIP_COLS]
    tv_ind_cols = [c for c in tv_df.columns if c not in _SKIP_COLS]

    # Align by the shared time column; a repeated time value in either file
    # makes one-to-one pairing impossible, so the comparison is invalid
    # before any numbers are computed (#2461).
    time_col: str | None = None
    if "unix_ts" in our_df.columns and "unix_ts" in tv_df.columns:
        time_col = "unix_ts"
    elif "iso_time" in our_df.columns and "iso_time" in tv_df.columns:
        time_col = "iso_time"

    if time_col is not None:
        our_dupes = int(our_df[time_col].duplicated().sum())
        tv_dupes = int(tv_df[time_col].duplicated().sum())
        if our_dupes or tv_dupes:
            return _build_invalid_report(
                ticker=ticker,
                time_col=time_col,
                our_dupes=our_dupes,
                tv_dupes=tv_dupes,
                our_total=our_total,
                tv_total=tv_total,
            )
        merged = our_df.merge(tv_df, on=time_col, suffixes=("_ours", "_tv"), how="inner")
        align_method = time_col
    else:
        # No shared time column: pair rows by position. The per-field numbers
        # below are a diagnostic, never a graded verdict (#2461).
        min_len = min(our_total, tv_total)
        merged = pd.concat(
            [
                our_df.head(min_len).add_suffix("_ours"),
                tv_df.head(min_len).add_suffix("_tv"),
            ],
            axis=1,
        )
        align_method = "positional"

    aligned_rows = len(merged)
    matched_rows = aligned_rows

    # Find common fields to compare
    common_fields = _find_common_fields(our_ind_cols, tv_ind_cols, merged.columns.tolist())

    # Per-field analysis
    field_reports: list[dict[str, Any]] = []
    all_divergence_points: list[dict[str, Any]] = []

    for our_col, tv_col, display_name in common_fields:
        if our_col not in merged.columns or tv_col not in merged.columns:
            continue

        our_vals = pd.to_numeric(merged[our_col], errors="coerce")
        tv_vals = pd.to_numeric(merged[tv_col], errors="coerce")

        our_present = our_vals.notna()
        tv_present = tv_vals.notna()
        both_valid = our_present & tv_present
        valid_count = int(both_valid.sum())
        missing_ours = int((~our_present & tv_present).sum())
        missing_tv = int((our_present & ~tv_present).sum())
        missing_both = int((~our_present & ~tv_present).sum())

        field_report: dict[str, Any] = {
            "field": display_name,
            "aligned": aligned_rows,
            "compared": valid_count,
            "missing_ours": missing_ours,
            "missing_tv": missing_tv,
            "missing_both": missing_both,
            "coverage_pct": round(valid_count / aligned_rows * 100, 1) if aligned_rows else 0.0,
        }
        field_reports.append(field_report)

        if valid_count == 0:
            continue

        diff = (our_vals[both_valid] - tv_vals[both_valid]).abs()
        pct_diff = (diff / tv_vals[both_valid].abs().clip(lower=1e-10)) * 100

        exact_count = int((pct_diff < _EXACT).sum())
        close_count = int(((pct_diff >= _EXACT) & (pct_diff < _CLOSE)).sum())
        ok_count = int(((pct_diff >= _CLOSE) & (pct_diff < _OK)).sum())
        bad_count = int((pct_diff >= _OK).sum())
        field_report.update(
            exact=exact_count,
            close=close_count,
            ok=ok_count,
            divergent=bad_count,
            mean_abs_diff=float(diff.mean()),
            max_abs_diff=float(diff.max()),
            mean_pct_diff=float(pct_diff.mean()),
            max_pct_diff=float(pct_diff.max()),
        )

        # Find top divergence points
        top_idx = pct_diff.nlargest(5).index
        for idx in top_idx:
            iso_col = "iso_time" if "iso_time" in merged.columns else "iso_time_ours"
            iso_val = merged.loc[idx, iso_col] if iso_col in merged.columns else ""

            all_divergence_points.append(
                {
                    "field": display_name,
                    "timestamp": str(iso_val),
                    "our_value": float(our_vals.loc[idx]) if pd.notna(our_vals.loc[idx]) else None,
                    "tv_value": float(tv_vals.loc[idx]) if pd.notna(tv_vals.loc[idx]) else None,
                    "abs_diff": float(diff.loc[idx]),
                    "pct_diff": float(pct_diff.loc[idx]),
                }
            )

    # Sort divergence points by pct_diff descending
    all_divergence_points.sort(key=lambda x: x["pct_diff"], reverse=True)
    top_divergences = all_divergence_points[:20]

    # The overall grade appears only when the evidence permits one (#2461).
    total_compared = sum(r["compared"] for r in field_reports)
    total_comparable = aligned_rows * len(field_reports)
    overall_coverage_pct: float | None = None
    grade_line: str
    if align_method == "positional":
        grade_line = (
            "withheld — **unverified comparison**: the files share no time column, so rows "
            "were paired by position. The per-field numbers below are a diagnostic, not a verdict."
        )
    elif not field_reports:
        grade_line = "withheld — the files share no comparable indicator field"
    else:
        overall_coverage_pct = total_compared / total_comparable * 100
        if overall_coverage_pct < _MIN_COVERAGE_PCT:
            grade_line = (
                f"withheld — insufficient comparable evidence: {overall_coverage_pct:.1f}% overall "
                f"coverage is below the {_MIN_COVERAGE_PCT:.0f}% minimum"
            )
        else:
            # Coverage ≥ _MIN_COVERAGE_PCT > 0 guarantees total_compared > 0.
            exact_pct = sum(r.get("exact", 0) for r in field_reports) / total_compared * 100
            ok_pct = (
                sum(r.get("exact", 0) + r.get("close", 0) + r.get("ok", 0) for r in field_reports)
                / total_compared
                * 100
            )
            grade_line = _grade(exact_pct, ok_pct)

    # Build markdown
    md = _build_markdown(
        ticker=ticker,
        our_total=our_total,
        tv_total=tv_total,
        matched_rows=matched_rows,
        align_method=align_method,
        field_reports=field_reports,
        top_divergences=top_divergences,
        our_cols=our_ind_cols,
        tv_cols=tv_ind_cols,
        grade_line=grade_line,
        overall_coverage_pct=overall_coverage_pct,
    )

    return md


def _find_common_fields(
    our_cols: list[str],
    tv_cols: list[str],
    merged_cols: list[str],
) -> list[tuple[str, str, str]]:
    """Find matching field pairs between our data and TradingView data."""
    pairs = []

    # Direct suffix matches from merge
    for col in our_cols:
        ours_suffixed = f"{col}_ours"
        tv_suffixed = f"{col}_tv"
        if ours_suffixed in merged_cols and tv_suffixed in merged_cols:
            pairs.append((ours_suffixed, tv_suffixed, col))

    # Try fuzzy matching for common indicators if no direct match
    if not pairs:
        # Map common TradingView column patterns to our column patterns
        tv_map = {}
        for col in tv_cols:
            lower = col.lower().replace(" ", "_")
            tv_map[lower] = col

        for our_col in our_cols:
            our_lower = our_col.lower()
            if our_lower in tv_map:
                tv_col = tv_map[our_lower]
                our_m = f"{our_col}_ours" if f"{our_col}_ours" in merged_cols else our_col
                tv_m = f"{tv_col}_tv" if f"{tv_col}_tv" in merged_cols else tv_col
                pairs.append((our_m, tv_m, our_col))

    return pairs


def _shared_markdown_header(ticker: str) -> list[str]:
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return [
        f"# Validation Report — {ticker}",
        "",
        f"**Generated:** {now}  ",
        "**Comparison:** pandas-ta (Polygon.io) vs TradingView CSV export  ",
    ]


def _build_invalid_report(
    *,
    ticker: str,
    time_col: str,
    our_dupes: int,
    tv_dupes: int,
    our_total: int,
    tv_total: int,
) -> str:
    """The duplicate-time-keys refusal: no numbers, no grade (#2461)."""
    lines = _shared_markdown_header(ticker)
    lines += [
        "**Alignment:** refused",
        "",
        "## Comparison invalid",
        "",
        f"Time values repeat within a file, so rows cannot pair one-to-one on "
        f"`{time_col}` and no comparison number would be trustworthy. Nothing was graded.",
        "",
        "| File | Rows | Duplicate time values |",
        "|------|-----:|----------------------:|",
        f"| pandas-ta (ours) | {our_total:,} | {our_dupes:,} |",
        f"| TradingView | {tv_total:,} | {tv_dupes:,} |",
        "",
        f"De-duplicate the `{time_col}` values in the flagged file(s) and re-run.",
        "",
    ]
    return "\n".join(lines)


def _build_markdown(
    *,
    ticker: str,
    our_total: int,
    tv_total: int,
    matched_rows: int,
    align_method: str,
    field_reports: list[dict[str, Any]],
    top_divergences: list[dict[str, Any]],
    our_cols: list[str],
    tv_cols: list[str],
    grade_line: str,
    overall_coverage_pct: float | None,
) -> str:
    """Build the full markdown validation report."""
    lines = _shared_markdown_header(ticker)
    lines += [
        f"**Alignment:** {align_method}  ",
        "",
        "---",
        "",
        "## 1. Row Alignment Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| pandas-ta rows | {our_total:,} |",
        f"| TradingView rows | {tv_total:,} |",
        f"| Matched (aligned) rows | {matched_rows:,} |",
        f"| Unmatched pandas-ta rows | {max(our_total - matched_rows, 0):,} |",
        f"| Unmatched TradingView rows | {max(tv_total - matched_rows, 0):,} |",
        f"| Match rate | {min(matched_rows / max(our_total, 1) * 100, 100.0):.1f}% |",
        "",
    ]

    # Overall grade
    total_compared = sum(r["compared"] for r in field_reports)
    total_exact = sum(r.get("exact", 0) for r in field_reports)
    total_close = sum(r.get("close", 0) for r in field_reports)
    total_ok = sum(r.get("ok", 0) for r in field_reports)
    total_bad = sum(r.get("divergent", 0) for r in field_reports)

    lines += [
        "## 2. Overall Accuracy",
        "",
        "| Classification | Count | Percentage |",
        "|---------------|------:|----------:|",
        f"| Exact match (< {_EXACT}%) | {total_exact:,} | {total_exact / max(total_compared, 1) * 100:.2f}% |",
        f"| Close match (< {_CLOSE}%) | {total_close:,} | {total_close / max(total_compared, 1) * 100:.2f}% |",
        f"| Acceptable (< {_OK}%) | {total_ok:,} | {total_ok / max(total_compared, 1) * 100:.2f}% |",
        f"| **Divergent (≥ {_OK}%)** | **{total_bad:,}** | **{total_bad / max(total_compared, 1) * 100:.2f}%** |",
        f"| **Total compared** | **{total_compared:,}** | |",
    ]
    if overall_coverage_pct is not None:
        lines.append(
            f"| **Overall coverage** | **{total_compared:,} of {matched_rows * len(field_reports):,} pairs** | **{overall_coverage_pct:.1f}%** |"
        )
    lines += [
        "",
        f"> **Overall grade:** {grade_line}",
        "",
    ]

    # Per-field table
    lines += [
        "## 3. Per-Field Accuracy",
        "",
        "| Field | Aligned | Compared | Coverage | Missing (ours) | Missing (TradingView) | Missing (both) | Exact | Close | OK | Divergent | Mean %Diff | Max %Diff | Max |Diff| |",
        "|-------|--------:|---------:|---------:|---------------:|----------------------:|---------------:|------:|------:|---:|----------:|-----------:|----------:|----------:|",
    ]

    for r in sorted(field_reports, key=lambda x: x.get("max_pct_diff", 0), reverse=True):
        if r["compared"] == 0:
            lines.append(
                f"| {r['field']} | {r['aligned']:,} | 0 | {r['coverage_pct']:.1f}% "
                f"| {r['missing_ours']:,} | {r['missing_tv']:,} | {r['missing_both']:,} "
                f"| — | — | — | — | — | — | — |"
            )
            continue
        lines.append(
            f"| {r['field']} "
            f"| {r['aligned']:,} "
            f"| {r['compared']:,} "
            f"| {r['coverage_pct']:.1f}% "
            f"| {r['missing_ours']:,} "
            f"| {r['missing_tv']:,} "
            f"| {r['missing_both']:,} "
            f"| {r['exact']:,} "
            f"| {r['close']:,} "
            f"| {r['ok']:,} "
            f"| {r['divergent']:,} "
            f"| {r['mean_pct_diff']:.6f}% "
            f"| {r['max_pct_diff']:.4f}% "
            f"| {r['max_abs_diff']:.6f} |"
        )

    lines.append("")

    # Top divergence hotspots
    if top_divergences:
        lines += [
            "## 4. Top Divergence Hotspots",
            "",
            "These are the individual data points with the largest percentage difference.",
            "",
            "| # | Field | Timestamp | pandas-ta | TradingView | |Diff| | %Diff |",
            "|--:|-------|-----------|----------:|------------:|------:|------:|",
        ]
        for i, d in enumerate(top_divergences, 1):
            our_v = f"{d['our_value']:.4f}" if d["our_value"] is not None else "NaN"
            tv_v = f"{d['tv_value']:.4f}" if d["tv_value"] is not None else "NaN"
            lines.append(
                f"| {i} | {d['field']} | {d['timestamp']} | {our_v} | {tv_v} | {d['abs_diff']:.6f} | {d['pct_diff']:.4f}% |"
            )
        lines.append("")

    # Known behaviors
    lines += [
        "## 5. Known Divergence Causes",
        "",
        "| Cause | Impact | Explanation |",
        "|-------|--------|-------------|",
        "| **Polygon 07:00 ET bar contamination** | High | Late settlement trades inflate close by $4-6 at 07:00-07:02 ET. TradingView filters these. Poisons all downstream EMAs — longer periods recover more slowly. |",
        "| **Data feed difference** | Medium | Polygon uses consolidated tape; TradingView uses Cboe BZX composite. Close prices may differ by $0.01+. |",
        "| **Missing minute bars** | Medium | Polygon doesn't return zero-trade minutes. Forward-fill option mitigates this for indicator continuity. |",
        "| **Session mismatch** | High | If TradingView chart is RTH-only but data includes extended hours, bars won't align. Use session='rth' to match. |",
        "| **Supertrend split bands** | Expected | `supertl` is NaN during downtrends, `superts` during uptrends — by design. Compare `supert` (main line) and `supertd` (direction) instead. |",
        "| **VWAP definition** | Expected | Polygon VWAP is daily rolling (not per-bar) — routinely outside single bar's H/L range. |",
        "",
        "## 6. Columns in Each Dataset",
        "",
        f"**pandas-ta indicators ({len(our_cols)}):** {', '.join(our_cols[:30])}{'...' if len(our_cols) > 30 else ''}  ",
        f"**TradingView indicators ({len(tv_cols)}):** {', '.join(tv_cols[:30])}{'...' if len(tv_cols) > 30 else ''}  ",
        "",
        "---",
        "",
        "*Report generated by Data Lab validation engine. Calculation library: pandas-ta.*",
    ]

    return "\n".join(lines)


def _grade(exact_pct: float, ok_pct: float) -> str:
    if exact_pct > 95:
        return "🟢 Excellent — >95% exact matches"
    if ok_pct > 95:
        return "🟡 Good — >95% within acceptable tolerance, minor divergences present"
    if ok_pct > 80:
        return "🟠 Fair — >80% within tolerance, investigate divergent fields"
    return "🔴 Significant divergence — check session settings, data feed, and 07:00 ET contamination"
