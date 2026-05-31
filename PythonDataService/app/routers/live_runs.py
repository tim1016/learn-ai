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
    CommandsTimeline,
    CommandSummary,
    CommandTimelineEntry,
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


def _validate_path_segment(value: str, *, field: str) -> str:
    """Reject any operator-supplied value unsafe as a single path segment.

    API-boundary input validation: rejects path separators, ``.``/``..``
    segments, absolute paths, leading/trailing whitespace, the empty
    string, and NUL bytes. Returns a sanitized literal that breaks the
    CodeQL py/path-injection taint chain.

    TODO: converge with ``app.engine.live.identity.validate_strategy_instance_id``
    once PR #389 lands — that module is the canonical engine-layer validator.
    """
    if not value or value != value.strip():
        raise ValueError(f"{field} must be non-empty with no surrounding whitespace")
    if value in (".", ".."):
        raise ValueError(f"{field} must not be a path segment ('.' or '..')")
    if "\x00" in value or "/" in value or "\\" in value:
        raise ValueError(f"{field} must not contain path separators or NUL bytes")
    if Path(value).is_absolute():
        raise ValueError(f"{field} must not be an absolute path")
    return value


def _confine(root: Path, segment: str) -> Path:
    """Resolve ``root/segment`` and assert it stays within ``root``.

    Belt-and-suspenders confinement (the segment is already validated by
    ``_validate_path_segment``): rebuild the path from the validated
    literal, resolve it, and verify containment so the dataflow is
    obviously safe to the scanner.
    """
    root_resolved = root.resolve()
    resolved = (root_resolved / segment).resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ValueError(f"path traversal detected for segment {segment!r}") from exc
    return resolved


def _validate_run_id(run_id: str, root: Path) -> Path:
    """Validate run_id is safe and the resolved path stays within root."""
    safe = _validate_path_segment(run_id, field="run_id")
    if _RUN_ID_RE.fullmatch(safe) is None:
        raise ValueError(f"Invalid run_id format: {run_id!r}")
    # Use the validated literal — regex + confinement breaks the CodeQL
    # py/path-injection taint chain.
    return _confine(root, safe)


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
# Cache key: (run_id, mtime-signature). The signature is an opaque nested
# tuple from _mtime_sig — folds tracked sentinels + desired-state + commands.
_StatusCacheKey = tuple[str, tuple]
_status_cache: OrderedDict[_StatusCacheKey, LiveRunStatus] = OrderedDict()

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


def _commands_sig(run_dir: Path) -> tuple[tuple[str, float], ...]:
    """Signature of the run's ``commands/`` dir: (name, mtime) per entry.

    A new pending command or a fresh ack file changes the set or the
    mtimes, so folding this into the cache key invalidates a stale
    ``command_summary`` when controls are enqueued/acked.
    """
    commands_dir = run_dir / "commands"
    if not commands_dir.is_dir():
        return ()
    entries: list[tuple[str, float]] = []
    try:
        for p in commands_dir.iterdir():
            try:
                entries.append((p.name, p.stat().st_mtime))
            except OSError:
                entries.append((p.name, 0.0))
    except OSError:
        return ()
    entries.sort()
    return tuple(entries)


def _desired_state_sig(run_dir: Path) -> float:
    """mtime of the run's desired-state sidecar (0.0 if absent/unresolvable).

    Resolves the ``live_state/<sid>/desired_state.json`` path off the
    ledger binding so a control write busts the cached status.
    """
    sid = _status_sid(run_dir)
    if not sid:
        return 0.0
    try:
        path = _safe_desired_state_path(
            _desired_state_root(Path(get_settings().live_runs_root)), sid
        )
    except ValueError:
        return 0.0
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def _mtime_sig(run_dir: Path) -> tuple:
    """Cache signature: tracked sentinel mtimes + desired-state + commands.

    The tracked-file mtimes detect ledger/log/parquet/flag changes; the
    desired-state sidecar mtime and the ``commands/`` dir signature detect
    control writes so ``/status`` never serves a stale ``desired_state`` /
    ``command_summary``.
    """
    tracked = tuple(
        (run_dir / f).stat().st_mtime if (run_dir / f).exists() else 0.0 for f in _TRACKED_FILES
    )
    return (tracked, _desired_state_sig(run_dir), _commands_sig(run_dir))


