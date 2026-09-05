"""Presentation and Finish rules shared by the fenced research records.

A Grid Search row and a Walk-Forward Study row answer the same questions the
same way: is the job behind a ``running`` row still alive (Redis), how does
the row read back when it is not (``interrupted``), was it launched from a
dirty tree, and may a Finish run against it. The words differ by ``noun``;
the rules do not.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import redis

from app.engine.data.policy_store import resolve_data_roots
from app.jobs.progress import _state_key, get_redis
from app.research.sweep.identity import CodeIdentity, resolve_code_identity
from app.research.sweep.snapshot import DataSnapshot, verify_data_snapshot


class FencedRecord(Protocol):
    @property
    def status(self) -> str: ...
    @property
    def job_id(self) -> str | None: ...
    @property
    def incomplete(self) -> bool: ...
    @property
    def receipt(self) -> Mapping[str, Any]: ...


def job_is_live(job_id: str | None) -> bool | None:
    """Whether the Redis job record still says queued/running. ``None`` when Redis cannot answer."""
    if not job_id:
        return False
    try:
        status = get_redis().hget(_state_key(job_id), "status")
    except redis.RedisError:
        return None
    return status in ("queued", "running")


def request_cancel(job_id: str) -> None:
    """Set the same flag the .NET DELETE /api/jobs/{id} sets; the worker acknowledges by finishing the record."""
    get_redis().hset(_state_key(job_id), "cancel_requested", "1")


def presented_status(row: FencedRecord, *, live: bool | None) -> str:
    """A ``running`` record with no live job reads back as ``interrupted``."""
    if row.status in ("queued", "running") and live is False:
        return "interrupted"
    return row.status


def uncommitted_changes(row: FencedRecord) -> bool:
    return row.receipt.get("code_identity", {}).get("tree_state") == "dirty"


def roots_for(row: FencedRecord) -> list[Path]:
    """The lake roots the record was launched against, from its receipted execution contract."""
    return resolve_data_roots(source="polygon", adjusted=bool(row.receipt["execution_contract"]["data_policy"]["adjusted"]))


def resume_refusal(
    row: FencedRecord,
    *,
    noun: str,
    unit: str,
    live: bool | None,
    identity: CodeIdentity | None = None,
    verify_data: bool = False,
) -> str | None:
    """Why Finish is unavailable, or ``None`` when it may run.

    The status, tree-state and code-identity checks are cheap and answer the
    detail view; ``verify_data`` re-hashes every receipted artifact and is
    reserved for the Finish request itself.
    """
    if row.status == "completed":
        return f"the {noun} is complete"
    if row.status == "failed" and not row.incomplete:
        return f"every {unit} is recorded and failed; there is nothing to finish — launch a fresh {noun}"
    if row.status in ("queued", "running") and live is not False:
        return f"the {noun} is still running"
    if uncommitted_changes(row):
        return f"the {noun} was launched from a working tree with uncommitted changes and cannot be resumed; launch a fresh {noun}"
    recorded = CodeIdentity(**row.receipt["code_identity"])
    if not recorded.matches(identity or resolve_code_identity()):
        return f"the engine or strategy code changed since launch; launch a fresh {noun}"
    if not verify_data:
        return None
    moved = verify_data_snapshot(DataSnapshot.from_dict(row.receipt["data_snapshot"]), roots_for(row))
    if moved:
        return f"{len(moved)} data artifact(s) changed since launch ({moved[0]}{', …' if len(moved) > 1 else ''}); launch a fresh {noun}"
    return None
