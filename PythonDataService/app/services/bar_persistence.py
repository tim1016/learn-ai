"""Persistent storage for live OHLCV bars (Slice 3).

The aggregator owns an in-memory ring buffer; this module owns the
disk-resident replay log + compaction so a restart can:

1. Re-deliver today's bars to the chart immediately on subscribe.
2. Detect a misbehaving feed by surfacing duplicates / corrections as
   structured counters and quarantining a non-monotonic regression.

Storage layout (per-symbol, per-resolution, per-UTC-date):

    <root>/<symbol>/<resolution>/<YYYY-MM-DD>.jsonl
    <root>/<symbol>/<resolution>/<YYYY-MM-DD>.parquet
    <root>/<symbol>/<resolution>/<YYYY-MM-DD>.jsonl.compacted-<ts>
    <root>/<symbol>/<resolution>/<YYYY-MM-DD>.jsonl.quarantine-<ts>

Each JSONL line carries ``{action, ts_ms, bar}`` where ``action`` is one
of:

    * ``"append"`` — first arrival for this ``start_ms``.
    * ``"correction"`` — same ``start_ms`` as a prior line but a different
      payload (vendor revised the open bar). Replay picks the latest
      ``correction`` over the earliest ``append`` for the same key.

A non-monotonic ``start_ms`` (incoming < last accepted) is **never**
silently repaired; per ``.claude/rules/numerical-rigor.md`` →
"Timestamp rigor → Ban list" the day's JSONL is quarantined and
``BarPersistenceRegressionError`` is raised so the aggregator fails
fast. The quarantined file is forensic evidence for the operator —
retention leaves it in place.
"""

from __future__ import annotations

import io
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from app.broker.ibkr.models import IbkrMinuteBar

logger = logging.getLogger(__name__)

# Suffix matchers + writers — kept as constants so every callsite uses the
# same naming convention.
_JSONL_SUFFIX: Final = ".jsonl"
_PARQUET_SUFFIX: Final = ".parquet"
_QUARANTINE_PREFIX: Final = "quarantine-"
_COMPACTED_PREFIX: Final = "compacted-"

# UTC-date / ms-of-day math constants. Storage is keyed on the UTC date of
# the bar's ``start_ms``; the rigor rules require all wire/storage
# timestamps to be int64 ms UTC so the date partition uses the same TZ.
_MS_PER_DAY: Final = 86_400_000


class BarPersistenceError(Exception):
    """Base error for the persistence layer."""


class BarPersistenceRegressionError(BarPersistenceError):
    """Raised when an incoming bar's ``start_ms`` is earlier than the last
    accepted bar for the same (symbol, resolution). The day's JSONL is
    quarantined; the caller must treat the feed as compromised."""


class AppendOutcome:
    """Outcomes of an ``append`` call.

    Implemented as a class with class-level instances rather than an ``Enum``
    so the structured-log payload can carry a stable lowercase string
    (``written`` / ``skipped_duplicate`` / ``applied_correction``).
    """

    WRITTEN: AppendOutcome
    SKIPPED_DUPLICATE: AppendOutcome
    APPLIED_CORRECTION: AppendOutcome

    __slots__ = ("name",)

    def __init__(self, name: str) -> None:
        self.name = name

    def __repr__(self) -> str:
        return f"AppendOutcome.{self.name.upper()}"


AppendOutcome.WRITTEN = AppendOutcome("written")
AppendOutcome.SKIPPED_DUPLICATE = AppendOutcome("skipped_duplicate")
AppendOutcome.APPLIED_CORRECTION = AppendOutcome("applied_correction")


@dataclass
class Counters:
    """Per-(symbol, resolution) observability counters surfaced via
    :meth:`BarPersistence.counters`."""

    skipped_duplicate: int = 0
    applied_correction: int = 0
    regression_quarantined: int = 0


@dataclass
class _Cursor:
    """Per-(symbol, resolution) in-memory monotonicity guard."""

    last_start_ms: int | None = None
    last_payload_key: tuple | None = None
    counters: Counters = field(default_factory=Counters)


def _ms_to_date(ms: int) -> date:
    """UTC date of ``ms`` (int64 ms since epoch)."""
    return datetime.fromtimestamp(ms / 1000.0, tz=UTC).date()


