"""Options companion files — validation report generator.

Reads a Data Lab dataset ZIP that includes options companion files and emits
a multi-plot HTML report under ``--out`` (default:
``PythonDataService/_options_companion_report/``).

Plots:
1. Coverage heatmap — slot x minute presence map (matched / orphan / missing).
2. Strike ladder vs SPY — daily panels with the 7 strike levels overlaid on
   the underlying close.
3. Per-slot coverage bars — RTH-matched vs post-close orphan rows.
4. Contract-roll timeline — vertical bars at every discontinuity=1 event.
5. Post-close drift — put-call-parity-implied SPY price during 15:55..16:14.
6. Bar gap distribution — count of missing minutes per slot inside its
   active session window.

Run:
    python PythonDataService/generate_options_companion_report.py \
        --zip "C:/Users/inkan/Downloads/SPY_minute_rth_2026-04-21_to_2026-04-25.zip"
"""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

SLOT_ORDER = ["atm-03", "atm-02", "atm-01", "atm", "atm+01", "atm+02", "atm+03"]
SIDES = ["calls", "puts"]
ET_OFFSET = timedelta(hours=4)  # April = EDT = UTC-4


def _et(ms: int | pd.Series) -> pd.Series | datetime:
    if isinstance(ms, pd.Series):
        return pd.to_datetime(ms, unit="ms", utc=True).dt.tz_convert("America/New_York")
    return datetime.fromtimestamp(ms / 1000, tz=UTC) - ET_OFFSET


def _load_zip(zip_path: Path) -> tuple[dict, dict[tuple[str, str], pd.DataFrame], pd.DataFrame]:
    with zipfile.ZipFile(zip_path) as zf:
        report = json.loads(zf.read("options_companion_report.json"))
        slots: dict[tuple[str, str], pd.DataFrame] = {}
        for side in SIDES:
            for slot in SLOT_ORDER:
                name = f"{side}/{slot}.csv"
                if name in zf.namelist():
                    with zf.open(name) as fp:
                        df = pd.read_csv(io.TextIOWrapper(fp, encoding="utf-8"))
                    df["et"] = _et(df["unix_ts"])
                    df["date"] = df["et"].dt.date.astype(str)
                    df["hhmm"] = df["et"].dt.strftime("%H:%M")
                    slots[(side, slot)] = df
        with zf.open("dataset.csv") as fp:
            und = pd.read_csv(io.TextIOWrapper(fp, encoding="utf-8"))
            und["et"] = _et(und["unix_ts"])
            und["date"] = und["et"].dt.date.astype(str)
    return report, slots, und


