"""Tests for the BarPersistence module (Slice 3).

The persistence layer is the foundation for restart resilience on the
live bar aggregator (Slice 4) and the ``/chart-snapshot`` endpoint
(Slice 5). It must:

* Append closed bars to a per-(symbol, resolution, date) JSONL append-log,
  with idempotency on exact-duplicate redeliveries.
* Apply mid-aggregate corrections (same ``start_ms``, different payload).
* **Quarantine** the day's JSONL when a non-monotonic regression arrives
  (``start_ms < last accepted``) — never silently repair, per the rigor
  rules' ban on ``drop_duplicates`` / forward-fill (see
  ``.claude/rules/numerical-rigor.md`` → "Timestamp rigor").
* Replay today's JSONL on subscribe.
* Enumerate active dates.
* Emit structured counters for ``skipped_duplicate`` and
  ``applied_correction`` so an operator can spot a misbehaving feed.

All timestamps are ``int64`` ms UTC at every storage and wire boundary
(no ISO strings, no naive datetimes).
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.bar_models import IbkrMinuteBar
from app.services.bar_persistence import (
    AppendOutcome,
    BarPersistence,
)


def _bar(start_ms: int, *, close: str = "100.00", volume: int = 10) -> IbkrMinuteBar:
    """Build a closed 1-min bar at ``start_ms`` for testing."""
    return IbkrMinuteBar(
        symbol="SPY",
        start_ms=start_ms,
        end_ms=start_ms + 60_000,
        open=Decimal("100.00"),
        high=Decimal("100.10"),
        low=Decimal("99.90"),
        close=Decimal(close),
        volume=volume,
        fetched_at_ms=start_ms + 60_000,
    )


# 2026-04-01 00:00:00 UTC — used as the "today" anchor in tests so
# we never call ``datetime.now()`` and never have to thread a clock fake.
ANCHOR_MS = 1_775_001_600_000
ANCHOR_DATE = date(2026, 4, 1)


def test_append_writes_new_bar_to_dated_jsonl(tmp_path: Path) -> None:
    """A WRITTEN outcome lands one JSONL line in the date-partitioned file."""
    store = BarPersistence(root=tmp_path)
    outcome = store.append("SPY", "1m", _bar(ANCHOR_MS))
    assert outcome is AppendOutcome.WRITTEN

    jsonl = tmp_path / "SPY" / "1m" / "2026-04-01.jsonl"
    assert jsonl.is_file()
    lines = jsonl.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    record = json.loads(lines[0])
    # Action tag distinguishes initial appends from corrections during replay.
    assert record["action"] == "append"
    assert record["bar"]["start_ms"] == ANCHOR_MS
    assert record["bar"]["close"] == "100.00"


def test_append_skips_exact_duplicate_redelivery(tmp_path: Path) -> None:
    """Same ``start_ms`` + identical payload as the last accepted bar is a
    redelivery — never written twice, never folded into anything."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    outcome = store.append("SPY", "1m", _bar(ANCHOR_MS))
    assert outcome is AppendOutcome.SKIPPED_DUPLICATE

    jsonl = tmp_path / "SPY" / "1m" / "2026-04-01.jsonl"
    assert len(jsonl.read_text(encoding="utf-8").splitlines()) == 1

    # The skipped_duplicate counter is the operator's signal a feed is over-
    # delivering — gate against a future regression.
    counters = store.counters("SPY", "1m")
    assert counters.skipped_duplicate == 1


def test_append_records_correction_when_payload_differs(tmp_path: Path) -> None:
    """A second arrival at the same ``start_ms`` with a different payload is a
    mid-aggregate correction (vendor revised the bar before the next one
    arrived) — record it with an ``action=correction`` line so replay can
    deterministically reconstruct the final value."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS, close="100.00"))
    outcome = store.append("SPY", "1m", _bar(ANCHOR_MS, close="100.55"))
    assert outcome is AppendOutcome.APPLIED_CORRECTION

    jsonl = tmp_path / "SPY" / "1m" / "2026-04-01.jsonl"
    lines = [json.loads(line) for line in jsonl.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 2
    assert lines[0]["action"] == "append"
    assert lines[1]["action"] == "correction"
    assert lines[1]["bar"]["close"] == "100.55"

    counters = store.counters("SPY", "1m")
    assert counters.applied_correction == 1


def test_append_quarantines_jsonl_on_non_monotonic_regression(tmp_path: Path) -> None:
    """A bar whose ``start_ms`` is earlier than the last accepted bar is a
    silent-data-corruption signal — the file is renamed (quarantined) and
    a ``BarPersistenceRegressionError`` raised. Per numerical-rigor §
    "Timestamp rigor" the persistence layer must NOT silently repair the
    feed (no drop_duplicates, no reorder) — the regression must surface."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS + 60_000))

    with pytest.raises(Exception) as exc:
        store.append("SPY", "1m", _bar(ANCHOR_MS))
    # The exception type must signal "data regression" so callers (the
    # aggregator) treat it as a fatal-halt class of event.
    assert "non-monotonic" in str(exc.value).lower()

    day_dir = tmp_path / "SPY" / "1m"
    quarantined = list(day_dir.glob("2026-04-01.jsonl.quarantine-*"))
    assert len(quarantined) == 1, f"expected one quarantine file, found {quarantined}"
    assert not (day_dir / "2026-04-01.jsonl").exists()