def _now_ms_utc() -> int:
    """Wall-clock ``now`` in int64 ms UTC. Used for quarantine /
    compaction suffixes so the file name is monotonic."""
    return int(datetime.now(UTC).timestamp() * 1000)


def _payload_key(bar: IbkrMinuteBar) -> tuple:
    """Tuple identity for "is this bar payload exactly what we already
    accepted?" — used to discriminate dup vs correction."""
    return (
        bar.end_ms,
        str(bar.open),
        str(bar.high),
        str(bar.low),
        str(bar.close),
        bar.volume,
    )


def _bar_to_record(bar: IbkrMinuteBar) -> dict:
    """Serializable view of one bar for the JSONL."""
    return bar.model_dump(mode="json")


def _record_to_bar(record: dict) -> IbkrMinuteBar:
    return IbkrMinuteBar.model_validate(record)


class BarPersistence:
    """JSONL append-log + Parquet compaction for live OHLCV bars."""

    def __init__(self, root: Path, *, retention_days: int = 30) -> None:
        self._root = Path(root)
        self._retention_days = int(retention_days)
        # Per-(symbol, resolution) cursor + counter map. Reconstructed on
        # demand from the JSONL when a process starts cold.
        self._cursors: dict[tuple[str, str], _Cursor] = {}
        # The aggregator's pump runs in one task per symbol-resolution, but
        # a future fan-out (or a test spinning up two clients) could race —
        # serialize all writes through a single lock.
        self._lock = threading.Lock()

    # ─────────────────────────── public API ──────────────────────────────

    def append(self, symbol: str, resolution: str, bar: IbkrMinuteBar) -> AppendOutcome:
        """Persist one bar; return the outcome.

        Raises :class:`BarPersistenceRegressionError` on a non-monotonic
        regression after quarantining the day's JSONL.
        """
        with self._lock:
            cursor = self._get_or_load_cursor(symbol, resolution)
            jsonl = self._jsonl_path(symbol, resolution, _ms_to_date(bar.start_ms))

            if cursor.last_start_ms is None:
                self._write_line(jsonl, "append", bar)
                cursor.last_start_ms = bar.start_ms
                cursor.last_payload_key = _payload_key(bar)
                return AppendOutcome.WRITTEN

            if bar.start_ms == cursor.last_start_ms:
                payload = _payload_key(bar)
                if payload == cursor.last_payload_key:
                    cursor.counters.skipped_duplicate += 1
                    logger.info(
                        "bar_persistence: skipped_duplicate",
                        extra={
                            "symbol": symbol,
                            "resolution": resolution,
                            "start_ms": bar.start_ms,
                            "action": "skipped_duplicate",
                        },
                    )
                    return AppendOutcome.SKIPPED_DUPLICATE
                # Mid-aggregate correction: same start_ms, different payload.
                self._write_line(jsonl, "correction", bar)
                cursor.last_payload_key = payload
                cursor.counters.applied_correction += 1
                logger.info(
                    "bar_persistence: applied_correction",
                    extra={
                        "symbol": symbol,
                        "resolution": resolution,
                        "start_ms": bar.start_ms,
                        "action": "applied_correction",
                    },
                )
                return AppendOutcome.APPLIED_CORRECTION

            if bar.start_ms < cursor.last_start_ms:
                self._quarantine(jsonl)
                cursor.counters.regression_quarantined += 1
                logger.error(
                    "bar_persistence: non-monotonic regression — quarantining JSONL",
                    extra={
                        "symbol": symbol,
                        "resolution": resolution,
                        "incoming_start_ms": bar.start_ms,
                        "last_accepted_start_ms": cursor.last_start_ms,
                        "action": "regression_quarantined",
                    },
                )
                # Reset the cursor so a follow-up bar can start fresh, but
                # the caller is expected to treat the raised error as fatal.
                cursor.last_start_ms = None
                cursor.last_payload_key = None
                raise BarPersistenceRegressionError(
                    f"non-monotonic bar for {symbol}/{resolution}: "
                    f"incoming start_ms={bar.start_ms} < last_accepted={cursor.last_start_ms}"
                )

            # Strict forward progress.
            self._write_line(jsonl, "append", bar)
            cursor.last_start_ms = bar.start_ms
            cursor.last_payload_key = _payload_key(bar)
            return AppendOutcome.WRITTEN

    def replay(self, symbol: str, resolution: str, day: date) -> list[IbkrMinuteBar]:
        """Reconstruct the day's bars from JSONL.

        Later ``correction`` lines override earlier ``append`` lines for the
        same ``start_ms``. Exact-duplicate lines (writer crashed mid-fsync
        and the operator restarted from the prior fsynced bar) collapse.
        Output is ``start_ms``-sorted.
        """
        jsonl = self._jsonl_path(symbol, resolution, day)
        if not jsonl.is_file():
            return []
        by_start: dict[int, IbkrMinuteBar] = {}
        with jsonl.open("r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning("bar_persistence: malformed JSONL line skipped: %s", exc)
                    continue
                bar_payload = record.get("bar")
                if not isinstance(bar_payload, dict):
                    continue
                try:
                    bar = _record_to_bar(bar_payload)
                except (ValueError, TypeError) as exc:
                    logger.warning("bar_persistence: invalid bar payload skipped: %s", exc)
                    continue
                by_start[bar.start_ms] = bar
        return sorted(by_start.values(), key=lambda b: b.start_ms)

    def read_parquet(self, symbol: str, resolution: str, day: date) -> list[IbkrMinuteBar]:
        """Read a compacted day's bars from Parquet. Empty list if absent."""
        path = self._parquet_path(symbol, resolution, day)
        if not path.is_file():
            return []
        try:
            table = pq.read_table(path)
        except (OSError, pa.ArrowInvalid) as exc:
            logger.warning("bar_persistence: parquet read failed for %s: %s", path, exc)
            return []
        bars: list[IbkrMinuteBar] = []
        for row in table.to_pylist():
            try:
                bars.append(_record_to_bar(row))
            except (ValueError, TypeError) as exc:
                logger.warning("bar_persistence: invalid parquet row skipped: %s", exc)
        return sorted(bars, key=lambda b: b.start_ms)

    def compact(self, symbol: str, resolution: str, day: date) -> Path:
        """Write the day's bars to Parquet and archive the JSONL.

        Returns the Parquet path. The JSONL is renamed to
        ``<date>.jsonl.compacted-<now_ms>`` rather than deleted so an
        operator can audit the source after compaction.
        """
        bars = self.replay(symbol, resolution, day)
        parquet = self._parquet_path(symbol, resolution, day)
        parquet.parent.mkdir(parents=True, exist_ok=True)
        rows = [_bar_to_record(b) for b in bars]
        table = pa.Table.from_pylist(rows) if rows else pa.table({})
        pq.write_table(table, parquet)

        jsonl = self._jsonl_path(symbol, resolution, day)
        if jsonl.is_file():
            archive = jsonl.with_name(f"{jsonl.name}.{_COMPACTED_PREFIX}{_now_ms_utc()}")
            jsonl.rename(archive)
        return parquet

    def active_dates(self, symbol: str, resolution: str) -> list[date]:
        """Sorted dates that have either a JSONL or a Parquet for
        ``(symbol, resolution)``."""
        day_dir = self._dir(symbol, resolution)
        if not day_dir.is_dir():
            return []
        dates: set[date] = set()
        for entry in day_dir.iterdir():
            name = entry.name
            stem = self._date_stem(name)
            if stem is None:
                continue
            try:
                dates.add(date.fromisoformat(stem))
            except ValueError:
                continue
        return sorted(dates)

    def apply_retention(self, *, now: datetime) -> int:
        """Delete JSONL / Parquet files outside the retention window.

        Quarantined files are preserved — they are forensic evidence of a
        bad feed. Returns the number of files removed.
        """
        cutoff = (now.astimezone(UTC) - timedelta(days=self._retention_days)).date()
        deleted = 0
        if not self._root.is_dir():
            return 0
        for symbol_dir in self._root.iterdir():
            if not symbol_dir.is_dir():
                continue
            for resolution_dir in symbol_dir.iterdir():
                if not resolution_dir.is_dir():
                    continue
                for entry in resolution_dir.iterdir():
                    name = entry.name
                    if _QUARANTINE_PREFIX in name:
                        continue
                    stem = self._date_stem(name)
                    if stem is None:
                        continue
                    try:
                        entry_date = date.fromisoformat(stem)
                    except ValueError:
                        continue
                    if entry_date < cutoff:
                        try:
                            entry.unlink()
                            deleted += 1
                        except OSError as exc:
                            logger.warning(
                                "bar_persistence: retention unlink failed for %s: %s",
                                entry,
                                exc,
                            )
        return deleted

    def counters(self, symbol: str, resolution: str) -> Counters:
        """Snapshot of the per-(symbol, resolution) observability counters."""
        cursor = self._cursors.get(self._key(symbol, resolution))
        if cursor is None:
            return Counters()
        c = cursor.counters
        # Return a copy so callers can't mutate the live counter.
        return Counters(
            skipped_duplicate=c.skipped_duplicate,
            applied_correction=c.applied_correction,
            regression_quarantined=c.regression_quarantined,
        )

    # ─────────────────────────── internals ───────────────────────────────

    @staticmethod
    def _key(symbol: str, resolution: str) -> tuple[str, str]:
        return (symbol.strip().upper(), resolution.strip())

    def _get_or_load_cursor(self, symbol: str, resolution: str) -> _Cursor:
        """Return the cursor for ``(symbol, resolution)``; load from disk if
        the process is cold (no prior in-memory state).

        The cursor is the last accepted bar's ``start_ms`` + payload — by
        replaying the most-recent JSONL we recover the monotonicity guard
        across a restart so an old bar can't sneak in.
        """
        key = self._key(symbol, resolution)
        cursor = self._cursors.get(key)
        if cursor is not None:
            return cursor
        cursor = _Cursor()
        dates = self.active_dates(symbol, resolution)
        if dates:
            latest = dates[-1]
            bars = self.replay(symbol, resolution, latest)
            if bars:
                last = bars[-1]
                cursor.last_start_ms = last.start_ms
                cursor.last_payload_key = _payload_key(last)
        self._cursors[key] = cursor
        return cursor

    def _dir(self, symbol: str, resolution: str) -> Path:
        sym, res = self._key(symbol, resolution)
        return self._root / sym / res

    def _jsonl_path(self, symbol: str, resolution: str, day: date) -> Path:
        return self._dir(symbol, resolution) / f"{day.isoformat()}{_JSONL_SUFFIX}"

    def _parquet_path(self, symbol: str, resolution: str, day: date) -> Path:
        return self._dir(symbol, resolution) / f"{day.isoformat()}{_PARQUET_SUFFIX}"

    def _write_line(self, path: Path, action: str, bar: IbkrMinuteBar) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "action": action,
            "ts_ms": bar.fetched_at_ms,
            "bar": _bar_to_record(bar),
        }
        buf = io.StringIO()
        json.dump(record, buf, separators=(",", ":"))
        buf.write("\n")
        # Append in binary so the write is one atomic syscall per line.
        with path.open("ab") as fh:
            fh.write(buf.getvalue().encode("utf-8"))

    def _quarantine(self, jsonl: Path) -> None:
        """Rename the day's JSONL so the writer stops appending and an
        operator can audit it."""
        if not jsonl.is_file():
            return
        target = jsonl.with_name(f"{jsonl.name}.{_QUARANTINE_PREFIX}{_now_ms_utc()}")
        jsonl.rename(target)

    @staticmethod
    def _date_stem(name: str) -> str | None:
        """Return the ``YYYY-MM-DD`` stem of a persistence file name.

        Handles all four supported file shapes:
            * ``2026-04-01.jsonl``
            * ``2026-04-01.parquet``
            * ``2026-04-01.jsonl.compacted-<ts>``
            * ``2026-04-01.jsonl.quarantine-<ts>``
        """
        # Take everything up to the first dot — for the dated files in this
        # module the date string never contains a dot.
        head = name.split(".", 1)[0]
        if len(head) == 10 and head[4] == "-" and head[7] == "-":
            return head
        return None


__all__ = [
    "AppendOutcome",
    "BarPersistence",
    "BarPersistenceError",
    "BarPersistenceRegressionError",
    "Counters",
]