def _status_cache_get(key: _StatusCacheKey) -> LiveRunStatus | None:
    if key not in _status_cache:
        return None
    _status_cache.move_to_end(key)
    return _status_cache[key]


def _status_cache_set(key: _StatusCacheKey, value: LiveRunStatus) -> None:
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
    cache_key: _StatusCacheKey = (run_id, sig)

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


def _safe_desired_state_path(artifacts_root: Path, strategy_instance_id: str) -> Path:
    """Build the desired-state sidecar path with boundary validation + confinement.

    ``strategy_instance_id`` is operator-supplied and flows into the
    ``live_state/<sid>/`` path component; validate it as a single path
    segment and confine the per-instance dir under ``live_state/`` so the
    dataflow is obviously safe (CodeQL py/path-injection).
    """
    safe_sid = _validate_path_segment(strategy_instance_id, field="strategy_instance_id")
    live_state_root = artifacts_root / "live_state"
    _confine(live_state_root, safe_sid)
    return stable_desired_state_path(artifacts_root, safe_sid)


def _resolve_desired_state(live_runs_root: Path, strategy_instance_id: str) -> DesiredStateView:
    """Resolve the durable-intent view for a run's strategy instance (UI-1).

    Legacy ledgers (empty ``strategy_instance_id``) resolve to
    ``unknown_no_ledger_binding`` with a null state — never guessed from parquet.
    """
    if not strategy_instance_id:
        return DesiredStateView(path_status=DesiredStatePathStatus.unknown_no_ledger_binding)
    try:
        path = _safe_desired_state_path(_desired_state_root(live_runs_root), strategy_instance_id)
    except ValueError:
        return DesiredStateView(path_status=DesiredStatePathStatus.unknown_no_ledger_binding)
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
    """Pending/ack counts + latest verb from the run's command channel (UI-1).

    Latest verb/seq is the highest-seq command across BOTH pending and
    acked files: once the most recent command is acked, ``read_pending``
    filters it out, but the ack file still carries seq+verb — so the
    summary keeps reporting it rather than dropping to null.
    """
    commands_dir = run_dir / "commands"
    channel = CommandChannel(commands_dir)
    pending = channel.read_pending() if commands_dir.exists() else []
    candidates: list[tuple[int, str]] = [(c.seq, c.verb.value) for c in pending]
    acked_count = 0
    if commands_dir.exists():
        for ack_path in commands_dir.glob("command.*.ack.json"):
            acked_count += 1
            try:
                data = json.loads(ack_path.read_text(encoding="utf-8"))
                candidates.append((int(data["seq"]), str(data["verb"])))
            except (OSError, ValueError, KeyError):
                continue
    latest_verb: str | None = None
    latest_seq: int | None = None
    if candidates:
        latest_seq, latest_verb = max(candidates, key=lambda item: item[0])
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


def _validate_strategy_instance_id(sid: str) -> None:
    """Reject path-traversal-unsafe strategy-instance ids at the boundary.

    Delegates to ``_validate_path_segment``; a sibling remediation PR (#389)
    may introduce a canonical ``validate_strategy_instance_id`` in the
    engine layer — this guard should converge with it once importable.
    """
    try:
        _validate_path_segment(sid, field="strategy_instance_id")
    except ValueError as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="invalid strategy_instance_id"
        ) from exc


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


