"""A failed volume write cannot reopen a drained lane's in-process start gate (#3197)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.broker.alpaca.clerk import fleet_boot


def test_drain_refuses_starts_during_a_failed_volume_write_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    boot = fleet_boot.FleetLaneBoot(
        presence=SimpleNamespace(), clerk_id="clerk", worker_key="worker", volume_root=tmp_path,
        registry_id="registry", volume_id="volume",
    )
    attempts = 0

    def mark(_root: Path) -> bool:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("volume is read-only")
        return True

    monkeypatch.setattr(fleet_boot, "mark_confirmation_evidence_draining", mark)

    fleet_boot._learn_drain(boot)
    assert boot.draining and not boot.drain_evidence_marked

    fleet_boot._learn_drain(boot)
    assert boot.draining and boot.drain_evidence_marked and attempts == 2
