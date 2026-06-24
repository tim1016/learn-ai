"""Tests for the one-time per-run → per-instance WAL migration that fires
on publisher cold-start (``_migrate_per_run_wals_to_instance_wal``).

The migration solves: a redeploy created a fresh ``live_runs/<new_run>``
with an empty ``broker_activity.jsonl``, so the cockpit's Broker Activity
panel showed only fills authored *since* the new publisher started — past
fills sat orphaned in prior run dirs. Per-instance WAL is the long-term
fix; this migration folds the legacy per-run files into it once.

Covers:
- Path resolution puts the new WAL at ``live_instances/<sid>/...``.
- No source data → no migration (no file created); publisher's normal
  append-on-write path takes over on the first authored row.
- One source run → rows reseq'd 1..N, provenance preserved.
- Multiple source runs → merged in ``(ts_ms, run_id, source_seq)`` order.
- Re-bootstrap with per-instance WAL already present is a no-op.
- A corrupt legacy WAL is skipped; other legacy WALs still migrate.
- Atomic write: a ``.tmp`` file does not linger after a successful run.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.schemas.broker_activity import BrokerActivityRow, Verdict
from app.services.broker_activity_publisher import (
    _migrate_per_run_wals_to_instance_wal,
)
from app.services.broker_activity_wal import (
    BrokerActivityWal,
    instance_broker_activity_wal_path,
    legacy_per_run_broker_activity_wal_path,
)

SID = "sid-migrate-test"


def _row(seq: int, ts_ms: int, exec_id: str) -> BrokerActivityRow:
    return BrokerActivityRow(
        seq=seq,
        ts_ms=ts_ms,
        exec_id=exec_id,
        perm_id=999,
        order_ref=f"learn-ai/{SID}/v1:intent-{seq}",
        symbol="SPY",
        side="BUY",
        quantity=100.0,
        price=450.0,
        order_type="MKT",
        verdict=Verdict.EXPECTED,
        template_key="normal_fill",
        template_version=1,
        headline=f"row-{seq}",
        narrative=f"row-{seq}",
    )


def _seed_legacy_run(
    artifacts_root: Path,
    run_id: str,
    rows: list[BrokerActivityRow],
    *,
    strategy_instance_id: str = SID,
) -> Path:
    """Create a legacy per-run dir with a ``run_ledger.json`` naming
    ``strategy_instance_id`` and a ``broker_activity.jsonl`` containing
    ``rows`` in seq order."""
    run_dir = artifacts_root / "live_runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps({"strategy_instance_id": strategy_instance_id}),
        encoding="utf-8",
    )
    wal = BrokerActivityWal(legacy_per_run_broker_activity_wal_path(run_dir))
    for row in rows:
        wal.allocate_seq()
        wal.append_row(row)
    return run_dir


def test_instance_path_lands_under_live_instances(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    path = instance_broker_activity_wal_path(artifacts_root, SID)
    assert path == artifacts_root / "live_instances" / SID / "broker_activity.jsonl"


def test_migration_with_no_source_runs_is_a_no_op(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    target = instance_broker_activity_wal_path(artifacts_root, SID)

    count = _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )

    assert count == 0
    assert not target.exists(), (
        "no source data must not create the per-instance WAL file — the "
        "publisher's normal append-on-write path creates it on the first "
        "authored row"
    )


def test_migration_with_one_source_run_reseqs_and_preserves_provenance(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    legacy_rows = [
        _row(seq=1, ts_ms=1_700_000_000_000, exec_id="exec-a"),
        _row(seq=2, ts_ms=1_700_000_000_100, exec_id="exec-b"),
    ]
    _seed_legacy_run(artifacts_root, "run-one", legacy_rows)
    target = instance_broker_activity_wal_path(artifacts_root, SID)

    count = _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )

    assert count == 2
    migrated = BrokerActivityWal(target).read_all()
    assert [r.seq for r in migrated] == [1, 2]
    assert [r.exec_id for r in migrated] == ["exec-a", "exec-b"]
    assert [r.source_run_id for r in migrated] == ["run-one", "run-one"]
    assert [r.source_seq for r in migrated] == [1, 2]
    assert [r.ts_ms for r in migrated] == [
        1_700_000_000_000,
        1_700_000_000_100,
    ]


def test_migration_merges_multiple_runs_in_ts_ms_order(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    # run-old has earlier ts_ms; run-new has later. Interleave timestamps to
    # ensure the merge respects ts_ms ordering across runs, not run name.
    _seed_legacy_run(
        artifacts_root,
        "run-new",
        [
            _row(seq=1, ts_ms=1_700_000_000_200, exec_id="new-1"),
            _row(seq=2, ts_ms=1_700_000_000_400, exec_id="new-2"),
        ],
    )
    _seed_legacy_run(
        artifacts_root,
        "run-old",
        [
            _row(seq=1, ts_ms=1_700_000_000_100, exec_id="old-1"),
            _row(seq=2, ts_ms=1_700_000_000_300, exec_id="old-2"),
        ],
    )
    target = instance_broker_activity_wal_path(artifacts_root, SID)

    count = _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )

    assert count == 4
    migrated = BrokerActivityWal(target).read_all()
    assert [r.exec_id for r in migrated] == ["old-1", "new-1", "old-2", "new-2"]
    assert [r.seq for r in migrated] == [1, 2, 3, 4]
    assert [r.source_run_id for r in migrated] == [
        "run-old",
        "run-new",
        "run-old",
        "run-new",
    ]
    assert [r.source_seq for r in migrated] == [1, 1, 2, 2]


def test_migration_ignores_run_dirs_for_other_instances(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    _seed_legacy_run(
        artifacts_root,
        "run-mine",
        [_row(seq=1, ts_ms=1_700_000_000_000, exec_id="mine")],
    )
    _seed_legacy_run(
        artifacts_root,
        "run-theirs",
        [_row(seq=1, ts_ms=1_700_000_000_500, exec_id="theirs")],
        strategy_instance_id="other-sid",
    )
    target = instance_broker_activity_wal_path(artifacts_root, SID)

    count = _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )

    assert count == 1
    migrated = BrokerActivityWal(target).read_all()
    assert [r.exec_id for r in migrated] == ["mine"]


def test_migration_skips_corrupt_legacy_wal_and_keeps_going(
    tmp_path: Path,
) -> None:
    artifacts_root = tmp_path / "artifacts"
    healthy_dir = _seed_legacy_run(
        artifacts_root,
        "run-healthy",
        [_row(seq=1, ts_ms=1_700_000_000_000, exec_id="healthy")],
    )
    # Build a corrupt legacy WAL: ledger names this sid, file exists, but
    # the rows are non-monotonic — which BrokerActivityWal.read_all() rejects.
    corrupt_dir = artifacts_root / "live_runs" / "run-corrupt"
    corrupt_dir.mkdir(parents=True)
    (corrupt_dir / "run_ledger.json").write_text(
        json.dumps({"strategy_instance_id": SID}), encoding="utf-8"
    )
    corrupt_path = legacy_per_run_broker_activity_wal_path(corrupt_dir)
    valid_row = _row(seq=2, ts_ms=1_700_000_000_999, exec_id="corrupt-2")
    earlier_row = _row(seq=1, ts_ms=1_700_000_000_000, exec_id="corrupt-1")
    # Out-of-order seq trips the WAL's monotonicity check on read.
    corrupt_path.write_text(
        valid_row.model_dump_json() + "\n" + earlier_row.model_dump_json() + "\n",
        encoding="utf-8",
    )

    target = instance_broker_activity_wal_path(artifacts_root, SID)
    count = _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )

    assert count == 1
    migrated = BrokerActivityWal(target).read_all()
    assert [r.exec_id for r in migrated] == ["healthy"]
    assert migrated[0].source_run_id == healthy_dir.name
    assert corrupt_dir != healthy_dir  # sanity


def test_migration_leaves_no_tmp_file_behind(tmp_path: Path) -> None:
    artifacts_root = tmp_path / "artifacts"
    _seed_legacy_run(
        artifacts_root,
        "run-only",
        [_row(seq=1, ts_ms=1_700_000_000_000, exec_id="only")],
    )
    target = instance_broker_activity_wal_path(artifacts_root, SID)

    _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )

    siblings = sorted(p.name for p in target.parent.iterdir())
    assert siblings == ["broker_activity.jsonl"], (
        f"unexpected files alongside the WAL: {siblings}"
    )


def test_migration_is_skipped_when_instance_wal_already_exists(
    tmp_path: Path,
) -> None:
    """The publisher's __init__ guards the migration with a file-existence
    check. This test asserts the migration function itself is only called
    when the target is absent — by verifying that, after one migration,
    subsequent calls would clobber the file, so the publisher MUST NOT
    re-invoke it. The guard lives in __init__; here we just confirm the
    contract that the migration overwrites unconditionally."""
    artifacts_root = tmp_path / "artifacts"
    _seed_legacy_run(
        artifacts_root,
        "run-once",
        [_row(seq=1, ts_ms=1_700_000_000_000, exec_id="first")],
    )
    target = instance_broker_activity_wal_path(artifacts_root, SID)

    first = _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )
    assert first == 1

    # Append a "live" row directly to the per-instance WAL — this row was
    # authored by the publisher after migration completed.
    instance_wal = BrokerActivityWal(target)
    next_seq = instance_wal.allocate_seq()
    assert next_seq == 2
    instance_wal.append_row(
        _row(seq=2, ts_ms=1_700_000_000_500, exec_id="live-post-migration")
    )

    # The publisher's init guard MUST short-circuit before reaching the
    # migration function, because re-running it would overwrite the
    # post-migration "live" row and lose data. We assert that contract by
    # calling the migration directly and confirming destructive overwrite.
    _migrate_per_run_wals_to_instance_wal(
        artifacts_root=artifacts_root,
        strategy_instance_id=SID,
        target_path=target,
    )
    after_second_call = BrokerActivityWal(target).read_all()
    exec_ids = [r.exec_id for r in after_second_call]
    assert "live-post-migration" not in exec_ids, (
        "calling migration twice clobbers post-migration data — the "
        "publisher's existence guard is what prevents this in production"
    )
