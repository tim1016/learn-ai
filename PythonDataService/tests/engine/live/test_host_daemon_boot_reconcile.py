"""Daemon-boot reconciliation of durable ACTIVE account bindings."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.engine.live.account_registry import (
    AccountInstanceBinding,
    compute_reconcile_namespaces,
    crash_retired_restart_blocking_binding,
    evaluate_account_instance_binding,
    latest_account_instance_binding,
    pending_account_binding_retirements,
    read_account_instance_registry,
    retire_unmanaged_active_bindings_on_daemon_boot,
    write_account_instance_binding,
)
from app.engine.live.exit_taxonomy import LIVENESS_UNPROVEN_REGISTRY_SOURCE
from app.engine.live.host_daemon import (
    ManagedProcess,
    RunnerProcessManager,
    create_app,
)

ACCOUNT_ID = "DU1234567"
INSTANCE_ID = "spy-ema-paper"
RUN_ID = "run-orphaned"
NAMESPACE = f"learn-ai/{INSTANCE_ID}/v1"


def _active_binding() -> AccountInstanceBinding:
    return AccountInstanceBinding(
        account_id=ACCOUNT_ID,
        strategy_instance_id=INSTANCE_ID,
        run_id=RUN_ID,
        bot_order_namespace=NAMESPACE,
        lifecycle_state="ACTIVE",
        recorded_at_ms=1_700_000_000_000,
        source="host_daemon.start",
    )


@pytest.mark.asyncio
async def test_host_daemon_boot_proposes_orphaned_active_binding_for_clerk(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new daemon records unowned liveness as a Clerk-only retirement proposal."""
    from app.engine.live import host_daemon

    live_runs_root = tmp_path / "live_runs"
    (live_runs_root / RUN_ID).mkdir(parents=True)
    write_account_instance_binding(tmp_path, _active_binding())
    monkeypatch.setattr(host_daemon, "ensure_daemon_token", lambda _root: "test-token")

    manager = RunnerProcessManager(
        repo_root=tmp_path,
        live_runs_root=live_runs_root,
        boot_id="new-daemon-boot",
    )
    app = create_app(manager=manager, allowed_origins=["http://localhost"])

    async with app.router.lifespan_context(app):
        latest = latest_account_instance_binding(
            read_account_instance_registry(tmp_path, ACCOUNT_ID),
            account_id=ACCOUNT_ID,
            strategy_instance_id=INSTANCE_ID,
        )

    assert latest is not None
    assert latest.lifecycle_state == "ACTIVE"
    [proposal] = pending_account_binding_retirements(tmp_path, account_id=ACCOUNT_ID)
    assert proposal.source == LIVENESS_UNPROVEN_REGISTRY_SOURCE
    owned, siblings = compute_reconcile_namespaces(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        current_namespace="learn-ai/current-run/v1",
    )
    assert owned == frozenset({"learn-ai/current-run/v1"})
    assert siblings == frozenset({NAMESPACE})


def test_crash_retired_restart_blocking_binding_blocks_after_reboot(
    tmp_path: Path,
) -> None:
    """Liveness-unproven retirement requires recovery proof before restart."""
    write_account_instance_binding(tmp_path, _active_binding())

    result = retire_unmanaged_active_bindings_on_daemon_boot(
        tmp_path,
        managed_run_ids=frozenset(),
        now_ms=1_700_000_001_000,
    )
    blocking = crash_retired_restart_blocking_binding(
        tmp_path,
        account_id=ACCOUNT_ID,
        strategy_instance_id=INSTANCE_ID,
    )

    assert result.retirement_proposals_recorded == 1
    assert blocking is None
    gate = evaluate_account_instance_binding(
        tmp_path,
        account_id=ACCOUNT_ID,
        strategy_instance_id=INSTANCE_ID,
        run_id=RUN_ID,
        bot_order_namespace=NAMESPACE,
    )
    assert gate.status == "block"
    assert gate.operator_reason == "ACCOUNT_BINDING_RETIREMENT_PENDING"