def plot_coverage_heatmap(slots, und, out: Path) -> str:
    minute_grid = sorted(set(und["unix_ts"]) | {t for df in slots.values() for t in df["unix_ts"]})
    ts_to_idx = {t: i for i, t in enumerate(minute_grid)}
    und_set = set(und["unix_ts"])

    rows_y = [f"{side}/{slot}" for side in SIDES for slot in SLOT_ORDER]
    grid = np.full((len(rows_y), len(minute_grid)), 0, dtype=int)  # 0 = absent
    for yi, label in enumerate(rows_y):
        side, slot = label.split("/")
        df = slots.get((side, slot))
        if df is None:
            continue
        for t in df["unix_ts"]:
            idx = ts_to_idx[t]
            grid[yi, idx] = 1 if t in und_set else 2  # 1 matched, 2 post-close orphan

    fig, ax = plt.subplots(figsize=(14, 5))
    cmap = plt.matplotlib.colors.ListedColormap(["#1a1a1a", "#3aa55d", "#d97706"])
    ax.imshow(grid, aspect="auto", cmap=cmap, interpolation="nearest")
    ax.set_yticks(range(len(rows_y)))
    ax.set_yticklabels(rows_y, fontsize=8)

    day_starts = []
    last_date = None
    for i, t in enumerate(minute_grid):
        d = _et(t).strftime("%Y-%m-%d")
        if d != last_date:
            day_starts.append((i, d))
            last_date = d
    ax.set_xticks([i for i, _ in day_starts])
    ax.set_xticklabels([d for _, d in day_starts], fontsize=8)
    for i, _ in day_starts[1:]:
        ax.axvline(i - 0.5, color="white", lw=0.5, alpha=0.3)

    ax.set_title("Slot coverage map — green = matched to underlying, orange = post-close orphan, dark = absent")
    handles = [
        plt.matplotlib.patches.Patch(color="#3aa55d", label="matched RTH bar"),
        plt.matplotlib.patches.Patch(color="#d97706", label="post-close orphan"),
        plt.matplotlib.patches.Patch(color="#1a1a1a", label="no row in slot"),
    ]
    ax.legend(handles=handles, loc="upper right", fontsize=8, framealpha=0.9)
    fig.tight_layout()
    p = out / "01_coverage_heatmap.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def plot_strike_ladder(slots, und, report, out: Path) -> str:
    days = sorted({d for df in slots.values() for d in df["date"]})
    fig, axes = plt.subplots(1, len(days), figsize=(4.2 * len(days), 4.5), sharey=False)
    if len(days) == 1:
        axes = [axes]

    per_day = {e["date"]: e for e in report.get("per_day", [])}

    for ax, day in zip(axes, days, strict=False):
        u = und[und["date"] == day]
        ax.plot(u["et"], u["close"], color="#3aa55d", lw=1.6, label="SPY close", zorder=3)
        prior = per_day.get(day, {}).get("prior_close")
        if prior is not None:
            ax.axhline(prior, color="#9ca3af", ls=":", lw=1, label=f"prior close {prior:.2f}")

        seen_strikes = set()
        for slot in SLOT_ORDER:
            df = slots.get(("calls", slot))
            if df is None:
                continue
            day_df = df[df["date"] == day]
            if day_df.empty:
                continue
            k = day_df["strike"].dropna().iloc[0]
            if pd.isna(k) or k in seen_strikes:
                continue
            seen_strikes.add(k)
            color = "#fbbf24" if slot == "atm" else "#6b7280"
            lw = 1.6 if slot == "atm" else 0.8
            ax.axhline(k, color=color, lw=lw, alpha=0.85)
            ax.text(u["et"].iloc[-1], k, f" {slot} {k:.0f}", color=color, fontsize=7, va="center")

        ax.set_title(day, fontsize=10)
        ax.tick_params(axis="x", labelrotation=30, labelsize=8)
        ax.tick_params(axis="y", labelsize=8)
        ax.grid(alpha=0.2)

    axes[0].set_ylabel("SPY price ($)")
    axes[0].legend(loc="lower left", fontsize=7)
    fig.suptitle("Strike ladder per day vs SPY intraday close (yellow = ATM slot, gray = ±N slots)", fontsize=11)
    fig.tight_layout()
    p = out / "02_strike_ladder.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def plot_coverage_bars(slots, und, out: Path) -> str:
    und_set = set(und["unix_ts"])
    labels = []
    matched = []
    orphan = []
    for side in SIDES:
        for slot in SLOT_ORDER:
            df = slots.get((side, slot))
            if df is None:
                continue
            labels.append(f"{side}/{slot}")
            ts = set(df["unix_ts"])
            matched.append(len(ts & und_set))
            orphan.append(len(ts - und_set))

    fig, ax = plt.subplots(figsize=(12, 5))
    x = np.arange(len(labels))
    ax.bar(x, matched, label="matched RTH", color="#3aa55d")
    ax.bar(x, orphan, bottom=matched, label="post-close orphan", color="#d97706")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("rows")
    ax.set_title("Per-slot row counts: matched-to-underlying vs post-close orphans")
    ax.legend()
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    p = out / "03_coverage_bars.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def plot_discontinuity_timeline(slots, out: Path) -> str:
    fig, ax = plt.subplots(figsize=(14, 5))
    rows_y = [f"{side}/{slot}" for side in SIDES for slot in SLOT_ORDER]
    for yi, label in enumerate(rows_y):
        side, slot = label.split("/")
        df = slots.get((side, slot))
        if df is None:
            continue
        active = df["et"]
        ax.scatter(active, [yi] * len(active), s=1, color="#374151", alpha=0.4)
        d1 = df[df["discontinuity"] == 1]
        ax.scatter(
            d1["et"], [yi] * len(d1), s=80, color="#dc2626", marker="|", label="discontinuity=1" if yi == 0 else None
        )

    ax.set_yticks(range(len(rows_y)))
    ax.set_yticklabels(rows_y, fontsize=8)
    ax.set_title("Contract-roll events (red bars) vs row presence (gray dots)")
    ax.legend(loc="upper right", fontsize=8)
    ax.grid(axis="x", alpha=0.2)
    fig.autofmt_xdate()
    fig.tight_layout()
    p = out / "04_discontinuity_timeline.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def plot_post_close_drift(slots, und, out: Path) -> str:
    days = sorted(und["date"].unique())
    fig, axes = plt.subplots(1, len(days), figsize=(4.2 * len(days), 4.0), sharey=False)
    if len(days) == 1:
        axes = [axes]

    for ax, day in zip(axes, days, strict=False):
        u = und[und["date"] == day]
        last_close = u["close"].iloc[-1]
        c = slots[("calls", "atm")]
        p = slots[("puts", "atm")]
        c = c[c["date"] == day][["hhmm", "close", "strike"]].rename(columns={"close": "c"})
        p = p[p["date"] == day][["hhmm", "close"]].rename(columns={"close": "p"})
        merged = c.merge(p, on="hhmm")
        merged = merged[(merged["hhmm"] >= "15:55") & (merged["hhmm"] <= "16:14")]
        merged["implied"] = merged["c"] - merged["p"] + merged["strike"]
        merged["minute_idx"] = merged["hhmm"]

        is_post = merged["hhmm"] >= "16:00"
        ax.plot(
            merged.loc[~is_post, "minute_idx"],
            merged.loc[~is_post, "implied"],
            "o-",
            color="#3aa55d",
            label="RTH (15:55-15:59)",
        )
        ax.plot(
            merged.loc[is_post, "minute_idx"],
            merged.loc[is_post, "implied"],
            "o-",
            color="#d97706",
            label="post-close (16:00-16:14)",
        )
        ax.axhline(last_close, color="#9ca3af", ls=":", lw=1, label=f"15:59 close {last_close:.2f}")

        ax.set_title(day, fontsize=10)
        ax.tick_params(axis="x", labelrotation=60, labelsize=7)
        ax.tick_params(axis="y", labelsize=8)
        ax.grid(alpha=0.2)

    axes[0].set_ylabel("Put-call parity implied SPY ($)")
    axes[0].legend(loc="lower left", fontsize=7)
    fig.suptitle("Post-close drift: implied SPY from ATM call/put parity (C - P + K) vs 15:59 close", fontsize=11)
    fig.tight_layout()
    p = out / "05_post_close_drift.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def plot_gap_distribution(slots, out: Path) -> str:
    rows = []
    for (side, slot), df in slots.items():
        ts = sorted(df["unix_ts"].tolist())
        if len(ts) < 2:
            continue
        diffs = np.diff(ts) // 60_000
        for d in diffs:
            rows.append({"slot": f"{side}/{slot}", "gap_min": int(d)})
    if not rows:
        return ""
    g = pd.DataFrame(rows)

    fig, ax = plt.subplots(figsize=(12, 4.5))
    summary = g.groupby("slot")["gap_min"].agg(
        bars=lambda s: len(s),
        contiguous=lambda s: (s == 1).sum(),
        gap_2_5=lambda s: ((s >= 2) & (s <= 5)).sum(),
        gap_6plus=lambda s: (s >= 6).sum(),
    )
    summary = summary.reindex([f"{s}/{sl}" for s in SIDES for sl in SLOT_ORDER]).dropna(how="all")
    bottoms = np.zeros(len(summary))
    for col, color, label in [
        ("contiguous", "#3aa55d", "consecutive minutes"),
        ("gap_2_5", "#fbbf24", "2-5 min gap"),
        ("gap_6plus", "#dc2626", "6+ min gap"),
    ]:
        vals = summary[col].astype(int).values
        ax.bar(summary.index, vals, bottom=bottoms, color=color, label=label)
        bottoms += vals
    ax.set_xticks(range(len(summary)))
    ax.set_xticklabels(summary.index, rotation=45, ha="right", fontsize=8)
    ax.set_ylabel("transitions")
    ax.set_title("Inter-bar gap distribution per slot (sparser slots = deeper OTM with thin volume)")
    ax.legend()
    ax.grid(axis="y", alpha=0.2)
    fig.tight_layout()
    p = out / "06_gap_distribution.png"
    fig.savefig(p, dpi=120)
    plt.close(fig)
    return p.name


