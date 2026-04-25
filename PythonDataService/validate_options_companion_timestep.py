"""Options companion files — timestep equivalence validation study.

Verifies that ATM/ITM/OTM option slot CSVs emitted by the Data Lab options
companion pipeline share an identical UTC bar grid with each other and
with the underlying ticker dataset.

Spec: docs/options-companion-format.md (esp. § 8 bar-grid alignment, § 5
discontinuity, § 3 slot model).

Run:
    python PythonDataService/validate_options_companion_timestep.py \
        --zip "C:/Users/inkan/Downloads/SPY_minute_rth_2026-04-21_to_2026-04-25.zip"
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SLOT_ORDER = ["atm-03", "atm-02", "atm-01", "atm", "atm+01", "atm+02", "atm+03"]
SIDES = ["calls", "puts"]


@dataclass
class SlotRows:
    side: str
    slot: str
    timestamps: list[int] = field(default_factory=list)
    contract_per_ts: dict[int, str] = field(default_factory=dict)
    strike_per_ts: dict[int, float | None] = field(default_factory=dict)
    expiration_per_ts: dict[int, str] = field(default_factory=dict)
    discontinuity_per_ts: dict[int, int] = field(default_factory=dict)
    iso_per_ts: dict[int, str] = field(default_factory=dict)


@dataclass
class Finding:
    severity: str  # "FAIL" | "WARN" | "INFO"
    check: str
    detail: str


def _parse_float(s: str) -> float | None:
    s = s.strip()
    if s == "":
        return None
    return float(s)


def _read_slot(zf: zipfile.ZipFile, side: str, slot: str) -> SlotRows | None:
    name = f"{side}/{slot}.csv"
    if name not in zf.namelist():
        return None
    rows = SlotRows(side=side, slot=slot)
    with zf.open(name) as fp:
        reader = csv.DictReader(io.TextIOWrapper(fp, encoding="utf-8", newline=""))
        for r in reader:
            ts = int(r["unix_ts"])
            rows.timestamps.append(ts)
            rows.contract_per_ts[ts] = r.get("contract_ticker", "").strip()
            rows.strike_per_ts[ts] = _parse_float(r.get("strike", ""))
            rows.expiration_per_ts[ts] = r.get("expiration", "").strip()
            rows.discontinuity_per_ts[ts] = int(r.get("discontinuity", "0") or 0)
            rows.iso_per_ts[ts] = r.get("iso_time", "").strip()
    return rows


def _read_underlying(zf: zipfile.ZipFile) -> set[int]:
    with zf.open("dataset.csv") as fp:
        reader = csv.DictReader(io.TextIOWrapper(fp, encoding="utf-8", newline=""))
        return {int(r["unix_ts"]) for r in reader}


def _date_of_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).date().isoformat()


def _ny_date_of_ms(ms: int) -> str:
    # NY date partition: bars 13:30-20:00 UTC on date D = NY trading day D
    # (DST changes the offset but during regular RTH the date math is unambiguous
    # because 13:30Z..20:00Z always falls on the same UTC calendar day as the
    # NY trading day for SPY's session window).
    return _date_of_ms(ms)


def check_grid_alignment(slots: dict[tuple[str, str], SlotRows], bar_ms: int) -> list[Finding]:
    findings: list[Finding] = []
    for (side, slot), rows in slots.items():
        bad = [t for t in rows.timestamps if t % bar_ms != 0]
        if bad:
            findings.append(
                Finding(
                    "FAIL",
                    "grid_alignment",
                    f"{side}/{slot}: {len(bad)} timestamps not on {bar_ms}ms grid (e.g. {bad[:3]})",
                )
            )
    return findings


def check_strict_monotonic(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    findings: list[Finding] = []
    for (side, slot), rows in slots.items():
        ts = rows.timestamps
        violations = sum(1 for i in range(1, len(ts)) if ts[i] <= ts[i - 1])
        if violations:
            findings.append(
                Finding(
                    "FAIL",
                    "strict_monotonic",
                    f"{side}/{slot}: {violations} non-increasing or duplicate timestamp transitions",
                )
            )
    return findings


def check_cross_slot_ts_identity(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    """Whenever two slot files have a row at the same wall-clock minute,
    the int64 unix_ts must be byte-identical (no microsecond drift)."""
    findings: list[Finding] = []
    # Group all observed timestamps by side; each ts value is shared across slots
    # because they all rest on the same grid. Exact int equality across files is
    # what we want — any off-by-one-ms is a bug.
    all_ts = set()
    for rows in slots.values():
        all_ts.update(rows.timestamps)
    # The check above (grid_alignment) already proves all values are multiples
    # of bar_ms. The strict identity claim therefore reduces to: every ts in
    # any slot, when divided by bar_ms, is an integer; equality of timestamps
    # across slots is automatic. We still report a counter for visibility.
    findings.append(
        Finding(
            "INFO",
            "cross_slot_ts_identity",
            f"{len(all_ts)} distinct grid timestamps observed across all slot files",
        )
    )
    return findings


def check_per_day_single_contract(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    findings: list[Finding] = []
    for (side, slot), rows in slots.items():
        by_day: dict[str, set[str]] = defaultdict(set)
        for ts, ticker in rows.contract_per_ts.items():
            if ticker:
                by_day[_ny_date_of_ms(ts)].add(ticker)
        bad_days = {d: list(s) for d, s in by_day.items() if len(s) > 1}
        if bad_days:
            findings.append(
                Finding(
                    "FAIL",
                    "single_contract_per_slot_per_day",
                    f"{side}/{slot}: multiple contracts within a day → {bad_days}",
                )
            )
    return findings


def check_strike_ordering(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    """For each (side, day), strikes must satisfy atm-3 < atm-2 < atm-1 < atm < atm+1 < atm+2 < atm+3."""
    findings: list[Finding] = []
    days: dict[tuple[str, str], dict[str, float]] = defaultdict(dict)
    for (side, slot), rows in slots.items():
        for ts, strike in rows.strike_per_ts.items():
            if strike is None:
                continue
            day = _ny_date_of_ms(ts)
            prev = days[(side, day)].get(slot)
            if prev is not None and prev != strike:
                findings.append(
                    Finding(
                        "FAIL",
                        "strike_stable_within_day",
                        f"{side}/{slot} on {day}: strike changed within day ({prev} -> {strike})",
                    )
                )
            days[(side, day)][slot] = strike

    for (side, day), strike_map in sorted(days.items()):
        ordered = [strike_map.get(s) for s in SLOT_ORDER]
        present = [(s, v) for s, v in zip(SLOT_ORDER, ordered, strict=False) if v is not None]
        for i in range(1, len(present)):
            if present[i][1] <= present[i - 1][1]:
                findings.append(
                    Finding(
                        "FAIL",
                        "strike_price_ordering",
                        f"{side} {day}: strike order violation "
                        f"{present[i - 1][0]}={present[i - 1][1]} >= {present[i][0]}={present[i][1]}",
                    )
                )
                break
    return findings


def check_calls_puts_atm_strike_match(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    """For each slot label, the strike on day D should match between calls and puts —
    same listed-strike grid, same anchor."""
    findings: list[Finding] = []
    by_day_slot: dict[tuple[str, str, str], float] = {}
    for (side, slot), rows in slots.items():
        for ts, strike in rows.strike_per_ts.items():
            if strike is None:
                continue
            day = _ny_date_of_ms(ts)
            by_day_slot[(day, side, slot)] = strike
    days = sorted({k[0] for k in by_day_slot})
    for day in days:
        for slot in SLOT_ORDER:
            c = by_day_slot.get((day, "calls", slot))
            p = by_day_slot.get((day, "puts", slot))
            if c is not None and p is not None and c != p:
                findings.append(
                    Finding(
                        "FAIL",
                        "calls_puts_strike_match",
                        f"{day} {slot}: calls strike {c} != puts strike {p}",
                    )
                )
    return findings


def check_discontinuity_semantics(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    """`discontinuity` must be 1 iff the row's contract_ticker differs from the
    previous row in the same slot (or it's the first row in the file)."""
    findings: list[Finding] = []
    for (side, slot), rows in slots.items():
        wrong_one = 0
        wrong_zero = 0
        prev_ticker: str | None = None
        for ts in rows.timestamps:
            ticker = rows.contract_per_ts[ts]
            disc = rows.discontinuity_per_ts[ts]
            expected = 1 if (prev_ticker is None or ticker != prev_ticker) else 0
            if disc == 1 and expected == 0:
                wrong_one += 1
            elif disc == 0 and expected == 1:
                wrong_zero += 1
            prev_ticker = ticker
        if wrong_one or wrong_zero:
            findings.append(
                Finding(
                    "FAIL",
                    "discontinuity_semantics",
                    f"{side}/{slot}: {wrong_one} false-positive 1s, {wrong_zero} missed 1s",
                )
            )
    return findings


def check_iso_matches_unix(slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    findings: list[Finding] = []
    for (side, slot), rows in slots.items():
        bad = 0
        for ts, iso in rows.iso_per_ts.items():
            expected = datetime.fromtimestamp(ts / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            if iso != expected:
                bad += 1
        if bad:
            findings.append(
                Finding(
                    "FAIL",
                    "iso_matches_unix",
                    f"{side}/{slot}: {bad} rows where iso_time != reformat(unix_ts)",
                )
            )
    return findings


def check_underlying_alignment(
    slots: dict[tuple[str, str], SlotRows], underlying_ts: set[int], bar_ms: int
) -> list[Finding]:
    """Two-part check:
    (1) Grid sharing: every option timestamp must lie on the same UTC bar grid
        as the underlying (multiple of bar_ms). This is the strict timestep-
        equivalence claim from spec § 8.
    (2) Session overlap: option timestamps not present in the underlying are
        bucketed by NY hh:mm. If they all fall in a contiguous post-close
        window, that's a session-trim difference (RTH ends 15:59 ET for the
        ETF; SPY options trade through 16:15 ET closing rotation), not a
        grid bug. Surfaced as INFO with the breakdown.
    """
    findings: list[Finding] = []
    union: set[int] = set()
    for rows in slots.values():
        union.update(rows.timestamps)

    # (1) Grid sharing — ensure every option ts is congruent to underlying grid
    if underlying_ts:
        und_min = min(underlying_ts)
        off_grid = [t for t in union if (t - und_min) % bar_ms != 0]
        if off_grid:
            findings.append(
                Finding(
                    "FAIL",
                    "shared_grid",
                    f"{len(off_grid)} option timestamps not aligned to underlying grid (e.g. {off_grid[:3]})",
                )
            )
        else:
            findings.append(
                Finding(
                    "INFO",
                    "shared_grid",
                    f"All {len(union)} option timestamps lie on the same {bar_ms}ms grid as the underlying",
                )
            )

    # (2) Session overlap
    orphans = union - underlying_ts
    if orphans:
        from collections import Counter
        from datetime import timedelta

        # ET offset varies with DST; for April it's UTC-4 (EDT). Compute a
        # rough ET hh:mm by subtracting 4h — sufficient for bucketing.
        def _et_hhmm(ms: int) -> str:
            return (datetime.fromtimestamp(ms / 1000, tz=UTC) - timedelta(hours=4)).strftime("%H:%M")

        bucket = Counter(_et_hhmm(t) for t in orphans)
        post_close = sum(v for k, v in bucket.items() if k >= "16:00")
        pre_open = sum(v for k, v in bucket.items() if k < "09:30")
        intraday = len(orphans) - post_close - pre_open
        sev = "WARN" if intraday == 0 else "FAIL"
        findings.append(
            Finding(
                sev,
                "session_overlap",
                f"{len(orphans)} option timestamps with no underlying match: "
                f"post_close={post_close} pre_open={pre_open} intraday_gaps={intraday}. "
                f"Buckets={dict(sorted(bucket.items()))}. "
                f"Note: RTH-trimmed underlying ends 15:59 ET; SPY options trade through 16:15 ET "
                f"closing rotation — post_close orphans are expected by Polygon's session model.",
            )
        )
    else:
        findings.append(
            Finding(
                "INFO",
                "session_overlap",
                f"All {len(union)} option timestamps overlap the underlying session",
            )
        )

    coverage = {}
    for (side, slot), rows in slots.items():
        coverage[f"{side}/{slot}"] = (
            f"{len(rows.timestamps)} rows / {len(set(rows.timestamps) & underlying_ts)} matched"
        )
    findings.append(Finding("INFO", "coverage_per_slot", json.dumps(coverage, indent=2)))
    return findings


def check_anchor_atm_strike_vs_prior_close(report: dict, slots: dict[tuple[str, str], SlotRows]) -> list[Finding]:
    """The atm slot's strike on day D should be the closest listed strike to
    that day's prior_close (recorded in options_companion_report.json)."""
    findings: list[Finding] = []
    for entry in report.get("per_day", []):
        day = entry["date"]
        prior = entry["prior_close"]
        for side in SIDES:
            rows = slots.get((side, "atm"))
            if rows is None:
                continue
            day_strikes = [
                rows.strike_per_ts[t]
                for t in rows.timestamps
                if rows.strike_per_ts[t] is not None and _ny_date_of_ms(t) == day
            ]
            if not day_strikes:
                continue
            chosen = day_strikes[0]
            # The anchor is "closest listed strike". We can't reproduce the listed
            # set without the chain, so we just check the chosen anchor is within
            # one strike-step of prior_close. Inferring step from the slot grid:
            adj = slots.get((side, "atm+01"))
            step = None
            if adj is not None:
                adj_strikes = [
                    adj.strike_per_ts[t]
                    for t in adj.timestamps
                    if adj.strike_per_ts[t] is not None and _ny_date_of_ms(t) == day
                ]
                if adj_strikes:
                    step = adj_strikes[0] - chosen
            if step is None or abs(chosen - prior) > step:
                findings.append(
                    Finding(
                        "WARN",
                        "atm_anchor_vs_prior_close",
                        f"{side} {day}: atm strike {chosen} vs prior_close {prior} "
                        f"(strike step ≈ {step}) — distance > 1 strike",
                    )
                )
    return findings


def render_report(findings: list[Finding]) -> str:
    out: list[str] = []
    by_sev = defaultdict(list)
    for f in findings:
        by_sev[f.severity].append(f)
    for sev in ("FAIL", "WARN", "INFO"):
        items = by_sev.get(sev, [])
        out.append(f"\n=== {sev} ({len(items)}) ===")
        for f in items:
            out.append(f"  [{f.check}] {f.detail}")
    return "\n".join(out)


def run(zip_path: Path) -> int:
    with zipfile.ZipFile(zip_path) as zf:
        report = json.loads(zf.read("options_companion_report.json"))
        slots: dict[tuple[str, str], SlotRows] = {}
        for side in SIDES:
            for slot in SLOT_ORDER:
                rows = _read_slot(zf, side, slot)
                if rows is not None:
                    slots[(side, slot)] = rows
        underlying_ts = _read_underlying(zf)

    timespan = report.get("timespan", "minute")
    multiplier = report.get("multiplier", 1)
    bar_ms = {"minute": 60_000, "hour": 3_600_000, "day": 86_400_000}[timespan] * multiplier

    findings: list[Finding] = []
    findings.append(
        Finding(
            "INFO",
            "config",
            f"ticker={report['ticker']} range={report['from_date']}..{report['to_date']} "
            f"bar={multiplier}{timespan} ({bar_ms} ms) dte_distance={report['dte_distance']} "
            f"strikes_each_side={report['strikes_each_side']} files={len(slots)}",
        )
    )
    findings += check_grid_alignment(slots, bar_ms)
    findings += check_strict_monotonic(slots)
    findings += check_cross_slot_ts_identity(slots)
    findings += check_per_day_single_contract(slots)
    findings += check_strike_ordering(slots)
    findings += check_calls_puts_atm_strike_match(slots)
    findings += check_discontinuity_semantics(slots)
    findings += check_iso_matches_unix(slots)
    findings += check_underlying_alignment(slots, underlying_ts, bar_ms)
    findings += check_anchor_atm_strike_vs_prior_close(report, slots)

    print(render_report(findings))
    fails = sum(1 for f in findings if f.severity == "FAIL")
    print(f"\nResult: {'PASS' if fails == 0 else f'FAIL ({fails} failed checks)'}")
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--zip",
        required=True,
        help="Path to a Data Lab dataset ZIP that contains the options companion files",
    )
    args = parser.parse_args()
    sys.exit(run(Path(args.zip)))
