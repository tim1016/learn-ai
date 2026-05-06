"""Split-policy unit tests.

Covers:
  * Each policy emits the expected fold count for a known window.
  * Fold boundaries are non-overlapping and totally cover the test
    region the policy intends.
  * Bad inputs (negative window, train_pct out of range, etc.) raise
    ValueError before any folds are emitted.
  * The ``build_split_policy`` factory dispatches correctly and
    rejects unknown ``kind``.
"""

from __future__ import annotations

import itertools

import pytest

from app.research.walk_forward.splits import (
    AnchoredSplitPolicy,
    ChronologicalSplitPolicy,
    FoldWindow,
    RollingSplitPolicy,
    build_split_policy,
    date_str_to_ms,
)

# A 1-year window — Jan 1 2024 → Jan 1 2025, NY-midnight anchored.
START_MS = date_str_to_ms("2024-01-02")
END_MS = date_str_to_ms("2024-12-31")
ONE_YEAR_DAYS = (END_MS - START_MS) // (24 * 60 * 60 * 1000)


# ---------------------------------------------------------------------------
# Chronological.
# ---------------------------------------------------------------------------
class TestChronologicalSplit:
    def test_default_70_30_emits_one_fold(self):
        folds = ChronologicalSplitPolicy().folds(START_MS, END_MS)
        assert len(folds) == 1
        f = folds[0]
        assert f.fold_index == 0
        assert f.train_start_ms == START_MS
        assert f.test_end_ms == END_MS
        # Train ends == test starts (no gap, no overlap).
        assert f.train_end_ms == f.test_start_ms

    def test_train_pct_50_splits_evenly(self):
        folds = ChronologicalSplitPolicy(train_pct=0.5).folds(START_MS, END_MS)
        cut = folds[0].train_end_ms
        # Half the days go to train (within ±1 day for NY-midnight snap).
        train_days = (cut - START_MS) // (24 * 60 * 60 * 1000)
        assert abs(train_days - ONE_YEAR_DAYS // 2) <= 1

    def test_describe_round_trips(self):
        policy = ChronologicalSplitPolicy(train_pct=0.6)
        d = policy.describe()
        assert d == {"kind": "chronological", "train_pct": 0.6}

    def test_invalid_train_pct_raises(self):
        with pytest.raises(ValueError, match="train_pct"):
            ChronologicalSplitPolicy(train_pct=0.0)
        with pytest.raises(ValueError, match="train_pct"):
            ChronologicalSplitPolicy(train_pct=1.0)
        with pytest.raises(ValueError, match="train_pct"):
            ChronologicalSplitPolicy(train_pct=1.5)

    def test_reversed_window_raises(self):
        with pytest.raises(ValueError, match="empty or reversed"):
            ChronologicalSplitPolicy().folds(END_MS, START_MS)


# ---------------------------------------------------------------------------
# Rolling.
# ---------------------------------------------------------------------------
class TestRollingSplit:
    def test_basic_rolling_180_60_60_over_one_year(self):
        # 180-day train + 60-day test, slide 60 days. ~3 folds in a year.
        policy = RollingSplitPolicy(train_days=180, test_days=60, step_days=60)
        folds = policy.folds(START_MS, END_MS)
        assert len(folds) >= 2  # at minimum two folds in 365 days

        for f in folds:
            train_days = (f.train_end_ms - f.train_start_ms) // (24 * 60 * 60 * 1000)
            test_days = (f.test_end_ms - f.test_start_ms) // (24 * 60 * 60 * 1000)
            assert train_days == 180
            assert test_days == 60
            # Train immediately precedes test.
            assert f.test_start_ms == f.train_end_ms

    def test_rolling_step_advances_by_step_days(self):
        policy = RollingSplitPolicy(train_days=60, test_days=30, step_days=30)
        folds = policy.folds(START_MS, END_MS)
        for prev, cur in itertools.pairwise(folds):
            advance_days = (cur.train_start_ms - prev.train_start_ms) // (
                24 * 60 * 60 * 1000
            )
            assert advance_days == 30

    def test_window_too_short_raises(self):
        policy = RollingSplitPolicy(train_days=400, test_days=60, step_days=30)
        with pytest.raises(ValueError, match="too short"):
            policy.folds(START_MS, END_MS)

    def test_invalid_params_raise_at_construction(self):
        with pytest.raises(ValueError, match="train_days"):
            RollingSplitPolicy(train_days=0, test_days=30, step_days=30)
        with pytest.raises(ValueError, match="test_days"):
            RollingSplitPolicy(train_days=60, test_days=0, step_days=30)
        with pytest.raises(ValueError, match="step_days"):
            RollingSplitPolicy(train_days=60, test_days=30, step_days=0)


