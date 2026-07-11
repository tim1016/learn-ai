"""Bounded broker-session roster history."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from app.broker.ibkr.config import get_settings
from app.schemas.broker_session import (
    BrokerSessionHistoryPage,
    BrokerSessionHistoryPurgeRequest,
    BrokerSessionHistoryPurgeResult,
    BrokerSessionMirrorSnapshot,
    BrokerSessionRosterRow,
    broker_session_row_presentation,
    summarize_broker_session_rows,
)
from app.services.broker_session_events import (
    locked_jsonl_file,
    write_jsonl_lines_atomically,
)

_HISTORY_LOG_RELATIVE = Path("_broker") / "session_roster_history.jsonl"
_HISTORY_MAX_SNAPSHOTS = 500
# Hard byte ceiling for both reads and the on-disk log. Reads never load more
# than this many bytes (a log that grew past the budget before compaction
# existed must not balloon memory — a 1.3GB history file OOM-killed the data
# plane at boot on 2026-07-10), and appends compact the log back down to the
# retention window once it crosses the budget.
_HISTORY_MAX_BYTES = 64 * 1024 * 1024

logger = logging.getLogger(__name__)


class BrokerSessionHistoryService:
    """Persist and read recent broker-session roster snapshots."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        max_snapshots: int = _HISTORY_MAX_SNAPSHOTS,
        max_bytes: int = _HISTORY_MAX_BYTES,
    ) -> None:
        self._path = path
        self._max_snapshots = max(1, max_snapshots)
        self._max_bytes = max(1, max_bytes)

    def append_snapshot(self, snapshot: BrokerSessionMirrorSnapshot) -> None:
        """Append a snapshot and keep the log within its retention budget."""

        line = _snapshot_to_line(snapshot)
        path = self.history_log_path()
        with locked_jsonl_file(path):
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"{line}\n")
                fh.flush()
                os.fsync(fh.fileno())
            self._compact_past_budget(path)

    def history(self, *, limit: int = 100) -> BrokerSessionHistoryPage:
        """Return retained snapshots newest first."""

        snapshots = self._read_all_snapshots()
        return BrokerSessionHistoryPage(
            rows=list(reversed(snapshots))[:limit],
            retained_count=len(snapshots),
        )

    def past_closed_rows(
        self,
        *,
        current_rows: list[BrokerSessionRosterRow],
        limit: int = 50,
    ) -> list[BrokerSessionRosterRow]:
        """Return recent rows that were current before but are absent now."""

        seen_keys = {_history_row_key(row) for row in current_rows}
        out: list[BrokerSessionRosterRow] = []
        for snapshot in reversed(self._read_all_snapshots()):
            for row in snapshot.rows:
                if row.recency != "current" or not row.socket_present:
                    continue
                key = _history_row_key(row)
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                out.append(_past_closed_row(row))
                if len(out) >= limit:
                    return out
        return out

    def purge(
        self,
        request: BrokerSessionHistoryPurgeRequest,
    ) -> BrokerSessionHistoryPurgeResult:
        """Purge diagnostic roster history without touching live state."""

        path = self.history_log_path()
        with locked_jsonl_file(path):
            lines = self._read_retained_lines()
            if not lines:
                return BrokerSessionHistoryPurgeResult(
                    purged_row_count=0,
                    purged_snapshot_count=0,
                    remaining_snapshot_count=0,
                )

            kept_lines: list[str] = []
            purged_row_count = 0
            purged_snapshot_count = 0
            remaining_snapshot_count = 0
            for line in lines:
                snapshot = _snapshot_from_line(line)
                if snapshot is None:
                    kept_lines.append(line)
                    continue
                if not _snapshot_matches_purge_filter(snapshot, request):
                    kept_lines.append(line)
                    remaining_snapshot_count += 1
                    continue
                if request.client_id is None:
                    purged_row_count += len(snapshot.rows)
                    purged_snapshot_count += 1
                    continue

                kept_rows = [row for row in snapshot.rows if row.client_id != request.client_id]
                purged_row_count += len(snapshot.rows) - len(kept_rows)
                rewritten = snapshot.model_copy(
                    update={
                        "rows": kept_rows,
                        "summary": summarize_broker_session_rows(kept_rows),
                    }
                )
                kept_lines.append(_snapshot_to_line(rewritten))
                remaining_snapshot_count += 1

            if purged_row_count > 0 or purged_snapshot_count > 0:
                write_jsonl_lines_atomically(path, kept_lines[-self._max_snapshots :])
        return BrokerSessionHistoryPurgeResult(
            purged_row_count=purged_row_count,
            purged_snapshot_count=purged_snapshot_count,
            remaining_snapshot_count=remaining_snapshot_count,
        )

    def _read_all_snapshots(self) -> list[BrokerSessionMirrorSnapshot]:
        snapshots: list[BrokerSessionMirrorSnapshot] = []
        for line in self._read_retained_lines():
            snapshot = _snapshot_from_line(line)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def _read_retained_lines(self) -> list[str]:
        """Read at most the retention window without loading the whole file.

        The tail read is byte-bounded; a partial first line inside the budget
        window is dropped rather than parsed as garbage.
        """

        path = self.history_log_path()
        try:
            with open(path, "rb") as fh:
                size = fh.seek(0, os.SEEK_END)
                start = max(0, size - self._max_bytes)
                fh.seek(start)
                data = fh.read()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("failed to read broker session history: %s", exc)
            return []
        lines = data.decode("utf-8", errors="replace").splitlines()
        if start > 0 and lines:
            lines = lines[1:]
        return lines[-self._max_snapshots :]

    def _compact_past_budget(self, path: Path) -> None:
        try:
            if path.stat().st_size <= self._max_bytes:
                return
        except OSError as exc:
            logger.warning("failed to stat broker session history: %s", exc)
            return
        write_jsonl_lines_atomically(path, self._read_retained_lines())

    def history_log_path(self) -> Path:
        if self._path is not None:
            return self._path
        return Path(get_settings().live_runs_root) / _HISTORY_LOG_RELATIVE


