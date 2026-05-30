"""Live-runs read-only API.

Three endpoints:
  GET /api/live-runs                        — paginated summary list
  GET /api/live-runs/{run_id}/status        — full status snapshot
  GET /api/live-runs/{run_id}/log-tail      — last N parsed log lines

Three-layer caching:
  Layer 1 — 15 s TTL on the sorted directory listing.
  Layer 2 — mtime-signature LRU (256 entries) on per-run status.
  Layer 3 — inode-tracked incremental deque on log tail (max 1000 lines).
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import pyarrow.parquet as pq
from fastapi import APIRouter, HTTPException, Query, status

from app.broker.ibkr.config import get_settings
from app.engine.live.command_channel import CommandChannel, CommandVerb
from app.engine.live.desired_state import (
    DesiredState,
    DesiredStateCorruptError,
    DesiredStateRepo,
    stable_desired_state_path,
)
from app.engine.live.run_ledger import read_ledger
from app.schemas.live_runs import (
    ArtifactFile,
    ArtifactsSummary,
    CommandAckView,
    CommandSummary,
    CommandTimelineResponse,
    CommandView,
    DecisionsSummary,
    DesiredStateAction,
    DesiredStatePathStatus,
    DesiredStateRecordResponse,
    DesiredStateView,
    EnqueueCommandRequest,
    ExecutionsSummary,
    FlagsSummary,
    LiveRunStatus,
    LiveRunSummary,
    LogLine,
    ReconcileSummary,
    RunStatusSidecar,
    SetDesiredStateRequest,
    TradesSummary,
)
from app.services.live_log_parser import BarEvent, parse_log_tail
from app.services.live_run_state import infer_state

router = APIRouter(tags=["live-runs"])
logger = logging.getLogger(__name__)

_RUN_ID_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{1,127}$")


def _validate_run_id(run_id: str, root: Path) -> Path:
    """Validate run_id is safe and the resolved path stays within root."""
    m = _RUN_ID_RE.fullmatch(run_id)
    if m is None:
        raise ValueError(f"Invalid run_id format: {run_id!r}")
    # Use m.group(0) — regex-derived value breaks CodeQL py/path-injection taint chain.
    run_dir = (root / m.group(0)).resolve()
    if not run_dir.is_relative_to(root.resolve()):
        raise ValueError(f"Path traversal detected for run_id {run_id!r}")
    return run_dir


# ── Layer 1: directory listing cache (15 s TTL) ────────────────────────────

_DIR_TTL_S: float = 15.0
# root -> (expiry_monotonic, dirs)
_dir_cache: dict[str, tuple[float, list[Path]]] = {}


def _get_run_dirs(root: Path) -> list[Path]:
    """Return sorted list of run directories, newest first. 15 s TTL cache."""
    now = time.monotonic()
    key = str(root)
    cached = _dir_cache.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]
    dirs: list[Path] = (
        sorted(
            (d for d in root.iterdir() if d.is_dir()),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if root.exists()
        else []
    )
    _dir_cache[key] = (now + _DIR_TTL_S, dirs)
    return dirs


# ── Layer 2: per-run status cache (mtime-signature LRU, 256 entries) ──────

_STATUS_CACHE_MAX = 256
_status_cache: OrderedDict[tuple[str, tuple[float, ...]], LiveRunStatus] = OrderedDict()

_TRACKED_FILES = [
    "run_ledger.json",
    "run_status.json",
    "live.log",
    "halt.flag",
    "poisoned.flag",
    "decisions.parquet",
    "executions.parquet",
    "trades.parquet",
]


def _mtime_sig(run_dir: Path) -> tuple[float, ...]:
    """Build an 8-tuple of mtimes for the tracked sentinel files."""
    return tuple((run_dir / f).stat().st_mtime if (run_dir / f).exists() else 0.0 for f in _TRACKED_FILES)


def _status_cache_get(key: tuple[str, tuple[float, ...]]) -> LiveRunStatus | None:
    if key not in _status_cache:
        return None
    _status_cache.move_to_end(key)
    return _status_cache[key]


def _status_cache_set(key: tuple[str, tuple[float, ...]], value: LiveRunStatus) -> None:
    _status_cache[key] = value
    _status_cache.move_to_end(key)
    while len(_status_cache) > _STATUS_CACHE_MAX:
        _status_cache.popitem(last=False)


# ── Layer 3: log-tail incremental reader ──────────────────────────────────

_LOG_TAIL_MAX_LINES = 1000


@dataclass
class _LogTailState:
    inode: int
    last_offset: int
    lines: deque[str] = field(default_factory=lambda: deque(maxlen=_LOG_TAIL_MAX_LINES))


_log_tail_states: dict[str, _LogTailState] = {}  # run_id -> state


def _update_log_tail(run_id: str, log_path: Path) -> _LogTailState:
    """Read any new bytes from log_path into the per-run deque. Handles rotation."""
    state = _log_tail_states.get(run_id)

    try:
        stat = log_path.stat()
    except FileNotFoundError:
        empty_state = _LogTailState(inode=0, last_offset=0)
        _log_tail_states[run_id] = empty_state
        return empty_state

    cur_inode = stat.st_ino
    cur_size = stat.st_size

    if state is None or state.inode != cur_inode:
        # New file or log rotation — re-read from start
        state = _LogTailState(inode=cur_inode, last_offset=0)
        _log_tail_states[run_id] = state

    if cur_size < state.last_offset:
        # Log was truncated (copy-truncate rotation) — re-read from start
        state.last_offset = 0
        state.lines.clear()

    if cur_size > state.last_offset:
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            fh.seek(state.last_offset)
            new_text = fh.read()
        for line in new_text.splitlines():
            state.lines.append(line)
        state.last_offset = cur_size

    return state


# ── Private helpers ────────────────────────────────────────────────────────


def _read_ledger(run_dir: Path) -> dict:
    """Read run_ledger.json. Raises OSError / json.JSONDecodeError on failure."""
    return json.loads((run_dir / "run_ledger.json").read_text(encoding="utf-8"))


def _read_sidecar(run_dir: Path) -> RunStatusSidecar | None:
    path = run_dir / "run_status.json"
    if not path.exists():
        return None
    try:
        return RunStatusSidecar.model_validate(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def _read_flag(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _parquet_row_count(path: Path) -> int:
    """O(1) row count from Parquet footer metadata."""
    try:
        return pq.ParquetFile(path).metadata.num_rows
    except (FileNotFoundError, OSError, pq.lib.ArrowIOError, pq.lib.ArrowInvalid):
        return 0


def _last_activity_ms(run_dir: Path) -> int:
    """Return max mtime across all files in run_dir (ms UTC)."""
    best: float = 0.0
    try:
        for p in run_dir.iterdir():
            try:
                mtime = p.stat().st_mtime
                if mtime > best:
                    best = mtime
            except OSError as exc:
                logger.warning("Could not stat %s while scanning run_dir: %s", p, exc)
    except OSError as exc:
        logger.warning("Could not iterate run_dir %s: %s", run_dir, exc)
    return int(best * 1000)


def _build_summary(run_dir: Path, now_ms: int) -> LiveRunSummary:
    """Build a LiveRunSummary from the files in run_dir."""
    ledger = _read_ledger(run_dir)
    sidecar = _read_sidecar(run_dir)
    state = infer_state(run_dir, now_ms)

    decisions_path = run_dir / "decisions.parquet"
    executions_path = run_dir / "executions.parquet"

    return LiveRunSummary(
        run_id=ledger["run_id"],
        account_id=ledger["account_id"],
        session_start_ms=ledger["start_date_ms"],
        created_at_ms=ledger["created_at_ms"],
        run_started_at_ms=sidecar.started_at_ms if sidecar is not None else None,
        ended_at_ms=sidecar.ended_at_ms if sidecar is not None else None,
        last_activity_ms=_last_activity_ms(run_dir),
        state=state,
        decision_count=_parquet_row_count(decisions_path) if decisions_path.exists() else 0,
        execution_count=_parquet_row_count(executions_path) if executions_path.exists() else 0,
        halt_flag_set=(run_dir / "halt.flag").exists(),
        poisoned_flag_set=(run_dir / "poisoned.flag").exists(),
    )


def _read_parquet_tail(path: Path, n: int) -> list[dict]:
    """Read the last n rows of a Parquet file as a list of dicts."""
    try:
        nrows = pq.ParquetFile(path).metadata.num_rows
        if nrows == 0:
            return []
        table = pq.read_table(path).slice(max(0, nrows - n), n)
        return table.to_pylist()
    except (FileNotFoundError, OSError, pq.lib.ArrowIOError, pq.lib.ArrowInvalid):
        return []


def _build_decisions_summary(run_dir: Path) -> DecisionsSummary:
    path = run_dir / "decisions.parquet"
    if not path.exists():
        return DecisionsSummary(row_count=0)
    row_count = _parquet_row_count(path)
    latest: dict | None = None
    if row_count > 0:
        rows = _read_parquet_tail(path, 1)
        latest = rows[0] if rows else None
    return DecisionsSummary(row_count=row_count, latest_decision=latest)


def _build_executions_summary(run_dir: Path) -> ExecutionsSummary:
    path = run_dir / "executions.parquet"
    if not path.exists():
        return ExecutionsSummary(row_count=0)
    row_count = _parquet_row_count(path)
    last_fills = _read_parquet_tail(path, 5) if row_count > 0 else []
    return ExecutionsSummary(row_count=row_count, last_fills=last_fills)


def _build_trades_summary(run_dir: Path) -> TradesSummary:
    path = run_dir / "trades.parquet"
    if not path.exists():
        return TradesSummary(row_count=0)
    row_count = _parquet_row_count(path)
    open_position: dict | None = None
    if row_count > 0:
        rows = _read_parquet_tail(path, 1)
        open_position = rows[0] if rows else None
    return TradesSummary(row_count=row_count, open_position=open_position)


def _build_flags_summary(run_dir: Path) -> FlagsSummary:
    return FlagsSummary(
        halt_flag=_read_flag(run_dir / "halt.flag"),
        poisoned_flag=_read_flag(run_dir / "poisoned.flag"),
    )


def _build_artifacts_summary(run_dir: Path) -> ArtifactsSummary:
    files: list[ArtifactFile] = []
    try:
        for p in sorted(run_dir.iterdir(), key=lambda x: x.name):
            if not p.is_file():
                continue
            try:
                st = p.stat()
                row_count: int | None = None
                if p.suffix == ".parquet":
                    row_count = _parquet_row_count(p)
                files.append(
                    ArtifactFile(
                        name=p.name,
                        size_bytes=st.st_size,
                        mtime_ms=int(st.st_mtime * 1000),
                        row_count=row_count,
                    )
                )
            except OSError:
                pass
    except OSError:
        pass
    return ArtifactsSummary(files=files)


def _build_reconcile_summary(run_dir: Path) -> ReconcileSummary:
    reconcile_dir = run_dir / "reconcile"
    if not reconcile_dir.is_dir():
        return ReconcileSummary()
    # Find latest day-N.md by filename sort (day-10 > day-9 lexicographically, so sort by N)
    receipts = list(reconcile_dir.glob("day-*.md"))
    if not receipts:
        return ReconcileSummary()

    def _day_num(p: Path) -> int:
        stem = p.stem  # "day-N"
        try:
            return int(stem.split("-", 1)[1])
        except (IndexError, ValueError):
            return -1

    latest = max(receipts, key=_day_num)
    return ReconcileSummary(
        latest_receipt_name=latest.name,
        latest_receipt_url=f"/api/live-runs/{run_dir.name}/reconcile/{latest.name}",
    )


def _get_last_bar_event(run_id: str, run_dir: Path) -> BarEvent | None:
    """Return the most-recent BarEvent from the log tail deque, or None."""
    state = _log_tail_states.get(run_id)
    if state is None:
        # Trigger an incremental read to populate the state
        _update_log_tail(run_id, run_dir / "live.log")
        state = _log_tail_states.get(run_id)
    if state is None:
        return None
    tail = list(state.lines)
    parsed, _ = parse_log_tail(tail)
    for event in reversed(parsed):
        if isinstance(event, BarEvent):
            return event
    return None


def _build_run_status(run_dir: Path, now_ms: int) -> LiveRunStatus:
    """Build a full LiveRunStatus for a run directory."""
    ledger = _read_ledger(run_dir)
    sidecar = _read_sidecar(run_dir)
    run_id: str = ledger["run_id"]
    state = infer_state(run_dir, now_ms)

    # Heartbeat / last-bar info
    _update_log_tail(run_id, run_dir / "live.log")
    log_state = _log_tail_states.get(run_id)
    tail_lines = list(log_state.lines) if log_state is not None else []
    parsed_events, heartbeat_status = parse_log_tail(tail_lines)

    last_bar_event: BarEvent | None = None
    for event in reversed(parsed_events):
        if isinstance(event, BarEvent):
            last_bar_event = event
            break

    last_bar_time_ms: int | None = last_bar_event.ts_ms if last_bar_event is not None else None
    last_bar_age_s: float | None = (now_ms - last_bar_time_ms) / 1000.0 if last_bar_time_ms is not None else None

    _ = sidecar  # referenced for future extension; not needed in status body

    return LiveRunStatus(
        run_id=run_id,
        account_id=ledger["account_id"],
        state=state,
        last_bar_time_ms=last_bar_time_ms,
        last_bar_age_s=last_bar_age_s,
        heartbeat_parse_status=heartbeat_status,
        decisions=_build_decisions_summary(run_dir),
        executions=_build_executions_summary(run_dir),
        trades=_build_trades_summary(run_dir),
        flags=_build_flags_summary(run_dir),
        artifacts=_build_artifacts_summary(run_dir),
        reconcile=_build_reconcile_summary(run_dir),
        strategy_instance_id=(_status_sid(run_dir) or None),
        desired_state=_resolve_desired_state(
            Path(get_settings().live_runs_root), _status_sid(run_dir)
        ),
        command_summary=_command_summary(run_dir),
        fetched_at_ms=now_ms,
    )


# ── Endpoints ──────────────────────────────────────────────────────────────


@router.get("", response_model=list[LiveRunSummary])
async def list_live_runs(
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    from_ms: int | None = None,
    to_ms: int | None = None,
) -> list[LiveRunSummary]:
    """List live paper-trading runs, newest first.

    Args:
        limit: Maximum number of runs to return (1–100, default 20).
        cursor: Opaque pagination cursor (reserved; not yet implemented).
        status_filter: Filter by RunState value (e.g. ``running``, ``halted``).
        from_ms: Exclude runs whose reference timestamp is before this ms UTC.
        to_ms: Exclude runs whose reference timestamp is after this ms UTC.

    Returns:
        Paginated list of LiveRunSummary objects.
    """
    root = Path(get_settings().live_runs_root)
    now_ms = int(time.time() * 1000)
    summaries: list[LiveRunSummary] = []

    for run_dir in _get_run_dirs(root):
        try:
            summary = _build_summary(run_dir, now_ms)
        except Exception:
            logger.exception("Failed to build summary for run dir %s", run_dir.name)
            continue

        if status_filter is not None and summary.state.value != status_filter:
            continue

        ref_ms = summary.run_started_at_ms if summary.run_started_at_ms is not None else summary.created_at_ms
        if from_ms is not None and ref_ms < from_ms:
            continue
        if to_ms is not None and ref_ms > to_ms:
            continue

        summaries.append(summary)
        if len(summaries) >= limit:
            break

    return summaries


@router.get("/{run_id}/status", response_model=LiveRunStatus)
async def get_run_status(run_id: str) -> LiveRunStatus:
    """Return a full status snapshot for a single live run.

    Uses a mtime-signature LRU cache (256 entries). The cache key is
    ``(run_id, tuple-of-8-mtimes)``; any file change invalidates it.

    Args:
        run_id: 64-char hex run identifier.

    Returns:
        LiveRunStatus with decisions, executions, trades, flags, artifacts,
        reconcile summary, and last-bar timing.
    """
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")

    now_ms = int(time.time() * 1000)
    sig = _mtime_sig(run_dir)
    cache_key: tuple[str, tuple[float, ...]] = (run_id, sig)

    cached = _status_cache_get(cache_key)
    if cached is not None:
        return cached

    result = _build_run_status(run_dir, now_ms)
    _status_cache_set(cache_key, result)
    return result


@router.get("/{run_id}/log-tail", response_model=list[LogLine])
async def get_log_tail(
    run_id: str,
    lines: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> list[LogLine]:
    """Return parsed log lines from the tail of live.log.

    Uses an inode-tracked incremental deque (Layer 3 cache). New bytes are
    read on each request; log rotation is detected via inode change.

    Args:
        run_id: 64-char hex run identifier.
        lines: Number of lines to return from the tail (1–1000, default 200).

    Returns:
        List of LogLine objects, oldest first. Bar events carry ts_ms and
        consolidator/snapshot metadata; other lines have ``event_type="raw"``.
    """
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")

    log_path = run_dir / "live.log"
    log_state = _update_log_tail(run_id, log_path)
    tail = list(log_state.lines)[-lines:]
    parsed, _ = parse_log_tail(tail)

    result: list[LogLine] = []
    for event in parsed:
        if isinstance(event, BarEvent):
            result.append(
                LogLine(
                    ts_ms=event.ts_ms,
                    raw_text=event.raw_text,
                    event_type="bar",
                    consolidator_emitted=event.consolidator_emitted,
                    snapshot_set=str(event.snapshot_set),
                )
            )
        else:
            result.append(
                LogLine(
                    ts_ms=event.ts_ms,
                    raw_text=event.raw_text,
                    event_type="raw",
                )
            )

    return result


def _status_sid(run_dir: Path) -> str:
    """Best-effort strategy_instance_id from the ledger; \"\" on any failure."""
    ledger_path = run_dir / "run_ledger.json"
    if not ledger_path.exists():
        return ""
    try:
        return read_ledger(ledger_path).strategy_instance_id
    except (OSError, ValueError):
        return ""


