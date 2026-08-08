"""Self-test for the data-plane → launcher path.

Walks the layers an operator would hit when ``/api/lean-sidecar/*``
fails with ``LauncherUnreachable``: env config, URL parseability,
shared-secret token resolution, a live HTTP probe of the launcher's
``/healthz``, and the launcher's exact pinned-image readiness. Modelled on
:mod:`app.broker.ibkr.diagnostics` so the operator UX is consistent
across the two host-process integrations.

Important non-side-effects:

* Does **not** send a real ``/launch`` request — only ``/healthz``,
  which is unauthenticated on the launcher side. The launcher performs
  a read-only ``podman image exists`` check for its own pin. The token-
  resolution check is local-only (env / on-disk file inspection); it
  does not exercise the auth path against the launcher.
* Does not write to disk. Safe to call as often as the UI wants.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.lean_sidecar.config import LEAN_IMAGE_REPO, PINNED_LEAN_IMAGE_DIGEST
from app.lean_sidecar.launcher_auth import read_launcher_token
from app.lean_sidecar.launcher_client import DEFAULT_LAUNCHER_URL

logger = logging.getLogger(__name__)


# Probe timeout — short. A healthy launcher returns ``/healthz`` in
# single-digit ms; anything past a couple of seconds means the host
# alias resolved to the wrong network or the launcher is hung.
_HEALTHZ_PROBE_TIMEOUT_S = 2.0


LauncherDiagnosticStatus = Literal["pass", "warn", "fail", "skip"]


class LauncherDiagnosticCheck(BaseModel):
    """One step in the launcher self-test.

    Shape intentionally mirrors :class:`app.broker.ibkr.models.DiagnosticCheck`
    so the frontend can render both with the same component. Kept as a
    separate class (rather than importing the broker model) so the
    LEAN-sidecar surface does not depend on the broker package.
    """

    model_config = ConfigDict(frozen=True)

    name: str = Field(..., description="Stable identifier (e.g. 'launcher_healthz').")
    label: str = Field(..., description="Human-readable check name.")
    status: LauncherDiagnosticStatus
    detail: str
    fix: str | None = None


class LauncherDiagnosticReport(BaseModel):
    """Aggregate result of the launcher self-test."""

    model_config = ConfigDict(frozen=True)

    overall_status: Literal["pass", "warn", "fail"]
    checks: list[LauncherDiagnosticCheck]
    fetched_at_ms: int


def _now_ms() -> int:
    return int(datetime.now(tz=UTC).timestamp() * 1000)


def _resolved_launcher_url() -> str:
    """Match ``launcher_client._launcher_url`` exactly so the
    diagnostics probe and the real call hit the same URL."""
    return os.environ.get("LEAN_LAUNCHER_URL", DEFAULT_LAUNCHER_URL).rstrip("/")


def _private_lan_host(url: str) -> str | None:
    """Return the configured private LAN IP hostname, if any.

    Private LAN IPs work only while the host remains on the same
    network/interface and are a recurring source of Podman reachability
    failures. Loopback is intentionally allowed for non-container dev
    where the data plane and launcher share a namespace.
    """
    try:
        hostname = urlparse(url).hostname
    except ValueError:
        return None
    if hostname is None:
        return None
    try:
        parsed = ip_address(hostname)
    except ValueError:
        return None
    if parsed.is_private and not parsed.is_loopback:
        return hostname
    return None


def _check_url_configured(url: str) -> LauncherDiagnosticCheck:
    env_value = os.environ.get("LEAN_LAUNCHER_URL")
    if env_value:
        private_host = _private_lan_host(url)
        if private_host is not None:
            return LauncherDiagnosticCheck(
                name="launcher_url",
                label="LEAN_LAUNCHER_URL",
                status="warn",
                detail=f"LEAN_LAUNCHER_URL={url} (from env; private LAN IP {private_host})",
                fix=(
                    "Use http://host.containers.internal:8090 for Windows/Linux Podman "
                    "instead of pinning a machine-specific LAN IP."
                ),
            )
        return LauncherDiagnosticCheck(
            name="launcher_url",
            label="LEAN_LAUNCHER_URL",
            status="pass",
            detail=f"LEAN_LAUNCHER_URL={url} (from env)",
        )
    return LauncherDiagnosticCheck(
        name="launcher_url",
        label="LEAN_LAUNCHER_URL",
        status="pass",
        detail=f"LEAN_LAUNCHER_URL unset; using built-in default {url}",
    )


def _check_url_parseable(url: str) -> tuple[LauncherDiagnosticCheck, str | None, int | None]:
    """Verify the configured URL parses to a host + port we can probe."""
    try:
        parsed = urlparse(url)
    except ValueError as exc:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"could not parse LEAN_LAUNCHER_URL={url!r}: {exc}",
                fix=(
                    "Set LEAN_LAUNCHER_URL to a well-formed http URL like "
                    "http://host.containers.internal:8090."
                ),
            ),
            None,
            None,
        )
    if parsed.scheme not in {"http", "https"}:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"scheme {parsed.scheme!r} is not http(s); url={url!r}",
                fix="Set LEAN_LAUNCHER_URL to an http:// or https:// URL.",
            ),
            None,
            None,
        )
    if not parsed.hostname:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"no hostname in LEAN_LAUNCHER_URL={url!r}",
                fix=(
                    "Set LEAN_LAUNCHER_URL to a full URL including hostname, "
                    "e.g. http://host.containers.internal:8090."
                ),
            ),
            None,
            None,
        )
    # ``ParseResult.port`` validates lazily — accessing it on a URL
    # whose port component isn't a valid integer (e.g. ``host:abc``)
    # raises ``ValueError``. Catch here so a malformed env value
    # surfaces as a structured fail row instead of a 500 from /diagnose.
    try:
        parsed_port = parsed.port
    except ValueError as exc:
        return (
            LauncherDiagnosticCheck(
                name="launcher_url_parseable",
                label="URL parse",
                status="fail",
                detail=f"invalid port in LEAN_LAUNCHER_URL={url!r}: {exc}",
                fix=(
                    "Set LEAN_LAUNCHER_URL to a numeric port, e.g. "
                    "http://host.containers.internal:8090."
                ),
            ),
            None,
            None,
        )
    port = parsed_port if parsed_port is not None else (443 if parsed.scheme == "https" else 80)
    return (
        LauncherDiagnosticCheck(
            name="launcher_url_parseable",
            label="URL parse",
            status="pass",
            detail=f"host={parsed.hostname} port={port} scheme={parsed.scheme}",
        ),
        parsed.hostname,
        port,
    )


def _check_token_resolved() -> LauncherDiagnosticCheck:
    """Local-only: env or shared bind-mount file. No launcher call.

    The launcher generates the token at startup and writes it to a
    file the data plane reads through a bind mount. If neither env
    nor file has a token, every ``/launch`` request will 401.
    """
    if os.environ.get("LEAN_LAUNCHER_TOKEN"):
        return LauncherDiagnosticCheck(
            name="launcher_token",
            label="X-Launcher-Token",
            status="pass",
            detail="resolved from LEAN_LAUNCHER_TOKEN env",
        )
    token = read_launcher_token()
    if token:
        return LauncherDiagnosticCheck(
            name="launcher_token",
            label="X-Launcher-Token",
            status="pass",
            detail="resolved from launcher token file on shared artifacts mount",
        )
    return LauncherDiagnosticCheck(
        name="launcher_token",
        label="X-Launcher-Token",
        status="fail",
        detail="no token in env and none on the shared artifacts mount",
        fix=(
            "Start the launcher — it generates a token at startup and "
            "writes it to the artifacts root the container also bind-"
            "mounts. Alternatively, set LEAN_LAUNCHER_TOKEN on both "
            "processes to a shared secret."
        ),
    )


def _image_check_skip(detail: str) -> LauncherDiagnosticCheck:
    return LauncherDiagnosticCheck(
        name="launcher_image",
        label="Pinned LEAN image",
        status="skip",
        detail=detail,
    )


def _check_image_readiness(payload: object) -> LauncherDiagnosticCheck:
    """Validate the structured image readiness returned by ``/healthz``."""
    if PINNED_LEAN_IMAGE_DIGEST is None:
        return LauncherDiagnosticCheck(
            name="launcher_image",
            label="Pinned LEAN image",
            status="fail",
            detail="The data-plane has no default LEAN image digest configured.",
            fix="Pin a validated LEAN image digest in app/lean_sidecar/config.py, then restart the launcher.",
        )
    expected_reference = f"{LEAN_IMAGE_REPO}@{PINNED_LEAN_IMAGE_DIGEST}"
    if not isinstance(payload, dict):
        return LauncherDiagnosticCheck(
            name="launcher_image",
            label="Pinned LEAN image",
            status="fail",
            detail="Launcher /healthz did not return the required image-readiness object.",
            fix="Restart the launcher from the current learn-ai source so it reports pinned-image readiness.",
        )
    image = payload.get("image")
    if not isinstance(image, dict):
        return LauncherDiagnosticCheck(
            name="launcher_image",
            label="Pinned LEAN image",
            status="fail",
            detail="Launcher /healthz does not report pinned-image readiness.",
            fix="Restart the launcher from the current learn-ai source so it reports pinned-image readiness.",
        )
    reference = image.get("reference")
    available = image.get("available")
    failure_reason = image.get("failure_reason")
    detail = image.get("detail")
    if reference != expected_reference:
        return LauncherDiagnosticCheck(
            name="launcher_image",
            label="Pinned LEAN image",
            status="fail",
            detail=(
                f"Launcher expects {reference!r}, but the data plane requires "
                f"{expected_reference!r}."
            ),
            fix="Restart the launcher so it loads the same pinned-image configuration as the data plane.",
        )
    if available is not True:
        if failure_reason != "missing":
            return LauncherDiagnosticCheck(
                name="launcher_image",
                label="Pinned LEAN image",
                status="fail",
                detail=detail if isinstance(detail, str) else "Launcher could not verify the pinned LEAN image.",
                fix="Resolve the launcher or Podman readiness-check failure, then retry the diagnostic.",
            )
        return LauncherDiagnosticCheck(
            name="launcher_image",
            label="Pinned LEAN image",
            status="fail",
            detail=detail if isinstance(detail, str) else "Launcher could not verify the pinned LEAN image.",
            fix=(
                "Build the configured local LEAN derivative, pin its resulting digest in "
                "app/lean_sidecar/config.py, then restart the launcher."
            ),
        )
    return LauncherDiagnosticCheck(
        name="launcher_image",
        label="Pinned LEAN image",
        status="pass",
        detail=detail if isinstance(detail, str) else "Pinned LEAN image is available locally.",
    )


async def _check_launcher_health(url: str) -> list[LauncherDiagnosticCheck]:
    target = f"{url}/healthz"
    try:
        async with httpx.AsyncClient(timeout=_HEALTHZ_PROBE_TIMEOUT_S) as client:
            response = await client.get(target)
    except httpx.ConnectError as exc:
        return [
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label=f"GET {target}",
                status="fail",
                detail=f"connect failed: {exc}",
                fix=(
                    "Confirm the launcher process is running and listening on "
                    "the configured port. From the host: "
                    "``uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090``. "
                    "Loopback-only binds (127.0.0.1) are unreachable from the container."
                ),
            ),
            _image_check_skip("skipped because the launcher is not reachable"),
        ]
    except httpx.TimeoutException:
        return [
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label=f"GET {target}",
                status="fail",
                detail=f"timeout after {_HEALTHZ_PROBE_TIMEOUT_S:.1f}s",
                fix=(
                    "The hostname resolved but no TCP response arrived. Check "
                    "that host.containers.internal points at the host's bridge "
                    "address and that the host firewall allows inbound from "
                    "the container bridge."
                ),
            ),
            _image_check_skip("skipped because the launcher did not respond"),
        ]
    except httpx.HTTPError as exc:
        return [
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label=f"GET {target}",
                status="fail",
                detail=f"{type(exc).__name__}: {exc}",
            ),
            _image_check_skip("skipped because the launcher health probe failed"),
        ]
    if response.status_code != 200:
        return [
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label=f"GET {target}",
                status="fail",
                detail=f"unexpected status {response.status_code}: {response.text[:200]}",
                fix=(
                    "The launcher is reachable but /healthz did not return 200 — "
                    "check the launcher's logs for startup errors."
                ),
            ),
            _image_check_skip("skipped because the launcher health endpoint failed"),
        ]
    try:
        payload = response.json()
    except ValueError:
        return [
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label=f"GET {target}",
                status="fail",
                detail="200 OK but /healthz returned a non-JSON body.",
                fix="Restart the launcher from the current learn-ai source.",
            ),
            _image_check_skip("skipped because the launcher health response is invalid"),
        ]
    return [
        LauncherDiagnosticCheck(
            name="launcher_healthz",
            label=f"GET {target}",
            status="pass",
            detail=f"200 OK in <{_HEALTHZ_PROBE_TIMEOUT_S:.1f}s",
        ),
        _check_image_readiness(payload),
    ]


def _aggregate_status(checks: list[LauncherDiagnosticCheck]) -> Literal["pass", "warn", "fail"]:
    if any(c.status == "fail" for c in checks):
        return "fail"
    if any(c.status == "warn" for c in checks):
        return "warn"
    return "pass"


async def run_launcher_diagnostics() -> LauncherDiagnosticReport:
    """Run every check and return a :class:`LauncherDiagnosticReport`.

    Each check captures its own exception so the report always renders.
    Synchronous checks run inline; the only async check is the
    ``/healthz`` probe and its local pinned-image readiness result,
    bounded by ``_HEALTHZ_PROBE_TIMEOUT_S``.
    """
    url = _resolved_launcher_url()
    checks: list[LauncherDiagnosticCheck] = []
    checks.append(_check_url_configured(url))
    parse_check, host, _port = _check_url_parseable(url)
    checks.append(parse_check)
    checks.append(_check_token_resolved())
    if host is None:
        checks.append(
            LauncherDiagnosticCheck(
                name="launcher_healthz",
                label="GET /healthz",
                status="skip",
                detail="skipped because LEAN_LAUNCHER_URL did not parse to a usable host",
            )
        )
        checks.append(_image_check_skip("skipped because LEAN_LAUNCHER_URL did not parse to a usable host"))
    else:
        checks.extend(await _check_launcher_health(url))
    return LauncherDiagnosticReport(
        overall_status=_aggregate_status(checks),
        checks=checks,
        fetched_at_ms=_now_ms(),
    )


__all__ = [
    "LauncherDiagnosticCheck",
    "LauncherDiagnosticReport",
    "LauncherDiagnosticStatus",
    "run_launcher_diagnostics",
]
