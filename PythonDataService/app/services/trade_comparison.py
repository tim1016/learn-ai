"""Trade comparison service — matches reproduced trades against reference trades."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class TradeComparison:
    trade_num: int
    ref_entry_time: str | None
    our_entry_time: str | None
    ref_exit_time: str | None
    our_exit_time: str | None
    ref_entry_price: float | None
    our_entry_price: float | None
    ref_exit_price: float | None
    our_exit_price: float | None
    ref_pnl: float | None
    our_pnl: float | None
    ref_pnl_pct: float | None
    our_pnl_pct: float | None
    entry_price_delta: float | None
    exit_price_delta: float | None
    pnl_delta: float | None
    pnl_pct_delta: float | None
    timestamp_delta_s: float | None
    matched: bool
    source: str  # "matched", "extra_ours", "extra_ref"


@dataclass
class MatchStats:
    total_ref: int
    total_ours: int
    matched_count: int
    extra_ref: int
    extra_ours: int
    match_rate: float
    avg_ts_delta_s: float
    avg_entry_price_delta: float
    avg_pnl_delta: float


def _parse_ts(ts_val: str | int | float) -> float:
    """Parse a timestamp to epoch seconds.

    Accepts only:
    - int/float: treated as epoch seconds (or ms if > 1e10) — no ambiguity.
    - ISO 8601 strings with an explicit UTC offset (e.g. '2024-01-01T09:30:00Z',
      '2024-01-01T09:30:00+00:00'). Naive strings are rejected to fail-fast on
      producer-side format violations per numerical-rigor.md timestamp rules.
    """
    if isinstance(ts_val, (int, float)):
        # Epoch seconds vs ms heuristic: values > 1e10 are ms
        return float(ts_val) / 1000.0 if ts_val > 1e10 else float(ts_val)

    ts_str = ts_val.strip()
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z"):
        try:
            dt = datetime.strptime(ts_str, fmt)
            return dt.timestamp()
        except ValueError:
            continue
    raise ValueError(
        f"Cannot parse timestamp '{ts_str}': only ISO 8601 strings with explicit UTC offset "
        "are accepted (e.g. '2024-01-01T09:30:00Z'). Naive strings indicate a producer-side bug."
    )


def match_trades(
    our_trades: list[dict],
    ref_trades: list[dict],
    max_delta_s: float = 900,
) -> tuple[list[TradeComparison], MatchStats]:
    """Match reproduced trades against reference trades by entry timestamp proximity.

    Args:
        our_trades: list of dicts with entry_timestamp, exit_timestamp, entry_price, exit_price, pnl, pnl_pct
        ref_trades: list of dicts with entry_time, exit_time, entry_price, exit_price, pnl, pnl_pct
        max_delta_s: max seconds between entry timestamps to consider a match (default 15 min)

    Returns:
        (comparisons, match_stats)
    """
    our_parsed = []
    for t in our_trades:
        our_parsed.append(
            {
                **t,
                "_entry_epoch": _parse_ts(t["entry_timestamp"]),
            }
        )

    ref_parsed = []
    for t in ref_trades:
        ref_parsed.append(
            {
                **t,
                "_entry_epoch": _parse_ts(t["entry_time"]),
            }
        )

    our_parsed.sort(key=lambda x: x["_entry_epoch"])
    ref_parsed.sort(key=lambda x: x["_entry_epoch"])

    used_ours: set[int] = set()
    comparisons: list[TradeComparison] = []
    trade_num = 0

    for ref in ref_parsed:
        best_idx = -1
        best_delta = float("inf")

        for j, our in enumerate(our_parsed):
            if j in used_ours:
                continue
            delta = abs(our["_entry_epoch"] - ref["_entry_epoch"])
            if delta < best_delta and delta <= max_delta_s:
                best_delta = delta
                best_idx = j

        trade_num += 1

        if best_idx >= 0:
            used_ours.add(best_idx)
            our = our_parsed[best_idx]
            comparisons.append(
                TradeComparison(
                    trade_num=trade_num,
                    ref_entry_time=ref.get("entry_time"),
                    our_entry_time=our.get("entry_timestamp"),
                    ref_exit_time=ref.get("exit_time"),
                    our_exit_time=our.get("exit_timestamp"),
                    ref_entry_price=ref.get("entry_price"),
                    our_entry_price=our.get("entry_price"),
                    ref_exit_price=ref.get("exit_price"),
                    our_exit_price=our.get("exit_price"),
                    ref_pnl=ref.get("pnl"),
                    our_pnl=our.get("pnl"),
                    ref_pnl_pct=ref.get("pnl_pct"),
                    our_pnl_pct=our.get("pnl_pct"),
                    entry_price_delta=round(our["entry_price"] - ref["entry_price"], 4),
                    exit_price_delta=round(our["exit_price"] - ref["exit_price"], 4),
                    pnl_delta=round(our["pnl"] - ref["pnl"], 4),
                    pnl_pct_delta=round(our["pnl_pct"] - ref["pnl_pct"], 6),
                    timestamp_delta_s=round(best_delta, 0),
                    matched=True,
                    source="matched",
                )
            )
        else:
            comparisons.append(
                TradeComparison(
                    trade_num=trade_num,
                    ref_entry_time=ref.get("entry_time"),
                    our_entry_time=None,
                    ref_exit_time=ref.get("exit_time"),
                    our_exit_time=None,
                    ref_entry_price=ref.get("entry_price"),
                    our_entry_price=None,
                    ref_exit_price=ref.get("exit_price"),
                    our_exit_price=None,
                    ref_pnl=ref.get("pnl"),
                    our_pnl=None,
                    ref_pnl_pct=ref.get("pnl_pct"),
                    our_pnl_pct=None,
                    entry_price_delta=None,
                    exit_price_delta=None,
                    pnl_delta=None,
                    pnl_pct_delta=None,
                    timestamp_delta_s=None,
                    matched=False,
                    source="extra_ref",
                )
            )

    for j, our in enumerate(our_parsed):
        if j in used_ours:
            continue
        trade_num += 1
        comparisons.append(
            TradeComparison(
                trade_num=trade_num,
                ref_entry_time=None,
                our_entry_time=our.get("entry_timestamp"),
                ref_exit_time=None,
                our_exit_time=our.get("exit_timestamp"),
                ref_entry_price=None,
                our_entry_price=our.get("entry_price"),
                ref_exit_price=None,
                our_exit_price=our.get("exit_price"),
                ref_pnl=None,
                our_pnl=our.get("pnl"),
                ref_pnl_pct=None,
                our_pnl_pct=our.get("pnl_pct"),
                entry_price_delta=None,
                exit_price_delta=None,
                pnl_delta=None,
                pnl_pct_delta=None,
                timestamp_delta_s=None,
                matched=False,
                source="extra_ours",
            )
        )

    matched = [c for c in comparisons if c.matched]
    extra_ref = [c for c in comparisons if c.source == "extra_ref"]
    extra_ours = [c for c in comparisons if c.source == "extra_ours"]

    stats = MatchStats(
        total_ref=len(ref_trades),
        total_ours=len(our_trades),
        matched_count=len(matched),
        extra_ref=len(extra_ref),
        extra_ours=len(extra_ours),
        match_rate=len(matched) / len(ref_trades) if ref_trades else 0.0,
        avg_ts_delta_s=sum(c.timestamp_delta_s for c in matched) / len(matched) if matched else 0.0,
        avg_entry_price_delta=sum(abs(c.entry_price_delta) for c in matched) / len(matched) if matched else 0.0,
        avg_pnl_delta=sum(abs(c.pnl_delta) for c in matched) / len(matched) if matched else 0.0,
    )

    return comparisons, stats