# --- PRD-A UI-1/UI-3/UI-4 endpoints ---


def _now_ms() -> int:
    """Current wall-clock as int64 ms UTC (boundary conversion)."""
    return int(datetime.now(UTC).timestamp() * 1000)


def _desired_state_root(live_runs_root: Path) -> Path:
    """Artifacts root that holds both live_runs/ and live_state/.

    ``stable_desired_state_path`` keys off ``<artifacts_root>/live_state/...``;
    the per-run dirs live under ``<artifacts_root>/live_runs/...``. The
    settings value is the live_runs dir, so its parent is the artifacts root.
    """
    return live_runs_root.parent


def _resolve_desired_state(live_runs_root: Path, strategy_instance_id: str) -> DesiredStateView:
    """Resolve the durable-intent view for a run's strategy instance (UI-1).

    Legacy ledgers (empty ``strategy_instance_id``) resolve to
    ``unknown_no_ledger_binding`` with a null state — never guessed from parquet.
    """
    if not strategy_instance_id:
        return DesiredStateView(path_status=DesiredStatePathStatus.unknown_no_ledger_binding)
    path = stable_desired_state_path(_desired_state_root(live_runs_root), strategy_instance_id)
    repo = DesiredStateRepo(path)
    try:
        record = repo.read()
    except DesiredStateCorruptError:
        return DesiredStateView(path_status=DesiredStatePathStatus.corrupt)
    if record is None:
        return DesiredStateView(path_status=DesiredStatePathStatus.absent)
    return DesiredStateView(
        state=record.desired_state.value,
        updated_at_ms=record.updated_at_ms,
        updated_by=record.updated_by,
        reason=record.reason,
        version=record.version,
        path_status=DesiredStatePathStatus.ok,
    )


