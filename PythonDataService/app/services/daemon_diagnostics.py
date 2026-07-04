"""Composed daemon diagnostics.

One data-plane builder authors daemon/control-plane diagnostic meaning. It
reads facts from the daemon, broker session mirror, and connectivity monitor,
but it does not re-run socket probes or re-classify broker clients.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, ValidationError

from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.daemon_connectivity_monitor import (
    DaemonConnectivityState,
)
from app.engine.live.daemon_connectivity_monitor import (
    get_monitor as get_daemon_connectivity_monitor,
)
from app.engine.live.daemon_transport import DaemonResult
from app.schemas.broker_session import (
    BrokerSessionMirrorSnapshot,
    BrokerSessionRosterRow,
)
from app.schemas.daemon_diagnostics import (
    DaemonDiagnosticAction,
    DaemonDiagnosticCategory,
    DaemonDiagnosticCheck,
    DaemonDiagnosticHeadline,
    DaemonDiagnosticReport,
    DaemonDiagnosticStatus,
    DaemonDominantCondition,
    DaemonInstanceDiagnostic,
    DaemonReportStatus,
    DaemonTransport,
    DiagnosticEvidence,
)
from app.schemas.live_runs import (
    HostRunnerHealth,
    HostRunnerInstance,
    HostRunnerInstancesStatus,
    HostRunnerProcessStatus,
)
from app.services.broker_session_mirror import (
    BrokerSessionMirrorService,
    get_broker_session_mirror_service,
)
from app.utils.timestamps import now_ms_utc

logger = logging.getLogger(__name__)

_DEFAULT_LEASE_THRESHOLD_MS = 5_000
_RENEW_LEASE_ENDPOINT = "/api/live-instances/daemon-health/renew-lease"


@dataclass(frozen=True)
class _AuthoredCheck:
    check: DaemonDiagnosticCheck
    condition: DaemonDominantCondition | None = None


class DaemonDiagnosticsService:
    """Compose fresh daemon diagnostics from existing authorities."""

    def __init__(
        self,
        *,
        mirror_service: BrokerSessionMirrorService | None = None,
    ) -> None:
        self._mirror_service = mirror_service or get_broker_session_mirror_service()

    async def report(
        self,
        *,
        strategy_instance_id: str | None = None,
    ) -> DaemonDiagnosticReport:
        settings = get_settings()
        daemon_url = (settings.live_runner_daemon_url or "").strip()
        fetched_at_ms = now_ms_utc()
        connectivity = _current_connectivity_state()

        if daemon_url:
            (daemon_result, health), (_registry_result, instances), mirror = await asyncio.gather(
                host_daemon_client.fetch_health(daemon_url),
                _fetch_instances(daemon_url),
                self._safe_mirror_snapshot(fetched_at_ms=fetched_at_ms),
            )
        else:
            daemon_result = DaemonResult(
                kind="UNREACHABLE",
                detail="host daemon URL is not configured",
                error_category="not_configured",
            )
            health = None
            instances = None
            mirror = await self._safe_mirror_snapshot(fetched_at_ms=fetched_at_ms)

        return build_daemon_diagnostic_report(
            daemon_result=daemon_result,
            health=health,
            instances=instances,
            mirror=mirror,
            connectivity=connectivity,
            fetched_at_ms=fetched_at_ms,
            strategy_instance_ids=[strategy_instance_id] if strategy_instance_id else None,
        )

    async def _safe_mirror_snapshot(self, *, fetched_at_ms: int) -> BrokerSessionMirrorSnapshot:
        try:
            return await self._mirror_service.snapshot()
        except Exception as exc:
            logger.warning("daemon diagnostics could not read broker session mirror: %s", exc)
            settings = get_settings()
            return BrokerSessionMirrorSnapshot(
                as_of_ms=fetched_at_ms,
                gateway_port=settings.port,
                observer_status="degraded",
                ghost_detection_status="unknown",
                rows=[],
                degradation_reasons=[f"broker session mirror unavailable: {exc}"],
            )


async def _fetch_instances(
    daemon_url: str,
) -> tuple[DaemonResult, HostRunnerInstancesStatus | None]:
    result, payload = await host_daemon_client.fetch_instances(daemon_url)
    if payload is None:
        return result, None
    try:
        return result, HostRunnerInstancesStatus.model_validate(payload)
    except ValidationError as exc:
        return DaemonResult.incompatible_contract(detail=str(exc)), None


def build_daemon_diagnostic_report(
    *,
    daemon_result: DaemonResult,
    health: HostRunnerHealth | None,
    instances: HostRunnerInstancesStatus | None,
    mirror: BrokerSessionMirrorSnapshot,
    connectivity: DaemonConnectivityState | None,
    fetched_at_ms: int,
    strategy_instance_ids: list[str] | None = None,
) -> DaemonDiagnosticReport:
    """Pure builder over typed inputs."""

    transport = _transport(daemon_result, connectivity)
    global_checks = _global_checks(
        transport=transport,
        daemon_result=daemon_result,
        health=health,
        instances=instances,
        mirror=mirror,
        connectivity=connectivity,
        now_ms=fetched_at_ms,
    )
    per_instance = [
        _instance_report(
            strategy_instance_id=sid,
            transport=transport,
            global_checks=global_checks,
            instances=instances,
            mirror=mirror,
        )
        for sid in _strategy_instance_ids(
            instances=instances,
            mirror=mirror,
            requested=strategy_instance_ids,
        )
    ]

    global_dominant = _dominant_condition(
        global_checks,
        healthy=DaemonDominantCondition.HEALTHY,
    )
    per_dominant = next(
        (
            report.dominant_condition
            for report in per_instance
            if report.dominant_condition != DaemonDominantCondition.INSTANCE_HEALTHY
        ),
        None,
    )
    dominant = (
        per_dominant
        if global_dominant == DaemonDominantCondition.HEALTHY and per_dominant is not None
        else global_dominant
    )
    dominant_check = _dominant_check(
        global_checks,
        per_instance=per_instance,
        dominant=dominant,
    )
    checks = [item.check for item in global_checks]
    return DaemonDiagnosticReport(
        overall_status=_aggregate_status(
            [*checks, *(check for item in per_instance for check in item.checks)]
        ),
        transport=transport,
        dominant_condition=dominant,
        headline=_headline_for(dominant, dominant_check),
        checks=checks,
        per_instance=per_instance,
        daemon_boot_id=_short_id(health.daemon_boot_id if health else None),
        fetched_at_ms=fetched_at_ms,
    )


def project_daemon_diagnostic_report(
    report: DaemonDiagnosticReport,
    strategy_instance_id: str,
) -> DaemonDiagnosticReport:
    """Project one instance subreport from an already-built report."""

    matches = [
        item
        for item in report.per_instance
        if item.strategy_instance_id == strategy_instance_id
    ]
    return report.model_copy(update={"per_instance": matches})


def redact_host_runner_health(health: HostRunnerHealth) -> HostRunnerHealth:
    """Browser-safe HostRunnerHealth projection."""

    return health.model_copy(
        update={
            "repo_root": _redact_path(health.repo_root),
            "live_runs_root": _redact_path(health.live_runs_root),
            "process": _redact_process(health.process),
            "orphan_candidates": [
                _redact_facts(candidate)
                for candidate in getattr(health, "orphan_candidates", [])
                if isinstance(candidate, dict)
            ],
        }
    )


def _redact_process(process: HostRunnerProcessStatus) -> HostRunnerProcessStatus:
    return process.model_copy(
        update={
            "command": [],
            "log_path": _redact_path(process.log_path),
        }
    )


def _global_checks(
    *,
    transport: DaemonTransport,
    daemon_result: DaemonResult,
    health: HostRunnerHealth | None,
    instances: HostRunnerInstancesStatus | None,
    mirror: BrokerSessionMirrorSnapshot,
    connectivity: DaemonConnectivityState | None,
    now_ms: int,
) -> list[_AuthoredCheck]:
    checks: list[_AuthoredCheck] = []
    checks.append(_reachability_check(transport, daemon_result, connectivity))
    checks.append(_auth_check(transport, daemon_result))
    checks.append(_contract_check(transport, daemon_result))
    checks.append(_code_freshness_check(transport, health))
    checks.append(_lease_check(transport, health, now_ms=now_ms))
    checks.append(_boot_check(transport, health, connectivity))
    checks.append(_registry_check(transport, health, instances))
    checks.append(_orphans_check(transport, health))
    checks.append(_socket_probe_check(transport, mirror))
    return checks


def _reachability_check(
    transport: DaemonTransport,
    daemon_result: DaemonResult,
    connectivity: DaemonConnectivityState | None,
) -> _AuthoredCheck:
    if transport == "CONNECTED":
        return _authored(
            "daemon.reachable",
            DaemonDiagnosticCategory.REACHABILITY,
            "pass",
            "Live engine is answering",
            "The data plane reached the host live engine.",
            evidence=_evidence(
                {
                    "transport": transport,
                    "last_success_ms": connectivity.last_success_ms if connectivity else None,
                }
            ),
        )
    if transport == "RETRYING":
        return _authored(
            "daemon.reachable",
            DaemonDiagnosticCategory.REACHABILITY,
            "warn",
            "Live engine is reconnecting",
            "The last probe failed, but the data plane is still retrying before declaring the engine down.",
            remediation="Wait for the retry window or refresh diagnostics if the incident is active.",
            evidence=_evidence(
                {
                    "transport": transport,
                    "attempt": connectivity.attempt if connectivity else None,
                    "detail": daemon_result.detail,
                }
            ),
            condition=DaemonDominantCondition.RETRYING,
        )
    if transport == "UNREACHABLE":
        return _authored(
            "daemon.reachable",
            DaemonDiagnosticCategory.REACHABILITY,
            "fail",
            "Live engine is not answering",
            "The data plane could not reach the host live engine.",
            remediation="Start the host live engine on this machine, then refresh diagnostics.",
            technical_detail=daemon_result.detail,
            evidence=_evidence(
                {
                    "transport": transport,
                    "error_category": daemon_result.error_category,
                    "detail": daemon_result.detail,
                }
            ),
            condition=DaemonDominantCondition.UNREACHABLE,
        )
    return _authored(
        "daemon.reachable",
        DaemonDiagnosticCategory.REACHABILITY,
        "pass",
        "Live engine answered the HTTP hop",
        "The host live engine responded; downstream checks explain what it returned.",
        evidence=_evidence({"transport": transport}),
    )


def _auth_check(transport: DaemonTransport, daemon_result: DaemonResult) -> _AuthoredCheck:
    if transport in {"UNREACHABLE", "RETRYING"}:
        return _skip(
            "daemon.auth",
            DaemonDiagnosticCategory.AUTH,
            "Daemon token could not be checked",
            "Authentication cannot be verified until the live engine answers.",
        )
    if daemon_result.kind == "AUTH_FAILED":
        return _authored(
            "daemon.auth",
            DaemonDiagnosticCategory.AUTH,
            "fail",
            "Daemon token was rejected",
            "The host live engine answered but rejected the data plane token.",
            remediation="Restart the data plane and host live engine so they share the same daemon token.",
            technical_detail=daemon_result.detail,
            evidence=_evidence(
                {
                    "response_status": daemon_result.response_status,
                    "error_category": daemon_result.error_category,
                }
            ),
            condition=DaemonDominantCondition.AUTH_FAILED,
        )
    return _authored(
        "daemon.auth",
        DaemonDiagnosticCategory.AUTH,
        "pass",
        "Daemon token accepted",
        "The host live engine accepted the data plane's authenticated probe.",
    )


def _contract_check(transport: DaemonTransport, daemon_result: DaemonResult) -> _AuthoredCheck:
    if transport in {"UNREACHABLE", "RETRYING", "AUTH_FAILED"}:
        return _skip(
            "daemon.contract",
            DaemonDiagnosticCategory.CONTRACT,
            "Daemon response shape could not be checked",
            "The contract check waits until reachability and authentication pass.",
        )
    if daemon_result.kind == "PROTOCOL_ERROR":
        return _authored(
            "daemon.contract",
            DaemonDiagnosticCategory.CONTRACT,
            "fail",
            "Live engine returned an unreadable response",
            "The daemon answered, but the data plane could not read the response as a valid health envelope.",
            remediation="Restart the host live engine. If this repeats, check the daemon logs for a mismatched or broken build.",
            technical_detail=daemon_result.detail,
            evidence=_evidence({"error_category": daemon_result.error_category}),
            condition=DaemonDominantCondition.MALFORMED_RESPONSE,
        )
    if daemon_result.kind == "INCOMPATIBLE_CONTRACT":
        return _authored(
            "daemon.contract",
            DaemonDiagnosticCategory.CONTRACT,
            "fail",
            "Live engine build does not match the app",
            "The daemon answered with a schema this data plane cannot consume.",
            remediation="Restart or redeploy the host live engine from the same commit as the data plane.",
            technical_detail=daemon_result.detail,
            evidence=_evidence({"error_category": daemon_result.error_category}),
            condition=DaemonDominantCondition.BUILD_MISMATCH,
        )
    return _authored(
        "daemon.contract",
        DaemonDiagnosticCategory.CONTRACT,
        "pass",
        "Live engine contract is readable",
        "The daemon health envelope matched the data plane contract.",
    )


def _code_freshness_check(
    transport: DaemonTransport,
    health: HostRunnerHealth | None,
) -> _AuthoredCheck:
    if transport != "CONNECTED" or health is None:
        return _skip(
            "daemon.code_freshness",
            DaemonDiagnosticCategory.CODE_FRESHNESS,
            "Code freshness could not be checked",
            "The daemon must return health facts before its running commit can be verified.",
        )
    facts = {
        "running_sha": _short_id(health.git_sha),
        "repo_head_sha": _short_id(health.repo_head_sha),
        "commits_behind": health.commits_behind,
    }
    if not health.git_sha or not health.repo_head_sha:
        return _authored(
            "daemon.code_freshness",
            DaemonDiagnosticCategory.CODE_FRESHNESS,
            "skip",
            "Live engine code freshness is unavailable",
            "The daemon did not report both running and on-disk git SHAs.",
            remediation="Restart to a current host live engine build if this daemon predates the freshness contract.",
            evidence=_evidence(facts),
        )
    if health.code_stale:
        behind = health.commits_behind
        count = f"{behind} commit{'s' if behind != 1 else ''}" if behind else "one or more commits"
        return _authored(
            "daemon.code_freshness",
            DaemonDiagnosticCategory.CODE_FRESHNESS,
            "warn",
            "Live engine is running stale code",
            f"The host live engine is behind the on-disk repo by {count}.",
            remediation=_host_restart_guidance(health),
            evidence=_evidence(facts),
            condition=DaemonDominantCondition.STALE_CODE,
        )
    return _authored(
        "daemon.code_freshness",
        DaemonDiagnosticCategory.CODE_FRESHNESS,
        "pass",
        "Live engine is running current code",
        "The running daemon commit matches the on-disk repo head.",
        evidence=_evidence(facts),
    )


def _lease_check(
    transport: DaemonTransport,
    health: HostRunnerHealth | None,
    *,
    now_ms: int,
) -> _AuthoredCheck:
    if transport != "CONNECTED" or health is None:
        return _skip(
            "daemon.control_plane_lease",
            DaemonDiagnosticCategory.LEASE,
            "Control-plane lease could not be checked",
            "The daemon must answer before its lease file can be evaluated.",
        )
    write_error = getattr(health, "lease_write_error", None)
    threshold_ms = getattr(health, "lease_threshold_ms", None) or _DEFAULT_LEASE_THRESHOLD_MS
    last_written = health.last_lease_written_at_ms
    facts = {
        "lease_status": health.lease_status,
        "last_lease_written_at_ms": last_written,
        "lease_threshold_ms": threshold_ms,
        "lease_write_error": write_error,
    }
    if write_error:
        return _authored(
            "daemon.control_plane_lease",
            DaemonDiagnosticCategory.LEASE,
            "fail",
            "Control-plane directory is not writable",
            "The daemon is reachable but could not write its control-plane lease.",
            remediation="Fix permissions or disk availability for the control-plane directory on the host, then refresh.",
            technical_detail=str(write_error),
            evidence=_evidence(facts, redacted=True),
            condition=DaemonDominantCondition.LEASE_UNWRITABLE,
        )
    if last_written is None or now_ms - last_written > threshold_ms:
        return _authored(
            "daemon.control_plane_lease",
            DaemonDiagnosticCategory.LEASE,
            "warn",
            "Control-plane lease is stale",
            "The daemon is reachable, but its lease timestamp is missing or older than the allowed threshold.",
            remediation="Renew the control-plane lease, then refresh diagnostics.",
            evidence=_evidence(facts),
            action=DaemonDiagnosticAction(
                action_id="renew_lease",
                kind="recovery_mutation",
                label="Renew control-plane lease",
                endpoint=_RENEW_LEASE_ENDPOINT,
                confirm=True,
            ),
            condition=DaemonDominantCondition.LEASE_STALE,
        )
    return _authored(
        "daemon.control_plane_lease",
        DaemonDiagnosticCategory.LEASE,
        "pass",
        "Control-plane lease is fresh",
        "The daemon recently wrote its control-plane lease.",
        evidence=_evidence(facts),
    )


def _boot_check(
    transport: DaemonTransport,
    health: HostRunnerHealth | None,
    connectivity: DaemonConnectivityState | None,
) -> _AuthoredCheck:
    if transport != "CONNECTED" or health is None:
        return _skip(
            "daemon.boot_identity",
            DaemonDiagnosticCategory.BOOT,
            "Daemon boot identity could not be checked",
            "Boot identity is available only after a readable daemon health response.",
        )
    observed = connectivity.observed_daemon_boot_id if connectivity else None
    current = health.daemon_boot_id
    facts = {
        "observed_daemon_boot_id": _short_id(observed),
        "current_daemon_boot_id": _short_id(current),
    }
    if observed and current and observed != current:
        return _authored(
            "daemon.boot_identity",
            DaemonDiagnosticCategory.BOOT,
            "warn",
            "Live engine boot identity changed",
            "The latest health response does not match the daemon boot id remembered by the connectivity monitor.",
            remediation="Refresh affected bot views so they stop trusting stale process history.",
            evidence=_evidence(facts),
            condition=DaemonDominantCondition.BOOT_CHANGED,
        )
    return _authored(
        "daemon.boot_identity",
        DaemonDiagnosticCategory.BOOT,
        "pass",
        "Live engine boot identity is stable",
        "No daemon restart was detected between the monitor and this diagnostic snapshot.",
        evidence=_evidence(facts),
    )


def _registry_check(
    transport: DaemonTransport,
    health: HostRunnerHealth | None,
    instances: HostRunnerInstancesStatus | None,
) -> _AuthoredCheck:
    if transport != "CONNECTED" or health is None:
        return _skip(
            "registry.availability",
            DaemonDiagnosticCategory.PROCESS_REGISTRY,
            "Process registry could not be checked",
            "The process registry is read only after daemon health is available.",
        )
    if instances is None:
        return _authored(
            "registry.availability",
            DaemonDiagnosticCategory.PROCESS_REGISTRY,
            "fail",
            "Process registry snapshot is unavailable",
            "The daemon health check passed, but the data plane could not read the managed-process registry.",
            remediation="Restart the host live engine if the registry endpoint keeps failing.",
            condition=DaemonDominantCondition.REGISTRY_SNAPSHOT_UNAVAILABLE,
        )
    return _authored(
        "registry.availability",
        DaemonDiagnosticCategory.PROCESS_REGISTRY,
        "pass",
        "Process registry is readable",
        f"The daemon reported {len(instances.instances)} managed bot process record(s).",
        evidence=_evidence({"instance_count": len(instances.instances)}),
    )


def _orphans_check(
    transport: DaemonTransport,
    health: HostRunnerHealth | None,
) -> _AuthoredCheck:
    if transport != "CONNECTED" or health is None:
        return _skip(
            "orphans.candidates",
            DaemonDiagnosticCategory.ORPHANS,
            "Orphan candidates could not be checked",
            "Orphan-candidate facts are available only after daemon health is readable.",
        )
    candidates = getattr(health, "orphan_candidates", []) or []
    count = len(candidates) if candidates else health.orphan_candidates_count
    facts = {
        "orphan_candidates_count": count,
        "orphan_candidates": [_redact_facts(item) for item in candidates if isinstance(item, dict)],
    }
    if count > 0:
        return _authored(
            "orphans.candidates",
            DaemonDiagnosticCategory.ORPHANS,
            "warn",
            "Orphan socket candidates need review",
            f"The host live engine found {count} socket candidate(s) that require operator review.",
            remediation="Open the broker session mirror and decide whether any orphaned socket should be adopted or cleared.",
            evidence=_evidence(facts, redacted=True),
            action=DaemonDiagnosticAction(
                action_id="open_session_mirror",
                kind="navigation",
                label="Open session mirror",
                deep_link="/broker/session-mirror",
            ),
            condition=DaemonDominantCondition.ORPHANS_PRESENT,
        )
    return _authored(
        "orphans.candidates",
        DaemonDiagnosticCategory.ORPHANS,
        "pass",
        "No daemon orphan candidates",
        "The daemon did not report boot-time orphan socket candidates.",
        evidence=_evidence({"orphan_candidates_count": 0}),
    )


def _socket_probe_check(
    transport: DaemonTransport,
    mirror: BrokerSessionMirrorSnapshot,
) -> _AuthoredCheck:
    if transport in {"UNREACHABLE", "RETRYING", "AUTH_FAILED"}:
        return _skip(
            "broker.socket_probe",
            DaemonDiagnosticCategory.SOCKET_PROBE,
            "Socket probe could not be checked",
            "Socket visibility depends on a reachable authenticated daemon.",
        )
    if mirror.ghost_detection_status != "available":
        return _authored(
            "broker.socket_probe",
            DaemonDiagnosticCategory.SOCKET_PROBE,
            "warn",
            "Broker socket visibility is unavailable",
            "The broker session mirror could not verify host sockets for this snapshot.",
            remediation="Check that the host daemon can run the configured socket probe.",
            evidence=_evidence(
                {
                    "observer_status": mirror.observer_status,
                    "ghost_detection_status": mirror.ghost_detection_status,
                    "degradation_reasons": mirror.degradation_reasons,
                }
            ),
            condition=DaemonDominantCondition.SOCKET_PROBE_UNAVAILABLE,
        )
    return _authored(
        "broker.socket_probe",
        DaemonDiagnosticCategory.SOCKET_PROBE,
        "pass",
        "Broker socket probe is available",
        "The broker session mirror can verify host sockets.",
        evidence=_evidence({"gateway_port": mirror.gateway_port}),
    )


def _instance_report(
    *,
    strategy_instance_id: str,
    transport: DaemonTransport,
    global_checks: list[_AuthoredCheck],
    instances: HostRunnerInstancesStatus | None,
    mirror: BrokerSessionMirrorSnapshot,
) -> DaemonInstanceDiagnostic:
    rows = [row for row in mirror.rows if row.strategy_instance_id == strategy_instance_id]
    codes = {code for row in rows for code in row.attention_codes}
    registry = _registry_by_sid(instances).get(strategy_instance_id) if instances is not None else None
    checks: list[_AuthoredCheck] = []

    blocking = _global_blocking_check(global_checks)
    if blocking is not None:
        checks.append(
            _authored(
                "instance.global_control_plane",
                DaemonDiagnosticCategory.REACHABILITY,
                blocking.check.status if blocking.check.status != "pass" else "fail",
                blocking.check.title,
                "This bot cannot be diagnosed below the daemon hop until the global control-plane fault clears.",
                remediation=blocking.check.remediation,
                scope_ref=strategy_instance_id,
                condition=blocking.condition,
            )
        )
        checks.append(_instance_skip("instance.process_state", "Process state was skipped", strategy_instance_id))
        checks.append(_instance_skip("instance.socket", "Socket state was skipped", strategy_instance_id))
        return _finish_instance(strategy_instance_id, checks)

    if instances is None:
        checks.append(
            _authored(
                "instance.registry_available",
                DaemonDiagnosticCategory.PROCESS_REGISTRY,
                "fail",
                "Bot process registry is unavailable",
                "The daemon is reachable, but its process registry could not be read for this bot.",
                remediation="Restart the host live engine if the registry endpoint keeps failing.",
                scope_ref=strategy_instance_id,
                condition=DaemonDominantCondition.REGISTRY_SNAPSHOT_UNAVAILABLE,
            )
        )
        return _finish_instance(strategy_instance_id, checks)

    if "REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE" in codes:
        checks.append(
            _authored(
                "instance.registry_amnesia",
                DaemonDiagnosticCategory.PROCESS_REGISTRY,
                "fail",
                "Registry forgot a bot that still has a socket",
                "The broker session mirror sees this bot's socket while the daemon registry does not claim a live process.",
                remediation="Open the session mirror before starting another copy; avoid double-starting a bot that may still be connected.",
                scope_ref=strategy_instance_id,
                evidence=_row_evidence(rows),
                condition=DaemonDominantCondition.REGISTRY_AMNESIA,
            )
        )
    else:
        checks.append(
            _authored(
                "instance.registry_amnesia",
                DaemonDiagnosticCategory.PROCESS_REGISTRY,
                "pass",
                "Registry and socket ownership do not contradict each other",
                "The mirror did not report a live socket that contradicts the process registry for this bot.",
                scope_ref=strategy_instance_id,
            )
        )

    if "ORPHANED_BOT_SOCKET" in codes or any(row.identity_type == "orphaned_bot_socket" for row in rows):
        checks.append(
            _authored(
                "instance.orphaned_socket",
                DaemonDiagnosticCategory.SOCKETS,
                "fail",
                "Bot socket is orphaned",
                "A socket associated with this bot remains at the Gateway without a live owning process.",
                remediation="Review the broker session mirror before restarting; an orphaned client can collide with a new client id.",
                scope_ref=strategy_instance_id,
                evidence=_row_evidence(rows),
                condition=DaemonDominantCondition.ORPHANED_SOCKET,
            )
        )

    checks.append(_process_check(strategy_instance_id, registry))
    checks.append(_socket_check(strategy_instance_id, registry, rows, codes, mirror))
    checks.append(_runtime_check(strategy_instance_id, registry, rows, codes))
    return _finish_instance(strategy_instance_id, checks)


def _process_check(
    strategy_instance_id: str,
    registry: HostRunnerInstance | None,
) -> _AuthoredCheck:
    if registry is None:
        return _authored(
            "instance.process_state",
            DaemonDiagnosticCategory.PROCESS,
            "fail",
            "Bot has not been started in the live engine",
            "The daemon registry has no managed process for this strategy instance.",
            remediation="Start this bot from its Bot Cockpit when you are ready to run it.",
            scope_ref=strategy_instance_id,
            condition=DaemonDominantCondition.NOT_STARTED,
        )
    process = registry.process
    facts = {
        "run_id": registry.run_id,
        "process_state": process.state.value,
        "pid": process.pid,
        "exit_code": process.exit_code,
        "exit_reason": getattr(process, "exit_reason", None),
    }
    if process.state == "exited":
        reason = getattr(process, "exit_reason", None) or "unknown"
        return _authored(
            "instance.process_state",
            DaemonDiagnosticCategory.PROCESS,
            "fail",
            "Bot process exited",
            f"The daemon-managed process exited with reason: {reason}.",
            remediation="Review the bot cockpit evidence before restarting this strategy instance.",
            scope_ref=strategy_instance_id,
            evidence=_evidence(facts),
            condition=DaemonDominantCondition.PROCESS_EXITED,
        )
    if process.state == "idle":
        return _authored(
            "instance.process_state",
            DaemonDiagnosticCategory.PROCESS,
            "fail",
            "Bot is idle in the live engine",
            "The daemon registry knows this bot, but it is not running a process.",
            remediation="Start this bot from its Bot Cockpit when you are ready to run it.",
            scope_ref=strategy_instance_id,
            evidence=_evidence(facts),
            condition=DaemonDominantCondition.NOT_STARTED,
        )
    if process.state == "stopping":
        return _authored(
            "instance.process_state",
            DaemonDiagnosticCategory.PROCESS,
            "warn",
            "Bot process is stopping",
            "The daemon has accepted a stop and is waiting for the child process to exit.",
            remediation="Wait for the stop to settle, then refresh diagnostics.",
            scope_ref=strategy_instance_id,
            evidence=_evidence(facts),
        )
    return _authored(
        "instance.process_state",
        DaemonDiagnosticCategory.PROCESS,
        "pass",
        "Bot process is running",
        "The daemon registry reports an active managed process for this bot.",
        scope_ref=strategy_instance_id,
        evidence=_evidence(facts),
    )


def _socket_check(
    strategy_instance_id: str,
    registry: HostRunnerInstance | None,
    rows: list[BrokerSessionRosterRow],
    codes: set[str],
    mirror: BrokerSessionMirrorSnapshot,
) -> _AuthoredCheck:
    if registry is None or registry.process.state not in {"running", "stopping"}:
        return _instance_skip(
            "instance.socket",
            "Socket state waits for a live daemon process",
            strategy_instance_id,
            category=DaemonDiagnosticCategory.SOCKETS,
        )
    if mirror.ghost_detection_status != "available":
        return _instance_skip(
            "instance.socket",
            "Socket state could not be verified",
            strategy_instance_id,
            category=DaemonDiagnosticCategory.SOCKETS,
            summary="The socket probe was unavailable, so the report cannot prove whether this bot has an IBKR socket.",
        )
    has_socket = any(row.socket_present for row in rows)
    if "STARTED_BUT_NO_SOCKET" in codes or not has_socket:
        return _authored(
            "instance.socket",
            DaemonDiagnosticCategory.SOCKETS,
            "fail",
            "Bot started but has no broker socket",
            "The daemon process is live, but the broker session mirror does not see an IBKR socket for it.",
            remediation="Investigate the bot's broker connection rather than restarting the daemon process first.",
            scope_ref=strategy_instance_id,
            evidence=_row_evidence(rows),
            condition=DaemonDominantCondition.NO_SOCKET,
        )
    return _authored(
        "instance.socket",
        DaemonDiagnosticCategory.SOCKETS,
        "pass",
        "Bot broker socket is visible",
        "The broker session mirror sees a current socket for this bot.",
        scope_ref=strategy_instance_id,
        evidence=_row_evidence(rows),
    )


def _runtime_check(
    strategy_instance_id: str,
    registry: HostRunnerInstance | None,
    rows: list[BrokerSessionRosterRow],
    codes: set[str],
) -> _AuthoredCheck:
    if registry is None or registry.process.state not in {"running", "stopping"}:
        return _instance_skip(
            "instance.runtime_fresh",
            "Runtime freshness waits for a live daemon process",
            strategy_instance_id,
            category=DaemonDiagnosticCategory.RUNTIME_FRESHNESS,
        )
    if "CLIENT_SIGNAL_STALE" in codes:
        return _authored(
            "instance.runtime_fresh",
            DaemonDiagnosticCategory.RUNTIME_FRESHNESS,
            "warn",
            "Bot runtime evidence is stale",
            "The broker session mirror reported that the bot's client signal has not updated recently.",
            remediation="Distrust a suspiciously connected-looking bot until its runtime evidence refreshes.",
            scope_ref=strategy_instance_id,
            evidence=_row_evidence(rows),
            condition=DaemonDominantCondition.RUNTIME_STALE,
        )
    return _authored(
        "instance.runtime_fresh",
        DaemonDiagnosticCategory.RUNTIME_FRESHNESS,
        "pass",
        "Bot runtime evidence is not stale",
        "No stale runtime signal was reported for this bot in the broker session mirror snapshot.",
        scope_ref=strategy_instance_id,
    )


def _finish_instance(
    strategy_instance_id: str,
    authored: list[_AuthoredCheck],
) -> DaemonInstanceDiagnostic:
    dominant = _dominant_condition(
        authored,
        healthy=DaemonDominantCondition.INSTANCE_HEALTHY,
    )
    check = _first_matching_check(authored, dominant)
    checks = [item.check for item in authored]
    return DaemonInstanceDiagnostic(
        strategy_instance_id=strategy_instance_id,
        overall_status=_aggregate_status(checks),
        dominant_condition=dominant,
        headline=_headline_for(dominant, check),
        checks=checks,
    )


def _global_blocking_check(checks: list[_AuthoredCheck]) -> _AuthoredCheck | None:
    for item in checks:
        if item.condition in {
            DaemonDominantCondition.UNREACHABLE,
            DaemonDominantCondition.AUTH_FAILED,
            DaemonDominantCondition.MALFORMED_RESPONSE,
            DaemonDominantCondition.BUILD_MISMATCH,
        }:
            return item
        if item.condition == DaemonDominantCondition.RETRYING:
            return item
    return None


def _transport(
    daemon_result: DaemonResult,
    connectivity: DaemonConnectivityState | None,
) -> DaemonTransport:
    if (
        connectivity is not None
        and connectivity.kind == "RETRYING"
        and daemon_result.kind == "UNREACHABLE"
    ):
        return "RETRYING"
    return daemon_result.kind


def _strategy_instance_ids(
    *,
    instances: HostRunnerInstancesStatus | None,
    mirror: BrokerSessionMirrorSnapshot,
    requested: list[str] | None,
) -> list[str]:
    ids: set[str] = set()
    if instances is not None:
        ids.update(instance.strategy_instance_id for instance in instances.instances if instance.strategy_instance_id)
    ids.update(row.strategy_instance_id for row in mirror.rows if row.strategy_instance_id)
    if requested:
        ids.update(item for item in requested if item)
    return sorted(ids)


def _registry_by_sid(
    instances: HostRunnerInstancesStatus | None,
) -> dict[str, HostRunnerInstance]:
    if instances is None:
        return {}
    return {
        instance.strategy_instance_id: instance
        for instance in instances.instances
        if instance.strategy_instance_id
    }


def _dominant_condition(
    authored: list[_AuthoredCheck],
    *,
    healthy: DaemonDominantCondition,
) -> DaemonDominantCondition:
    for status in ("fail", "warn"):
        for item in authored:
            if item.check.status == status and item.condition is not None:
                return item.condition
    return healthy


def _dominant_check(
    global_checks: list[_AuthoredCheck],
    *,
    per_instance: list[DaemonInstanceDiagnostic],
    dominant: DaemonDominantCondition,
) -> DaemonDiagnosticCheck | None:
    check = _first_matching_check(global_checks, dominant)
    if check is not None:
        return check
    for report in per_instance:
        if report.dominant_condition == dominant:
            return report.checks[0] if report.checks else None
    return None


def _first_matching_check(
    authored: list[_AuthoredCheck],
    dominant: DaemonDominantCondition,
) -> DaemonDiagnosticCheck | None:
    for item in authored:
        if item.condition == dominant:
            return item.check
    return None


def _aggregate_status(checks: list[DaemonDiagnosticCheck]) -> DaemonReportStatus:
    if any(check.status == "fail" for check in checks):
        return "fail"
    if any(check.status == "warn" for check in checks):
        return "warn"
    return "pass"


def _headline_for(
    condition: DaemonDominantCondition,
    check: DaemonDiagnosticCheck | None,
) -> DaemonDiagnosticHeadline:
    if condition in {
        DaemonDominantCondition.HEALTHY,
        DaemonDominantCondition.INSTANCE_HEALTHY,
    }:
        return DaemonDiagnosticHeadline(
            title="Live engine diagnostics are clear",
            summary="No daemon-control-plane fault was found in this snapshot.",
        )
    if check is not None:
        return DaemonDiagnosticHeadline(
            title=check.title,
            summary=check.summary,
            remediation=check.remediation,
        )
    return DaemonDiagnosticHeadline(
        title="Live engine needs attention",
        summary="The diagnostics report found a daemon-control-plane fault.",
    )


def _authored(
    check_id: str,
    category: DaemonDiagnosticCategory,
    status: DaemonDiagnosticStatus,
    title: str,
    summary: str,
    *,
    remediation: str | None = None,
    technical_detail: str | None = None,
    evidence: DiagnosticEvidence | None = None,
    action: DaemonDiagnosticAction | None = None,
    scope_ref: str | None = None,
    condition: DaemonDominantCondition | None = None,
) -> _AuthoredCheck:
    return _AuthoredCheck(
        check=DaemonDiagnosticCheck(
            check_id=check_id,
            category=category,
            status=status,
            title=title,
            summary=summary,
            remediation=remediation,
            technical_detail=technical_detail,
            evidence=evidence,
            action=action,
            scope="instance" if scope_ref else "global",
            scope_ref=scope_ref,
        ),
        condition=condition,
    )


def _skip(
    check_id: str,
    category: DaemonDiagnosticCategory,
    title: str,
    summary: str,
) -> _AuthoredCheck:
    return _authored(check_id, category, "skip", title, summary)


def _instance_skip(
    check_id: str,
    title: str,
    strategy_instance_id: str,
    *,
    category: DaemonDiagnosticCategory = DaemonDiagnosticCategory.PROCESS,
    summary: str | None = None,
) -> _AuthoredCheck:
    return _authored(
        check_id,
        category,
        "skip",
        title,
        summary or "This rung is skipped until the earlier daemon diagnostics pass.",
        scope_ref=strategy_instance_id,
    )


def _evidence(
    facts: Mapping[str, object],
    *,
    redacted: bool = False,
) -> DiagnosticEvidence:
    return DiagnosticEvidence(
        facts={key: value for key, value in _redact_facts(dict(facts)).items() if value is not None},
        redacted=redacted,
    )


def _row_evidence(rows: list[BrokerSessionRosterRow]) -> DiagnosticEvidence:
    return _evidence(
        {
            "rows": [
                {
                    "row_id": row.row_id,
                    "identity_type": row.identity_type,
                    "socket_present": row.socket_present,
                    "run_id": row.run_id,
                    "client_id": row.client_id,
                    "pid": row.pid,
                    "attention_codes": list(row.attention_codes),
                    "run_dir": row.run_dir,
                }
                for row in rows
            ]
        },
        redacted=True,
    )


def _redact_facts(facts: Mapping[str, object]) -> dict[str, JsonValue]:
    out: dict[str, JsonValue] = {}
    for key, value in facts.items():
        lowered = key.lower()
        if any(secret in lowered for secret in ("token", "secret", "password", "connection_string")):
            out[key] = "[redacted]"
            continue
        out[key] = _redact_value(value)
    return out


def _redact_value(value: object) -> JsonValue:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _looks_sensitive(value):
            return "[redacted]"
        if _looks_like_path(value):
            return _redact_path(value)
        return value
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return _redact_facts(value)
    return str(value)


def _looks_sensitive(value: str) -> bool:
    lowered = value.lower()
    return any(token in lowered for token in ("x-live-runner-token", "password=", "secret=", "token="))


def _looks_like_path(value: str) -> bool:
    return "/" in value or "\\" in value


def _redact_path(value: str | None) -> str | None:
    if not value:
        return value
    normalized = value.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part]
    if "PythonDataService" in parts:
        idx = parts.index("PythonDataService")
        return "/".join(parts[idx:])
    if "artifacts" in parts:
        idx = parts.index("artifacts")
        return "/".join(parts[idx:])
    if "learn-ai" in parts:
        idx = parts.index("learn-ai")
        return "/".join(parts[idx:])
    return Path(normalized).name or "[redacted-path]"


def _short_id(value: str | None) -> str | None:
    if not value:
        return None
    return value[:12] if len(value) > 12 else value


def _host_restart_guidance(health: HostRunnerHealth) -> str:
    platform = (getattr(health, "platform", None) or "").lower()
    supervisor = (getattr(health, "supervisor", None) or "").lower()
    if supervisor == "systemd" or platform == "linux":
        return "Restart the host live engine service with systemd, then refresh diagnostics."
    if supervisor == "launchd" or platform == "darwin":
        return "Restart the host live engine with launchd, then refresh diagnostics."
    if supervisor == "nssm" or platform == "windows":
        return "Restart the host live engine service from NSSM or Windows Services, then refresh diagnostics."
    return "Restart the host live engine on the host machine, then refresh diagnostics."


def _current_connectivity_state() -> DaemonConnectivityState | None:
    monitor = get_daemon_connectivity_monitor()
    return monitor.state if monitor is not None else None


def get_daemon_diagnostics_service() -> DaemonDiagnosticsService:
    return _SERVICE


_SERVICE = DaemonDiagnosticsService()
