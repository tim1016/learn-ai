"""Daemon-boot reconciliation of durable ACTIVE account bindings."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.engine.live.account_registry import (
    AccountInstanceBinding,
    compute_reconcile_namespaces,
    crash_retired_restart_blocking_binding,
    latest_account_instance_binding,
    read_account_instance_registry,
    retire_unmanaged_active_bindings_on_daemon_boot,
    write_account_instance_binding,
)
from app.engine.live.exit_taxonomy import LIVENESS_UNPROVEN_REGISTRY_SOURCE
from app.engine.live.host_daemon import RunnerProcessManager, create_app

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
async def test_host_daemon_boot_retires_orphaned_active_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new daemon cannot trust an ACTIVE row it does not process-own."""
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
    assert latest.lifecycle_state == "RETIRED"
    assert latest.source == LIVENESS_UNPROVEN_REGISTRY_SOURCE
    owned, siblings = compute_reconcile_namespaces(
        artifacts_root=tmp_path,
        account_id=ACCOUNT_ID,
        current_namespace="learn-ai/current-run/v1",
    )
    assert owned == frozenset({"learn-ai/current-run/v1"})
    assert siblings == frozenset()


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

    assert result.bindings_retired == 1
    assert blocking is not None
    assert blocking.lifecycle_state == "RETIRED"
    assert blocking.source == LIVENESS_UNPROVEN_REGISTRY_SOURCE


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

    assert result.bindings_retired == 0
    assert result.preserved_managed_run_ids == (RUN_ID,)
    assert latest is not None
    assert latest.lifecycle_state == "ACTIVE"