def _command_summary(run_dir: Path) -> CommandSummary:
    """Pending/ack counts + latest verb from the run's command channel (UI-1)."""
    commands_dir = run_dir / "commands"
    channel = CommandChannel(commands_dir)
    pending = channel.read_pending() if commands_dir.exists() else []
    acked_count = len(list(commands_dir.glob("command.*.ack.json"))) if commands_dir.exists() else 0
    latest_verb: str | None = None
    latest_seq: int | None = None
    if pending:
        latest = max(pending, key=lambda c: c.seq)
        latest_verb = latest.verb.value
        latest_seq = latest.seq
    return CommandSummary(
        pending_count=len(pending),
        acked_count=acked_count,
        latest_verb=latest_verb,
        latest_seq=latest_seq,
    )


def _ledger_or_404(run_dir: Path, run_id: str):
    """Read the run ledger, mapping read failures to a 404."""
    ledger_path = run_dir / "run_ledger.json"
    if not ledger_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} has no ledger")
    try:
        return read_ledger(ledger_path)
    except (OSError, ValueError) as exc:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} ledger unreadable"
        ) from exc


_INVALID_SID_CHARS = ("/", "\\", "..", "\x00")


def _validate_strategy_instance_id(sid: str) -> None:
    """Reject path-traversal-unsafe strategy-instance ids at the boundary.

    A sibling remediation PR may introduce a canonical
    ``validate_strategy_instance_id`` in the engine layer; this inline guard
    should converge with that validator once importable on this base.
    """
    if not sid or any(c in sid for c in _INVALID_SID_CHARS) or Path(sid).is_absolute():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id")


