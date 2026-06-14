"""Phase 6D / VCR-P3-P / VCR-P3-Q / VCR-0018-G — per-instance start lock
+ pre-flight rerun at start time."""

from __future__ import annotations

import threading
from pathlib import Path


def test_per_instance_start_lock_exists() -> None:
    """The manager owns a per-instance lock table so two concurrent ``start``
    requests for the same instance serialize the (check, spawn, register)
    sequence."""
    from app.engine.live.host_daemon import RunnerProcessManager

    manager = RunnerProcessManager(
        repo_root=Path("/tmp/x"), live_runs_root=Path("/tmp/x/runs")
    )
    lock_a = manager._instance_start_lock("inst-A")
    lock_a_again = manager._instance_start_lock("inst-A")
    lock_b = manager._instance_start_lock("inst-B")

    # Same key returns the same lock — without this, two requests get
    # different locks and the serialization is illusory.
    assert lock_a is lock_a_again
    # Different keys get different locks so independent instances coexist.
    assert lock_a is not lock_b


def test_concurrent_starts_for_same_instance_share_one_lock() -> None:
    """Drive the lock acquisition from two threads to assert serialization.
    Without the lock, both threads would enter the critical section
    simultaneously."""
    from app.engine.live.host_daemon import RunnerProcessManager

    manager = RunnerProcessManager(
        repo_root=Path("/tmp/x"), live_runs_root=Path("/tmp/x/runs")
    )
    seen_concurrent: list[bool] = []
    entered = threading.Event()

    def _take_lock_and_block() -> None:
        with manager._instance_start_lock("inst-X"):
            entered.set()
            # Hold the lock; the second thread should be blocked.
            import time

            time.sleep(0.1)

    def _check_lock_is_held() -> None:
        entered.wait(timeout=1.0)
        lock = manager._instance_start_lock("inst-X")
        # Attempt non-blocking acquire — if the lock is held by another
        # thread, this returns False, confirming the serialization.
        got = lock.acquire(blocking=False)
        seen_concurrent.append(got)
        if got:
            lock.release()

    t1 = threading.Thread(target=_take_lock_and_block)
    t2 = threading.Thread(target=_check_lock_is_held)
    t1.start()
    t2.start()
    t1.join(timeout=2.0)
    t2.join(timeout=2.0)
    assert seen_concurrent == [False], (
        "second thread should fail to acquire the lock while the first holds it"
    )


def test_cmd_start_refuses_when_halt_flag_present(tmp_path, capsys) -> None:
    """VCR-P3-Q / Phase 6D — ``cmd_start`` re-runs ``check_no_halt_flag``
    at start time. A halt.flag from yesterday refuses today's start even if
    the operator skipped ``cmd_pre_flight``."""
    from app.engine.live.run import main

    import json as _json

    # Build a minimal valid ledger + a halt.flag.
    (tmp_path / "run_ledger.json").write_text(
        _json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "x",
                "code_sha": "abc",
                "strategy_spec_path": "/x",
                "strategy_spec_sha256": "y",
                "qc_audit_copy_path": "/x",
                "qc_audit_copy_sha256": "z",
                "qc_cloud_backtest_id": "bt",
                "account_id": "DU111",
                "start_date_ms": 1700000000000,
                "live_config": {"sizing": {"kind": "FixedShares", "value": 1}},
                "created_at_ms": 1700000000000,
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "halt.flag").write_text("{}", encoding="utf-8")

    rc = main(
        [
            "start",
            "--run-dir",
            str(tmp_path),
            "--strategy",
            "spy_ema_crossover",
            "--readonly",
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "halt.flag" in err
    assert "prior-session halt" in err.lower()
