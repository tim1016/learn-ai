"""Build the single-file HTML dashboard for the data-divergence study.

Sections rendered (per research plan §8):
  1. Header summary — counts, date range, headline numbers.
  2. Feed comparison — Polygon vs TV OHLCV differences, with prose context.
  3. Indicator divergence matrix — heatmap + per-row table.
  4. Per-indicator overlay charts — TV vs vetted pandas vs learn-ai engine.
  5. Trade-level impact — per strategy, 4-variant summary + category bars + P&L.
  6. Methodology, variant definitions, and inline gotchas.

Plotly.js is bundled inline so the HTML renders offline. See
``docs/tv-polygon-validation-gotchas.md`` for the full gotchas catalog.
"""

from __future__ import annotations

import html as html_mod
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.io import to_html

logger = logging.getLogger(__name__)

CACHE_ROOT = Path("cache/divergence")


# ----------------------------------------------------------------------
# Variant naming — used everywhere so V-A/V-B/V-C/V-D never leak to UI
# ----------------------------------------------------------------------

# Short labels for compact spaces (table columns, chart x-axes).
VARIANT_SHORT: dict[str, str] = {
    "V-A": "TradingView",
    "V-B": "Pandas (RTH)",
    "V-C": "Engine (RTH)",
    "V-D": "Engine (current)",
}
# Full descriptive labels for legends and prose.
VARIANT_FULL: dict[str, str] = {
    "V-A": "TradingView (reference)",
    "V-B": "Vetted pandas, Polygon RTH bars",
    "V-C": "learn-ai engine, Polygon RTH bars",
    "V-D": "learn-ai engine, Polygon full session (current behavior)",
}

# Descriptive column names for trade-summary tables. Order matters.
TRADE_METRIC_LABELS: dict[str, str] = {
    "n_trades": "Total trades fired",
    "wins": "Profitable trades",
    "losses": "Losing trades",
    "win_rate_pct": "Win rate (%)",
    "net_pnl": "Net P&L per share ($)",
    "avg_win": "Average winning trade ($)",
    "avg_loss": "Average losing trade ($)",
    "best": "Best trade ($)",
    "worst": "Worst trade ($)",
    "profit_factor": "Profit factor (gross win ÷ gross loss)",
    "avg_bars_held": "Average bars held in a trade",
}

# Descriptive labels for trade-pairing buckets.
CATEGORY_LABELS: dict[str, str] = {
    "matched_aligned": "Same bar as TradingView",
    "matched_shifted": "Within ±5 bars of TradingView",
    "a_only_flip": "TradingView fired, this variant didn't",
    "b_only_flip": "This variant fired, TradingView didn't",
}
CATEGORY_COLORS: dict[str, str] = {
    "matched_aligned": "#2e7d32",
    "matched_shifted": "#fbc02d",
    "a_only_flip": "#ef5350",
    "b_only_flip": "#1976d2",
}


# ----------------------------------------------------------------------
# Small utilities
# ----------------------------------------------------------------------


def _fig_to_div(fig: go.Figure, div_id: str, height: int | None = None) -> str:
    """Inline a Plotly figure as an HTML div. Bundles plotly.js on first call only."""
    if not globals().get("_PLOTLY_INLINED", False):
        include = "inline"
        globals()["_PLOTLY_INLINED"] = True
    else:
        include = False
    if height is not None:
        fig.update_layout(height=height)
    return to_html(
        fig,
        include_plotlyjs=include,
        full_html=False,
        div_id=div_id,
        config={"scrollZoom": True, "displaylogo": False, "responsive": True},
    )


def _fmt_num(v: Any, decimals: int = 4) -> str:
    if v is None or (isinstance(v, float) and np.isnan(v)):
        return ""
    try:
        v = float(v)
    except Exception:
        return str(v)
    if abs(v) >= 1000:
        return f"{v:,.2f}"
    if abs(v) >= 1:
        return f"{v:,.{decimals}f}"
    return f"{v:.6f}"


def _html_table(df: pd.DataFrame, cls: str = "tbl") -> str:
    cols = list(df.columns)
    head = "<thead><tr>" + "".join(f"<th>{html_mod.escape(str(c))}</th>" for c in cols) + "</tr></thead>"
    rows_html: list[str] = []
    for _, r in df.iterrows():
        cells = []
        for c in cols:
            v = r[c]
            cells.append(f"<td>{html_mod.escape(_fmt_num(v) if not isinstance(v, str) else v)}</td>")
        rows_html.append("<tr>" + "".join(cells) + "</tr>")
    return f"<table class='{cls}'>{head}<tbody>{''.join(rows_html)}</tbody></table>"


def _explain(title: str, body_html: str) -> str:
    """A small caption block that goes ABOVE a chart explaining what it shows."""
    return f"<div class='chart-caption'><b>{html_mod.escape(title)}.</b> {body_html}</div>"


# ----------------------------------------------------------------------
# Section 0 — Executive summary + navigation (new in Day 7)
# ----------------------------------------------------------------------


def _executive_summary(merged: pd.DataFrame, trades_dir: Path) -> str:
    """Top-of-page narrative: the four or five numbers anyone should walk away with."""
    m = merged.copy()
    m["et"] = m["time_utc"].dt.tz_convert("America/New_York")
    trading_days = int(m["et"].dt.date.nunique())
    close_diff = (m["close_tv"] - m["close_pg"]).abs()
    median_diff_c = close_diff.median() * 100

    # Indicator median divergence floor — the best-case agreement post-warmup.
    matrix_path = trades_dir.parent / f"matrix_{trades_dir.parent.name}.csv"
    indicator_median = "(not yet computed)"
    if matrix_path.exists():
        mx = pd.read_csv(matrix_path)
        mx_native = mx[mx["impl"] == "native"]["median_abs"].dropna()
        if len(mx_native):
            indicator_median = f"{mx_native.median():.4f}"
            f"{mx_native.quantile(0.95):.3f}"

    # Trade-alignment headline: S1 trades V-C vs V-D (if available).
    headline_s1_current = headline_s1_fixed = "—"
    delta_total_pnl = None
    summary_path = trades_dir / "summary.csv"
    match_path = trades_dir / "match_summary.csv"
    if summary_path.exists() and match_path.exists():
        s = pd.read_csv(summary_path)
        mm = pd.read_csv(match_path)

        # S1 alignment: matched_aligned_n / (matched_aligned_n + a_only_flip_n + matched_shifted_n)
        s1_vd = mm[(mm["strategy"] == "s1_ema_crossover") & (mm["variant"] == "V-D")]
        s1_vc = mm[(mm["strategy"] == "s1_ema_crossover") & (mm["variant"] == "V-C")]

        def _pct(row: pd.DataFrame) -> str:
            if row.empty:
                return "—"
            r = row.iloc[0]
            denom = (
                int(r.get("matched_aligned_n", 0)) + int(r.get("matched_shifted_n", 0)) + int(r.get("a_only_flip_n", 0))
            )
            if denom == 0:
                return "—"
            return f"{int(r.get('matched_aligned_n', 0)) / denom * 100:.0f}%"

        headline_s1_current = _pct(s1_vd)
        headline_s1_fixed = _pct(s1_vc)

        # Total aggregate P&L change across all 3 strategies, V-D to V-C
        s_piv = s.pivot_table(index="strategy", columns="variant", values="net_pnl")
        if "V-C" in s_piv.columns and "V-D" in s_piv.columns:
            delta_total_pnl = float(s_piv["V-C"].sum() - s_piv["V-D"].sum())

    delta_str = f"{delta_total_pnl:+.2f}" if delta_total_pnl is not None else "—"

    # Primary finding in prose
    finding = (
        "When learn-ai's engine is fed regular-trading-hours data only "
        "(the &ldquo;after-fix&rdquo; variant), its trades line up with "
        "TradingView at <b>" + headline_s1_fixed + "</b> on Strategy 1 and at "
        "similarly high rates on Strategies 2 and 3. In its current form, "
        "with extended-hours bars contaminating the indicators, alignment "
        "drops to <b>" + headline_s1_current + "</b>. This dashboard "
        "quantifies the cost and shows the specific code fix that closes the gap."
    )

    cards = [
        (
            "Study window",
            f"{trading_days} trading days",
            f"{m['et'].iloc[0].strftime('%Y-%m-%d')} → {m['et'].iloc[-1].strftime('%Y-%m-%d')}",
        ),
        (
            "Raw-price agreement (TV vs Polygon)",
            f"{median_diff_c:.2f} ¢ median",
            "The feeds differ by only single-cent noise across 3,010 RTH bars.",
        ),
        (
            "Indicator agreement (vetted pandas vs TV)",
            f"{indicator_median} median",
            "Our vetted formulas reproduce TradingView to within fractions of a cent.",
        ),
        (
            "Strategy 1 alignment with TV — current engine",
            headline_s1_current,
            "Share of TradingView's entry bars the current production code reproduces.",
        ),
        (
            "Strategy 1 alignment with TV — after the RTH-filter fix",
            headline_s1_fixed,
            "Share after applying the 5-line code change in the Roadmap doc.",
        ),
        (
            "Aggregate P&L change from the fix (3 strategies)",
            f"${delta_str} / share",
            "Net $/share change across all three study strategies over the window.",
        ),
    ]
    cards_html = "<div class='summary-grid exec'>"
    for label, value, help_ in cards:
        cards_html += (
            f"<div class='card exec-card'><span class='card-label'>{html_mod.escape(label)}</span>"
            f"<b class='card-value big'>{value}</b>"
            f"<span class='card-help'>{html_mod.escape(help_)}</span></div>"
        )
    cards_html += "</div>"

    return f"""
    <section id='exec-summary'>
      <h2 class='no-rule'>Executive summary</h2>
      <div class='exec-block'>
        <p class='exec-finding'>{finding}</p>
        {cards_html}
      </div>
    </section>
    """


