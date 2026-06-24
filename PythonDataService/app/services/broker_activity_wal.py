"""Append-only WAL for ``BrokerActivityRow`` (ADR 0014 §5, ADR 0008 amendment).

Mirrors the ``IntentWal`` pattern (atomic append + fsync + monotonic
``seq``) for the broker-activity reconciliation stream. One file per
strategy-instance per run, sibling to ``intent_events.jsonl``.

Differences from ``IntentWal``:

- This WAL is observation/audit-only (the publisher writes *after* the
  broker event has happened), not a pre-side-effect durability gate. We
  still fsync-before-return so that on a crash-and-recover the operator
  never sees an SSE row that is not in the WAL.
- ``read_from(seq, limit)`` supports paginated REST backfill (the SSE
  channel pushes live increments; cold-start clients fetch backwards
  history via this method).

Read contract (ADR-0008 §3, reused): exactly one anomaly is tolerated —
a single trailing line with no terminating newline. Every other
malformation (unparseable line, non-monotonic seq) raises
``BrokerActivityWalCorruptError``.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from app.engine.live.identity import validate_strategy_instance_id
from app.engine.live.live_state_sidecar import _fsync_parent_dir
from app.schemas.broker_activity import BrokerActivityRow


class BrokerActivityWalCorruptError(RuntimeError):
    """Raised on any malformation other than a single tolerated trailing
    partial line. The publisher treats this as a fatal: the operator
    surface cannot reliably backfill from a corrupt WAL, and silently
    skipping records would give the operator a false sense of completeness.
    """

    def __init__(self, path: Path, detail: str) -> None:
        super().__init__(f"broker-activity WAL at {path} is corrupt: {detail}")
        self.path = path
        self.detail = detail


def legacy_per_run_broker_activity_wal_path(run_dir: Path) -> Path:
    """Legacy per-run WAL location: ``<run_dir>/broker_activity.jsonl``.

    Retained for (a) reading any pre-migration WAL files that still live
    under their original run dir, and (b) the one-time migration that
    folds those files into the per-instance WAL. New publishers write to
    ``instance_broker_activity_wal_path`` instead — see the docstring on
    that function for the why.
    """
    return run_dir / "broker_activity.jsonl"


def instance_broker_activity_wal_path(
    artifacts_root: Path, strategy_instance_id: str
) -> Path:
    """Canonical path: ``<artifacts_root>/live_instances/<sid>/broker_activity.jsonl``.

    The WAL is scoped to the strategy instance, not the run, so it
    persists across redeploys. Before this scoping, each redeploy created
    a fresh empty WAL under ``live_runs/<run_id>/``, which made the
    cockpit's Broker Activity panel drop every fill that happened in a
    prior run — even though those fills were durably on disk in the old
    run dir. The per-instance WAL accumulates the full lifetime of broker
    events for the instance; ``_migrate_per_run_wals_to_instance_wal`` in
    ``broker_activity_publisher`` does the one-time fold of the legacy
    per-run files into it.

    ``strategy_instance_id`` is validated at this seam (mirrors the
    pattern on ``stable_desired_state_path``): the value flows into a
    directory segment, so we fail closed on path-traversal /
    separator / NUL / empty inputs before touching the filesystem. This
    is also what keeps the value off the CodeQL ``py/path-injection``
    taint chain for downstream callers (the migration helper and the
    WAL writer's existence/atomic-rename ops).
    """
    validate_strategy_instance_id(strategy_instance_id)
    return artifacts_root / "live_instances" / strategy_instance_id / "broker_activity.jsonl"


class BrokerActivityWal:
    """Append-only WAL writer/reader scoped to one strategy-instance run dir."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._next_seq: int | None = None

    @property
    def path(self) -> Path:
        return self._path

    def allocate_seq(self) -> int:
        """Return the next ``seq`` the publisher should stamp on a row.

        The publisher's row construction needs the seq BEFORE the row
        is built (the seq is on the row). This method is idempotent
        within a single process; ``append_row`` consumes the seq and
        advances internal state.
        """
        return self._allocate_seq()

    def append_row(self, row: BrokerActivityRow) -> None:
        """Persist one row. The row's ``seq`` must equal the value last
        returned by ``allocate_seq`` (we don't re-stamp here — the
        publisher constructed the row with that seq already)."""
        expected_seq = self._allocate_seq()
        if row.seq != expected_seq:
            raise ValueError(
                f"broker-activity WAL append got row.seq={row.seq} but the next "
                f"available seq is {expected_seq} (the publisher must construct "
                "the row with the seq returned by allocate_seq)"
            )
        self._path.parent.mkdir(parents=True, exist_ok=True)
        line = row.model_dump_json() + "\n"
        with open(self._path, "a", encoding="utf-8") as fh:
            fh.write(line)
            # Advance the seq before flush/fsync so a partial-write retry
            # uses seq+1 (cold-start tolerates one trailing torn line).
            # Mirrors IntentWal's discipline.
            self._next_seq = row.seq + 1
            fh.flush()
            os.fsync(fh.fileno())
        _fsync_parent_dir(self._path)

    def read_all(self) -> list[BrokerActivityRow]:
        """Read every complete row in seq order.

        Used by cold-start (publisher fold) and by the REST backfill
        endpoint when no ``after_seq`` cursor is supplied.
        """
        return self._read_range(after_seq=0, limit=None)

    def read_from(
        self, *, after_seq: int, limit: int | None = None
    ) -> list[BrokerActivityRow]:
        """Read rows with ``seq > after_seq`` in seq order, capped at ``limit``.

        Drives the REST paginated backfill: client passes the highest
        seq they have, we return the next page.
        """
        if after_seq < 0:
            raise ValueError(f"after_seq must be >= 0; got {after_seq}")
        return self._read_range(after_seq=after_seq, limit=limit)

    def last_seq(self) -> int:
        """Return the highest seq currently persisted, or ``0`` if empty.

        Drives the LiveStateEnvelope cursor (``last_broker_activity_wal_seq``)
        and the SSE handoff cursor for clients backfilling via REST then
        switching to SSE.
        """
        rows = self._read_range(after_seq=0, limit=None)
        return rows[-1].seq if rows else 0

    # ── internals ──────────────────────────────────────────────────

    def _read_range(
        self, *, after_seq: int, limit: int | None
    ) -> list[BrokerActivityRow]:
        if not self._path.exists():
            return []
        raw = self._path.read_bytes()
        if not raw:
            return []
        ends_with_newline = raw.endswith(b"\n")
        byte_lines = raw.split(b"\n")
        if byte_lines and byte_lines[-1] == b"":
            byte_lines.pop()  # the empty tail produced by a final newline

        rows: list[BrokerActivityRow] = []
        last_seq = 0
        n = len(byte_lines)
        for idx, bline in enumerate(byte_lines):
            if idx == n - 1 and not ends_with_newline:
                break  # tolerated: single trailing un-fsynced partial line
            try:
                row = BrokerActivityRow.model_validate_json(bline)
            except (ValidationError, ValueError) as exc:
                raise BrokerActivityWalCorruptError(
                    self._path, f"unparseable line {idx + 1}: {exc}"
                ) from exc
            if row.seq <= last_seq:
                raise BrokerActivityWalCorruptError(
                    self._path,
                    f"non-monotonic seq at line {idx + 1}: "
                    f"{row.seq} after {last_seq}",
                )
            last_seq = row.seq
            if row.seq > after_seq:
                rows.append(row)
                if limit is not None and len(rows) >= limit:
                    break
        return rows

    def _allocate_seq(self) -> int:
        if self._next_seq is None:
            existing = self.read_all()
            self._next_seq = (existing[-1].seq + 1) if existing else 1
        return self._next_seq


__all__ = [
    "BrokerActivityWal",
    "BrokerActivityWalCorruptError",
    "instance_broker_activity_wal_path",
    "legacy_per_run_broker_activity_wal_path",
]
