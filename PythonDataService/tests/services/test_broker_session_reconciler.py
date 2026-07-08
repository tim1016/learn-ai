from __future__ import annotations

from app.broker.ibkr.models import IbkrConnectionHealth
from app.schemas.broker_session import GatewaySocketRow
from app.schemas.live_runs import (
    HostRunnerInstance,
    HostRunnerInstancesStatus,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
)
from app.services.broker_session_reconciler import (
    RuntimeIndexEntry,
    reconcile_broker_session_roster,
    reconcile_broker_session_snapshot,
)

AS_OF_MS = 1_783_120_000_000


def test_reconciler_surfaces_registry_offline_socket_live() -> None:
    run_dir = "/runs/run-a"
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=21760,
                command="python",
                run_dir=run_dir,
                local_port=50123,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=_registry(
            _instance(
                "PrajiTSLADemo",
                "run-a",
                run_dir,
                HostRunnerProcessStatus(
                    state=HostRunnerProcessState.exited,
                    pid=21760,
                    ended_at_ms=AS_OF_MS - 1_000,
                ),
            )
        ),
        runtime_index={
            run_dir: RuntimeIndexEntry(
                strategy_instance_id="PrajiTSLADemo",
                run_id="run-a",
                run_dir=run_dir,
                account_id="DU123",
                connection_state="connected",
            )
        },
        as_of_ms=AS_OF_MS,
    )

    assert len(rows) == 1
    assert rows[0].identity_type == "bot"
    assert rows[0].recency == "current"
    assert rows[0].socket_present is True
    assert rows[0].strategy_instance_id == "PrajiTSLADemo"
    assert rows[0].recovery_state == "HEALTHY"
    assert rows[0].attention_codes == ["REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE"]


def test_reconciler_does_not_claim_registry_offline_when_registry_unavailable() -> None:
    run_dir = "/runs/run-a"
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=21760,
                command="python",
                run_dir=run_dir,
                local_port=50123,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=None,
        runtime_index={
            run_dir: RuntimeIndexEntry(
                strategy_instance_id="PrajiTSLADemo",
                run_id="run-a",
                run_dir=run_dir,
                account_id="DU123",
                connection_state="connected",
            )
        },
        as_of_ms=AS_OF_MS,
    )

    assert rows[0].identity_type == "bot"
    assert rows[0].attention_codes == []


def test_reconciler_matches_container_runtime_by_run_id_when_socket_path_is_host_path() -> None:
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=21760,
                command="python",
                run_dir="/Users/inkant/learn-ai/live_runs/run-a",
                local_port=50123,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=None,
        runtime_index={
            "/app/live_runs/run-a": RuntimeIndexEntry(
                strategy_instance_id="PrajiTSLADemo",
                run_id="run-a",
                run_dir="/app/live_runs/run-a",
                account_id="DU123",
                client_id=42,
                connection_state="connected",
            )
        },
        as_of_ms=AS_OF_MS,
    )

    assert rows[0].identity_type == "bot"
    assert rows[0].strategy_instance_id == "PrajiTSLADemo"
    assert rows[0].client_id == 42


def test_reconciler_surfaces_started_but_no_socket() -> None:
    run_dir = "/runs/run-b"
    rows = reconcile_broker_session_roster(
        socket_rows=[],
        registry_snapshot=_registry(
            _instance(
                "DEPVALJUL1",
                "run-b",
                run_dir,
                HostRunnerProcessStatus(
                    state=HostRunnerProcessState.running,
                    pid=22332,
                    started_at_ms=AS_OF_MS - 5_000,
                ),
            )
        ),
        runtime_index={
            run_dir: RuntimeIndexEntry(
                strategy_instance_id="DEPVALJUL1",
                run_id="run-b",
                run_dir=run_dir,
            )
        },
        as_of_ms=AS_OF_MS,
    )

    assert len(rows) == 1
    assert rows[0].identity_type == "bot"
    assert rows[0].recency == "unknown"
    assert rows[0].socket_present is False
    assert rows[0].attention_codes == ["STARTED_BUT_NO_SOCKET"]