_ACTION_TO_STATE = {
    DesiredStateAction.pause: DesiredState.PAUSED,
    DesiredStateAction.resume: DesiredState.RUNNING,
    DesiredStateAction.stop: DesiredState.STOPPED,
}


@router.get("/{run_id}/desired-state", response_model=DesiredStateView)
async def get_desired_state(run_id: str) -> DesiredStateView:
    """Return the resolved durable-intent view for a run (UI-1)."""
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")
    ledger = _ledger_or_404(run_dir, run_id)
    return _resolve_desired_state(root, ledger.strategy_instance_id)


@router.post("/{run_id}/desired-state", response_model=DesiredStateRecordResponse)
async def set_desired_state(run_id: str, body: SetDesiredStateRequest) -> DesiredStateRecordResponse:
    """Write durable operator intent for a run's strategy instance (UI-3)."""
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")
    ledger = _ledger_or_404(run_dir, run_id)
    sid = ledger.strategy_instance_id
    if not sid:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            detail="run has no strategy_instance_id binding (legacy ledger)",
        )
    _validate_strategy_instance_id(sid)
    repo = DesiredStateRepo(stable_desired_state_path(_desired_state_root(root), sid))
    record = repo.set(
        _ACTION_TO_STATE[body.action],
        updated_by=body.updated_by,
        reason=body.reason,
        now_ms=_now_ms(),
    )
    return DesiredStateRecordResponse(
        state=record.desired_state.value,
        updated_at_ms=record.updated_at_ms,
        updated_by=record.updated_by,
        reason=record.reason,
        version=record.version,
    )


