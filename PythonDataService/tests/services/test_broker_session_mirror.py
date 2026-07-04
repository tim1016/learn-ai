from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from app.engine.live.engine_runtime import (
    BarLoopBlock,
    BrokerBlock,
    CommandLoopBlock,
    ControlPlaneBlock,
    EngineRuntimeSnapshot,
    write_engine_runtime_snapshot,
)
from app.schemas.broker_session import (
    BrokerSessionRosterRow,
    GatewaySocketsSnapshot,
)
from app.services import broker_session_mirror
from app.services.broker_session_mirror import (
    BrokerSessionMirrorService,
    _build_runtime_index,
)


class _FakeEventService:
    def counts_by_client_id(self) -> dict[int, dict[str, int]]:
        return {}


class _FakeHistoryService:
    def __init__(self, past_rows=None) -> None:
        self.past_rows = past_rows or []
        self.current_rows = None
        self.snapshots = []

    def past_closed_rows(self, *, current_rows):
        self.current_rows = list(current_rows)
        return self.past_rows

    def append_snapshot(self, snapshot) -> None:
        self.snapshots.append(snapshot)


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


async def test_mirror_snapshot_records_roster_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    history = _FakeHistoryService()
    monkeypatch.setattr(
        broker_session_mirror,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "live_runner_daemon_url": "",
                "live_runs_root": str(tmp_path),
                "port": 4002,
            },
        )(),
    )
    monkeypatch.setattr(broker_session_mirror, "_data_plane_health", lambda: None)
    service = BrokerSessionMirrorService(
        event_service=_FakeEventService(),
        history_service=history,
    )

    snapshot = await service.snapshot()

    assert history.snapshots == [snapshot]
    assert snapshot.observer_status == "degraded"


async def test_mirror_snapshot_includes_past_closed_history_when_observer_online(
    tmp_path: Path,
    monkeypatch,
) -> None:
    past_row = BrokerSessionRosterRow(
        row_id="bot:run-a",
        identity_type="bot",
        recency="past_closed",
        socket_present=False,
        run_id="run-a",
        client_id=42,
        as_of_ms=1_783_120_000_000,
    )
    history = _FakeHistoryService(past_rows=[past_row])
    monkeypatch.setattr(
        broker_session_mirror,
        "get_settings",
        lambda: type(
            "Settings",
            (),
            {
                "live_runner_daemon_url": "http://daemon.test",
                "live_runs_root": str(tmp_path),
                "port": 4002,
            },
        )(),
    )
    monkeypatch.setattr(broker_session_mirror, "_data_plane_health", lambda: None)

    async def _fetch_gateway_sockets(_daemon_url, *, gateway_port):
        return (
            SimpleNamespace(detail=None),
            GatewaySocketsSnapshot(
                fetched_at_ms=1_783_120_000_100,
                gateway_port=gateway_port,
                sockets=[],
            ),
        )

    async def _fetch_instances(_daemon_url):
        return (
            SimpleNamespace(detail=None),
            {"instances": [], "fetched_at_ms": 1_783_120_000_100},
        )

    monkeypatch.setattr(
        broker_session_mirror.host_daemon_client,
        "fetch_gateway_sockets",
        _fetch_gateway_sockets,
    )
    monkeypatch.setattr(
        broker_session_mirror.host_daemon_client,
        "fetch_instances",
        _fetch_instances,
    )
    service = BrokerSessionMirrorService(
        event_service=_FakeEventService(),
        history_service=history,
    )

    snapshot = await service.snapshot()

    assert snapshot.observer_status == "online"
    assert snapshot.rows == [past_row]
    assert history.current_rows == []
