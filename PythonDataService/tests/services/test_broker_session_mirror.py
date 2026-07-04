from __future__ import annotations

import json
from pathlib import Path

from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
    write_engine_runtime_snapshot,
)
from app.services.broker_session_mirror import _build_runtime_index


def test_runtime_index_reads_child_client_id_from_engine_runtime(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-a"
    run_dir.mkdir()
    (run_dir / "run_ledger.json").write_text(
        json.dumps(
            {
                "run_id": "run-a",
                "strategy_instance_id": "PrajiTSLADemo",
                "account_id": "DU123",
            }
        ),
        encoding="utf-8",
    )
    write_engine_runtime_snapshot(
        run_dir,
        EngineRuntimeSnapshot(
            strategy_instance_id="PrajiTSLADemo",
            run_id="run-a",
            pid=21760,
            process_start_identity="child-boot-0001",
            snapshot_seq=3,
            written_at_ms=1_783_120_000_000,
            command_loop=CommandLoopBlock(
                heartbeat_at_ms=1_783_120_000_000,
                state="RUNNING",
            ),
            broker=BrokerBlock(
                identity="PAPER_VERIFIED",
                submission_capability="PAPER_ORDERS_ENABLED",
                effective_posture="PAPER_EXECUTION",
                connection_state="connected",
                recovery_state="RECONNECTING",
                connection_epoch=1,
                client_id=17,
                connected_account="DU123",
                port_class="paper_port",
                observation_at_ms=1_783_120_000_000,
                probe_completed_at_ms=1_783_119_999_900,
                reconnect_attempt=0,
            ),
            bar_loop=BarLoopBlock(
                heartbeat_at_ms=1_783_120_000_000,
                latest_source_bar_ms=1_783_119_940_000,
                expected_interval_ms=60_000,
            ),
            control_plane=ControlPlaneBlock(
                lease_observed_at_ms=1_783_119_999_500,
                observed_daemon_boot_id="daemon-boot-0001",
            ),
        ),
    )

    index = _build_runtime_index(tmp_path)

    entry = index[str(run_dir.resolve())]
    assert entry.client_id == 17
    assert entry.strategy_instance_id == "PrajiTSLADemo"
    assert entry.account_id == "DU123"
    assert entry.recovery_state == "RECONNECTING"