def test_boot_reconcile_preserves_process_owned_active_binding(tmp_path: Path) -> None:
    """A process already owned by this manager remains ACTIVE."""
    write_account_instance_binding(tmp_path, _active_binding())

    result = retire_unmanaged_active_bindings_on_daemon_boot(
        tmp_path,
        managed_run_ids=frozenset({RUN_ID}),
        now_ms=1_700_000_001_000,
    )
    latest = latest_account_instance_binding(
        read_account_instance_registry(tmp_path, ACCOUNT_ID),
        account_id=ACCOUNT_ID,
        strategy_instance_id=INSTANCE_ID,
    )

    assert result.retirement_proposals_recorded == 0
    assert result.preserved_managed_run_ids == (RUN_ID,)
    assert latest is not None
    assert latest.lifecycle_state == "ACTIVE"


@pytest.mark.asyncio
async def test_process_reaper_retires_post_boot_crash_without_status_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The daemon settles child crashes without any health/status request."""
    from app.engine.live import host_daemon

    live_runs_root = tmp_path / "live_runs"
    run_dir = live_runs_root / RUN_ID
    run_dir.mkdir(parents=True)
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "account_id": ACCOUNT_ID,
                "strategy_instance_id": INSTANCE_ID,
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "run_status.json").write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "exit_code": 1,
                "exit_reason": "exception",
            }
        ),
        encoding="utf-8",
    )
    write_account_instance_binding(tmp_path, _active_binding())
    monkeypatch.setattr(host_daemon, "ensure_daemon_token", lambda _root: "test-token")
    monkeypatch.setattr(host_daemon, "_PROCESS_REAPER_INTERVAL_SECONDS", 0.01)

    log_path = run_dir / "host_daemon.log"
    log_handle = log_path.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(0.2); raise SystemExit(1)"],
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    manager = RunnerProcessManager(
        repo_root=tmp_path,
        live_runs_root=live_runs_root,
        boot_id="current-daemon-boot",
    )
    manager._managed[INSTANCE_ID] = ManagedProcess(
        strategy_instance_id=INSTANCE_ID,
        run_id=RUN_ID,
        run_dir=run_dir,
        process=process,
        command=[sys.executable, "-c", "post-boot-crash"],
        started_at_ms=1_700_000_000_100,
        log_path=log_path,
        log_handle=log_handle,
    )
    app = create_app(manager=manager, allowed_origins=["http://localhost"])

    try:
        async with app.router.lifespan_context(app):
            latest = latest_account_instance_binding(
                read_account_instance_registry(tmp_path, ACCOUNT_ID),
                account_id=ACCOUNT_ID,
                strategy_instance_id=INSTANCE_ID,
            )
            assert latest is not None
            assert latest.lifecycle_state == "ACTIVE"

            for _ in range(100):
                await asyncio.sleep(0.01)
                latest = latest_account_instance_binding(
                    read_account_instance_registry(tmp_path, ACCOUNT_ID),
                    account_id=ACCOUNT_ID,
                    strategy_instance_id=INSTANCE_ID,
                )
                if pending_account_binding_retirements(tmp_path, account_id=ACCOUNT_ID):
                    break

            assert latest is not None
            assert latest.lifecycle_state == "ACTIVE"
            [proposal] = pending_account_binding_retirements(tmp_path, account_id=ACCOUNT_ID)
            assert proposal.source == "host_daemon.process_crashed"
            _owned, siblings = compute_reconcile_namespaces(
                artifacts_root=tmp_path,
                account_id=ACCOUNT_ID,
                current_namespace="learn-ai/current-run/v1",
            )
            assert siblings == frozenset({NAMESPACE})
            assert (
                crash_retired_restart_blocking_binding(
                    tmp_path,
                    account_id=ACCOUNT_ID,
                    strategy_instance_id=INSTANCE_ID,
                )
                is None
            )
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        if not log_handle.closed:
            log_handle.close()