@router.post("/{run_id}/commands", response_model=CommandView)
async def enqueue_command(run_id: str, body: EnqueueCommandRequest) -> CommandView:
    """Enqueue a per-run command-channel verb (UI-4)."""
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")
    try:
        verb = CommandVerb(body.verb)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="invalid command verb") from exc
    channel = CommandChannel(run_dir / "commands")
    command = channel.write_from_operator(verb)
    return CommandView(seq=command.seq, verb=command.verb.value)


@router.get("/{run_id}/commands", response_model=CommandTimelineResponse)
async def get_command_timeline(run_id: str) -> CommandTimelineResponse:
    """Return the pending + ack timeline for a run's command channel (UI-4)."""
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")
    commands_dir = run_dir / "commands"
    channel = CommandChannel(commands_dir)
    pending = [
        CommandView(seq=c.seq, verb=c.verb.value)
        for c in (channel.read_pending() if commands_dir.exists() else [])
    ]
    acks: list[CommandAckView] = []
    if commands_dir.exists():
        for ack_path in sorted(commands_dir.glob("command.*.ack.json")):
            try:
                data = json.loads(ack_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            acks.append(
                CommandAckView(
                    seq=int(data["seq"]),
                    verb=str(data["verb"]),
                    outcome=dict(data.get("outcome", {})),
                )
            )
    acks.sort(key=lambda a: a.seq)
    return CommandTimelineResponse(pending=pending, acks=acks)
