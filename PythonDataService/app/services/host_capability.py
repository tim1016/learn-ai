"""Browser-safe access to the host-side IBKR capability bridge."""

from __future__ import annotations

from pathlib import Path

from pydantic import ValidationError

from app.broker.ibkr.config import get_settings
from app.engine.live import host_daemon_client
from app.engine.live.daemon_transport import DaemonResult
from app.schemas.live_runs import HostRunnerHealth


class HostCapabilityProbeError(RuntimeError):
    """The host capability probe did not return a usable health envelope."""

    def __init__(self, result: DaemonResult) -> None:
        super().__init__(result.detail or f"host capability bridge returned {result.kind}")
        self.result = result


class HostCapabilityPayloadError(RuntimeError):
    """The capability bridge returned an invalid renewal envelope."""


class HostCapabilityService:
    """Forward authenticated health and lease renewal without bot control."""

    async def health(self) -> HostRunnerHealth:
        settings = get_settings()
        result, health = await host_daemon_client.fetch_health(
            settings.live_runner_daemon_url
        )
        if health is None:
            raise HostCapabilityProbeError(result)
        return _redact_health(health)

    async def renew_lease(self) -> HostRunnerHealth:
        settings = get_settings()
        payload = await host_daemon_client.renew_control_plane_lease(
            settings.live_runner_daemon_url
        )
        try:
            return _redact_health(HostRunnerHealth.model_validate(payload))
        except ValidationError as exc:
            raise HostCapabilityPayloadError(
                "host capability bridge returned an invalid renew-lease envelope"
            ) from exc


def get_host_capability_service() -> HostCapabilityService:
    return _SERVICE


def _redact_health(health: HostRunnerHealth) -> HostRunnerHealth:
    return health.model_copy(
        update={
            "repo_root": _redact_path(health.repo_root),
            "live_runs_root": _redact_path(health.live_runs_root),
            "process": health.process.model_copy(
                update={"command": [], "log_path": _redact_path(health.process.log_path)}
            ),
            "orphan_candidates": [],
            "orphan_candidates_count": 0,
        }
    )


def _redact_path(value: str | None) -> str | None:
    if not value:
        return value
    normalized = value.replace("\\", "/")
    parts = [part for part in Path(normalized).parts if part not in {"/", "\\"}]
    for anchor in ("PythonDataService", "artifacts", "learn-ai"):
        if anchor in parts:
            return "/".join(parts[parts.index(anchor) :])
    return Path(normalized).name


_SERVICE = HostCapabilityService()
