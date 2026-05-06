"""Split policies for walk-forward analysis.

Three policies ship in v1:

  * **Chronological** — single train/test cut. ``train_pct`` of the
    window goes to a single train fold; the remainder goes to one
    test fold. Used as the simplest "did this overfit?" check.

  * **Rolling** — fixed train + test window sizes that slide by a
    configurable step. Each fold has the same look-back length;
    older folds discard their earliest data as the window moves.
    Standard walk-forward in the LMDP / López de Prado sense.

  * **Anchored** — fixed start, growing train window, fixed test
    window, slide by a configurable step. Each fold's train spans
    *all* available history up to a moving cut-off. Useful when
    "the more history I see, the better" is the right model.

The policies operate on ``int64 ms UTC`` boundaries (matching the
ledger's wire format) and produce ``FoldWindow`` records that the
runner consumes. Date arithmetic is in NY-local because the engine's
session boundaries are NY-local — this matches the
``_date_to_ny_midnight_ms`` convention in ``app/research/runs/runner.py``.

Per the "fail fast on bad input" rule, every policy validates its
parameters at construction (negative window, train > total, step
larger than the window, etc.) rather than emitting an empty fold list
silently downstream.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def _add_days_ny(ms: int, days: int) -> int:
    """Advance an ``int64 ms UTC`` boundary by ``days`` *NY-local calendar days*.

    Plain ms arithmetic (``ms + days * 86_400_000``) silently drifts
    by one hour after a DST transition because UTC seconds-per-day is
    constant but NY's wall-clock day length isn't. Stepping in the
    NY zone preserves the "always at NY midnight" property the rest
    of the policy code depends on.

    Re-snaps to NY midnight after the addition because
    ``timedelta(days=1)`` on a tz-aware datetime may land an hour off
    on the DST boundary day; the ``replace(hour=0, …)`` call brings
    it back to the local-midnight invariant.
    """
    dt = datetime.fromtimestamp(ms / 1000, tz=_NY) + timedelta(days=days)
    midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def _ny_calendar_days_between(start_ms: int, end_ms: int) -> int:
    """Return the calendar-day distance between two NY-midnight ms boundaries.

    Counted in NY-local calendar days, so a window from 2024-03-01
    through 2024-04-01 is 31 days regardless of the DST transition
    inside it. Required because plain ``(end - start) // _MS_PER_DAY``
    is off by ±1 across DST transitions.
    """
    start_date = datetime.fromtimestamp(start_ms / 1000, tz=_NY).date()
    end_date = datetime.fromtimestamp(end_ms / 1000, tz=_NY).date()
    return (end_date - start_date).days


@dataclass(frozen=True)
class FoldWindow:
    """One train+test pair in ``int64 ms UTC``.

    Phase 4A only consumes the test side (the spec runs against the
    test window only); train fields are recorded for ledger
    transparency and Phase 4B reuse.
    """

    fold_index: int
    train_start_ms: int
    train_end_ms: int
    test_start_ms: int
    test_end_ms: int

    def __post_init__(self) -> None:
        # Order invariant: train precedes test, neither is degenerate.
        if self.train_start_ms >= self.train_end_ms:
            raise ValueError(
                f"Fold {self.fold_index}: train_start_ms must be < train_end_ms "
                f"(got {self.train_start_ms} >= {self.train_end_ms})"
            )
        if self.test_start_ms >= self.test_end_ms:
            raise ValueError(
                f"Fold {self.fold_index}: test_start_ms must be < test_end_ms "
                f"(got {self.test_start_ms} >= {self.test_end_ms})"
            )
        if self.test_start_ms < self.train_end_ms:
            raise ValueError(
                f"Fold {self.fold_index}: test cannot start before train ends "
                f"(test_start={self.test_start_ms}, train_end={self.train_end_ms})"
            )


class SplitPolicy(ABC):
    """Abstract base — concrete subclasses emit folds for a window."""

    @abstractmethod
    def folds(self, start_ms: int, end_ms: int) -> list[FoldWindow]:
        """Generate folds inside the half-open window ``[start_ms, end_ms)``.

        ``start_ms`` and ``end_ms`` are NY-midnight ``int64 ms UTC``
        boundaries (the same anchoring the run ledger uses). Returned
        folds also use those units.

        Implementations must raise ``ValueError`` on degenerate inputs
        (window too short to contain even one fold, etc.) so callers
        get a clear failure rather than a silent zero-fold result.
        """

    @abstractmethod
    def describe(self) -> dict:
        """Serializable description of this policy for the ledger."""


# ---------------------------------------------------------------------------
# Chronological — one train, one test, single cut.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ChronologicalSplitPolicy(SplitPolicy):
    """A single train/test cut at ``train_pct`` of the window.

    ``train_pct=0.7`` means the first 70% (in calendar ms) is the train
    fold and the remaining 30% is the test fold. The train window is
    informational only in Phase 4A — the runner only executes the
    test side.
    """

    train_pct: float = 0.7

    def __post_init__(self) -> None:
        if not 0.0 < self.train_pct < 1.0:
            raise ValueError(
                f"train_pct must be in (0, 1) exclusive (got {self.train_pct})"
            )

    def folds(self, start_ms: int, end_ms: int) -> list[FoldWindow]:
        if end_ms <= start_ms:
            raise ValueError(
                f"Window is empty or reversed (start={start_ms}, end={end_ms})"
            )
        cut_ms = start_ms + int((end_ms - start_ms) * self.train_pct)
        # Snap the cut to NY midnight so train/test boundaries don't
        # straddle a session — keeps ledger windows readable and avoids
        # off-by-one bar-attribution surprises.
        cut_ms = _snap_to_ny_midnight(cut_ms)
        if cut_ms <= start_ms or cut_ms >= end_ms:
            raise ValueError(
                f"train_pct={self.train_pct} produced a degenerate cut at "
                f"{cut_ms}; widen the window or pick a different pct"
            )
        return [
            FoldWindow(
                fold_index=0,
                train_start_ms=start_ms,
                train_end_ms=cut_ms,
                test_start_ms=cut_ms,
                test_end_ms=end_ms,
            )
        ]

    def describe(self) -> dict:
        return {"kind": "chronological", "train_pct": self.train_pct}


# ---------------------------------------------------------------------------
# Rolling — fixed train + test, slide by step.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RollingSplitPolicy(SplitPolicy):
    """Fixed train + test window sizes; slide by ``step_days`` per fold.

    Fold ``k`` uses train span
    ``[start + k*step, start + k*step + train_days)`` and test span
    ``[start + k*step + train_days, start + k*step + train_days + test_days)``.
    Folds are emitted while the test span fits inside the overall
    window. Older history outside the train span is *not* visible to
    fold ``k`` — that's the "rolling" property.
    """

    train_days: int
    test_days: int
    step_days: int

    def __post_init__(self) -> None:
        if self.train_days <= 0:
            raise ValueError(f"train_days must be positive (got {self.train_days})")
        if self.test_days <= 0:
            raise ValueError(f"test_days must be positive (got {self.test_days})")
        if self.step_days <= 0:
            raise ValueError(f"step_days must be positive (got {self.step_days})")

    def folds(self, start_ms: int, end_ms: int) -> list[FoldWindow]:
        if end_ms <= start_ms:
            raise ValueError(
                f"Window is empty or reversed (start={start_ms}, end={end_ms})"
            )
        total_days_needed = self.train_days + self.test_days
        window_days = _ny_calendar_days_between(start_ms, end_ms)
        if window_days < total_days_needed:
            raise ValueError(
                f"Window of {window_days} days is too short to fit a "
                f"{self.train_days}-day train + {self.test_days}-day test fold"
            )

        # Step in NY-local calendar days so every fold boundary lands
        # on NY midnight even after a DST transition (plain ms
        # arithmetic drifts by an hour each spring/fall).
        folds: list[FoldWindow] = []
        fold_index = 0
        cursor = start_ms
        while True:
            train_end = _add_days_ny(cursor, self.train_days)
            test_end = _add_days_ny(cursor, self.train_days + self.test_days)
            if test_end > end_ms:
                break
            folds.append(
                FoldWindow(
                    fold_index=fold_index,
                    train_start_ms=cursor,
                    train_end_ms=train_end,
                    test_start_ms=train_end,
                    test_end_ms=test_end,
                )
            )
            fold_index += 1
            cursor = _add_days_ny(cursor, self.step_days)
        return folds

    def describe(self) -> dict:
        return {
            "kind": "rolling",
            "train_days": self.train_days,
            "test_days": self.test_days,
            "step_days": self.step_days,
        }


# ---------------------------------------------------------------------------
# Anchored — fixed start, train grows, test slides.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AnchoredSplitPolicy(SplitPolicy):
    """Anchored walk-forward: fixed start, growing train, fixed test.

    Fold ``k`` uses train span
    ``[start, start + initial_train_days + k*step_days)`` and test span
    immediately after. Each fold sees *more* history than the
    previous one — appropriate when "longer training is strictly
    better" matches the model's behavior.
    """

    initial_train_days: int
    test_days: int
    step_days: int

    def __post_init__(self) -> None:
        if self.initial_train_days <= 0:
            raise ValueError(
                f"initial_train_days must be positive (got {self.initial_train_days})"
            )
        if self.test_days <= 0:
            raise ValueError(f"test_days must be positive (got {self.test_days})")
        if self.step_days <= 0:
            raise ValueError(f"step_days must be positive (got {self.step_days})")

    def folds(self, start_ms: int, end_ms: int) -> list[FoldWindow]:
        if end_ms <= start_ms:
            raise ValueError(
                f"Window is empty or reversed (start={start_ms}, end={end_ms})"
            )
        window_days = _ny_calendar_days_between(start_ms, end_ms)
        if window_days < self.initial_train_days + self.test_days:
            raise ValueError(
                f"Window of {window_days} days is too short for an anchored "
                f"split with {self.initial_train_days}-day initial train + "
                f"{self.test_days}-day test"
            )

        # NY-local stepping (see RollingSplitPolicy for the DST rationale).
        folds: list[FoldWindow] = []
        fold_index = 0
        train_end = _add_days_ny(start_ms, self.initial_train_days)
        while True:
            test_end = _add_days_ny(train_end, self.test_days)
            if test_end > end_ms:
                break
            folds.append(
                FoldWindow(
                    fold_index=fold_index,
                    train_start_ms=start_ms,
                    train_end_ms=train_end,
                    test_start_ms=train_end,
                    test_end_ms=test_end,
                )
            )
            fold_index += 1
            train_end = _add_days_ny(train_end, self.step_days)
        return folds

    def describe(self) -> dict:
        return {
            "kind": "anchored",
            "initial_train_days": self.initial_train_days,
            "test_days": self.test_days,
            "step_days": self.step_days,
        }


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------
def build_split_policy(spec: dict) -> SplitPolicy:
    """Construct a policy from a kind-discriminated dict.

    The HTTP layer passes the policy as a dict on the request body
    (matches the ``StrategySpec`` precedent — JSON-as-spec is the
    repository's convention). This function is the boundary between
    untyped JSON and the typed dataclass policies.
    """
    if "kind" not in spec:
        raise ValueError("split_policy must include a 'kind' discriminator")
    kind = spec["kind"]
    if kind == "chronological":
        return ChronologicalSplitPolicy(train_pct=float(spec.get("train_pct", 0.7)))
    if kind == "rolling":
        return RollingSplitPolicy(
            train_days=int(spec["train_days"]),
            test_days=int(spec["test_days"]),
            step_days=int(spec["step_days"]),
        )
    if kind == "anchored":
        return AnchoredSplitPolicy(
            initial_train_days=int(spec["initial_train_days"]),
            test_days=int(spec["test_days"]),
            step_days=int(spec["step_days"]),
        )
    raise ValueError(
        f"unknown split policy kind {kind!r} — expected one of "
        f"chronological / rolling / anchored"
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------
def _snap_to_ny_midnight(ms: int) -> int:
    """Round ``ms`` *down* to the previous NY-local midnight in UTC ms.

    Keeps train/test boundaries aligned with session boundaries — the
    engine filters bars by NY-local date, so a cut mid-session would
    leave a bar dangling between train and test.
    """
    dt_utc = datetime.fromtimestamp(ms / 1000, tz=_NY)
    midnight = dt_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp() * 1000)


def ms_to_date_str(ms: int) -> str:
    """Format an ``int64 ms UTC`` (anchored at NY midnight) as ``YYYY-MM-DD``.

    The runner accepts ``date`` objects as the start/end window; this
    helper converts a fold's ms boundary into the date the runner
    expects.
    """
    return datetime.fromtimestamp(ms / 1000, tz=_NY).strftime("%Y-%m-%d")


def date_str_to_ms(s: str) -> int:
    """Inverse of ``ms_to_date_str`` — parse ``YYYY-MM-DD`` to NY-midnight ms."""
    dt = datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=_NY)
    return int(dt.timestamp() * 1000)
