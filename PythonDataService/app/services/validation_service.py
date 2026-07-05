"""Validation service: compare pandas-ta generated data against TradingView CSV exports."""

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

    # Determine common indicator columns (exclude time/ohlcv)
    skip_cols = {"unix_ts", "iso_time", "open", "high", "low", "close", "volume", "vwap", "transactions"}
    our_ind_cols = [c for c in our_df.columns if c not in skip_cols]
    tv_ind_cols = [c for c in tv_df.columns if c not in skip_cols]

    # Try to align by timestamp
    if "unix_ts" in our_df.columns and "unix_ts" in tv_df.columns:
        merged = our_df.merge(tv_df, on="unix_ts", suffixes=("_ours", "_tv"), how="inner")
        align_method = "unix_ts"
    elif "iso_time" in our_df.columns and "iso_time" in tv_df.columns:
        merged = our_df.merge(tv_df, on="iso_time", suffixes=("_ours", "_tv"), how="inner")
        align_method = "iso_time"
    else:
        # Positional alignment
        min_len = min(len(our_df), len(tv_df))
        merged = pd.concat(
            [
                our_df.head(min_len).add_suffix("_ours"),
                tv_df.head(min_len).add_suffix("_tv"),
            ],
            axis=1,
        )
        align_method = "positional"

    matched_rows = len(merged)
    our_total = len(our_df)
    tv_total = len(tv_df)

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

        both_valid = our_vals.notna() & tv_vals.notna()
        valid_count = int(both_valid.sum())

        if valid_count == 0:
            field_reports.append(
                {
                    "field": display_name,
                    "valid_pairs": 0,
                    "our_nans": int(our_vals.isna().sum()),
                    "tv_nans": int(tv_vals.isna().sum()),
                }
            )
            continue

        diff = (our_vals[both_valid] - tv_vals[both_valid]).abs()
        pct_diff = (diff / tv_vals[both_valid].abs().clip(lower=1e-10)) * 100

        exact_count = int((pct_diff < _EXACT).sum())
        close_count = int(((pct_diff >= _EXACT) & (pct_diff < _CLOSE)).sum())
        ok_count = int(((pct_diff >= _CLOSE) & (pct_diff < _OK)).sum())
        bad_count = int((pct_diff >= _OK).sum())

        mean_abs_diff = float(diff.mean())
        max_abs_diff = float(diff.max())
        mean_pct_diff = float(pct_diff.mean())
        max_pct_diff = float(pct_diff.max())

        # Find top divergence points
        top_idx = pct_diff.nlargest(5).index
        for idx in top_idx:
            ts_col = "unix_ts" if "unix_ts" in merged.columns else "unix_ts_ours"
            merged.loc[idx, ts_col] if ts_col in merged.columns else idx
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

        field_reports.append(
            {
                "field": display_name,
                "valid_pairs": valid_count,
                "our_nans": int(our_vals.isna().sum()),
                "tv_nans": int(tv_vals.isna().sum()),
                "exact": exact_count,
                "close": close_count,
                "ok": ok_count,
                "divergent": bad_count,
                "exact_pct": round(exact_count / valid_count * 100, 2),
                "close_pct": round((exact_count + close_count) / valid_count * 100, 2),
                "mean_abs_diff": mean_abs_diff,
                "max_abs_diff": max_abs_diff,
                "mean_pct_diff": mean_pct_diff,
                "max_pct_diff": max_pct_diff,
            }
        )

    # Sort divergence points by pct_diff descending
    all_divergence_points.sort(key=lambda x: x["pct_diff"], reverse=True)
    top_divergences = all_divergence_points[:20]

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


def _build_markdown(
    ticker: str,
    our_total: int,
    tv_total: int,
    matched_rows: int,
    align_method: str,
    field_reports: list[dict[str, Any]],
    top_divergences: list[dict[str, Any]],
    our_cols: list[str],
    tv_cols: list[str],
) -> str:
    """Build the full markdown validation report."""
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")

    lines = [
        f"# Validation Report — {ticker}",
        "",
        f"**Generated:** {now}  ",
        "**Comparison:** pandas-ta (Polygon.io) vs TradingView CSV export  ",
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
        f"| Unmatched pandas-ta rows | {our_total - matched_rows:,} |",
        f"| Unmatched TradingView rows | {tv_total - matched_rows:,} |",
        f"| Match rate | {matched_rows / max(our_total, 1) * 100:.1f}% |",
        "",
    ]

    # Overall grade
    total_valid = sum(r.get("valid_pairs", 0) for r in field_reports)
    total_exact = sum(r.get("exact", 0) for r in field_reports)
    total_close = sum(r.get("close", 0) for r in field_reports)
    total_ok = sum(r.get("ok", 0) for r in field_reports)
    total_bad = sum(r.get("divergent", 0) for r in field_reports)

    if total_valid > 0:
        overall_exact_pct = total_exact / total_valid * 100
        overall_ok_pct = (total_exact + total_close + total_ok) / total_valid * 100
    else:
        overall_exact_pct = 0
        overall_ok_pct = 0

    lines += [
        "## 2. Overall Accuracy",
        "",
        "| Classification | Count | Percentage |",
        "|---------------|------:|----------:|",
        f"| Exact match (< {_EXACT}%) | {total_exact:,} | {total_exact / max(total_valid, 1) * 100:.2f}% |",
        f"| Close match (< {_CLOSE}%) | {total_close:,} | {total_close / max(total_valid, 1) * 100:.2f}% |",
        f"| Acceptable (< {_OK}%) | {total_ok:,} | {total_ok / max(total_valid, 1) * 100:.2f}% |",
        f"| **Divergent (≥ {_OK}%)** | **{total_bad:,}** | **{total_bad / max(total_valid, 1) * 100:.2f}%** |",
        f"| **Total compared** | **{total_valid:,}** | |",
        "",
        f"> **Overall grade:** {_grade(overall_exact_pct, overall_ok_pct)}",
        "",
    ]

    # Per-field table
    lines += [
        "## 3. Per-Field Accuracy",
        "",
        "| Field | Pairs | Exact | Close | OK | Divergent | Mean %Diff | Max %Diff | Max |Diff| |",
        "|-------|------:|------:|------:|---:|----------:|-----------:|----------:|----------:|",
    ]

    for r in sorted(field_reports, key=lambda x: x.get("max_pct_diff", 0), reverse=True):
        if r.get("valid_pairs", 0) == 0:
            lines.append(f"| {r['field']} | 0 | — | — | — | — | — | — | — |")
            continue
        lines.append(
            f"| {r['field']} "
            f"| {r['valid_pairs']:,} "
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
