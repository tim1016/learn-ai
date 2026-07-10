from __future__ import annotations

from app.engine.live.daemon_transport import DaemonResult
from app.schemas.broker_session import (
    BrokerSessionMirrorSnapshot,
    BrokerSessionMirrorSummary,
    BrokerSessionRosterRow,
)
from app.schemas.daemon_diagnostics import DaemonDominantCondition
from app.schemas.live_runs import (
    HostRunnerHealth,
    HostRunnerInstance,
    HostRunnerInstancesStatus,
    HostRunnerProcessState,
    HostRunnerProcessStatus,
)
from app.services.daemon_diagnostics import (
    _fetch_instances,
    build_daemon_diagnostic_report,
    redact_host_runner_health,
)
from app.services.fleet_daemon_snapshot_provider import FleetDaemonObservation

NOW_MS = 1_700_000_000_000


async def test_stale_fleet_payload_is_not_reused_as_current_registry() -> None:
    result = DaemonResult(
        kind="UNREACHABLE",
        detail="connection refused",
        error_category="connect_error",
    )
    observation = FleetDaemonObservation(
        result=result,
        payload={"instances": [], "fetched_at_ms": NOW_MS - 1_000},
        processes_by_id={},
        source_fetched_at_ms=NOW_MS - 1_000,
        observed_at_ms=NOW_MS,
    )

    returned_result, instances = await _fetch_instances(
        "http://daemon",
        fleet_observation=observation,
    )

    assert returned_result == result
    assert instances is None


def _process(**overrides: object) -> HostRunnerProcessStatus:
    payload = {
        "state": HostRunnerProcessState.idle,
        "run_id": None,
        "strategy_instance_id": None,
        "pid": None,
        "started_at_ms": None,
        "ended_at_ms": None,
        "exit_code": None,
        "exit_reason": None,
        "command": [],
        "log_path": None,
        "message": None,
    }
    payload.update(overrides)
    return HostRunnerProcessStatus.model_validate(payload)


def _health(**overrides: object) -> HostRunnerHealth:
    payload = {
        "ok": True,
        "repo_root": "/Users/inkant/learn-ai",
        "live_runs_root": "/Users/inkant/learn-ai/PythonDataService/artifacts/live_runs",
        "fetched_at_ms": NOW_MS,
        "process": _process(),
        "git_sha": "abc123456789",
        "repo_head_sha": "abc123456789",
        "code_stale": False,
        "commits_behind": None,
        "daemon_boot_id": "boot-123456789",
        "lease_status": "CONNECTED",
        "last_lease_written_at_ms": NOW_MS - 100,
        "lease_threshold_ms": 5_000,
        "lease_write_error": None,
        "orphan_candidates_count": 0,
        "orphan_candidates": [],
        "platform": "linux",
        "supervisor": "systemd",
    }
    payload.update(overrides)
    return HostRunnerHealth.model_validate(payload)


def _mirror(*, rows: list[BrokerSessionRosterRow] | None = None) -> BrokerSessionMirrorSnapshot:
    rows = rows or []
    return BrokerSessionMirrorSnapshot(
        as_of_ms=NOW_MS,
        gateway_port=4002,
        observer_status="online",
        ghost_detection_status="available",
        rows=rows,
        summary=BrokerSessionMirrorSummary(
            current=sum(1 for row in rows if row.recency == "current"),
            past=0,
            unknown=sum(1 for row in rows if row.recency == "unknown"),
            attention=sum(1 for row in rows if row.attention_codes),
        ),
    )


def test_unreachable_report_is_http_body_ready_and_skips_downstream() -> None:
    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult(
            kind="UNREACHABLE",
            detail="connection refused",
            error_category="connect_error",
        ),
        health=None,
        instances=None,
        mirror=_mirror(),
        connectivity=None,
        fetched_at_ms=NOW_MS,
    )

    assert report.transport == "UNREACHABLE"
    assert report.overall_status == "fail"
    assert report.dominant_condition == DaemonDominantCondition.UNREACHABLE
    assert report.headline.title == "Live engine is not answering"
    assert {check.check_id: check.status for check in report.checks}[
        "daemon.auth"
    ] == "skip"


def test_stale_lease_is_distinct_actuatable_recovery_action() -> None:
    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult.connected(daemon_boot_id="boot-123456789"),
        health=_health(last_lease_written_at_ms=NOW_MS - 6_000),
        instances=HostRunnerInstancesStatus(instances=[], fetched_at_ms=NOW_MS),
        mirror=_mirror(),
        connectivity=None,
        fetched_at_ms=NOW_MS,
    )

    lease = next(check for check in report.checks if check.check_id == "daemon.control_plane_lease")
    assert report.dominant_condition == DaemonDominantCondition.LEASE_STALE
    assert lease.status == "warn"
    assert lease.action is not None
    assert lease.action.kind == "recovery_mutation"
    assert lease.action.action_id == "renew_lease"
    assert lease.action.endpoint == "/api/live-instances/daemon-health/renew-lease"


def test_lease_write_error_is_unwritable_not_stale_and_has_no_button() -> None:
    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult.connected(daemon_boot_id="boot-123456789"),
        health=_health(lease_write_error="permission denied"),
        instances=HostRunnerInstancesStatus(instances=[], fetched_at_ms=NOW_MS),
        mirror=_mirror(),
        connectivity=None,
        fetched_at_ms=NOW_MS,
    )

    lease = next(check for check in report.checks if check.check_id == "daemon.control_plane_lease")
    assert report.dominant_condition == DaemonDominantCondition.LEASE_UNWRITABLE
    assert lease.status == "fail"
    assert lease.action is None


