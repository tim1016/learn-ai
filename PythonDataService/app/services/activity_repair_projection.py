"""Derived Activity repair projection for durable trade evidence.

The canonical broker-activity WAL is owned by the live publisher. This module
builds a separate projection/cache for the Activity endpoint when durable run
artifacts contain fills or closed trades that are missing from that WAL.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
from collections.abc import Iterable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from app.schemas.broker_activity import BrokerActivityRow
from app.schemas.live_runs import ActivityBrokerEventRow
from app.services.broker_activity_reconstruction import project_broker_activity_for_run

logger = logging.getLogger(__name__)

_CACHE_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ActivityRepairProjection:
    broker_rows: tuple[BrokerActivityRow, ...]
    closed_trade_rows: tuple[ActivityBrokerEventRow, ...]


def load_activity_repair_projection(
    *,
    artifacts_root: Path,
    strategy_instance_id: str,
    runs: Iterable[dict[str, Any]],
    start_ms: int,
    end_ms: int,
    existing_rows: list[BrokerActivityRow],
) -> ActivityRepairProjection:
    """Return repaired fill rows and closed-trade summaries for a session.

    The function may write the derived repair cache, but never mutates the
    authoritative per-instance broker-activity WAL. Cached rows are filtered
    against the caller's current WAL rows before returning so a live row that
    arrives after cache creation suppresses its repaired counterpart.
    """
    run_specs = tuple(_run_specs_with_repair_artifacts(runs))
    if not run_specs:
        return ActivityRepairProjection(broker_rows=(), closed_trade_rows=())

    cache_dir = artifacts_root / "live_instances" / strategy_instance_id / "activity_repair"
    cache_path = cache_dir / f"{start_ms}-{end_ms}.json"
    lock_path = cache_dir / f"{start_ms}-{end_ms}.lock"
    signature = _source_signature(
        artifacts_root=artifacts_root,
        strategy_instance_id=strategy_instance_id,
        run_specs=run_specs,
    )

    cached = _read_cache(cache_path, signature=signature)
    if cached is None:
        with _file_lock(lock_path):
            cached = _read_cache(cache_path, signature=signature)
            if cached is None:
                cached = _build_projection(
                    artifacts_root=artifacts_root,
                    run_specs=run_specs,
                    start_ms=start_ms,
                    end_ms=end_ms,
                )
                _write_cache(cache_path, signature=signature, projection=cached)

    existing_keys = _row_keys(existing_rows)
    broker_rows = tuple(row for row in cached.broker_rows if _row_key(row) not in existing_keys)
    return ActivityRepairProjection(
        broker_rows=broker_rows,
        closed_trade_rows=cached.closed_trade_rows,
    )


def _build_projection(
    *,
    artifacts_root: Path,
    run_specs: tuple[dict[str, Any], ...],
    start_ms: int,
    end_ms: int,
) -> ActivityRepairProjection:
    broker_rows: list[BrokerActivityRow] = []
    closed_trade_rows: list[ActivityBrokerEventRow] = []
    for run in run_specs:
        run_id = str(run["run_id"])
        run_dir = Path(str(run["run_dir"]))
        try:
            result = project_broker_activity_for_run(
                run_id,
                artifacts_root=artifacts_root,
                existing_rows=broker_rows,
            )
        except (OSError, ValueError, FileNotFoundError) as exc:
            logger.warning(
                "activity repair projection skipped run %s: %s",
                run_id,
                exc,
                extra={"run_id": run_id, "run_dir": str(run_dir)},
            )
            continue
        broker_rows.extend(
            row for row in result.rows if start_ms <= int(row.exec_ts_ms or row.ts_ms) < end_ms
        )
        closed_trade_rows.extend(
            _closed_trade_events_for_run(
                run_id=run_id,
                run_dir=run_dir,
                start_ms=start_ms,
                end_ms=end_ms,
            )
        )
    broker_rows.sort(key=lambda row: int(row.exec_ts_ms or row.ts_ms))
    closed_trade_rows.sort(key=lambda row: row.ts_ms)
    return ActivityRepairProjection(
        broker_rows=tuple(broker_rows),
        closed_trade_rows=tuple(closed_trade_rows),
    )


def _closed_trade_events_for_run(
    *,
    run_id: str,
    run_dir: Path,
    start_ms: int,
    end_ms: int,
) -> list[ActivityBrokerEventRow]:
    rows = _read_parquet_rows(run_dir / "trades.parquet")
    if not rows:
        return []
    execution_rows = _read_parquet_rows(run_dir / "executions.parquet")
    fallback_symbol = _first_symbol(execution_rows)
    out: list[ActivityBrokerEventRow] = []
    for row in rows:
        entry_ms = _as_int_or_none(row.get("entry_time_ms"))
        exit_ms = _as_int_or_none(row.get("exit_time_ms"))
        entry_price = _as_float_or_none(row.get("entry_price"))
        exit_price = _as_float_or_none(row.get("exit_price"))
        pnl_points = _as_float_or_none(row.get("pnl_points"))
        if exit_ms is None or not (start_ms <= exit_ms < end_ms):
            continue
        if entry_ms is None or entry_price is None or exit_price is None or pnl_points is None:
            continue
        trade_symbol = _as_str_or_none(row.get("symbol")) or fallback_symbol
        summary = (
            f"Closed {trade_symbol or 'trade'} round trip: "
            f"{entry_price:.2f} to {exit_price:.2f}, "
            f"{pnl_points:+.2f} points"
        )
        stable_id = _closed_trade_stable_id(
            run_id,
            entry_ms=entry_ms,
            exit_ms=exit_ms,
            entry_price=entry_price,
            exit_price=exit_price,
            pnl_points=pnl_points,
            symbol=trade_symbol,
        )
        out.append(
            ActivityBrokerEventRow(
                id=stable_id,
                visible_row_id=stable_id,
                ts_ms=exit_ms,
                row_type="closed_trade_summary",
                display_type="Closed trade",
                source="trade_artifact",
                source_label="Trade history",
                symbol=trade_symbol,
                status="Closed",
                summary=summary,
                verdict="trade_summary",
                cluster_key=stable_id,
                cluster_label="Round trip",
            )
        )
    return out


def _run_specs_with_repair_artifacts(runs: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    for run in runs:
        run_dir = Path(str(run.get("run_dir", "")))
        if not run_dir.is_dir():
            continue
        if (run_dir / "broker_callbacks.jsonl").is_file():
            yield run
            continue
        if _parquet_artifact_exists(run_dir / "executions.parquet"):
            yield run
            continue
        if _parquet_artifact_exists(run_dir / "trades.parquet"):
            yield run
            continue


def _source_signature(
    *,
    artifacts_root: Path,
    strategy_instance_id: str,
    run_specs: tuple[dict[str, Any], ...],
) -> str:
    live_state = artifacts_root / "live_state" / strategy_instance_id / "live_state.json"
    parts = [f"schema:{_CACHE_SCHEMA_VERSION}", _live_state_repair_fingerprint(live_state)]
    for run in run_specs:
        run_dir = Path(str(run["run_dir"]))
        parts.append(f"run:{run['run_id']}")
        for name in (
            "run_ledger.json",
            "broker_callbacks.jsonl",
            "intent_events.jsonl",
            "executions.parquet",
            "trades.parquet",
        ):
            parts.append(_file_fingerprint(run_dir / name))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


def _live_state_repair_fingerprint(path: Path) -> str:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "live_state:missing"
    except (OSError, json.JSONDecodeError):
        return f"live_state:unreadable:{_file_fingerprint(path)}"
    if not isinstance(payload, dict):
        return f"live_state:unusable:{_file_fingerprint(path)}"
    relevant = {
        "bot_order_namespace": payload.get("bot_order_namespace"),
        "sizing_resolutions": payload.get("sizing_resolutions"),
        "submitted_orders": payload.get("submitted_orders"),
    }
    encoded = json.dumps(relevant, sort_keys=True, separators=(",", ":"), default=str)
    return "live_state:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _file_fingerprint(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return f"{path.name}:missing"
    return f"{path.name}:{stat.st_size}:{stat.st_mtime_ns}"


def _read_cache(path: Path, *, signature: str) -> ActivityRepairProjection | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if payload.get("schema_version") != _CACHE_SCHEMA_VERSION:
        return None
    if payload.get("source_signature") != signature:
        return None
    try:
        return ActivityRepairProjection(
            broker_rows=tuple(BrokerActivityRow.model_validate(row) for row in payload["broker_rows"]),
            closed_trade_rows=tuple(
                ActivityBrokerEventRow.model_validate(row) for row in payload["closed_trade_rows"]
            ),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _write_cache(
    path: Path, *, signature: str, projection: ActivityRepairProjection
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": _CACHE_SCHEMA_VERSION,
        "source_signature": signature,
        "broker_rows": [row.model_dump(mode="json") for row in projection.broker_rows],
        "closed_trade_rows": [
            row.model_dump(mode="json") for row in projection.closed_trade_rows
        ],
    }
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)


@contextmanager
def _file_lock(path: Path) -> Iterable[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    if not _parquet_artifact_exists(path):
        return []
    try:
        return pq.read_table(path).to_pylist()
    except (OSError, pa.ArrowException) as exc:
        logger.warning("activity repair parquet read failed for %s: %s", path, exc, exc_info=True)
        return []


def _parquet_artifact_exists(path: Path) -> bool:
    return path.is_file() or path.is_dir()


def _first_symbol(rows: list[dict[str, Any]]) -> str | None:
    for row in rows:
        symbol = _as_str_or_none(row.get("symbol"))
        if symbol:
            return symbol
    return None


def _row_keys(rows: list[BrokerActivityRow]) -> set[tuple[str, str]]:
    return {_row_key(row) for row in rows}


def _row_key(row: BrokerActivityRow) -> tuple[str, str]:
    if row.exec_id:
        return ("exec_id", row.exec_id)
    if row.order_ref:
        return ("lifecycle", f"{row.order_ref}|{row.template_key}")
    return ("row", str(row.seq))


def _closed_trade_stable_id(
    run_id: str,
    *,
    entry_ms: int,
    exit_ms: int,
    entry_price: float,
    exit_price: float,
    pnl_points: float,
    symbol: str | None,
) -> str:
    payload = {
        "entry_ms": entry_ms,
        "entry_price": entry_price,
        "exit_ms": exit_ms,
        "exit_price": exit_price,
        "pnl_points": pnl_points,
        "run_id": run_id,
        "symbol": symbol or "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
    return f"closed:{run_id}:{entry_ms}:{exit_ms}:{digest}"


def _as_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _as_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text or None


__all__ = ["ActivityRepairProjection", "load_activity_repair_projection"]