def get_broker_session_history_service() -> BrokerSessionHistoryService:
    return _SERVICE


def _snapshot_from_line(line: str) -> BrokerSessionMirrorSnapshot | None:
    if not line.strip():
        return None
    try:
        payload = json.loads(line)
        return BrokerSessionMirrorSnapshot.model_validate(payload)
    except ValueError as exc:
        logger.warning("skipping malformed broker session history row: %s", exc)
        return None


def _snapshot_matches_purge_filter(
    snapshot: BrokerSessionMirrorSnapshot,
    request: BrokerSessionHistoryPurgeRequest,
) -> bool:
    if request.start_ms is not None and snapshot.as_of_ms < request.start_ms:
        return False
    return not (request.end_ms is not None and snapshot.as_of_ms > request.end_ms)


def _history_row_key(row: BrokerSessionRosterRow) -> tuple[str, str, str]:
    if row.strategy_instance_id is not None and row.run_id is not None:
        return ("run", row.strategy_instance_id, row.run_id)
    if row.client_id is not None:
        return ("client", row.identity_type, str(row.client_id))
    return ("row", row.row_id, "")


def _past_closed_row(row: BrokerSessionRosterRow) -> BrokerSessionRosterRow:
    updated = row.model_copy(
        update={
            "recency": "past_closed",
            "socket_present": False,
            "notice": None,
        }
    )
    return updated.model_copy(
        update={"presentation": broker_session_row_presentation(updated)}
    )


def _snapshot_to_line(snapshot: BrokerSessionMirrorSnapshot) -> str:
    return json.dumps(
        snapshot.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    )


_SERVICE = BrokerSessionHistoryService()