def test_zero_lease_threshold_is_not_replaced_with_default() -> None:
    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult.connected(daemon_boot_id="boot-123456789"),
        health=_health(
            last_lease_written_at_ms=NOW_MS - 1,
            lease_threshold_ms=0,
        ),
        instances=HostRunnerInstancesStatus(instances=[], fetched_at_ms=NOW_MS),
        mirror=_mirror(),
        connectivity=None,
        fetched_at_ms=NOW_MS,
    )

    lease = next(check for check in report.checks if check.check_id == "daemon.control_plane_lease")
    assert report.dominant_condition == DaemonDominantCondition.LEASE_STALE
    assert lease.evidence is not None
    assert lease.evidence.facts["lease_threshold_ms"] == 0


def test_registry_amnesia_wins_before_never_started() -> None:
    row = BrokerSessionRosterRow(
        row_id="socket:123:51000:4002:0",
        identity_type="bot",
        recency="current",
        socket_present=True,
        strategy_instance_id="TSLA_DEMO",
        run_id="run-tsla",
        as_of_ms=NOW_MS,
        attention_codes=["REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE"],
    )

    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult.connected(daemon_boot_id="boot-123456789"),
        health=_health(),
        instances=HostRunnerInstancesStatus(instances=[], fetched_at_ms=NOW_MS),
        mirror=_mirror(rows=[row]),
        connectivity=None,
        fetched_at_ms=NOW_MS,
    )

    instance = report.per_instance[0]
    assert instance.strategy_instance_id == "TSLA_DEMO"
    assert instance.dominant_condition == DaemonDominantCondition.REGISTRY_AMNESIA
    assert instance.headline.title == "Registry forgot a bot that still has a socket"


def test_process_exited_uses_daemon_authored_exit_reason() -> None:
    instances = HostRunnerInstancesStatus(
        fetched_at_ms=NOW_MS,
        instances=[
            HostRunnerInstance(
                strategy_instance_id="SPY_DEMO",
                run_id="run-spy",
                run_dir="/Users/inkant/learn-ai/PythonDataService/artifacts/live_runs/run-spy",
                process=_process(
                    state=HostRunnerProcessState.exited,
                    run_id="run-spy",
                    strategy_instance_id="SPY_DEMO",
                    pid=123,
                    ended_at_ms=NOW_MS - 1_000,
                    exit_code=1,
                    exit_reason="fatal_halt",
                ),
            )
        ],
    )

    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult.connected(daemon_boot_id="boot-123456789"),
        health=_health(),
        instances=instances,
        mirror=_mirror(),
        connectivity=None,
        fetched_at_ms=NOW_MS,
    )

    instance = report.per_instance[0]
    assert instance.dominant_condition == DaemonDominantCondition.PROCESS_EXITED
    assert "fatal_halt" in instance.headline.summary
    assert report.dominant_condition == DaemonDominantCondition.PROCESS_EXITED
    assert report.headline.title == "Bot process exited"


def test_run_directory_invisible_wins_before_process_socket_runtime() -> None:
    instances = HostRunnerInstancesStatus(
        fetched_at_ms=NOW_MS,
        instances=[
            HostRunnerInstance(
                strategy_instance_id="SPY_DEMO",
                run_id="run-spy",
                run_dir="/host/artifacts/live_runs/run-spy",
                process=_process(
                    state=HostRunnerProcessState.running,
                    run_id="run-spy",
                    strategy_instance_id="SPY_DEMO",
                    pid=123,
                    started_at_ms=NOW_MS - 1_000,
                ),
            )
        ],
    )

    report = build_daemon_diagnostic_report(
        daemon_result=DaemonResult.connected(daemon_boot_id="boot-123456789"),
        health=_health(),
        instances=instances,
        mirror=_mirror(),
        connectivity=None,
        fetched_at_ms=NOW_MS,
        run_dir_visibility={"run-spy": False},
    )

    instance = report.per_instance[0]
    assert instance.dominant_condition == DaemonDominantCondition.RUN_DIR_INVISIBLE
    assert instance.headline.title == "Bot run directory is not visible"
    assert [check.check_id for check in instance.checks][:3] == [
        "instance.registry_amnesia",
        "instance.run_directory",
        "instance.process_state",
    ]


def test_health_redaction_removes_paths_and_argv_before_browser_serialisation() -> None:
    raw = _health(
        process=_process(
            state=HostRunnerProcessState.running,
            command=["/Users/inkant/learn-ai/PythonDataService/.venv/bin/python", "-m", "app.engine.live.run"],
            log_path="/Users/inkant/learn-ai/PythonDataService/artifacts/live_runs/run-spy/live.log",
        ),
        orphan_candidates=[
            {
                "run_id": "run-spy",
                "run_dir": "/Users/inkant/learn-ai/PythonDataService/artifacts/live_runs/run-spy",
                "token": "secret-token",
            }
        ],
    )

    redacted = redact_host_runner_health(raw)
    body = redacted.model_dump(mode="json")

    assert body["repo_root"] == "learn-ai"
    assert body["live_runs_root"] == "PythonDataService/artifacts/live_runs"
    assert body["process"]["command"] == []
    assert body["process"]["log_path"] == "PythonDataService/artifacts/live_runs/run-spy/live.log"
    assert body["orphan_candidates"][0]["run_dir"] == "PythonDataService/artifacts/live_runs/run-spy"
    assert body["orphan_candidates"][0]["token"] == "[redacted]"