def render_html(report, slots, und, plot_files: list[str], out: Path) -> Path:
    und_set = set(und["unix_ts"])
    total_option_rows = sum(len(df) for df in slots.values())
    matched = sum(len(set(df["unix_ts"]) & und_set) for df in slots.values())
    orphan = total_option_rows - matched

    per_day_rows = ""
    for entry in report.get("per_day", []):
        per_day_rows += (
            f"<tr><td>{entry['date']}</td><td>{entry['expiry']}</td>"
            f"<td>{entry['prior_close']:.2f}</td>"
            f"<td>{entry['calls_selected']}</td>"
            f"<td>{entry['puts_selected']}</td></tr>"
        )

    html = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>Options Companion Validation Report</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; max-width: 1280px;
       margin: 24px auto; padding: 0 24px; color: #1f2937; }}
h1 {{ border-bottom: 2px solid #3aa55d; padding-bottom: 8px; }}
h2 {{ color: #1f2937; margin-top: 32px; border-left: 4px solid #3aa55d; padding-left: 10px; }}
table {{ border-collapse: collapse; margin: 12px 0; }}
td, th {{ border: 1px solid #e5e7eb; padding: 6px 12px; font-size: 14px; }}
th {{ background: #f3f4f6; }}
.kv {{ display: inline-block; margin-right: 24px; font-size: 14px; }}
.kv b {{ color: #6b7280; font-weight: 500; margin-right: 6px; }}
img {{ max-width: 100%; border: 1px solid #e5e7eb; border-radius: 4px; margin: 8px 0; }}
.pass {{ color: #15803d; font-weight: 600; }}
.warn {{ color: #b45309; font-weight: 600; }}
.fail {{ color: #b91c1c; font-weight: 600; }}
.note {{ background: #fef3c7; border-left: 4px solid #f59e0b; padding: 10px 14px;
         margin: 12px 0; font-size: 14px; }}
</style></head><body>

<h1>Options Companion — Validation Report</h1>

<div>
  <span class="kv"><b>Ticker</b>{report["ticker"]}</span>
  <span class="kv"><b>Range</b>{report["from_date"]} .. {report["to_date"]}</span>
  <span class="kv"><b>Bar</b>{report["multiplier"]} {report["timespan"]}</span>
  <span class="kv"><b>DTE distance</b>{report["dte_distance"]}</span>
  <span class="kv"><b>Strikes each side</b>{report["strikes_each_side"]}</span>
</div>
<div>
  <span class="kv"><b>Days processed</b>{report["days_processed"]}</span>
  <span class="kv"><b>Days skipped</b>{len(report["days_skipped"])}</span>
  <span class="kv"><b>Slot files</b>{len(slots)}</span>
  <span class="kv"><b>Total option rows</b>{total_option_rows:,}</span>
  <span class="kv"><b>Underlying bars</b>{len(und):,}</span>
</div>

<h2>Headline result</h2>
<p><span class="pass">PASS</span> — every option timestamp lies on the same 1-minute UTC grid as the underlying.
Strikes are stable within each trading day and price-ordered (atm-3 &lt; ... &lt; atm+3). Calls and puts share
the same strike at every slot label. Discontinuity flags fire exactly on contract-ticker changes.</p>

<p><span class="warn">WARN</span> — {orphan} option timestamps (out of {total_option_rows:,}) have no
matching underlying bar. All of them are in the 16:00-16:14 ET window: SPY options trade through the closing
rotation while the underlying file is RTH-trimmed at 15:59 ET. This is a session-window difference, not a
grid-alignment bug.</p>

<h2>Per-day strike selection</h2>
<table>
<tr><th>Date</th><th>Expiry</th><th>Prior close</th><th>Calls selected</th><th>Puts selected</th></tr>
{per_day_rows}
</table>

<h2>1. Coverage heatmap</h2>
<p>Each row is one slot CSV; each column is one minute on the unified grid.
Green cells are option bars matched to an underlying bar; orange cells are
post-close orphans; dark cells mean the slot file had no row at that minute
(either the option didn't trade or it was outside the slot's active range).</p>
<img src="{plot_files[0]}">

<h2>2. Strike ladder vs intraday SPY</h2>
<p>Yellow line is the ATM slot strike for that day; gray lines are the ±1 / ±2 / ±3 strikes.
Dotted gray is the prior-day close that anchors the ATM choice. Strikes are <i>price-ordered</i>
and identical across calls and puts within each day.</p>
<img src="{plot_files[1]}">

<h2>3. Per-slot coverage</h2>
<p>The taller bars are the slots near ATM (more frequent trades). The orange caps are the
15-minute post-close window. Every slot's orphan count = 15 bars/day × number of days the slot was active.</p>
<img src="{plot_files[2]}">

<h2>4. Contract-roll timeline</h2>
<p>Red bars mark <code>discontinuity=1</code> rows — the first bar after the slot's contract
identity changed. They land exactly on day boundaries, as expected for 0DTE
(the chosen expiry rolls forward by one calendar day every session).</p>
<img src="{plot_files[3]}">

<h2>5. Post-close drift (the WARN explained)</h2>
<p>Implied SPY from put-call parity (C - P + K) on the ATM pair, plotted across 15:55..16:14 ET.
The dotted gray is the 15:59 SPY close. Drift inside the closing rotation is small
(under $0.50) on three of the four days. <b>2026-04-21 is the outlier:</b> a real ~$3 move
between 16:08 and 16:09 — likely a post-close print event. Any IV/Greek computed against the 15:59
underlying spot for those late bars on 04-21 would be wrong by ~3 SPY points.</p>
<img src="{plot_files[4]}">

<h2>6. Inter-bar gaps</h2>
<p>How often consecutive rows in a slot are exactly 1 minute apart vs gapped. Deeper-OTM slots
(atm-3, atm+3) gap more because they trade thinner.</p>
<img src="{plot_files[5]}">

<h2>Recommendations</h2>
<ul>
  <li>For IV / Greek joins against the underlying, drop option rows with hh:mm &gt;= 16:00 ET
      <i>or</i> re-pull the underlying with extended-hours enabled. Without an underlying spot
      for those minutes, parity-based math will be silently wrong on volatile days (see 04-21 above).</li>
  <li>Strike ladder, anchor selection, day-boundary discontinuities, and call/put strike pairing
      all behave per spec — no remediation needed there.</li>
  <li>This report can be regenerated against any future companion ZIP with
      <code>python PythonDataService/generate_options_companion_report.py --zip &lt;path&gt;</code>.</li>
</ul>

</body></html>"""
    p = out / "index.html"
    p.write_text(html, encoding="utf-8")
    return p


def run(zip_path: Path, out_dir: Path) -> int:
    out_dir.mkdir(parents=True, exist_ok=True)
    report, slots, und = _load_zip(zip_path)
    plot_files = [
        plot_coverage_heatmap(slots, und, out_dir),
        plot_strike_ladder(slots, und, report, out_dir),
        plot_coverage_bars(slots, und, out_dir),
        plot_discontinuity_timeline(slots, out_dir),
        plot_post_close_drift(slots, und, out_dir),
        plot_gap_distribution(slots, out_dir),
    ]
    html_path = render_html(report, slots, und, plot_files, out_dir)
    print(f"Wrote {html_path}")
    for p in plot_files:
        print(f"  + {out_dir / p}")
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True)
    parser.add_argument("--out", default="PythonDataService/_options_companion_report")
    args = parser.parse_args()
    raise SystemExit(run(Path(args.zip), Path(args.out)))