def test_replay_reconstructs_bars_with_corrections_applied(tmp_path: Path) -> None:
    """Replay yields one bar per ``start_ms``; ``correction`` lines override
    earlier ``append`` lines on the same key. Output is ``start_ms``-sorted."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    store.append("SPY", "1m", _bar(ANCHOR_MS, close="100.55"))  # correction
    store.append("SPY", "1m", _bar(ANCHOR_MS + 60_000, close="101.00"))

    bars = store.replay("SPY", "1m", ANCHOR_DATE)
    assert len(bars) == 2
    assert bars[0].start_ms == ANCHOR_MS
    assert bars[0].close == Decimal("100.55")
    assert bars[1].start_ms == ANCHOR_MS + 60_000
    assert bars[1].close == Decimal("101.00")


def test_replay_skips_exact_duplicate_lines_in_jsonl(tmp_path: Path) -> None:
    """Two identical lines (a writer that wrote, crashed mid-fsync, replayed)
    must collapse to one bar on replay — no double-count."""
    jsonl = tmp_path / "SPY" / "1m" / "2026-04-01.jsonl"
    jsonl.parent.mkdir(parents=True)
    record = {"action": "append", "ts_ms": ANCHOR_MS, "bar": _bar(ANCHOR_MS).model_dump(mode="json")}
    jsonl.write_text(
        json.dumps(record) + "\n" + json.dumps(record) + "\n", encoding="utf-8"
    )

    store = BarPersistence(root=tmp_path)
    bars = store.replay("SPY", "1m", ANCHOR_DATE)
    assert len(bars) == 1


def test_replay_returns_empty_when_no_jsonl(tmp_path: Path) -> None:
    """A date with no JSONL (pre-persistence, or post-compaction-only) yields
    an empty list — never raises."""
    store = BarPersistence(root=tmp_path)
    assert store.replay("SPY", "1m", ANCHOR_DATE) == []


def test_active_dates_lists_every_jsonl_day(tmp_path: Path) -> None:
    """``active_dates`` includes every date that has a JSONL."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    store.append("SPY", "1m", _bar(ANCHOR_MS + 86_400_000))

    dates = store.active_dates("SPY", "1m")
    assert dates == [ANCHOR_DATE, ANCHOR_DATE + timedelta(days=1)]


def test_active_dates_empty_when_no_data(tmp_path: Path) -> None:
    store = BarPersistence(root=tmp_path)
    assert store.active_dates("SPY", "1m") == []


def test_counters_are_per_symbol_resolution(tmp_path: Path) -> None:
    """The counters scope is ``(symbol, resolution)`` — a duplicate on the 5s
    stream must not bump the 1m counter."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    store.append("SPY", "1m", _bar(ANCHOR_MS))  # 1m dup
    store.append("SPY", "5s", _bar(ANCHOR_MS))

    assert store.counters("SPY", "1m").skipped_duplicate == 1
    assert store.counters("SPY", "5s").skipped_duplicate == 0


def test_resumption_after_restart_picks_up_cursor_from_jsonl(tmp_path: Path) -> None:
    """A fresh BarPersistence pointed at an existing directory must reconstruct
    the per-(symbol, resolution) cursor from the JSONL on first ``append`` so
    a restart doesn't lose monotonicity guards."""
    store1 = BarPersistence(root=tmp_path)
    store1.append("SPY", "1m", _bar(ANCHOR_MS + 60_000))

    # Restart simulates the daemon coming back up after a crash.
    store2 = BarPersistence(root=tmp_path)
    # An earlier bar must still quarantine — the cursor survived.
    with pytest.raises(Exception):
        store2.append("SPY", "1m", _bar(ANCHOR_MS))


@pytest.mark.parametrize(
    "symbol,resolution",
    [
        ("../../ETC", "1m"),
        ("..", "1m"),
        ("SPY", "../1m"),
        ("SPY/../..", "1m"),
        ("SPY", ".."),
    ],
)
def test_traversal_components_are_refused_before_any_path_is_touched(
    tmp_path: Path, symbol: str, resolution: str
) -> None:
    """``symbol``/``resolution`` are public query parameters, not path input.

    They name directories under the artifacts root, so a traversal component
    would otherwise let a caller read and write outside it (CodeQL
    ``py/path-injection``). The refusal is a ``ValueError`` at the
    ``_key``/``_dir`` choke point, so no file is created on the way out.
    """
    root = tmp_path / "bars"
    store = BarPersistence(root)

    with pytest.raises(ValueError, match="unsafe"):
        store.append(symbol, resolution, _bar(ANCHOR_MS))
    with pytest.raises(ValueError, match="unsafe"):
        store.replay(symbol, resolution, ANCHOR_DATE)
    with pytest.raises(ValueError, match="unsafe"):
        store.active_dates(symbol, resolution)

    assert list(root.rglob("*")) == []


def test_ordinary_symbols_and_resolutions_still_resolve_under_the_root(
    tmp_path: Path,
) -> None:
    """Containment must not cost the dotted tickers the feed really sends."""
    root = tmp_path / "bars"
    store = BarPersistence(root)

    assert store.append("brk.b", "1m", _bar(ANCHOR_MS)) is AppendOutcome.WRITTEN

    written = list(root.rglob("*.jsonl"))
    assert len(written) == 1
    assert written[0].parent.name == "1m"
    assert written[0].parent.parent.name == "BRK.B"
