"""HTTP client: main service -> host live-run daemon (ADR 0004).

The host daemon owns the subprocesses and is therefore the sole authority for
the live ``strategy_instance_id -> run_id`` binding. The instance-status
endpoint in ``polygon-data-service`` cannot prove liveness from artifacts, so it
queries the daemon here.

Read path (PRD #619-C2): every GET returns ``(DaemonResult, dict | None)``.
The ``DaemonResult`` classifies the transport outcome (CONNECTED / UNREACHABLE
/ AUTH_FAILED / PROTOCOL_ERROR / INCOMPATIBLE_CONTRACT); the dict carries the
parsed body iff ``result.kind == "CONNECTED"``. Callers that only need
fail-closed semantics keep checking ``payload is None`` — the typed result is
additive context for log/UX surfacing and for the connectivity monitor
(``fetch_health``) to fold into a running state.

Write path: POST helpers continue to raise ``HostDaemonError`` for now. The
typed mutation classification (619-C5) is a separate refactor that ties
``outcome_ambiguous=True`` to operator-surface ``OUTCOME_UNKNOWN``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Literal

import httpx
from pydantic import ValidationError

from app.engine.live.daemon_auth import TOKEN_HEADER, read_daemon_token
from app.engine.live.daemon_transport import DaemonResult
from app.schemas.broker_session import GatewaySocketsSnapshot
from app.schemas.live_runs import HostRunnerHealth

logger = logging.getLogger(__name__)

_TIMEOUT = httpx.Timeout(2.0)
# The host-daemon socket route shells out to lsof (5s cap) and may then inspect
# owning processes. Keep this read bounded, but do not force false degradation
# with the generic low-latency health timeout.
_SOCKET_PROBE_TIMEOUT = httpx.Timeout(10.0)
# The roll-call falls back to a per-bot /instances/{id}/process probe for idle
# candidates the daemon's bulk snapshot omits. Under concurrent load the single-
# loop daemon (managing running bots + their fill/order streams) can exceed the 2s
# health timeout, which would silently drop an otherwise-ready member from the roll
# call at its slot. A startability probe can afford to wait longer than a liveness
# GET; keep it bounded so a genuinely wedged daemon still surfaces.
_INSTANCE_PROBE_TIMEOUT = httpx.Timeout(10.0)
# Starting a run and ensuring its account Clerk can both wait behind
# the host daemon's broker reconciliation work. They are admission operations,
# not low-latency liveness reads: keep a bounded deadline so a busy but healthy
# daemon does not turn a safe launch into a false ``connect_timeout``.
_START_ADMISSION_TIMEOUT = httpx.Timeout(10.0)
# Deploy runs git + file hashing on the host; allow more headroom than the
# liveness GETs, but still bounded so a wedged daemon surfaces as 503.
_DEPLOY_TIMEOUT = httpx.Timeout(15.0)
# Clerk release can legitimately wait two seconds for TERM and another two
# seconds after KILL. Let the daemon author the bounded-shutdown outcome.
_CLERK_RELEASE_TIMEOUT = httpx.Timeout(6.0)
# Emergency flatten round-trips to the broker synchronously (the daemon caps the
# CLI at 120s); give the HTTP hop a little more so the daemon's own timeout wins.
_FLATTEN_TIMEOUT = httpx.Timeout(130.0)


class HostDaemonCircuitBreaker:
    """Transport-only bounded backoff for repeated unreachable results.

    Operator meaning remains owned by the diagnostic projection. This class
    only decides whether another daemon exchange is allowed yet (ADR-0028).
    """

    def __init__(
        self,
        *,
        initial_backoff_seconds: float,
        max_backoff_seconds: float,
    ) -> None:
        if initial_backoff_seconds <= 0:
            raise ValueError("initial_backoff_seconds must be positive")
        if max_backoff_seconds < initial_backoff_seconds:
            raise ValueError(
                "max_backoff_seconds must be at least initial_backoff_seconds"
            )
        self._initial_backoff_seconds = initial_backoff_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._consecutive_failures = 0
        self._open_until = 0.0

    @property
    def open_until(self) -> float:
        return self._open_until

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def is_open(self, now: float) -> bool:
        return now < self._open_until

    def observe(self, result: DaemonResult, *, now: float) -> None:
        if result.kind != "UNREACHABLE":
            self._consecutive_failures = 0
            self._open_until = 0.0
            return
        self._consecutive_failures += 1
        exponent = min(self._consecutive_failures - 1, 30)
        delay = min(
            self._max_backoff_seconds,
            self._initial_backoff_seconds * (2**exponent),
        )
        self._open_until = now + delay


def _auth_headers() -> dict[str, str]:
    """Attach ``X-Live-Runner-Token`` to every daemon request (ADR 0007).

    Resolves the token from ``LIVE_RUNNER_DAEMON_TOKEN`` env (operator override)
    or, when env is unset, from the daemon's token file shared via the artifacts
    bind mount. If no token is resolvable (daemon not started yet, env unset, no
    file on the mount) we send no header — the daemon then 401s and the caller
    surfaces that as it would any other error (deploy: re-raised; GETs:
    ``DaemonResult.auth_failed`` + ``payload=None``).
    """
    # Lazy import keeps the engine/live client from pulling broker config into
    # module import order and keeps test monkeypatching of settings effective.
    from app.broker.ibkr.config import get_settings

    artifacts_root = Path(get_settings().live_runs_root).parent
    token = read_daemon_token(artifacts_root)
    return {TOKEN_HEADER: token} if token else {}


HostDaemonErrorDetail = str | dict[str, object]


class HostDaemonError(Exception):
    """A daemon call that must surface its status to the caller.

    Carries the daemon's HTTP status and detail verbatim so the
    data-plane endpoint can re-raise (dirty-tree 409, missing-input
    400, git 503). An unambiguous connection failure (ConnectError /
    ConnectTimeout / PoolTimeout — no bytes left the wire) maps to 503
    "daemon unreachable".

    For *ambiguous* transport failures (PRD #619-C5 — ReadTimeout /
    WriteTimeout / RemoteProtocolError after the request was partly or
    fully sent) the mutation paths raise :class:`HostDaemonOutcomeUnknownError`
    instead; the data-plane endpoint surfaces it as a typed 409
    response so the operator can refresh state before retrying.
    """

    def __init__(self, status_code: int, detail: HostDaemonErrorDetail) -> None:
        super().__init__(
            detail if isinstance(detail, str) else detail.get("message", str(detail))
        )
        self.status_code = status_code
        self.detail = detail


class HostDaemonOutcomeUnknownError(Exception):
    """A mutation POST whose transport outcome could not be proven
    (PRD #619-C5).

    Raised by the typed POST helpers when ``DaemonResult.kind ==
    UNREACHABLE and DaemonResult.outcome_ambiguous is True`` —
    i.e., a ``ReadTimeout`` / ``WriteTimeout`` / ``RemoteProtocolError``
    after request bytes may have reached the daemon. The data-plane
    endpoint surfaces this as a typed 409 with
    ``reason_code='OUTCOME_UNKNOWN'`` so the operator refreshes state
    before retrying (the mutation may or may not have executed). The
    full durable mutation_attempt record + Reconcile action land in
    619-D.

    Distinct from :class:`HostDaemonError` so the four mutation
    endpoints can branch with a typed ``except`` rather than inspect a
    status code.
    """

    def __init__(
        self,
        *,
        error_category: str,
        detail: str | None,
    ) -> None:
        super().__init__(detail or error_category)
        self.error_category = error_category
        self.detail = detail


# ---------------------------------------------------------------------------
# Write path (HostDaemonError — to be typed in 619-C5).
# ---------------------------------------------------------------------------


async def deploy(base_url: str, payload: dict) -> dict:
    """POST /deploy to the daemon and return the parsed body.

    Raises :class:`HostDaemonOutcomeUnknownError` when the transport
    fails ambiguously (ReadTimeout / WriteTimeout / RemoteProtocolError
    after bytes may have reached the daemon); the data-plane endpoint
    surfaces this as a typed 409 + ``OUTCOME_UNKNOWN`` (PRD #619-C5).
    Raises :class:`HostDaemonError` on any non-2xx response or
    unambiguous connection failure (status + detail propagated, 503 for
    pre-send connection failures, 502 for malformed JSON).
    """
    return await _post_action(f"{base_url.rstrip('/')}/deploy", payload, timeout=_DEPLOY_TIMEOUT)


async def start_run(base_url: str, run_id: str, payload: dict) -> dict:
    """POST /runs/{run_id}/start to the daemon and return the parsed body.

    Mirrors :func:`deploy`: domain failures propagate via
    :class:`HostDaemonError`, transport-ambiguous failures via
    :class:`HostDaemonOutcomeUnknownError`. Browsers must never hold the
    daemon's shared secret, so the UI routes Start through the data
    plane (which forwards the token from the artifacts bind mount)
    rather than calling the daemon directly (ADR 0007).
    """
    return await _post_action(
        f"{base_url.rstrip('/')}/runs/{run_id}/start",
        payload,
        timeout=_START_ADMISSION_TIMEOUT,
    )


async def stop_run(base_url: str, run_id: str, payload: dict) -> dict:
    """POST /runs/{run_id}/stop. Same contract as :func:`start_run`."""
    return await _post_action(f"{base_url.rstrip('/')}/runs/{run_id}/stop", payload)


async def emergency_flatten_run(base_url: str, run_id: str, payload: dict) -> dict:
    """POST /runs/{run_id}/emergency-flatten.

    Same contract as :func:`start_run` but with a longer timeout — the
    daemon round-trips to the broker synchronously. A read-timeout here
    is far more likely than for the lighter mutations, and the
    consequence (broker positions in an unknown post-mutation state) is
    the highest-stakes ambiguous-outcome case 619-C5 surfaces.
    """
    return await _post_action(
        f"{base_url.rstrip('/')}/runs/{run_id}/emergency-flatten",
        payload,
        timeout=_FLATTEN_TIMEOUT,
    )


async def emergency_flatten_account(base_url: str, account_id: str, payload: dict) -> dict:
    """POST an account-scoped emergency flatten with the broker timeout."""

    return await _post_action(
        f"{base_url.rstrip('/')}/accounts/{account_id}/emergency-flatten",
        payload,
        timeout=_FLATTEN_TIMEOUT,
    )


async def renew_control_plane_lease(base_url: str) -> dict:
    """POST /control-plane/renew-lease and return HostRunnerHealth as a dict."""
    return await _post_action(f"{base_url.rstrip('/')}/control-plane/renew-lease", {})


async def ensure_account_clerk(
    base_url: str,
    account_id: str,
    *,
    ibkr_host: str = "127.0.0.1",
) -> dict:
    """Ensure one Clerk is live and generation-handshaken for an operator action."""

    return await _post_action(
        f"{base_url.rstrip('/')}/accounts/{account_id}/clerk/ensure",
        {"ibkr_host": ibkr_host},
        timeout=_START_ADMISSION_TIMEOUT,
    )


async def release_account_clerk(base_url: str, account_id: str) -> dict:
    """Release the account-scoped Clerk after an explicit broker disconnect."""

    return await _post_action(
        f"{base_url.rstrip('/')}/accounts/{account_id}/clerk/release",
        {},
        timeout=_CLERK_RELEASE_TIMEOUT,
    )


async def _post_action(url: str, payload: dict, *, timeout: httpx.Timeout = _TIMEOUT) -> dict:
    """Typed POST core for the four mutation forwards.

    Internally uses :func:`_typed_post_json` to classify the transport
    outcome. Maps the closed-kind ``DaemonResult`` into:

    - ``CONNECTED`` → return the parsed body.
    - ``UNREACHABLE`` with ``outcome_ambiguous=True`` →
      :class:`HostDaemonOutcomeUnknownError` (PRD #619-C5; the
      endpoint surfaces this as a typed 409 + ``OUTCOME_UNKNOWN``).
    - ``UNREACHABLE`` with ``outcome_ambiguous=False`` →
      :class:`HostDaemonError(503, ...)` (clean pre-send failure;
      retry is safe).
    - Any other outcome with a ``response_status >= 400`` (the daemon
      spoke and authored its own status — auth, dirty-tree 409,
      missing-input 400, 5xx) → :class:`HostDaemonError(<status>, ...)`,
      propagating the daemon's status verbatim.
    - Anything else (malformed JSON, non-dict payload, no response at
      all) → :class:`HostDaemonError(502, ...)` (the daemon spoke but
      in a way we can't consume).
    """
    result, response = await _classify_http(
        url,
        method="POST",
        payload=payload,
        timeout=timeout,
        keep_error_response=True,
    )
    if result.kind == "CONNECTED" and response is not None:
        parsed_result, body = _parse_json_body(result, response)
        if parsed_result.kind == "CONNECTED" and body is not None:
            return body
        result = parsed_result

    if result.kind == "UNREACHABLE":
        if result.outcome_ambiguous:
            raise HostDaemonOutcomeUnknownError(
                error_category=result.error_category or "transport_error",
                detail=result.detail,
            )
        raise HostDaemonError(
            503, result.detail or "host daemon unreachable"
        )

    if result.response_status is not None and result.response_status >= 400:
        raise HostDaemonError(
            result.response_status,
            _error_detail_of(response)
            if response is not None
            else (result.detail or f"host daemon returned {result.response_status}"),
        )

    raise HostDaemonError(
        502, result.detail or "host daemon returned a non-JSON body"
    )


async def _typed_post_json(
    url: str, payload: dict, *, timeout: httpx.Timeout = _TIMEOUT
) -> tuple[DaemonResult, dict | None]:
    """Typed POST. Mirrors :func:`_typed_get_json` over the shared
    transport classifier so auth / 4xx / 5xx / malformed-body handling
    is the same across the GET and POST paths."""
    result, response = await _classify_http(
        url, method="POST", payload=payload, timeout=timeout
    )
    return _parse_json_body(result, response)


def _detail_of(response: httpx.Response) -> str:
    """Extract a human-readable detail from a daemon error response."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"host daemon returned {response.status_code}"
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return f"host daemon returned {response.status_code}"


# ---------------------------------------------------------------------------
# Read path — typed DaemonResult (PRD #619-C2).
# ---------------------------------------------------------------------------


async def fetch_instances(base_url: str) -> tuple[DaemonResult, dict | None]:
    """GET /instances. Returns ``(DaemonResult, dict | None)``.

    The dict is the parsed body iff ``result.kind == "CONNECTED"``. Existing
    fail-closed callers can keep checking ``payload is None``; the result is
    additive context for typed-failure surfacing.
    """
    return await _typed_get_json(f"{base_url.rstrip('/')}/instances")


async def fetch_instance_process(
    base_url: str, strategy_instance_id: str
) -> tuple[DaemonResult, dict | None]:
    """GET /instances/{id}/process. Returns ``(DaemonResult, dict | None)``."""
    return await _typed_get_json(
        f"{base_url.rstrip('/')}/instances/{strategy_instance_id}/process",
        timeout=_INSTANCE_PROBE_TIMEOUT,
    )


async def fetch_run_process(
    base_url: str, run_id: str
) -> tuple[DaemonResult, dict | None]:
    """GET /runs/{id}/process for proof about one immutable run identity."""
    return await _typed_get_json(f"{base_url.rstrip('/')}/runs/{run_id}/process")


async def fetch_qc_audit_copies(base_url: str) -> tuple[DaemonResult, dict | None]:
    """GET /qc-audit-copies. Returns ``(DaemonResult, dict | None)``."""
    return await _typed_get_json(f"{base_url.rstrip('/')}/qc-audit-copies")


async def fetch_audit_copy_sizing_lookup(
    base_url: str,
    audit_copy_path: str,
    proposed_sizing: dict | None = None,
) -> tuple[DaemonResult, dict | None]:
    """GET /audit-copy-sizing-lookup. Returns ``(DaemonResult, dict | None)``.

    ``proposed_sizing`` is JSON-encoded into the query string.
    """
    import json as _json
    from urllib.parse import quote

    params = f"audit_copy_path={quote(audit_copy_path, safe='/')}"
    if proposed_sizing is not None:
        params += f"&proposed_sizing={quote(_json.dumps(proposed_sizing, sort_keys=True))}"
    return await _typed_get_json(
        f"{base_url.rstrip('/')}/audit-copy-sizing-lookup?{params}"
    )


async def fetch_health(
    base_url: str,
) -> tuple[DaemonResult, HostRunnerHealth | None]:
    """GET /health. Returns ``(DaemonResult, HostRunnerHealth | None)``.

    The parsed envelope is non-None iff ``result.kind == "CONNECTED"``.

    Two consumers, two read patterns:

    - ``DaemonConnectivityMonitor`` (619-C2) discards the envelope and
      keeps only the typed result. The monitor cares about the
      connectivity classification + the daemon's declared
      ``daemon_boot_id``, both carried on ``DaemonResult``.
    - The data plane's instance-less ``/daemon-health`` route forwards
      the envelope so the cockpit / deploy form connectivity strip can
      observe the authenticated probe through the data plane (the
      browser never holds the daemon token; see host_daemon.py docstring
      on PRD #619-C P2).

    Parse failures classify via the typed signal:

    - ``pydantic.ValidationError`` → ``DaemonResult.incompatible_contract(...)``
    - JSON decode ``ValueError`` → ``DaemonResult.malformed_body(...)``
    """
    return await _fetch_health(base_url, timeout=_TIMEOUT)


async def fetch_startability_health(
    base_url: str,
) -> tuple[DaemonResult, HostRunnerHealth | None]:
    """GET /health with the bounded deadline used for start admission."""

    return await _fetch_health(base_url, timeout=_INSTANCE_PROBE_TIMEOUT)


async def _fetch_health(
    base_url: str,
    *,
    timeout: httpx.Timeout,
) -> tuple[DaemonResult, HostRunnerHealth | None]:
    result, response = await _classify_http(
        f"{base_url.rstrip('/')}/health", method="GET", timeout=timeout
    )
    if response is None:
        return result, None
    try:
        health = HostRunnerHealth.model_validate_json(response.content)
    except ValidationError as exc:
        return (
            DaemonResult.incompatible_contract(
                status=response.status_code, detail=str(exc)
            ),
            None,
        )
    except ValueError as exc:
        return (
            DaemonResult.malformed_body(
                status=response.status_code, detail=str(exc)
            ),
            None,
        )
    return (
        DaemonResult.connected(
            status=response.status_code,
            daemon_boot_id=health.daemon_boot_id,
            daemon_api_version=None,
        ),
        health,
    )


async def fetch_gateway_sockets(
    base_url: str,
    *,
    gateway_port: int,
) -> tuple[DaemonResult, GatewaySocketsSnapshot | None]:
    """GET /broker/sockets from the host daemon.

    The data plane passes the configured IBKR port; the browser never calls this
    daemon route directly because it requires the shared live-runner token.
    """

    result, response = await _classify_http(
        f"{base_url.rstrip('/')}/broker/sockets?gateway_port={gateway_port}",
        method="GET",
        timeout=_SOCKET_PROBE_TIMEOUT,
    )
    if response is None:
        return result, None
    try:
        snapshot = GatewaySocketsSnapshot.model_validate_json(response.content)
    except ValidationError as exc:
        return (
            DaemonResult.incompatible_contract(
                status=response.status_code,
                detail=str(exc),
            ),
            None,
        )
    except ValueError as exc:
        return (
            DaemonResult.malformed_body(
                status=response.status_code,
                detail=str(exc),
            ),
            None,
        )
    return DaemonResult.connected(status=response.status_code), snapshot


async def _typed_get_json(
    url: str, *, timeout: httpx.Timeout = _TIMEOUT
) -> tuple[DaemonResult, dict | None]:
    """Shared body for the typed read path. Returns ``(result, payload)``.

    The payload is a parsed JSON object iff ``result.kind == "CONNECTED"``.
    Non-2xx, transport failures, malformed JSON, and non-object payloads all
    classify deterministically and return ``payload=None``.
    """
    result, response = await _classify_http(url, method="GET", timeout=timeout)
    return _parse_json_body(result, response)


async def _classify_http(
    url: str,
    *,
    method: Literal["GET", "POST"],
    payload: dict | None = None,
    timeout: httpx.Timeout = _TIMEOUT,
    keep_error_response: bool = False,
) -> tuple[DaemonResult, httpx.Response | None]:
    """Classify the transport outcome of one daemon HTTP exchange.

    Shared chokepoint for ``_typed_get_json`` / ``_typed_post_json`` /
    ``fetch_health``. Returns:

    - ``(connected, response)`` when the daemon answered with 2xx — the
      raw ``httpx.Response`` is passed back so the caller can parse the
      body (untyped JSON, ``HostRunnerHealth``, …).
    - ``(failure_result, None)`` for every transport / auth / 4xx-5xx
      outcome: the closed-kind ``DaemonResult`` already carries the
      detail and the caller does not need to inspect the response.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if method == "GET":
                response = await client.get(url, headers=_auth_headers())
            else:
                response = await client.post(
                    url, json=payload, headers=_auth_headers()
                )
    except Exception as exc:
        logger.warning("host daemon unreachable at %s: %s", url, exc)
        return DaemonResult.from_httpx_exception(exc), None

    if response.status_code in (401, 403):
        return (
            DaemonResult.auth_failed(
                status=response.status_code, detail=_detail_of(response)
            ),
            response if keep_error_response else None,
        )
    if response.status_code >= 400:
        return (
            DaemonResult.protocol_error(
                status=response.status_code, detail=_detail_of(response)
            ),
            response if keep_error_response else None,
        )
    # 2xx — body parsing belongs to the caller.
    return DaemonResult.connected(status=response.status_code), response


def _error_detail_of(response: httpx.Response) -> HostDaemonErrorDetail:
    """Extract the daemon-authored error detail, preserving structured contracts."""
    try:
        body = response.json()
    except ValueError:
        return response.text or f"host daemon returned {response.status_code}"
    if isinstance(body, dict):
        detail = body.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, dict):
            return detail
    return f"host daemon returned {response.status_code}"


def _parse_json_body(
    result: DaemonResult, response: httpx.Response | None
) -> tuple[DaemonResult, dict | None]:
    """Parse the 2xx body as an untyped JSON object.

    No-op pass-through when ``response is None`` (transport already
    classified). Otherwise: ``ValueError`` → ``malformed_body``;
    non-dict → ``incompatible_contract``; dict → return as-is.
    """
    if response is None:
        return result, None
    try:
        body = response.json()
    except ValueError as exc:
        return (
            DaemonResult.malformed_body(
                status=response.status_code, detail=str(exc)
            ),
            None,
        )
    if not isinstance(body, dict):
        return (
            DaemonResult.incompatible_contract(
                status=response.status_code,
                detail=f"expected JSON object, got {type(body).__name__}",
            ),
            None,
        )
    return DaemonResult.connected(status=response.status_code), body
