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

from app.engine.live.identity import _INSTANCE_ID_RE, validate_strategy_instance_id
from app.schemas.broker_activity import BrokerActivityRow
from app.services.jsonl_wal import JsonlWal


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

    ``strategy_instance_id`` flows into a directory segment, so we
    apply the same three-layer guard the LEAN-sidecar workspace builder
    uses on ``run_id`` (``app.lean_sidecar.workspace.create_for_run``):

    1. Validate via :func:`validate_strategy_instance_id` (fails closed
       on path-traversal / separator / NUL / empty / non-pattern
       inputs).
    2. **Reconstruct from the regex match group** so the value used in
       path construction below is provably from the regex capture
       rather than from the raw caller input. CodeQL's
       ``py/path-injection`` rule recognises ``re.Match.group(0)``
       output as sanitised; the cross-function ``validate_*`` return
       value it does not follow.
    3. Resolve the constructed path and verify it stays under
       ``<artifacts_root>/live_instances`` via :func:`os.path.commonpath`
       — catches symlink escapes the regex cannot see.
    """
    # Boundary check (raises on bad input).
    validate_strategy_instance_id(strategy_instance_id)
    # Inline match.group(0) reconstruction so the value flowing into the
    # path below is dataflow-provably from the regex capture, not the
    # raw caller input. The cross-function ``validate_*`` return CodeQL
    # does not follow.
    match = _INSTANCE_ID_RE.fullmatch(strategy_instance_id)
    if match is None:
        # Defensive: validate_strategy_instance_id already raised on this case.
        raise ValueError(
            f"strategy_instance_id rejected on second check: {strategy_instance_id!r}"
        )
    safe_sid = match.group(0)

    instances_root = (artifacts_root / "live_instances").resolve()
    # ``resolve(strict=False)`` returns the canonical path even when the
    # target does not yet exist — the publisher materialises the dir on
    # first append.
    candidate = (instances_root / safe_sid / "broker_activity.jsonl").resolve(strict=False)
    try:
        common = os.path.commonpath([str(candidate), str(instances_root)])
    except ValueError as exc:
        # ``commonpath`` raises on drive-letter mismatches (Windows).
        raise ValueError(
            f"broker-activity WAL path {candidate} cannot share a root with {instances_root}"
        ) from exc
    if common != str(instances_root):
        raise ValueError(
            f"broker-activity WAL path {candidate} escapes instances root {instances_root}"
        )
    return candidate


class BrokerActivityWal:
    """Append-only WAL writer/reader scoped to one strategy-instance run dir."""

    def __init__(self, path: Path) -> None:
        self._wal = JsonlWal(
            path,
            record_model=BrokerActivityRow,
            corrupt_error=BrokerActivityWalCorruptError,
            seq_of=lambda row: row.seq,
            label="broker-activity",
        )

    @property
    def path(self) -> Path:
        return self._wal.path

    def allocate_seq(self) -> int:
        """Return the next ``seq`` the publisher should stamp on a row.

        The publisher's row construction needs the seq BEFORE the row
        is built (the seq is on the row). This method is idempotent
        within a single process; ``append_row`` consumes the seq and
        advances internal state.
        """
        return self._wal.allocate_seq()

    def append_row(self, row: BrokerActivityRow) -> None:
        """Persist one row. The row's ``seq`` must equal the value last
        returned by ``allocate_seq`` (we don't re-stamp here — the
        publisher constructed the row with that seq already)."""
        try:
            self._wal.append(row)
        except ValueError as exc:
            raise ValueError(
                f"broker-activity WAL append got row.seq={row.seq} but the next "
                "available seq is "
                f"{self._wal.allocate_seq()} (the publisher must construct "
                "the row with the seq returned by allocate_seq)"
            ) from exc

    def read_all(self) -> list[BrokerActivityRow]:
        """Read every complete row in seq order.

        Used by cold-start (publisher fold) and by the REST backfill
        endpoint when no ``after_seq`` cursor is supplied.
        """
        return self._wal.read_all()

    def read_from(
        self, *, after_seq: int, limit: int | None = None
    ) -> list[BrokerActivityRow]:
        """Read rows with ``seq > after_seq`` in seq order, capped at ``limit``.

        Drives the REST paginated backfill: client passes the highest
        seq they have, we return the next page.
        """
        return self._wal.read_from(after_seq=after_seq, limit=limit)

    def last_seq(self) -> int:
        """Return the highest seq currently persisted, or ``0`` if empty.

        Drives the LiveStateEnvelope cursor (``last_broker_activity_wal_seq``)
        and the SSE handoff cursor for clients backfilling via REST then
        switching to SSE.
        """
        return self._wal.last_seq()


__all__ = [
    "BrokerActivityWal",
    "BrokerActivityWalCorruptError",
    "instance_broker_activity_wal_path",
    "legacy_per_run_broker_activity_wal_path",
]