def test_reconciler_classifies_known_socket_without_pid_as_orphan() -> None:
    run_dir = "/runs/run-c"
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=None,
                command="",
                run_dir=run_dir,
                local_port=50125,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=_registry(),
        runtime_index={
            run_dir: RuntimeIndexEntry(
                strategy_instance_id="orphan-demo",
                run_id="run-c",
                run_dir=run_dir,
                client_id=17,
            )
        },
        as_of_ms=AS_OF_MS,
    )

    assert len(rows) == 1
    assert rows[0].identity_type == "orphaned_bot_socket"
    assert rows[0].recency == "current"
    assert rows[0].attention_codes == ["SOCKET_WITHOUT_LIVE_PID", "ORPHANED_BOT_SOCKET"]
    assert rows[0].notice is not None
    assert rows[0].notice.code == "broker_session.orphaned_socket"
    assert rows[0].notice.tier == "critical"
    assert rows[0].notice.actionability == "routed"
    assert rows[0].notice.action.kind == "external_manual_check"
    assert rows[0].notice.action.target == "ibkr_sessions"
    assert rows[0].notice.forensic_facts["client_id"] == 17


def test_reconciler_classifies_unattributed_socket_as_ghost() -> None:
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=999,
                command="external",
                local_port=50126,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=_registry(),
        runtime_index={},
        as_of_ms=AS_OF_MS,
    )

    assert len(rows) == 1
    assert rows[0].identity_type == "ghost"
    assert rows[0].attention_codes == ["GHOST_SOCKET"]


def test_reconciler_surfaces_last_known_rows_when_socket_probe_unavailable() -> None:
    run_dir = "/runs/run-last-known"
    rows = reconcile_broker_session_roster(
        socket_rows=[],
        registry_snapshot=_registry(),
        runtime_index={
            run_dir: RuntimeIndexEntry(
                strategy_instance_id="stale-demo",
                run_id="run-last-known",
                run_dir=run_dir,
                account_id="DU123",
                client_id=17,
                connection_state="connected",
                last_event_ms=AS_OF_MS - 30_000,
            )
        },
        as_of_ms=AS_OF_MS,
        socket_probe_available=False,
        stale_after_ms=25_000,
    )

    assert len(rows) == 1
    assert rows[0].identity_type == "bot"
    assert rows[0].recency == "past_last_known"
    assert rows[0].socket_present is False
    assert rows[0].client_id == 17
    assert rows[0].attention_codes == [
        "GHOST_DETECTION_UNAVAILABLE",
        "CLIENT_SIGNAL_STALE",
    ]


def test_snapshot_reconciler_preserves_connected_degraded_data_plane_state() -> None:
    result = reconcile_broker_session_snapshot(
        socket_rows=[],
        registry_snapshot=_registry(),
        runtime_index={},
        data_plane_health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=42,
            connected=True,
            account_id="DU123",
            is_paper=True,
            fetched_at_ms=AS_OF_MS,
            connection_state="degraded_data_farm",
            last_transition_ms=AS_OF_MS - 500,
        ),
        as_of_ms=AS_OF_MS,
    )

    assert result.rows == []
    assert len(result.global_events) == 1
    event = result.global_events[0]
    assert event.code == "DATA_PLANE_BROKER_CLIENT"
    assert event.current is True
    assert event.severity == "critical"
    assert (
        event.summary
        == "The data-plane IBKR client is socket-connected, but IBKR data-farm evidence is degraded."
    )


