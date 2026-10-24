"""The rule that puts recorded IV snapshots on a study's bars (#3525).

Before this rule a snapshot reached a bar only when the two timestamps were equal, which a snapshot taken at 09:35:02
and a bar stamped 09:30 (or ET midnight) never were. The cases below pin what the rule does: a snapshot goes to the
bar that contains it, the latest one wins a bar, a snapshot outside every bar is counted and dropped, and nothing is
ever carried, interpolated or averaged. A bar that contains no snapshot has no IV.
"""

from __future__ import annotations

import pytest

from app.engine.edge.iv_alignment import IV_ALIGNMENT_RULE, align_iv_snapshots_to_bars

MINUTE = 60_000
#: Three 15-minute bars 09:30, 09:45, 10:00 on one day (offsets from an arbitrary base), then a gap, then 11:00.
BASE = 1_700_000_000_000
STARTS = [BASE, BASE + 15 * MINUTE, BASE + 30 * MINUTE, BASE + 90 * MINUTE]
ENDS = [start + 15 * MINUTE for start in STARTS]


def snapshot(ts: int, iv30: float, **extra: float | None) -> dict:
    return {"ts": ts, "iv30": iv30, **extra}


def align(snapshots: list[dict]):
    return align_iv_snapshots_to_bars(snapshots, bar_starts_ms=STARTS, bar_ends_ms=ENDS)


def test_a_snapshot_inside_a_bar_goes_to_that_bar_stamped_at_its_start():
    result = align([snapshot(STARTS[1] + 5 * MINUTE + 2_123, 0.21)])

    assert result.points == [{"ts": STARTS[1], "iv30": 0.21}]
    assert (result.snapshots_read, result.bars_with_iv, result.superseded, result.outside_bars) == (1, 1, 0, 0)


@pytest.mark.parametrize("index", range(len(STARTS)))
def test_a_snapshot_stamped_at_a_bar_start_is_unchanged_so_input_that_already_lined_up_still_does(index):
    given = [snapshot(STARTS[index], 0.2 + index / 100, health_score=0.9)]

    assert align(given).points == given


def test_the_interval_is_half_open_so_the_end_instant_belongs_to_the_next_bar_or_to_none():
    adjacent = align([snapshot(ENDS[0], 0.2)])  # 09:45:00.000 is the second bar's start
    before_gap = align([snapshot(ENDS[2], 0.2)])  # 10:15:00.000 ends the third bar; the fourth starts at 11:00
    last_instant = align([snapshot(ENDS[0] - 1, 0.2)])

    assert [p["ts"] for p in adjacent.points] == [STARTS[1]]
    assert before_gap.points == [] and before_gap.outside_bars == 1
    assert [p["ts"] for p in last_instant.points] == [STARTS[0]]


def test_the_latest_snapshot_in_a_bar_wins_and_the_others_are_counted_as_superseded():
    result = align([snapshot(STARTS[0] + 1 * MINUTE, 0.20), snapshot(STARTS[0] + 9 * MINUTE, 0.23), snapshot(STARTS[0] + 4 * MINUTE, 0.21)])

    assert result.points == [{"ts": STARTS[0], "iv30": 0.23}], "the 09:39 snapshot, not the last one listed"
    assert (result.snapshots_read, result.bars_with_iv, result.superseded) == (3, 1, 2)


def test_two_snapshots_at_the_same_instant_go_to_the_one_read_last():
    result = align([snapshot(STARTS[2] + 100, 0.30), snapshot(STARTS[2] + 100, 0.31)])

    assert result.points == [{"ts": STARTS[2], "iv30": 0.31}]
    assert result.superseded == 1


def test_a_snapshot_inside_no_bar_is_counted_and_dropped():
    outside = [
        snapshot(STARTS[0] - 1, 0.2),  # before the first bar
        snapshot(STARTS[2] + 20 * MINUTE, 0.2),  # in the hole between the third bar and the fourth
        snapshot(ENDS[3] + 5, 0.2),  # after the last bar
    ]

    result = align(outside)

    assert result.points == []
    assert (result.snapshots_read, result.bars_with_iv, result.outside_bars) == (3, 0, 3)


def test_nothing_is_filled_carried_or_interpolated_a_bar_with_no_snapshot_has_no_iv():
    result = align([snapshot(STARTS[0] + 1, 0.20), snapshot(STARTS[3] + 1, 0.25)])

    assert [p["ts"] for p in result.points] == [STARTS[0], STARTS[3]], "the two bars between them are absent, not filled"
    assert [p["iv30"] for p in result.points] == [0.20, 0.25], "the values are the snapshots' own"


def test_every_other_field_travels_with_the_chosen_snapshot():
    result = align([snapshot(STARTS[0] + 1, 0.2, health_score=0.5), snapshot(STARTS[0] + 2, 0.3, health_score=None, variance_contribution_synthetic=0.1)])

    assert result.points == [{"ts": STARTS[0], "iv30": 0.3, "health_score": None, "variance_contribution_synthetic": 0.1}], (
        "a field the chosen snapshot lacks stays lacking, and an explicit null stays null"
    )


@pytest.mark.parametrize("count", [0, 1, 7])
def test_every_snapshot_is_accounted_for_exactly_once(count):
    snapshots = [snapshot(BASE - 20 * MINUTE + i * 9 * MINUTE + 17, 0.2 + i / 100) for i in range(count)]

    result = align(snapshots)

    assert result.bars_with_iv + result.superseded + result.outside_bars == result.snapshots_read == count


def test_the_input_order_does_not_change_which_snapshot_a_bar_holds():
    snapshots = [snapshot(STARTS[1] + i * MINUTE, 0.2 + i / 100) for i in range(5)]

    assert align(snapshots).points == align(list(reversed(snapshots))).points


def test_the_result_names_the_rule():
    assert align([]).as_dict() == {
        "rule": IV_ALIGNMENT_RULE, "snapshots_read": 0, "bars_with_iv": 0, "superseded": 0, "outside_bars": 0,
    }


@pytest.mark.parametrize(
    ("starts", "ends", "message"),
    [
        ([10, 20], [15], "same length"),
        ([10, 20], [10, 30], "not after it starts"),
        ([10, 20], [25, 30], "before bar 0 ends"),
    ],
)
def test_bars_that_are_not_a_clean_sequence_are_refused_rather_than_aligned_on_a_guess(starts, ends, message):
    with pytest.raises(ValueError, match=message):
        align_iv_snapshots_to_bars([snapshot(12, 0.2)], bar_starts_ms=starts, bar_ends_ms=ends)
