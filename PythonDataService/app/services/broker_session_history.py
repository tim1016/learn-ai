"""Bounded broker-session roster history."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.broker.ibkr.config import get_settings
from app.schemas.broker_session import (
    BrokerSessionHistoryPage,
    BrokerSessionMirrorSnapshot,
)
from app.services.broker_session_events import write_jsonl_lines_atomically

_HISTORY_LOG_RELATIVE = Path("_broker") / "session_roster_history.jsonl"
_HISTORY_MAX_SNAPSHOTS = 500

logger = logging.getLogger(__name__)


class BrokerSessionHistoryService:
    """Persist and read recent broker-session roster snapshots."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        max_snapshots: int = _HISTORY_MAX_SNAPSHOTS,
    ) -> None:
        self._path = path
        self._max_snapshots = max(1, max_snapshots)

    def append_snapshot(self, snapshot: BrokerSessionMirrorSnapshot) -> None:
        """Append a snapshot while keeping the bounded retention window."""

        line = json.dumps(
            snapshot.model_dump(mode="json"),
            separators=(",", ":"),
            sort_keys=True,
        )
        path = self.history_log_path()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            lines = []
        lines.append(line)
        write_jsonl_lines_atomically(path, lines[-self._max_snapshots :])

    def history(self, *, limit: int = 100) -> BrokerSessionHistoryPage:
        """Return retained snapshots newest first."""

        snapshots = self._read_all_snapshots()
        return BrokerSessionHistoryPage(
            rows=list(reversed(snapshots))[:limit],
            retained_count=len(snapshots),
        )

    def _read_all_snapshots(self) -> list[BrokerSessionMirrorSnapshot]:
        try:
            lines = self.history_log_path().read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("failed to read broker session history: %s", exc)
            return []
        snapshots: list[BrokerSessionMirrorSnapshot] = []
        for line in lines:
            snapshot = _snapshot_from_line(line)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

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


_SERVICE = BrokerSessionHistoryService()