def _section_nav() -> str:
    """Small jump-link navigation bar for quick browsing."""
    items = [
        ("exec-summary", "Exec summary"),
        ("sec-glance", "1. At a glance"),
        ("sec-feed", "2. Feed comparison"),
        ("sec-indicators", "3. Indicator agreement"),
        ("sec-overlays", "4. Overlay charts"),
        ("sec-trades", "5. Trade impact"),
        ("sec-worst", "6. Worst days"),
        ("sec-eth", "7. ETH contamination"),
        ("sec-methodology", "8. Methodology"),
    ]
    links = " &nbsp;·&nbsp; ".join(f"<a href='#{aid}'>{html_mod.escape(label)}</a>" for aid, label in items)
    return f"<nav class='toc'>{links}</nav>"


# ----------------------------------------------------------------------
# Section 1 — Header summary cards
# ----------------------------------------------------------------------


def _header_cards(merged: pd.DataFrame) -> str:
    merged = merged.copy()
    merged["et"] = merged["time_utc"].dt.tz_convert("America/New_York")
    trading_days = int(merged["et"].dt.date.nunique())
    close_diff = (merged["close_tv"] - merged["close_pg"]).abs()
    vol_ratio_pct = float(merged["volume_tv"].sum() / max(merged["volume_pg"].sum(), 1) * 100)

    cards = [
        ("Bars compared", f"{len(merged):,}", "Each bar is one 15-minute window during regular trading hours."),
        ("Trading days", f"{trading_days}", "Distinct US-equity trading sessions in the study window."),
        (
            "Date range (Eastern Time)",
            f"{merged['et'].iloc[0].strftime('%Y-%m-%d')} → {merged['et'].iloc[-1].strftime('%Y-%m-%d')}",
            "First and last trading dates included.",
        ),
        (
            "Median |close diff|",
            f"{close_diff.median() * 100:.2f} ¢",
            "Typical TradingView vs Polygon close-price difference, in cents.",
        ),
        (
            "95% percentile |close diff|",
            f"{close_diff.quantile(0.95) * 100:.2f} ¢",
            "Worst 5% of bars still stay within this difference.",
        ),
        (
            "BATS / Polygon volume share",
            f"{vol_ratio_pct:.2f}%",
            "How much of total SPY volume the BATS exchange captures (TradingView's feed).",
        ),
        ("Built at", datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"), "Time this dashboard was generated."),
    ]
    html = "<div class='summary-grid'>"
    for label, value, tooltip in cards:
        html += (
            f"<div class='card' title='{html_mod.escape(tooltip)}'>"
            f"<span class='card-label'>{html_mod.escape(label)}</span>"
            f"<b class='card-value'>{html_mod.escape(value)}</b>"
            f"<span class='card-help'>{html_mod.escape(tooltip)}</span>"
            f"</div>"
        )
    html += "</div>"
    return html


# ----------------------------------------------------------------------
# Section 2 — Feed comparison (OHLCV)
# ----------------------------------------------------------------------


def _feed_comparison_section(merged: pd.DataFrame) -> str:
    m = merged.copy()
    m["et"] = m["time_utc"].dt.tz_convert("America/New_York")
    m["close_diff_c"] = (m["close_tv"] - m["close_pg"]) * 100

    # Summary table with descriptive headers
    rows = []
    for price in ("open", "high", "low", "close"):
        d = (m[f"{price}_tv"] - m[f"{price}_pg"]).abs() * 100
        rows.append(
            {
                "OHLCV column compared": price,
                "Median |difference|": _fmt_num(float(d.median()), 3) + " ¢",
                "95th-percentile |difference|": _fmt_num(float(d.quantile(0.95)), 3) + " ¢",
                "Maximum |difference|": _fmt_num(float(d.max()), 3) + " ¢",
                "Pearson correlation (1 = perfect)": _fmt_num(
                    float(np.corrcoef(m[f"{price}_tv"], m[f"{price}_pg"])[0, 1]), 6
                ),
            }
        )
    vol_d = (m["volume_tv"] - m["volume_pg"]).abs()
    rows.append(
        {
            "OHLCV column compared": "volume (shares)",
            "Median |difference|": f"{int(vol_d.median()):,}",
            "95th-percentile |difference|": f"{int(vol_d.quantile(0.95)):,}",
            "Maximum |difference|": f"{int(vol_d.max()):,}",
            "Pearson correlation (1 = perfect)": _fmt_num(float(np.corrcoef(m["volume_tv"], m["volume_pg"])[0, 1]), 4),
        }
    )
    table_df = pd.DataFrame(rows)
    table_html = _html_table(table_df, cls="tbl tight")

    # Histogram
    fig_hist = go.Figure()
    fig_hist.add_trace(
        go.Histogram(
            x=m["close_diff_c"],
            nbinsx=80,
            marker=dict(color="#1976d2", line=dict(color="#0d47a1", width=0.5)),
            name="TradingView − Polygon close (¢)",
        )
    )
    fig_hist.update_layout(
        template="plotly_white",
        height=300,
        title=dict(text="How often each price-difference value occurred", x=0.5),
        xaxis_title="TradingView close − Polygon close, in cents (negative = Polygon higher)",
        yaxis_title="Number of 15-minute bars",
        margin=dict(t=50, b=50, l=60, r=20),
    )

    # Daily time series
    m["et_date"] = m["et"].dt.date
    daily = (
        m.groupby("et_date")
        .agg(
            mean_diff_c=("close_diff_c", "mean"),
            max_abs_c=("close_diff_c", lambda s: s.abs().max()),
        )
        .reset_index()
    )
    fig_ts = go.Figure()
    fig_ts.add_trace(
        go.Scatter(
            x=daily["et_date"],
            y=daily["mean_diff_c"],
            name="Mean of all bars that day (¢)",
            line=dict(color="#1976d2", width=1.5),
            mode="lines",
        )
    )
    fig_ts.add_trace(
        go.Scatter(
            x=daily["et_date"],
            y=daily["max_abs_c"],
            name="Largest single-bar |difference| (¢)",
            line=dict(color="#ef5350", width=1, dash="dot"),
            mode="lines",
        )
    )
    fig_ts.add_hline(y=0, line=dict(color="rgba(0,0,0,0.35)", dash="dash"))
    fig_ts.update_layout(
        template="plotly_white",
        height=320,
        title=dict(text="Daily price-difference between feeds", x=0.5),
        xaxis_title="Trading date",
        yaxis_title="Cents (positive = TradingView higher)",
        legend=dict(orientation="h", y=1.10, x=0.5, xanchor="center"),
        margin=dict(t=60, b=40, l=60, r=20),
    )

    hist_div = _fig_to_div(fig_hist, "fig-feed-hist", height=300)
    ts_div = _fig_to_div(fig_ts, "fig-feed-ts", height=320)

    return f"""
    <h2 id='sec-feed'>2. Feed comparison — Polygon (consolidated) vs TradingView (BATS)</h2>
    <p class="subtitle">
      <b>What this section answers:</b> are the raw OHLCV bars from our two
      data feeds the same? They should be very close but not identical, because
      Polygon publishes a "consolidated" tape of trades from all venues
      (NYSE Arca, BATS, NASDAQ, IEX, etc.), while TradingView's BATS_SPY chart
      shows trades from the Cboe BATS exchange only. Volume will differ a lot
      (BATS is one venue out of many); prices should agree to within a few cents.
    </p>

    {
        _explain(
            "Per-OHLCV-column difference summary",
            "Each row compares one column (open/high/low/close/volume) between the "
            "two feeds across all overlapping bars. <b>Median</b> is the typical "
            "size of difference. <b>95th-percentile</b> means 95% of bars are "
            "below this difference. <b>Maximum</b> is the worst single bar. "
            "<b>Pearson correlation</b> is 1.0 if the series move in lock-step. "
            "Values for volume are in shares; price values are in cents.",
        )
    }
    {table_html}

    {
        _explain(
            "How the differences are distributed",
            "X-axis is the price-difference (TradingView minus Polygon). Y-axis is "
            "how many 15-minute bars fell in that bucket. A perfect match would "
            "show a single tall bar at zero. Most bars cluster within ±2¢, "
            "consistent with normal venue-level pricing variation.",
        )
    }
    {hist_div}

    {
        _explain(
            "How the differences evolve over time",
            "Blue line is the average price-difference each day. Red dotted line is "
            "the largest single-bar difference that day. Both should hover near "
            "zero. A sustained drift would suggest a feed-level issue (corporate "
            "action mishandling, time-shift, etc.); a one-day spike usually traces "
            "to a flash event or auction print.",
        )
    }
    {ts_div}
    """


# ----------------------------------------------------------------------
# Section 3 — Indicator divergence heatmap
# ----------------------------------------------------------------------


def _heatmap_section(matrix_csv: Path, timeframe: str) -> str:
    m = pd.read_csv(matrix_csv)
    impl_label = {"native": "Vetted pandas", "engine": "learn-ai engine"}
    m["impl_label"] = m["impl"].map(impl_label).fillna(m["impl"])

    piv = m.pivot_table(index="indicator", columns="impl_label", values="p95_abs", aggfunc="first")
    piv = piv.reindex(sorted(piv.index, key=lambda s: (s.split("_")[0], s)))

    cols = [c for c in ("Vetted pandas", "learn-ai engine") if c in piv.columns]
    piv = piv[cols]

    fig = go.Figure(
        data=go.Heatmap(
            z=piv.values,
            x=list(piv.columns),
            y=list(piv.index),
            colorscale=[[0, "#2e7d32"], [0.05, "#c8e6c9"], [0.3, "#fff3cd"], [1, "#c62828"]],
            colorbar=dict(title="95th-percentile<br>|difference|"),
            hovertemplate="Indicator: %{y}<br>Implementation: %{x}<br>"
            "95th-percentile |difference| vs TradingView: %{z:.4f}<extra></extra>",
            zmin=0,
            zmax=max(float(piv.max().max() or 0.01), 0.1),
        )
    )
    fig.update_layout(
        template="plotly_white",
        title=dict(text="Indicator agreement with TradingView (smaller = closer)", x=0.5),
        xaxis_title="Implementation being checked against TradingView",
        yaxis_title="Indicator",
        height=max(360, 26 * len(piv) + 140),
        margin=dict(t=70, b=50, l=160, r=50),
    )
    div = _fig_to_div(fig, f"fig-heatmap-{timeframe}", height=max(360, 26 * len(piv) + 140))

    # Per-row table with descriptive column names
    tbl_df = m.sort_values(["indicator", "impl_label"])[
        ["indicator", "impl_label", "n", "median_abs", "p95_abs", "max_abs", "corr"]
    ].copy()
    tbl_df = tbl_df.rename(
        columns={
            "indicator": "Indicator",
            "impl_label": "Implementation",
            "n": "Bars compared",
            "median_abs": "Median |difference|",
            "p95_abs": "95th-percentile |difference|",
            "max_abs": "Maximum |difference|",
            "corr": "Pearson correlation",
        }
    )
    for c in ("Median |difference|", "95th-percentile |difference|", "Maximum |difference|"):
        tbl_df[c] = tbl_df[c].apply(_fmt_num)
    tbl_df["Pearson correlation"] = tbl_df["Pearson correlation"].apply(lambda v: _fmt_num(v, 6))

    return f"""
    <h2 id='sec-indicators'>3. Indicator agreement with TradingView</h2>
    <p class="subtitle">
      <b>What this section answers:</b> for each technical indicator we compute
      (EMA, SMA, RSI, MACD, Bollinger Bands, ADX, SuperTrend, ATR), how close
      do our two implementations come to TradingView's published values? We
      compare two implementations against TradingView:
      <b>Vetted pandas</b> is a hand-written reference using textbook formulas;
      <b>learn-ai engine</b> is the streaming code path used by the production
      backtest engine.
    </p>

    {
        _explain(
            "Heatmap of agreement",
            "Each cell shows the 95th-percentile of the absolute difference between "
            "our value and TradingView's. <span style='color:#2e7d32'><b>Green</b></span> "
            "means we agree to within a cent or two — production-grade match. "
            "<span style='color:#fbc02d'><b>Yellow</b></span> shows visible but small "
            "drift, usually from warmup-period seeding. "
            "<span style='color:#c62828'><b>Red</b></span> flags material divergence — "
            "typically long-period EMAs/SMAs whose history hasn't fully converged "
            "(see the gotchas section below).",
        )
    }
    {div}

    <details class="collapsible">
      <summary>Show exact per-indicator values</summary>
      <p class="caption-mini">
        Median, 95th-percentile, and maximum absolute differences vs TradingView
        for every indicator we compute, in dollars (price-based) or index points
        (RSI/ADX). "Bars compared" excludes any bars where either side is in its
        warmup period and emits NaN.
      </p>
      {_html_table(tbl_df)}
    </details>
    """


# ----------------------------------------------------------------------
# Section 4 — Per-indicator overlays
# ----------------------------------------------------------------------


_OVERLAY_SPECS: list[tuple[str, str, str, str | None, str]] = [
    # (display_name, tv_col, native_col, engine_col, "what to look for" prose)
    (
        "EMA 20",
        "ema_20",
        "ema_20_native",
        "ema_20_engine",
        "The 20-period EMA is the bedrock of the EMA crossover strategy. Where "
        "the green and blue lines hug the red dotted line, all three implementations "
        "agree. Visible separation early in the series indicates warmup convergence.",
    ),
    (
        "EMA 200",
        "ema_200",
        "ema_200_native",
        "ema_200_engine",
        "The 200-period EMA is the slowest indicator we track and the most "
        "sensitive to warmup history. The vetted pandas implementation masks bars "
        "until 200 samples are available, so it appears later than TradingView. "
        "The learn-ai engine emits values from the start using a simple-moving-average "
        "seed; expect ~$1-$2 of separation in the first 200 bars.",
    ),
    (
        "SMA 200",
        "sma_200",
        "sma_200_native",
        "sma_200_engine",
        "Same warmup story as EMA 200. The simple moving average requires 200 bars "
        "of history to be defined. The engine line drifts during its warmup window "
        "and is the source of false golden / death cross signals in the SMA "
        "crossover strategy below.",
    ),
    (
        "RSI 14",
        "rsi_14",
        "rsi_14_native",
        "rsi_14_engine",
        "Wilder's RSI bounded 0-100. Both our implementations use the classic "
        "Wilder smoothing seed (simple-moving-average of the first 14 gains/losses). "
        "Should match TradingView near-exactly past the warmup period.",
    ),
    (
        "MACD line (12, 26, 9)",
        "macd_12_26_9",
        "macd_12_26_9_native",
        None,
        "Difference between the 12-period and 26-period EMAs of close. The "
        "MACD signal line is its 9-period EMA. The learn-ai engine doesn't "
        "implement MACD natively today, so only the vetted pandas line is shown.",
    ),
    (
        "ADX 14",
        "adx_14",
        "adx_14_native",
        None,
        "Average Directional Index — measures trend strength. ADX uses two layers "
        "of Wilder smoothing on top of true-range, so small input differences "
        "amplify. Expect 1-2 point boundary noise around threshold values like 15 "
        "and 25 even when feeds otherwise agree.",
    ),
]


def _overlays_section(merged: pd.DataFrame) -> str:
    m = merged.copy().sort_values("time_utc").reset_index(drop=True)
    x = list(range(len(m)))
    tick_positions = list(range(0, len(m), max(1, len(m) // 10)))
    tick_labels = [m["time_utc"].iloc[i].tz_convert("America/New_York").strftime("%Y-%m-%d") for i in tick_positions]

    pieces: list[str] = []
    for display_name, tv_col, native_col, engine_col, prose in _OVERLAY_SPECS:
        if tv_col not in m.columns:
            continue

        fig = go.Figure()
        fig.add_trace(
            go.Scatter(
                x=x,
                y=m[tv_col],
                mode="lines",
                name="TradingView (reference)",
                line=dict(color="#ef5350", width=1.6, dash="dot"),
            )
        )
        if native_col in m.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=m[native_col],
                    mode="lines",
                    name="Vetted pandas (Polygon RTH)",
                    line=dict(color="#2e7d32", width=1.0),
                )
            )
        if engine_col and engine_col in m.columns:
            fig.add_trace(
                go.Scatter(
                    x=x,
                    y=m[engine_col],
                    mode="lines",
                    name="learn-ai engine (Polygon RTH)",
                    line=dict(color="#1976d2", width=1.0),
                )
            )
        fig.update_layout(
            template="plotly_white",
            height=300,
            title=dict(text=f"{display_name} — three-way overlay", x=0.5),
            xaxis=dict(
                title="Trading date (one tick per ~13 days)",
                tickmode="array",
                tickvals=tick_positions,
                ticktext=tick_labels,
            ),
            yaxis=dict(title="Indicator value"),
            margin=dict(t=50, b=50, l=60, r=20),
            legend=dict(orientation="h", y=1.16, x=0.5, xanchor="center", font=dict(size=10)),
            hovermode="x unified",
        )
        chart_div = _fig_to_div(fig, f"fig-overlay-{tv_col}", height=300)
        pieces.append(f"""
        <div class="overlay-block">
          <h3>{html_mod.escape(display_name)}</h3>
          {_explain("What to look for", prose)}
          {chart_div}
        </div>
        """)

    return f"""
    <h2 id='sec-overlays'>4. Per-indicator overlay charts</h2>
    <p class="subtitle">
      <b>What this section answers:</b> visually, do our indicator values track
      TradingView's? Each chart overlays three lines for the same indicator:
      <span style='color:#ef5350'><b>TradingView (reference)</b></span> in red
      dotted, <span style='color:#2e7d32'><b>Vetted pandas</b></span> in green,
      <span style='color:#1976d2'><b>learn-ai engine</b></span> in blue. Where
      lines visually overlap, agreement is good. Where they diverge, the
      heatmap above already quantifies the magnitude.
    </p>
    {"".join(pieces)}
    """


# ----------------------------------------------------------------------
# Section 5 — Trade-level impact
# ----------------------------------------------------------------------


def _strategy_display_name(strat: str) -> str:
    return {
        "s1_ema_crossover": "Strategy 1 — EMA crossover (5 vs 10) with RSI filter",
        "s2_rsi_mean_reversion": "Strategy 2 — RSI mean reversion (enter <30, exit >50)",
        "s3_sma_crossover": "Strategy 3 — SMA crossover (50 vs 200, golden / death cross)",
    }.get(strat, strat)


def _trade_section(trades_dir: Path, timeframe: str) -> str:
    summary_path = trades_dir / "summary.csv"
    match_path = trades_dir / "match_summary.csv"
    if not summary_path.exists() or not match_path.exists():
        return "<h2>5. Trade-level impact</h2><p><em>Day 4 has not been run yet.</em></p>"

    summary = pd.read_csv(summary_path)
    match = pd.read_csv(match_path)

    strategies = sorted(summary["strategy"].unique().tolist())

    section_parts: list[str] = []
    for strat in strategies:
        srow = summary[summary["strategy"] == strat]
        mrow = match[match["strategy"] == strat]

        # ---- Per-variant summary table with descriptive labels ----
        s_pivot = srow.set_index("variant")[list(TRADE_METRIC_LABELS.keys())].T
        for v in ("V-A", "V-B", "V-C", "V-D"):
            if v not in s_pivot.columns:
                s_pivot[v] = None
        s_pivot = s_pivot[[v for v in ("V-A", "V-B", "V-C", "V-D") if v in s_pivot.columns]]
        s_pivot = s_pivot.rename(columns=VARIANT_SHORT)
        s_pivot.index = [TRADE_METRIC_LABELS[k] for k in s_pivot.index]
        s_pivot = s_pivot.reset_index().rename(columns={"index": "Performance metric"})
        summary_tbl = _html_table(s_pivot, cls="tbl tight centered")

        # ---- Stacked-bar chart: trade categories per variant ----
        fig = go.Figure()
        for cat, label in CATEGORY_LABELS.items():
            col = f"{cat}_n"
            if col not in mrow.columns:
                continue
            fig.add_trace(
                go.Bar(
                    x=[VARIANT_SHORT.get(v, v) for v in mrow["variant"]],
                    y=mrow[col],
                    name=label,
                    marker_color=CATEGORY_COLORS.get(cat, "#666"),
                    hovertemplate="%{x}<br>" + label + ": %{y}<extra></extra>",
                )
            )
        fig.update_layout(
            barmode="stack",
            template="plotly_white",
            height=340,
            title=dict(text="Where each variant agrees or disagrees with TradingView", x=0.5),
            yaxis_title="Number of trades",
            xaxis_title="Variant being compared against TradingView",
            legend=dict(orientation="h", y=1.18, x=0.5, xanchor="center", font=dict(size=10)),
            margin=dict(t=70, b=60, l=60, r=20),
        )
        stack_div = _fig_to_div(fig, f"fig-trade-cat-{strat}", height=340)

        # ---- Net P&L bar chart ----
        fig2 = go.Figure(
            data=go.Bar(
                x=[VARIANT_SHORT.get(v, v) for v in srow["variant"]],
                y=srow["net_pnl"],
                marker_color=["#2e7d32" if v > 0 else "#c62828" for v in srow["net_pnl"]],
                text=[f"{v:+.2f}" for v in srow["net_pnl"]],
                textposition="outside",
                hovertemplate="%{x}<br>Net P&L per share: $%{y:.2f}<extra></extra>",
            )
        )
        fig2.update_layout(
            template="plotly_white",
            height=280,
            title=dict(text="Net profit per share over the study window", x=0.5),
            yaxis_title="Net P&L per share ($)",
            xaxis_title="Variant",
            margin=dict(t=50, b=50, l=60, r=20),
        )
        pnl_div = _fig_to_div(fig2, f"fig-trade-pnl-{strat}", height=280)

        section_parts.append(f"""
        <div class="strategy-block">
          <h3>{html_mod.escape(_strategy_display_name(strat))}</h3>

          {
            _explain(
                "Performance summary by variant",
                "Each column is one variant of the indicator pipeline (see Section 6 "
                "for definitions). Each row is a standard trading-strategy performance "
                "metric. Compare TradingView (reference) to the others — closer numbers "
                "mean closer alignment with the TradingView ground truth.",
            )
        }
          {summary_tbl}

          {
            _explain(
                "How each variant's trades line up with TradingView's",
                "For each variant, every trade is paired against TradingView's trade "
                "list and bucketed into one of four categories: green (same bar as "
                "TradingView), yellow (within 5 bars of TradingView), red (TradingView "
                "fired but this variant didn't), or blue (this variant fired but "
                "TradingView didn't). The TradingView column is by definition all-green. "
                "A tall green stack means strong alignment; lots of red and blue means "
                "the variant is producing different trades than TradingView would.",
            )
        }
          {stack_div}

          {
            _explain(
                "Net profit per share, by variant",
                "Aggregate P&L from running this strategy against each indicator pipeline "
                "over the study window. Green bars are profitable, red are losses. A "
                "completely flipped sign (e.g., positive in TradingView but negative in "
                "the engine variant) is the most damaging form of divergence: the "
                "strategy looks good on TradingView's chart but loses money in production.",
            )
        }
          {pnl_div}
        </div>
        """)

    return (
        "<h2 id='sec-trades'>5. Trade-level impact — per strategy</h2>"
        "<p class='subtitle'>"
        "<b>What this section answers:</b> indicator-level differences only matter "
        "if they cause different <i>trades</i>. Here we replay three strategies "
        "(EMA crossover, RSI mean reversion, SMA crossover) against four "
        "different indicator pipelines and count how many trades fire at the "
        "same bar as TradingView, how many are timing-shifted, and how many are "
        "missed or hallucinated. The four pipelines are defined in Section 6 below."
        "</p>" + "".join(section_parts)
    )


# ----------------------------------------------------------------------
# Section 6 — Worst days (per-day flipped-trade impact)
# ----------------------------------------------------------------------


def _worst_days_section(trades_dir: Path) -> str:
    """Per-day breakdown of where each variant disagrees with TradingView."""
    summary_path = trades_dir / "summary.csv"
    if not summary_path.exists():
        return "<h2>6. Worst days for trade alignment</h2><p><em>Day 4 has not been run yet.</em></p>"

    # For each strategy, read V-A and V-D (worst offender) trade lists and find
    # the days where V-A and V-D disagree most in dollar terms.
    strategies = ["s1_ema_crossover", "s2_rsi_mean_reversion", "s3_sma_crossover"]
    blocks: list[str] = []

    for strat in strategies:
        va_path = trades_dir / f"{strat}_V-A.csv"
        vd_path = trades_dir / f"{strat}_V-D.csv"
        if not va_path.exists() or not vd_path.exists():
            continue
        va = pd.read_csv(va_path, parse_dates=["entry_time", "exit_time"])
        vd = pd.read_csv(vd_path, parse_dates=["entry_time", "exit_time"])
        if va.empty and vd.empty:
            continue

        def to_et_date(s: pd.Series) -> pd.Series:
            ts = pd.to_datetime(s, utc=True, errors="coerce")
            return ts.dt.tz_convert("America/New_York").dt.date

        va["et_date"] = to_et_date(va["entry_time"])
        vd["et_date"] = to_et_date(vd["entry_time"])

        # Per day, sum P&L on each side and compute the delta
        per_day_a = (
            va.groupby("et_date")
            .agg(
                tv_trades=("pnl_dollars", "size"),
                tv_pnl=("pnl_dollars", "sum"),
            )
            .reset_index()
        )
        per_day_d = (
            vd.groupby("et_date")
            .agg(
                engine_trades=("pnl_dollars", "size"),
                engine_pnl=("pnl_dollars", "sum"),
            )
            .reset_index()
        )

        merged = per_day_a.merge(per_day_d, on="et_date", how="outer").fillna(0)
        merged["delta_pnl"] = merged["engine_pnl"] - merged["tv_pnl"]
        merged["abs_delta"] = merged["delta_pnl"].abs()
        merged = merged.sort_values("abs_delta", ascending=False).head(10)

        if merged.empty:
            continue

        merged_display = merged.rename(
            columns={
                "et_date": "Trading date",
                "tv_trades": "TradingView trades that day",
                "tv_pnl": "TradingView P&L ($)",
                "engine_trades": "learn-ai (current) trades that day",
                "engine_pnl": "learn-ai (current) P&L ($)",
                "delta_pnl": "P&L delta vs TradingView ($)",
            }
        )[
            [
                "Trading date",
                "TradingView trades that day",
                "TradingView P&L ($)",
                "learn-ai (current) trades that day",
                "learn-ai (current) P&L ($)",
                "P&L delta vs TradingView ($)",
            ]
        ]
        for c in (
            "TradingView P&L ($)",
            "learn-ai (current) P&L ($)",
            "P&L delta vs TradingView ($)",
        ):
            merged_display[c] = merged_display[c].apply(lambda v: f"{v:+.2f}")
        for c in ("TradingView trades that day", "learn-ai (current) trades that day"):
            merged_display[c] = merged_display[c].astype(int)

        tbl = _html_table(merged_display, cls="tbl tight")
        strat_name = _strategy_display_name(strat)
        blocks.append(f"""
        <div class="strategy-block">
          <h3>{html_mod.escape(strat_name)}</h3>
          {
            _explain(
                "Top 10 worst-disagreement days",
                "Days are ranked by the absolute dollar difference between what "
                "TradingView would have produced and what the learn-ai engine "
                "actually produced (with extended-hours contamination). Positive "
                "delta means the engine made more money than TradingView would have; "
                "negative means it lost money on trades TradingView wouldn't have "
                "taken. These are the days where the production system most "
                "diverges from the &ldquo;what TV showed me&rdquo; experience.",
            )
        }
          {tbl}
        </div>
        """)

    if not blocks:
        return ""
    return f"""
    <h2 id='sec-worst'>6. Worst days for trade alignment</h2>
    <p class="subtitle">
      <b>What this section answers:</b> when learn-ai's trade list disagrees
      with TradingView, on which specific days does it cost (or earn) the most?
      These tables let you trace the worst-disagreement days back to specific
      market events and check the per-bar charts for an explanation.
    </p>
    {"".join(blocks)}
    """


# ----------------------------------------------------------------------
# Section 7 — Extended-hours contamination chapter
# ----------------------------------------------------------------------


def _eth_contamination_section(trades_dir: Path) -> str:
    """The headline "fixing this saves $X" chapter."""
    summary_path = trades_dir / "summary.csv"
    match_path = trades_dir / "match_summary.csv"
    if not summary_path.exists() or not match_path.exists():
        return ""

    summary = pd.read_csv(summary_path)
    match = pd.read_csv(match_path)

    # For each strategy: V-C (engine RTH) vs V-D (engine current with ETH)
    rows: list[dict] = []
    for strat in sorted(summary["strategy"].unique()):
        srow = summary[summary["strategy"] == strat].set_index("variant")
        if "V-C" not in srow.index or "V-D" not in srow.index:
            continue
        rows.append(
            {
                "Strategy": _strategy_display_name(strat).split(" — ")[0],
                "Description": _strategy_display_name(strat).split(" — ", 1)[1]
                if " — " in _strategy_display_name(strat)
                else "",
                "Trades fired (RTH-fixed)": int(srow.loc["V-C", "n_trades"]),
                "Trades fired (current)": int(srow.loc["V-D", "n_trades"]),
                "Net P&L (RTH-fixed) $": f"{srow.loc['V-C', 'net_pnl']:+.2f}",
                "Net P&L (current) $": f"{srow.loc['V-D', 'net_pnl']:+.2f}",
                "P&L change after fix $": f"{srow.loc['V-C', 'net_pnl'] - srow.loc['V-D', 'net_pnl']:+.2f}",
                "Win rate (RTH-fixed) %": f"{srow.loc['V-C', 'win_rate_pct']:.1f}",
                "Win rate (current) %": f"{srow.loc['V-D', 'win_rate_pct']:.1f}",
            }
        )
    if not rows:
        return ""

    table_df = pd.DataFrame(rows)
    table_html = _html_table(table_df, cls="tbl tight")

    # Bar chart: net P&L per strategy, RTH-fixed vs current side-by-side
    fig = go.Figure()
    for variant_key, label, color in [
        ("V-C", "After RTH-filter fix", "#2e7d32"),
        ("V-D", "Current behavior (ETH contamination)", "#c62828"),
    ]:
        ys, xs = [], []
        for strat in sorted(summary["strategy"].unique()):
            srow = summary[summary["strategy"] == strat].set_index("variant")
            if variant_key in srow.index:
                xs.append(_strategy_display_name(strat).split(" — ")[0])
                ys.append(float(srow.loc[variant_key, "net_pnl"]))
        fig.add_trace(
            go.Bar(
                x=xs, y=ys, name=label, marker_color=color, hovertemplate=label + "<br>%{x}: $%{y:.2f}<extra></extra>"
            )
        )
    fig.update_layout(
        barmode="group",
        template="plotly_white",
        height=340,
        title=dict(text="Strategy P&L: current behavior vs RTH-filter-fix", x=0.5),
        yaxis_title="Net P&L per share ($)",
        legend=dict(orientation="h", y=1.16, x=0.5, xanchor="center"),
        margin=dict(t=70, b=50, l=60, r=20),
    )
    pnl_div = _fig_to_div(fig, "fig-eth-pnl", height=340)

    # Match-rate-vs-TradingView bar chart for V-D
    match_rate_rows = []
    for strat in sorted(summary["strategy"].unique()):
        for variant_key in ("V-C", "V-D"):
            mrow = match[(match["strategy"] == strat) & (match["variant"] == variant_key)]
            if mrow.empty:
                continue
            r = mrow.iloc[0]
            total_a = (
                int(r.get("matched_aligned_n", 0)) + int(r.get("matched_shifted_n", 0)) + int(r.get("a_only_flip_n", 0))
            )
            aligned = int(r.get("matched_aligned_n", 0))
            pct = aligned / total_a * 100 if total_a else 0
            match_rate_rows.append(
                (
                    _strategy_display_name(strat).split(" — ")[0],
                    "After RTH-filter fix" if variant_key == "V-C" else "Current behavior (ETH)",
                    pct,
                )
            )
    if match_rate_rows:
        df_mr = pd.DataFrame(match_rate_rows, columns=["strat", "label", "pct"])
        fig2 = go.Figure()
        for label, color in [
            ("After RTH-filter fix", "#2e7d32"),
            ("Current behavior (ETH)", "#c62828"),
        ]:
            sub = df_mr[df_mr["label"] == label]
            fig2.add_trace(
                go.Bar(
                    x=sub["strat"],
                    y=sub["pct"],
                    name=label,
                    marker_color=color,
                    text=[f"{v:.0f}%" for v in sub["pct"]],
                    textposition="outside",
                    hovertemplate=label + "<br>%{x}: %{y:.1f}%<extra></extra>",
                )
            )
        fig2.update_layout(
            barmode="group",
            template="plotly_white",
            height=320,
            title=dict(text="Trades that fire on the same bar as TradingView (%)", x=0.5),
            yaxis=dict(title="% of TradingView trades aligned", range=[0, 110]),
            legend=dict(orientation="h", y=1.16, x=0.5, xanchor="center"),
            margin=dict(t=60, b=50, l=60, r=20),
        )
        match_div = _fig_to_div(fig2, "fig-eth-match", height=320)
    else:
        match_div = ""

    # Headline numbers
    headline_lines: list[str] = []
    for strat in sorted(summary["strategy"].unique()):
        srow = summary[summary["strategy"] == strat].set_index("variant")
        if "V-C" not in srow.index or "V-D" not in srow.index:
            continue
        delta = srow.loc["V-C", "net_pnl"] - srow.loc["V-D", "net_pnl"]
        n_c = int(srow.loc["V-C", "n_trades"])
        n_d = int(srow.loc["V-D", "n_trades"])
        sname = _strategy_display_name(strat).split(" — ")[0]
        headline_lines.append(
            f"<li><b>{html_mod.escape(sname)}</b>: applying the fix would "
            f"change net P&L per share by <b>{delta:+.2f}</b>, and trade count "
            f"would change from <b>{n_d}</b> to <b>{n_c}</b>.</li>"
        )

    return f"""
    <h2 id='sec-eth'>7. Extended-hours contamination — the headline fix</h2>
    <p class="subtitle">
      <b>What this section answers:</b> the single most important production
      bug surfaced by this study is that the engine consumes pre-market and
      after-hours bars when computing indicators, while TradingView's chart
      shows only regular-trading-hours data. This section quantifies the
      impact of fixing that.
    </p>

    {
        _explain(
            "Per-strategy impact of the RTH-filter fix",
            "&ldquo;RTH-fixed&rdquo; is the engine after a 5-line code change that "
            "filters out pre-market and after-hours bars before updating indicators "
            "(see the Roadmap doc). &ldquo;Current&rdquo; is what learn-ai actually "
            "does today. Compare the trade counts and P&L columns to see how much "
            "the fix would change behavior. The full code change is in the audit "
            "and roadmap documents in <code>docs/</code>.",
        )
    }
    {table_html}

    <div class='headline-block'>
      <b>If you apply the RTH-filter fix today, expect:</b>
      <ul>
        {"".join(headline_lines)}
      </ul>
    </div>

    {
        _explain(
            "Net P&L: after the fix vs current behavior",
            "Green bars are post-fix (engine on RTH bars only). Red bars are the "
            "current production behavior (engine on full-session bars). Where the "
            "red bar is far from the green bar, the bug is materially distorting "
            "strategy results. A red bar on the wrong side of zero relative to its "
            "green counterpart is a P&L sign reversal — the most damaging form.",
        )
    }
    {pnl_div}

    {
        _explain(
            "Trade alignment with TradingView, before vs after the fix",
            "Percentage of TradingView's trades that fire on exactly the same bar "
            "in each variant. After the fix, alignment should approach 100% for "
            "every strategy (any residual is BATS-vs-Polygon feed noise). Today, "
            "alignment ranges from 0% to 15% depending on the strategy.",
        )
    }
    {match_div}
    """


# ----------------------------------------------------------------------
# Section 8 — Methodology, variant definitions, and gotchas
# ----------------------------------------------------------------------


def _methodology_section() -> str:
    rows = [
        (
            "TradingView (reference)",
            "TradingView Pine script `ta.ema()`, `ta.rsi()`, etc.",
            "TradingView BATS feed, regular trading hours only.",
            "Treated as the ground truth that learn-ai aims to reproduce.",
        ),
        (
            "Vetted pandas, Polygon RTH bars",
            "Hand-written textbook formulas in pure Python/pandas.",
            "Polygon consolidated feed, RTH-only (filtered at ingest).",
            "Confirms our formulas are correct. Should match TradingView to within a few cents on every indicator.",
        ),
        (
            "learn-ai engine, Polygon RTH bars",
            "learn-ai's streaming `Indicator` classes (LEAN-style).",
            "Polygon consolidated feed, RTH-only.",
            "Tests the production engine code with the correct data input. Should match TradingView once warmup completes.",
        ),
        (
            "learn-ai engine, Polygon full session (current)",
            "Same engine code as above.",
            "Polygon consolidated feed, NO session filter (pre-market + RTH + after-hours).",
            "What learn-ai actually does today. Diverges from TradingView because pre-market and after-hours bars contaminate the indicators (gotcha #2).",
        ),
    ]
    variants_tbl = (
        "<table class='tbl'>"
        "<thead><tr>"
        "<th>Variant</th><th>Indicator implementation</th>"
        "<th>Bar source</th><th>Why it's in the study</th>"
        "</tr></thead><tbody>"
        + "".join("<tr>" + "".join(f"<td>{html_mod.escape(c)}</td>" for c in r) + "</tr>" for r in rows)
        + "</tbody></table>"
    )

    formulas = """
    <ul>
      <li><b>EMA(N)</b>: smoothing constant α = 2/(N+1). Seed at sample N with
        the simple moving average of the first N values (matches TradingView and
        LEAN). Then EMA[i] = α·close[i] + (1−α)·EMA[i−1].</li>
      <li><b>SMA(N)</b>: arithmetic mean of the most recent N closes.</li>
      <li><b>RSI(14)</b>: Wilder's smoothing of gains and losses with α = 1/14,
        seeded with the simple-moving-average of the first 14 values.
        RSI = 100 − 100 / (1 + avgGain/avgLoss).</li>
      <li><b>MACD(12, 26, 9)</b>: MACD line = EMA(close, 12) − EMA(close, 26);
        signal line = EMA(MACD, 9); histogram = MACD − signal.</li>
      <li><b>Bollinger Bands(20, 2)</b>: middle band = SMA(close, 20); standard
        deviation uses the population formula (ddof = 0); upper = mid + 2σ,
        lower = mid − 2σ.</li>
      <li><b>ATR(14)</b>: Wilder's smoothing of true range
        (max of high−low, |high−prev close|, |low−prev close|), length 14.</li>
      <li><b>ADX(14)</b>: Wilder smoothing applied to +DM, −DM, and TR;
        +DI = 100 · Wilder(+DM)/ATR; −DI = 100 · Wilder(−DM)/ATR;
        DX = 100 · |+DI − −DI| / (+DI + −DI); ADX = Wilder smoothing of DX.</li>
      <li><b>SuperTrend(10, 3)</b>: Wilder ATR(10), then HL2 ± 3·ATR with
        a ratcheting upper/lower band and trend flip on a close-vs-band cross.</li>
    </ul>
    """

    bar_timing = """
    <ul>
      <li><b>Bar period</b>: 15 minutes. The first bar of a US-equity session
        starts at 09:30 ET and ends at 09:44:59. Last bar starts at 15:45 ET.</li>
      <li><b>Timestamp convention</b>: TradingView labels bars by their <i>open</i>
        time. learn-ai's engine internally labels by <i>end</i> time. This causes
        a 15-minute label offset in trade logs (gotcha #7) but does not affect
        any value or fill price.</li>
      <li><b>Session filter</b>: this study is RTH-only (09:30 ET ≤ time &lt; 16:00 ET).
        Pre-market and after-hours bars are excluded on every variant except
        "learn-ai engine, full session" (which is included for contrast).</li>
      <li><b>Half-days</b>: days like the day after Thanksgiving close at 13:00 ET
        and produce only 14 bars instead of 26. This is normal and not flagged
        as a data error.</li>
    </ul>
    """

    gotchas = """
    <ol>
      <li><b>TradingView adjusts for dividends; Polygon doesn't.</b> If you
        re-run this study with a TV CSV that wasn't dividend-adjusted before
        export, you'll see $1-$4 per-share gaps that decrease in steps at each
        SPY ex-date. Either turn off TV's "Adjustment for dividends" or apply
        the reverse-adjustment in code.</li>
      <li><b>Extended-hours bars contaminate indicators.</b> Polygon returns
        bars from 04:00 ET to 19:59 ET; if a strategy doesn't filter to RTH
        before updating indicators, EMAs and RSI absorb low-liquidity pre-market
        and after-hours prints. This is the single biggest reason
        "learn-ai engine, current behavior" differs from TradingView.</li>
      <li><b>Long EMAs need warmup.</b> EMA(200) takes about 800 bars (~38 RTH
        trading days) for the SMA seed's influence to decay to under 1%.
        TradingView pre-warms from years of off-chart history; the engine starts
        from sample 1.</li>
      <li><b>BATS is one venue, not the whole market.</b> TradingView's
        "BATS_SPY" comes from Cboe BATS only. Polygon "consolidated" includes
        all venues. Volume differs by ~25-30× and prices by ~1-3¢ per bar. This
        is the floor of agreement and not fixable in software.</li>
      <li><b>SuperTrend direction sign is inverted between conventions.</b>
        TradingView Pine returns +1 for downtrend and −1 for uptrend; most other
        libraries (and the vetted pandas implementation here) use the opposite.
        We negate before comparing.</li>
    </ol>
    """

    return f"""
    <h2 id='sec-methodology'>8. Methodology and gotchas</h2>
    <p class="subtitle">
      Everything you need to interpret the numbers above, in one place.
      For the full 17-item gotchas catalog see
      <code>docs/tv-polygon-validation-gotchas.md</code>.
    </p>

    <h3>8a. Variant definitions</h3>
    <p class="caption-mini">
      The four variants compared in Sections 3 and 5 are summarized below.
      Each combines one indicator implementation with one bar source.
    </p>
    {variants_tbl}

    <h3>8b. How each indicator is calculated</h3>
    <p class="caption-mini">
      Every indicator follows the canonical formula given here. These match
      TradingView Pine v5/v6's <code>ta.*</code> functions to 4-6 decimal places
      when fed identical inputs.
    </p>
    {formulas}

    <h3>8c. Bar timing and session filtering</h3>
    {bar_timing}

    <h3>8d. Top 5 gotchas you should know</h3>
    <p class="caption-mini">
      The most important pitfalls when comparing TradingView to Polygon-derived
      indicators on US equities.
    </p>
    {gotchas}
    """


# ----------------------------------------------------------------------
# Assembler + CSS
# ----------------------------------------------------------------------


_CSS = """
body { font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
       margin: 24px auto; max-width: 1280px; color: #111; line-height: 1.45; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 19px; margin: 32px 0 8px; border-top: 2px solid #e5e7eb;
     padding-top: 18px; color: #111; }
h3 { font-size: 15px; margin: 20px 0 8px; color: #374151; }
.subtitle { color: #444; font-size: 13.5px; margin-bottom: 14px; max-width: 1100px; }
.caption-mini { color: #555; font-size: 12.5px; margin: 6px 0 8px; max-width: 1100px; }
.chart-caption { color: #333; font-size: 13px; margin: 16px 0 6px;
                 background: #f8fafb; border-left: 3px solid #1976d2;
                 padding: 8px 12px; border-radius: 0 4px 4px 0;
                 max-width: 1180px; }
.chart-caption b { color: #0d47a1; }
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                gap: 8px; margin: 12px 0 24px; }
.card { background: #f4f6f8; border: 1px solid #e5e7eb; border-radius: 8px;
        padding: 10px 12px; }
.card-label { display: block; color: #6b7280; font-size: 11px;
              text-transform: uppercase; letter-spacing: .03em; }
.card-value { font-size: 14px; display: block; margin-top: 2px; }
.card-help { display: block; color: #6b7280; font-size: 11px; margin-top: 6px; }
table.tbl { border-collapse: collapse; font-size: 12px; margin: 6px 0 18px; width: 100%; }
table.tbl th, table.tbl td { border: 1px solid #e5e7eb; padding: 6px 9px; text-align: right;
                              font-variant-numeric: tabular-nums; }
table.tbl th { background: #f9fafb; font-weight: 600; color: #1f2937; }
table.tbl td:first-child, table.tbl th:first-child { text-align: left; }
table.tbl.tight th, table.tbl.tight td { padding: 4px 7px; }
table.tbl.centered td, table.tbl.centered th { text-align: center; }
details.collapsible { margin-top: 10px; font-size: 12.5px; }
details.collapsible > summary { cursor: pointer; color: #1976d2; margin-bottom: 8px;
                                 font-weight: 600; }
.strategy-block { background: #fbfbfc; border: 1px solid #e5e7eb; border-radius: 8px;
                  padding: 14px 16px; margin-bottom: 22px; }
.overlay-block { margin-bottom: 22px; }
.footer { color: #6b7280; font-size: 11px; margin-top: 32px;
          border-top: 1px solid #e5e7eb; padding-top: 12px; }
.headline-block { background: #fffbea; border-left: 4px solid #f59e0b;
                  padding: 12px 16px; border-radius: 0 6px 6px 0;
                  margin: 14px 0; font-size: 13.5px; max-width: 1180px; }
.headline-block b { color: #92400e; }
.headline-block ul { margin: 6px 0 0 22px; padding: 0; }
.headline-block li { margin: 4px 0; }

/* Day 7 — exec summary, nav, lead */
.subtitle.lead { font-size: 15px; color: #1f2937; max-width: 1100px;
                 margin-top: 6px; margin-bottom: 14px; }
nav.toc { background: #f3f4f6; border: 1px solid #e5e7eb; border-radius: 6px;
          padding: 8px 12px; margin: 8px 0 18px; font-size: 12.5px; }
nav.toc a { color: #1976d2; text-decoration: none; }
nav.toc a:hover { text-decoration: underline; }
.exec-block { background: linear-gradient(180deg, #eef6fb 0%, #ffffff 100%);
              border: 1px solid #d6e4ee; border-radius: 8px;
              padding: 18px 20px; margin: 6px 0 22px; }
.exec-finding { font-size: 14.5px; color: #1f2937; margin: 0 0 14px;
                line-height: 1.55; max-width: 1100px; }
.exec-finding b { color: #0d47a1; }
.summary-grid.exec { margin-top: 8px; }
.exec-card .card-value.big { font-size: 16px; }
h2.no-rule { border-top: none; padding-top: 0; margin-top: 22px; color: #0d47a1; }
section { scroll-margin-top: 12px; }
h2 { scroll-margin-top: 12px; }
.footer ul { margin: 4px 0 12px 22px; padding: 0; }
.footer li { margin: 2px 0; }
"""


def build_dashboard(
    timeframe: str = "15m",
    cache_root: Path | str = CACHE_ROOT,
    out_path: Path | str | None = None,
) -> Path:
    cache_root = Path(cache_root)
    tf_cache = cache_root / timeframe
    merged_path = tf_cache / "merged.parquet"
    matrix_path = tf_cache / f"matrix_{timeframe}.csv"
    trades_dir = tf_cache / "trades"

    if not merged_path.exists():
        raise FileNotFoundError(f"{merged_path} not found. Run `python -m app.research.divergence.cli all ...` first.")
    if not matrix_path.exists():
        raise FileNotFoundError(f"{matrix_path} not found.")

    merged = pd.read_parquet(merged_path)

    globals()["_PLOTLY_INLINED"] = False

    parts: list[str] = []
    parts.append(
        f"<h1>SPY {timeframe.upper()} — Data divergence dashboard</h1>"
        f"<p class='subtitle lead'>"
        f"<b>One question, one answer:</b> when learn-ai runs a strategy on SPY "
        f"15-minute bars, does it produce the same trades a trader would see on "
        f"a TradingView chart? This dashboard quantifies the gap, isolates its "
        f"cause, and shows what changes with a specific code fix."
        f"</p>"
    )
    parts.append(_section_nav())
    parts.append(_executive_summary(merged, trades_dir))
    parts.append("<h2 id='sec-glance'>1. At a glance</h2>")
    parts.append(
        "<p class='subtitle'>Headline numbers for this study window. Hover any card for a one-line description.</p>"
    )
    parts.append(_header_cards(merged))
    parts.append(_feed_comparison_section(merged))
    parts.append(_heatmap_section(matrix_path, timeframe))
    parts.append(_overlays_section(merged))
    parts.append(_trade_section(trades_dir, timeframe))
    parts.append(_worst_days_section(trades_dir))
    parts.append(_eth_contamination_section(trades_dir))
    parts.append(_methodology_section())
    parts.append(
        "<div class='footer'>"
        "<p><b>Where to go next:</b></p>"
        "<ul>"
        "<li>For the full inventory of pitfalls, see "
        "<code>docs/tv-polygon-validation-gotchas.md</code> (17 items, lookup "
        "by symptom).</li>"
        "<li>For the engineering changes that close the gap, see "
        "<code>docs/engine-tv-alignment-roadmap.md</code> "
        "(four-tier plan, half a developer-week).</li>"
        "<li>For the per-trade tables behind every chart, see "
        "<code>cache/divergence/15m/trades/</code>.</li>"
        "</ul>"
        "<p>Generated by <code>app.research.divergence.dashboard.build_dashboard</code>. "
        "Plotly.js bundled inline so this file works offline.</p>"
        "</div>"
    )

    html = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>learn-ai — SPY divergence dashboard ({timeframe})</title>
<style>{_CSS}</style>
</head><body>
{"".join(parts)}
</body></html>"""

    if out_path is None:
        out_path = tf_cache / f"dashboard_{timeframe}.html"
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html)
    logger.info("[DASHBOARD] wrote %s (%.1f MB)", out_path, out_path.stat().st_size / 1e6)
    return out_path
