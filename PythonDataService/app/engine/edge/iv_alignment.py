"""Recorded IV snapshots aligned to a study's bars (#3525).

The IV recorder writes one to four snapshots a day, each stamped with the moment it was taken (09:35 ET, 12:30, ...).
A study's bars are stamped with their start. Matching the two by equal timestamp lined a snapshot up with a bar
only by coincidence, so a recorded IV almost never reached a bar. This is the rule that replaces it.

Formula: a snapshot taken at ``t`` belongs to the bar whose half-open interval ``[start, end)`` contains ``t``. A bar
  holds the latest snapshot it contains (ties on ``t`` go to the one read last). A snapshot inside no bar (a weekend,
  the overnight hole between extended sessions, a bar the vendor sent no trades for) is dropped and counted. A bar
  that contains no snapshot has no IV. Nothing is carried forward, interpolated or averaged: sparse stays sparse
  (ADR 0071 decision 9), and the counts say how sparse.
Reference: internal. Neither an external algorithm nor a vendor convention; it is a repo decision that keeps ADR 0071
  decision 9 and replaces the exact-equality match it left implicit. Which rule the page should use is an owner
  decision (D02 of PRD #3490): a snapshot at an exact bar start still lands on that bar, so a caller whose IV was
  already stamped at bar starts is unchanged.
Canonical implementation: this file.
Validated against: tests/edge/test_iv_alignment.py
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

#: The name a result and a cache identity give this rule.
IV_ALIGNMENT_RULE = "snapshot_in_bar_latest"


@dataclass(frozen=True)
class IvAlignment:
    """The snapshots that reached a bar, re-stamped at that bar's start, and what became of every other snapshot."""

    #: One point per bar that holds an IV, oldest first: the chosen snapshot with ``ts`` set to the bar's start.
    points: list[dict[str, Any]]
    snapshots_read: int
    #: Snapshots dropped because a later snapshot in the same bar was chosen instead.
    superseded: int
    #: Snapshots that fall inside no bar.
    outside_bars: int

    @property
    def bars_with_iv(self) -> int:
        return len(self.points)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": IV_ALIGNMENT_RULE,
            "snapshots_read": self.snapshots_read,
            "bars_with_iv": self.bars_with_iv,
            "superseded": self.superseded,
            "outside_bars": self.outside_bars,
        }


def align_iv_snapshots_to_bars(
    snapshots: Sequence[Mapping[str, Any]],
    *,
    bar_starts_ms: Sequence[int],
    bar_ends_ms: Sequence[int],
) -> IvAlignment:
    """Assign each ``{ts, iv30, ...}`` snapshot to the bar containing ``ts``; see the module docstring for the rule.

    ``bar_starts_ms`` must be strictly increasing and each bar must end after it starts and no later than the next
    bar starts; a series that is not is a bug upstream and raises rather than being aligned on a guess.
    """
    if len(bar_starts_ms) != len(bar_ends_ms):
        raise ValueError("bar_starts_ms and bar_ends_ms must be the same length")
    for i, (start, end) in enumerate(zip(bar_starts_ms, bar_ends_ms, strict=True)):
        if end <= start:
            raise ValueError(f"bar {i} ends at {end} ms, not after it starts at {start} ms")
        if i > 0 and start < bar_ends_ms[i - 1]:
            raise ValueError(f"bar {i} starts at {start} ms, before bar {i - 1} ends at {bar_ends_ms[i - 1]} ms")

    chosen: dict[int, Mapping[str, Any]] = {}
    superseded = 0
    outside = 0
    # A stable sort keeps the read order among equal timestamps, so the one read last wins them.
    for snapshot in sorted(snapshots, key=lambda s: int(s["ts"])):
        ts = int(snapshot["ts"])
        index = bisect_right(bar_starts_ms, ts) - 1
        if index < 0 or ts >= bar_ends_ms[index]:
            outside += 1
            continue
        if index in chosen:
            superseded += 1
        chosen[index] = snapshot
    points = [{**chosen[index], "ts": int(bar_starts_ms[index])} for index in sorted(chosen)]
    return IvAlignment(points=points, snapshots_read=len(snapshots), superseded=superseded, outside_bars=outside)