# ---------------------------------------------------------------------------
# Anchored.
# ---------------------------------------------------------------------------
class TestAnchoredSplit:
    def test_anchored_train_grows_test_slides(self):
        policy = AnchoredSplitPolicy(initial_train_days=120, test_days=60, step_days=60)
        folds = policy.folds(START_MS, END_MS)
        assert len(folds) >= 2

        # Every fold starts at the window start (anchored).
        for f in folds:
            assert f.train_start_ms == START_MS

        # Train end grows monotonically.
        for prev, cur in itertools.pairwise(folds):
            assert cur.train_end_ms > prev.train_end_ms

    def test_anchored_window_too_short_raises(self):
        policy = AnchoredSplitPolicy(
            initial_train_days=300, test_days=120, step_days=30
        )
        with pytest.raises(ValueError, match="too short"):
            policy.folds(START_MS, END_MS)


# ---------------------------------------------------------------------------
# FoldWindow invariants.
# ---------------------------------------------------------------------------
class TestFoldWindowInvariants:
    def test_train_must_precede_test(self):
        with pytest.raises(ValueError, match="test cannot start before train ends"):
            FoldWindow(
                fold_index=0,
                train_start_ms=START_MS,
                train_end_ms=START_MS + 100,
                test_start_ms=START_MS + 50,  # before train_end
                test_end_ms=START_MS + 200,
            )

    def test_degenerate_train_raises(self):
        with pytest.raises(ValueError, match="train_start_ms must be"):
            FoldWindow(
                fold_index=0,
                train_start_ms=START_MS,
                train_end_ms=START_MS,
                test_start_ms=START_MS + 1,
                test_end_ms=START_MS + 2,
            )

    def test_degenerate_test_raises(self):
        with pytest.raises(ValueError, match="test_start_ms must be"):
            FoldWindow(
                fold_index=0,
                train_start_ms=START_MS,
                train_end_ms=START_MS + 100,
                test_start_ms=START_MS + 100,
                test_end_ms=START_MS + 100,
            )


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------
class TestBuildSplitPolicy:
    def test_chronological_default(self):
        policy = build_split_policy({"kind": "chronological"})
        assert isinstance(policy, ChronologicalSplitPolicy)
        assert policy.train_pct == 0.7

    def test_chronological_custom_pct(self):
        policy = build_split_policy({"kind": "chronological", "train_pct": 0.6})
        assert isinstance(policy, ChronologicalSplitPolicy)
        assert policy.train_pct == 0.6

    def test_rolling(self):
        policy = build_split_policy(
            {"kind": "rolling", "train_days": 90, "test_days": 30, "step_days": 30}
        )
        assert isinstance(policy, RollingSplitPolicy)
        assert (policy.train_days, policy.test_days, policy.step_days) == (90, 30, 30)

    def test_anchored(self):
        policy = build_split_policy(
            {
                "kind": "anchored",
                "initial_train_days": 120,
                "test_days": 60,
                "step_days": 60,
            }
        )
        assert isinstance(policy, AnchoredSplitPolicy)
        assert policy.initial_train_days == 120

    def test_unknown_kind_raises(self):
        with pytest.raises(ValueError, match="unknown split policy"):
            build_split_policy({"kind": "some_made_up_thing"})

    def test_missing_kind_raises(self):
        with pytest.raises(ValueError, match="must include a 'kind'"):
            build_split_policy({})
