"""LEAN factor-file CSV builder.

Spec: docs/superpowers/specs/2026-05-20-polygon-lean-data-lake-design.md § 5.1

LEAN factor-file format (path_policy: factor_files/<sym>.csv under the equity/usa subtree):
  date,price_factor,split_factor,ref_price
  - date: YYYYMMDD
  - price_factor: cumulative dividend back-adjustment multiplier
  - split_factor: cumulative split-adjustment multiplier
  - ref_price: closing price on the date (sanity-check value; we emit 0 in v1c)

V1c is intentionally minimal:
  - We emit two anchor rows (history_start and history_end) plus one row per
    corp-action event.
  - Factors are cumulative back-adjustment so historical raw prices multiplied
    by the factor give the back-adjusted view.
  - Real LEAN-vendor parity is deferred to Slice 5 (per spec deferred list).

LEAN's parser is forgiving about ref_price=0; the column is used only for a
sanity check that's bypassed when the value is non-positive.
"""

from __future__ import annotations

from datetime import date

from app.data_lake.polygon_corp_actions import DividendEvent, SplitEvent


def build_factor_file_bytes(
    symbol: str,
    splits: list[SplitEvent],
    dividends: list[DividendEvent],
    history_start: date,
    history_end: date,
) -> bytes:
    """Build the deterministic factor-file CSV body for one symbol.

    All inputs must be sorted ascending by date (polygon_corp_actions returns
    them that way). The returned bytes are ASCII CSV without a header row,
    which is what LEAN expects.
    """
    events = _merge_events(splits, dividends)

    # Compute cumulative factors traversing events from oldest to newest.
    # LEAN multiplies historical prices by the factors at the row's date to
    # back-adjust into the present-day view.
    cumulative_split_factor = 1.0
    for ev in events:
        if isinstance(ev, SplitEvent):
            cumulative_split_factor *= ev.split_from / ev.split_to

    cumulative_price_factor = 1.0
    # Dividends back-adjust by (1 - cash_amount / ref_price); we don't have
    # ref_price in Slice 1c, so approximate using cash_amount alone (factor
    # < 1 for any dividend). Vendor parity is deferred.
    for ev in events:
        if isinstance(ev, DividendEvent):
            cumulative_price_factor *= max(0.001, 1.0 - ev.cash_amount / 500.0)

    rows: list[tuple[str, float, float, float]] = []

    # Anchor at history_start with the full cumulative factors (these apply
    # to the entire pre-event window).
    rows.append(
        (
            _yyyymmdd(history_start),
            cumulative_price_factor,
            cumulative_split_factor,
            0.0,
        )
    )

    # One row per event with monotonically advancing factors.
    running_split = cumulative_split_factor
    running_price = cumulative_price_factor
    for ev in events:
        if isinstance(ev, SplitEvent):
            running_split = running_split / (ev.split_from / ev.split_to)
            ev_date = ev.execution_date
        else:
            running_price = running_price / max(0.001, 1.0 - ev.cash_amount / 500.0)
            ev_date = ev.ex_dividend_date
        rows.append((_dash_to_compact(ev_date), running_price, running_split, 0.0))

    # End-of-history anchor.
    rows.append((_yyyymmdd(history_end), 1.0, 1.0, 0.0))

    body_lines = [f"{d},{_f(pf)},{_f(sf)},{_f(rp)}" for d, pf, sf, rp in rows]
    return ("\n".join(body_lines) + "\n").encode("ascii")


def _yyyymmdd(d: date) -> str:
    return d.strftime("%Y%m%d")


def _dash_to_compact(iso: str) -> str:
    """'2020-08-31' -> '20200831'."""
    return iso.replace("-", "")


def _f(x: float) -> str:
    """Format a factor — LEAN accepts standard %g; we use %g for compactness."""
    return f"{x:g}"


def _merge_events(splits: list[SplitEvent], dividends: list[DividendEvent]) -> list[SplitEvent | DividendEvent]:
    """Merge into a single chronologically-sorted list.

    Date keys: SplitEvent.execution_date / DividendEvent.ex_dividend_date.
    Both are 'YYYY-MM-DD' strings; lexical sort = chronological sort.
    """

    def _date_key(ev: SplitEvent | DividendEvent) -> str:
        return ev.execution_date if isinstance(ev, SplitEvent) else ev.ex_dividend_date

    return sorted([*splits, *dividends], key=_date_key)
