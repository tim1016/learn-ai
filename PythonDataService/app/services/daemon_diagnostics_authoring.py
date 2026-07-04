"""Authoring helpers for daemon diagnostic reports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue

from app.schemas.broker_session import BrokerSessionRosterRow
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
    DiagnosticEvidence,
)
from app.schemas.live_runs import HostRunnerHealth, HostRunnerProcessStatus


@dataclass(frozen=True)
class _AuthoredCheck:
    check: DaemonDiagnosticCheck
    condition: DaemonDominantCondition | None = None


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


def _dominant_headline(
    global_checks: list[_AuthoredCheck],
    *,
    per_instance: list[DaemonInstanceDiagnostic],
    dominant: DaemonDominantCondition,
) -> DaemonDiagnosticHeadline:
    check = _first_matching_check(global_checks, dominant)
    if check is not None:
        return _headline_for(dominant, check)
    for report in per_instance:
        if report.dominant_condition == dominant:
            return report.headline
    return _headline_for(dominant, None)


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