@router.post("/{run_id}/desired-state", response_model=DesiredStateRecordResponse, deprecated=True)
async def set_desired_state(run_id: str, body: SetDesiredStateRequest) -> DesiredStateRecordResponse:
    """DEPRECATED (#400 cutover): superseded by the instance-addressed intent knob
    ``POST /api/live-instances/{id}/desired-state``, which writes durable intent
    *and* actuates the live binding. Run-addressed routes are evidence-only;
    operator mutations move to the instance console. Kept temporarily for
    back-compat — slated for removal once the cutover is signed off."""
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
    repo = DesiredStateRepo(_safe_desired_state_path(_desired_state_root(root), sid))
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


@router.post("/{run_id}/commands", response_model=CommandView, deprecated=True)
async def enqueue_command(run_id: str, body: EnqueueCommandRequest) -> CommandView:
    """DEPRECATED (#400 cutover): superseded by the instance-addressed one-shot
    command ``POST /api/live-instances/{id}/commands`` (reserved to
    FLATTEN/RECONCILE/MARK_POISONED). Run-addressed routes are evidence-only.
    Kept temporarily for back-compat — slated for removal once the cutover is
    signed off."""
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


# The bot polls the command dir at ~1s, independent of the bar loop
# (plan §16.4 Resolution 7). Server-provided so the client's staleness
# threshold derives from the dispatcher's cadence, not a magic constant.
COMMAND_POLL_INTERVAL_MS = 1000


def _file_mtime_ms(path: Path) -> int | None:
    try:
        return int(path.stat().st_mtime * 1000)
    except OSError:
        return None


def build_command_timeline(commands_dir: Path) -> CommandsTimeline:
    """Join pending + ack command files into one entry-per-command timeline (#397).

    ``status``: queued (pending, no ack) -> acknowledged (ack, ok outcome) |
    failed (ack, error outcome). ``issued_by``/``reason`` come from the command
    payload where present; timestamps fall back to file mtime (legacy-derived).
    """
    entries: dict[int, CommandTimelineEntry] = {}
    if commands_dir.is_dir():
        for pending_path in commands_dir.glob("command.*.pending.json"):
            try:
                data = json.loads(pending_path.read_text(encoding="utf-8"))
                seq = int(data["seq"])
                payload = data.get("payload") or {}
                entries[seq] = CommandTimelineEntry(
                    seq=seq,
                    verb=str(data["verb"]),
                    status="queued",
                    reason=payload.get("reason"),
                    issued_by=payload.get("issued_by") or "operator",
                    queued_at_ms=_file_mtime_ms(pending_path),
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
        for ack_path in commands_dir.glob("command.*.ack.json"):
            try:
                data = json.loads(ack_path.read_text(encoding="utf-8"))
                seq = int(data["seq"])
                outcome = dict(data.get("outcome", {}))
                raw_status = str(outcome.get("status", "ok"))
                prior = entries.get(seq)
                entries[seq] = CommandTimelineEntry(
                    seq=seq,
                    verb=str(data["verb"]),
                    status="acknowledged" if raw_status == "ok" else "failed",
                    reason=prior.reason if prior else None,
                    issued_by=prior.issued_by if prior else "operator",
                    queued_at_ms=prior.queued_at_ms if prior else None,
                    acked_at_ms=_file_mtime_ms(ack_path),
                    outcome=raw_status,
                    outcome_detail=outcome.get("effect") or outcome.get("detail"),
                )
            except (OSError, ValueError, KeyError, TypeError):
                continue
    ordered = sorted(entries.values(), key=lambda e: e.seq, reverse=True)
    return CommandsTimeline(entries=ordered, poll_interval_ms=COMMAND_POLL_INTERVAL_MS)


@router.get("/{run_id}/commands", response_model=CommandsTimeline)
async def get_command_timeline(run_id: str) -> CommandsTimeline:
    """Return the unified command timeline for a run's channel (#397, evidence read)."""
    root = Path(get_settings().live_runs_root)
    try:
        run_dir = _validate_run_id(run_id, root)
    except ValueError:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail=f"Invalid run_id: {run_id!r}")
    if not run_dir.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail=f"Run {run_id!r} not found")
    return build_command_timeline(_confine(run_dir, "commands"))
