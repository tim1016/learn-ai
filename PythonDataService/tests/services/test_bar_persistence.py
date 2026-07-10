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
* Compact a closed day's JSONL into Parquet.
* Enumerate active dates and apply a retention policy.
* Emit structured counters for ``skipped_duplicate`` and
  ``applied_correction`` so an operator can spot a misbehaving feed.

All timestamps are ``int64`` ms UTC at every storage and wire boundary
(no ISO strings, no naive datetimes).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.broker.ibkr.models import IbkrMinuteBar
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


def test_compact_emits_parquet_and_archives_jsonl(tmp_path: Path) -> None:
    """``compact`` writes the day's bars to Parquet and renames the JSONL
    into a ``.compacted`` archive so the aggregator stops appending to it."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    store.append("SPY", "1m", _bar(ANCHOR_MS + 60_000))

    parquet_path = store.compact("SPY", "1m", ANCHOR_DATE)
    assert parquet_path.is_file()
    assert parquet_path.suffix == ".parquet"

    # JSONL is archived, not deleted, so an operator can audit the source.
    archived = list((tmp_path / "SPY" / "1m").glob("2026-04-01.jsonl.compacted-*"))
    assert len(archived) == 1
    assert not (tmp_path / "SPY" / "1m" / "2026-04-01.jsonl").exists()
    assert list((tmp_path / "SPY" / "1m").glob(".*.tmp")) == []


def test_compact_is_idempotent_after_jsonl_is_archived(tmp_path: Path) -> None:
    """A second compactor must not replace the published dataset with empty."""
    first = BarPersistence(root=tmp_path)
    second = BarPersistence(root=tmp_path)
    first.append("SPY", "1m", _bar(ANCHOR_MS))

    first_path = first.compact("SPY", "1m", ANCHOR_DATE)
    second_path = second.compact("SPY", "1m", ANCHOR_DATE)

    assert second_path == first_path
    assert [bar.start_ms for bar in second.read_parquet("SPY", "1m", ANCHOR_DATE)] == [
        ANCHOR_MS
    ]


def test_compact_failed_publish_preserves_parquet_and_jsonl(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed atomic replace leaves both prior truth sources intact."""
    from app.services import bar_persistence

    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    parquet = tmp_path / "SPY" / "1m" / "2026-04-01.parquet"
    bar_persistence.pq.write_table(
        bar_persistence.pa.table({"sentinel": [1]}),
        parquet,
    )
    original = parquet.read_bytes()
    monkeypatch.setattr(
        bar_persistence.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        store.compact("SPY", "1m", ANCHOR_DATE)

    assert parquet.read_bytes() == original
    assert (tmp_path / "SPY" / "1m" / "2026-04-01.jsonl").is_file()
    assert list((tmp_path / "SPY" / "1m").glob(".*.tmp")) == []


def test_append_waits_for_compaction_and_preserves_both_bars(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two store instances cannot place an append inside compaction's snapshot.

    The compactor must publish and archive its exact JSONL snapshot before a
    second instance creates the next active JSONL. Otherwise the second bar can
    land only in the archived source and disappear from both replay surfaces.
    """
    from app.services import bar_persistence

    compactor = BarPersistence(root=tmp_path)
    appender = BarPersistence(root=tmp_path)
    compactor.append("SPY", "1m", _bar(ANCHOR_MS))

    publish_started = threading.Event()
    allow_publish = threading.Event()
    append_finished = threading.Event()
    errors: list[BaseException] = []
    real_publish = bar_persistence._publish_parquet_atomic

    def paused_publish(table, path) -> None:
        publish_started.set()
        assert allow_publish.wait(timeout=5)
        real_publish(table, path)

    def compact() -> None:
        try:
            compactor.compact("SPY", "1m", ANCHOR_DATE)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def append() -> None:
        try:
            appender.append("SPY", "1m", _bar(ANCHOR_MS + 60_000))
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)
        finally:
            append_finished.set()

    monkeypatch.setattr(bar_persistence, "_publish_parquet_atomic", paused_publish)
    compact_thread = threading.Thread(target=compact)
    append_thread = threading.Thread(target=append)
    compact_thread.start()
    assert publish_started.wait(timeout=5)
    append_thread.start()
    assert not append_finished.wait(timeout=0.1)

    allow_publish.set()
    compact_thread.join(timeout=5)
    append_thread.join(timeout=5)

    assert not compact_thread.is_alive()
    assert not append_thread.is_alive()
    assert errors == []
    assert [bar.start_ms for bar in compactor.read_parquet("SPY", "1m", ANCHOR_DATE)] == [
        ANCHOR_MS
    ]
    assert [bar.start_ms for bar in appender.replay("SPY", "1m", ANCHOR_DATE)] == [
        ANCHOR_MS + 60_000
    ]


def test_read_parquet_round_trips_bars(tmp_path: Path) -> None:
    """``read_parquet`` returns the same bars that went in (start_ms-sorted,
    Decimal OHLC preserved)."""
    store = BarPersistence(root=tmp_path)
    store.append("SPY", "1m", _bar(ANCHOR_MS, close="100.00"))
    store.append("SPY", "1m", _bar(ANCHOR_MS + 60_000, close="100.50"))
    store.compact("SPY", "1m", ANCHOR_DATE)

    bars = store.read_parquet("SPY", "1m", ANCHOR_DATE)
    assert [b.start_ms for b in bars] == [ANCHOR_MS, ANCHOR_MS + 60_000]
    assert bars[0].close == Decimal("100.00")
    assert bars[1].close == Decimal("100.50")


def test_active_dates_lists_jsonl_and_parquet(tmp_path: Path) -> None:
    """``active_dates`` includes any date that has either a JSONL OR a
    Parquet — the operator's date picker shows the union, not the
    intersection."""
    store = BarPersistence(root=tmp_path)
    # Day 1 — JSONL only (still streaming or pre-compaction)
    store.append("SPY", "1m", _bar(ANCHOR_MS))
    # Day 2 — JSONL then compacted to Parquet
    store.append("SPY", "1m", _bar(ANCHOR_MS + 86_400_000))
    store.compact("SPY", "1m", ANCHOR_DATE + timedelta(days=1))

    dates = store.active_dates("SPY", "1m")
    assert dates == [ANCHOR_DATE, ANCHOR_DATE + timedelta(days=1)]


def test_active_dates_empty_when_no_data(tmp_path: Path) -> None:
    store = BarPersistence(root=tmp_path)
    assert store.active_dates("SPY", "1m") == []


def test_retention_deletes_files_older_than_window(tmp_path: Path) -> None:
    """Files outside the retention window are removed; recent ones are kept."""
    store = BarPersistence(root=tmp_path, retention_days=7)
    # Day -10 — outside the 7-day window, should be deleted.
    store.append("SPY", "1m", _bar(ANCHOR_MS - 10 * 86_400_000))
    # Day -3 — inside the window, should be kept.
    store.append("SPY", "1m", _bar(ANCHOR_MS - 3 * 86_400_000))

    deleted = store.apply_retention(now=datetime(2026, 4, 1, tzinfo=UTC))
    assert deleted == 1

    remaining = store.active_dates("SPY", "1m")
    assert remaining == [date(2026, 3, 29)]  # ANCHOR - 3 days


def test_retention_keeps_quarantined_files(tmp_path: Path) -> None:
    """Quarantined files survive retention until an operator audits them — they
    are *forensic evidence* of a bad feed, not normal data."""
    store = BarPersistence(root=tmp_path, retention_days=7)
    store.append("SPY", "1m", _bar(ANCHOR_MS - 10 * 86_400_000 + 60_000))
    # Force a regression to create a quarantine file.
    with pytest.raises(Exception):
        store.append("SPY", "1m", _bar(ANCHOR_MS - 10 * 86_400_000))

    store.apply_retention(now=datetime(2026, 4, 1, tzinfo=UTC))
    day_dir = tmp_path / "SPY" / "1m"
    quarantined = list(day_dir.glob("*.quarantine-*"))
    assert len(quarantined) == 1


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