def test_reconciler_projects_runtime_recovery_states() -> None:
    cases = [
        ("soft_lost", "LINK_INTERRUPTED"),
        ("subscriptions_stale", "RESTORING"),
        ("recovering", "RESTORING"),
        ("reconnecting", "RECONNECTING"),
        ("hard_down", "HARD_DOWN"),
        ("disconnected", "SOCKET_DOWN"),
    ]

    for connection_state, expected_recovery_state in cases:
        run_dir = f"/runs/{connection_state}"
        rows = reconcile_broker_session_roster(
            socket_rows=[
                GatewaySocketRow(
                    pid=21760,
                    command="python",
                    run_dir=run_dir,
                    local_port=50123,
                    remote_host="127.0.0.1",
                    remote_port=4002,
                )
            ],
            registry_snapshot=_registry(),
            runtime_index={
                run_dir: RuntimeIndexEntry(
                    strategy_instance_id=f"bot-{connection_state}",
                    run_id=f"run-{connection_state}",
                    run_dir=run_dir,
                    connection_state=connection_state,
                )
            },
            as_of_ms=AS_OF_MS,
        )

        assert rows[0].recovery_state == expected_recovery_state


def test_reconciler_prefers_runtime_recovery_state_for_child_row() -> None:
    run_dir = "/runs/runtime-recovery"
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=21760,
                command="python",
                run_dir=run_dir,
                local_port=50123,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=_registry(),
        runtime_index={
            run_dir: RuntimeIndexEntry(
                strategy_instance_id="bot-runtime-recovery",
                run_id="run-runtime-recovery",
                run_dir=run_dir,
                connection_state="connected",
                recovery_state="RECONNECTING",
            )
        },
        as_of_ms=AS_OF_MS,
    )

    assert rows[0].connection_state == "connected"
    assert rows[0].recovery_state == "RECONNECTING"


def test_snapshot_reconciler_lifts_gvproxy_and_data_plane_to_global_events() -> None:
    result = reconcile_broker_session_snapshot(
        socket_rows=[
            GatewaySocketRow(
                pid=101,
                command="gvproxy",
                argv=["/usr/bin/gvproxy"],
                local_port=50123,
                remote_host="127.0.0.1",
                remote_port=4002,
            ),
            GatewaySocketRow(
                pid=102,
                command="/Applications/Docker.app/Contents/MacOS/gvproxy",
                local_port=50124,
                remote_host="127.0.0.1",
                remote_port=4002,
            ),
        ],
        registry_snapshot=_registry(),
        runtime_index={},
        data_plane_health=IbkrConnectionHealth(
            mode="paper",
            host="127.0.0.1",
            port=4002,
            client_id=42,
            connected=True,
            account_id="DU123",
            is_paper=True,
            fetched_at_ms=AS_OF_MS,
            connection_state="connected",
            last_transition_ms=AS_OF_MS - 500,
        ),
        as_of_ms=AS_OF_MS,
    )

    assert result.rows == []
    assert [event.code for event in result.global_events] == [
        "GATEWAY_NETWORK_PROXY",
        "DATA_PLANE_BROKER_CLIENT",
    ]
    assert result.global_events[0].label == "Gateway network proxy"
    assert result.global_events[0].current is True
    assert "2 virtual-machine network proxy sockets" in result.global_events[0].summary
    assert result.global_events[1].label == "Data-plane broker client"
    assert result.global_events[1].current is True


def test_reconciler_authors_row_display_labels() -> None:
    rows = reconcile_broker_session_roster(
        socket_rows=[
            GatewaySocketRow(
                pid=999,
                command="external",
                local_port=50126,
                remote_host="127.0.0.1",
                remote_port=4002,
            )
        ],
        registry_snapshot=_registry(),
        runtime_index={},
        as_of_ms=AS_OF_MS,
    )

    row = rows[0]
    assert row.presentation.identity.label == "Unattributed broker socket"
    assert row.presentation.identity.severity == "warning"
    assert row.presentation.recency.label == "Live now"
    assert row.attention_items[0].label == "Unattributed broker socket"


def _registry(*instances: HostRunnerInstance) -> HostRunnerInstancesStatus:
    return HostRunnerInstancesStatus(instances=list(instances), fetched_at_ms=AS_OF_MS)


def _instance(
    strategy_instance_id: str,
    run_id: str,
    run_dir: str,
    process: HostRunnerProcessStatus,
) -> HostRunnerInstance:
    return HostRunnerInstance(
        strategy_instance_id=strategy_instance_id,
        run_id=run_id,
        run_dir=run_dir,
        process=process,
    )
